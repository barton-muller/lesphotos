#!/usr/bin/env python3
"""
Standalone daemon: watches each user's configured upload folder(s) for new
photos and files them into the library within moments of arrival, instead
of waiting for someone to run the indexer by hand.

A separate process, not a thread inside the API server - CLAUDE.md's own
"everything runs synchronously in the request, no job queue" invariant is
about api.py staying request-driven, not about this app never having a
background process. Isolating this here means a stuck/crashed watcher
can't take the API down with it, the same reasoning _derive_heic_isolated
uses to isolate HEIC decode into its own subprocess. Started as its own
DSM Task Scheduler boot-up entry, alongside (not inside) run.sh/uvicorn -
see deploy/README.md.

For each new file under a watched folder:
  1. wait for it to stop growing (a phone backup writes over some seconds;
     touching a file mid-write is how you get a truncated/corrupt copy)
  2. read its real capture date (read_capture_datetime - the same
     HEIC-aware, never-raises reader reorganize.py already uses)
  3. move it into <library root>/<YYYY>/<YYYY-MM>/<original filename> -
     NOT a full YYYY-MM-DD-moment; that's auto_generate_moments' job,
     run periodically, separately, on this intermediate shape
  4. tell Synology's own index about the move (synoindex -n) so its
     mobile-backup dedup and @eaDir bookkeeping stay consistent
  5. index just that one destination folder - cheap (a handful of files,
     not the whole library) and immediately makes the photo visible to
     the running server, since /timeline/count is a live, uncached query
"""

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import auth
import config
import indexer
import platform_hosts
from moment_dates import parse_moment_date_prefix
from reorganize import read_capture_datetime

log = logging.getLogger("watcher")

# A phone backup writes a file over real time; polling until its size
# stops changing is simpler and more portable than trying to detect
# "still open" file handles, and cheap enough at this event rate.
_STABLE_CHECK_INTERVAL_S = 1.0
_STABLE_CHECKS_REQUIRED = 2
_STABLE_TIMEOUT_S = 120

SYNOINDEX_BIN = "/usr/syno/bin/synoindex"


def _wait_until_stable(path: Path) -> bool:
    """
    True once `path`'s size has stopped changing for
    _STABLE_CHECKS_REQUIRED consecutive checks, or False if it never
    settles within _STABLE_TIMEOUT_S (or the file vanishes - a rename/
    delete racing the watcher, not an error condition here).
    """
    deadline = time.monotonic() + _STABLE_TIMEOUT_S
    last_size = -1
    stable_count = 0
    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == last_size:
            stable_count += 1
            if stable_count >= _STABLE_CHECKS_REQUIRED:
                return True
        else:
            stable_count = 0
        last_size = size
        time.sleep(_STABLE_CHECK_INTERVAL_S)
    log.warning("gave up waiting for %s to stabilize", path)
    return False


def _synoindex_rename(old_path: Path, new_path: Path) -> None:
    """
    Tells Synology's own index a file moved, so its mobile-backup dedup
    and @eaDir bookkeeping don't go stale. Best-effort: a missing binary
    or non-zero exit is logged, not raised - the file has already moved
    and is already indexable by lesphotos regardless of whether DSM's own
    index agrees, and today's testing found no case where skipping this
    caused an actual duplicate re-upload.
    """
    if not Path(SYNOINDEX_BIN).exists():
        return
    try:
        r = subprocess.run(
            [SYNOINDEX_BIN, "-n", str(new_path), str(old_path)],
            capture_output=True, timeout=30,
        )
        if r.returncode != 0:
            log.warning("synoindex -n failed for %s: %s", new_path, r.stderr.decode(errors="replace")[:200])
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("synoindex -n errored for %s: %s", new_path, e)


def handle_new_file(library: config.Library, path: Path) -> None:
    """
    Moves one newly-arrived file into <root>/YYYY/YYYY-MM/ and indexes
    just that destination folder. Any single file's failure is logged and
    swallowed - one bad file (permissions, a race with something else
    touching the same path, a corrupt partial write) must not take the
    watcher down for every other file and every other user it serves.
    """
    try:
        if not path.is_file():
            return
        if not _wait_until_stable(path):
            return

        when = read_capture_datetime(path)
        dest_dir = library.photos_root / f"{when.year:04d}" / f"{when.year:04d}-{when.month:02d}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / path.name

        if dest_path.exists():
            log.warning("skipping %s: %s already exists", path, dest_path)
            return

        shutil.move(str(path), str(dest_path))
        log.info("moved %s -> %s", path, dest_path)

        _synoindex_rename(path, dest_path)

        stats = indexer.index(library, root=dest_dir, verbose=False)
        indexer.backfill_moment_dates(library, root=dest_dir)
        log.info(
            "indexed %s: seen=%d indexed=%d failed=%d",
            dest_dir, stats["seen"], stats["indexed"], stats["failed"],
        )
    except Exception:
        log.exception("failed to handle new file: %s", path)


class _NewFileHandler(FileSystemEventHandler):
    """
    Debounces watchdog's raw events: a phone backup burst fires several
    on_created events close together, and on_created itself can fire
    before a single file has finished writing (handled separately, by
    _wait_until_stable) - this layer only dedupes "the same path queued
    twice before it's been processed once."
    """

    def __init__(self, library: config.Library):
        self.library = library
        self._seen = set()

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        # recursive=True watches everything under the folder, including
        # @eaDir - DSM's own sidecar cache, which contains files
        # (SYNOPHOTO_THUMB_XL.jpg etc.) that watchdog reports as newly
        # created exactly like a real photo. Confirmed in testing: without
        # this check the watcher moved DSM's own thumbnails into the
        # library as if they were photos. Same filter iter_media_files
        # already applies for the same reason.
        if any(part.startswith(".") or part in platform_hosts.IGNORED_DIRNAMES
               for part in path.parts):
            return
        if path in self._seen:
            return
        self._seen.add(path)
        try:
            handle_new_file(self.library, path)
        finally:
            self._seen.discard(path)

    # DSM/phone backup clients sometimes write to a temp name and rename
    # into place rather than creating the final name directly - catch
    # that shape too, not just on_created.
    def on_moved(self, event):
        if event.is_directory:
            return
        self.on_created(type(event)(event.dest_path))


def _resolve_iter_users_watch_paths():
    """
    Yields (library, watch_path) for every currently-configured watch
    path across every user - read fresh each call (not cached at
    startup) so add-watch-path/remove-watch-path take effect on the next
    restart without code changes; a running watcher picking up config
    changes live is future work, not needed for the first version.
    """
    conn = auth.connect()
    for user in auth.list_users(conn):
        library = auth.library_for(user)
        for watch_path in auth.get_watch_paths(conn, user["name"]):
            yield user["name"], library, Path(watch_path)


def _scan_backlog(library: config.Library, watch_path: Path) -> None:
    """
    Handles files already sitting in a watched folder before this process
    started - e.g. photos that arrived while the watcher wasn't running
    (no boot trigger set up yet, or a permission error that only root can
    read). watchdog's Observer is purely event-driven and never sees
    these, so without this pass they'd sit unindexed forever.
    """
    for path in watch_path.rglob("*"):
        if any(part.startswith(".") or part in platform_hosts.IGNORED_DIRNAMES
               for part in path.parts):
            continue
        if path.is_file():
            handle_new_file(library, path)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    # This process needs to run as root (or some other account the phone's
    # uploading account doesn't match) so it can read files the uploading
    # app created owner-only - but root's own default umask then creates
    # derivatives (thumb.webp/preview.webp, cache/<photo_id>/) the API
    # server's own account can't read back, just the same mismatch in the
    # other direction. Confirmed in testing: a root-run watcher moved and
    # indexed two new photos correctly, but neither thumbnail rendered -
    # even as a placeholder - because the server got a permission error,
    # not a 404. 0o022 makes every file/dir this process creates
    # world-readable (world-executable too, needed for directories to be
    # traversable), which costs nothing since a photo library's
    # derivatives were never meant to be private from other processes on
    # the same box in the first place.
    os.umask(0o022)

    observer = Observer()
    watched_any = False
    for name, library, watch_path in _resolve_iter_users_watch_paths():
        if not watch_path.is_dir():
            log.warning("skipping watch path for %s: %s does not exist", name, watch_path)
            continue
        _scan_backlog(library, watch_path)

        handler = _NewFileHandler(library)
        observer.schedule(handler, str(watch_path), recursive=True)
        log.info("watching %s for %s -> %s", watch_path, name, library.photos_root)
        watched_any = True

    if not watched_any:
        log.warning(
            "no watch paths configured for any user - nothing to do. "
            "See: python3 manage.py add-watch-path <user> <folder>"
        )
        return

    observer.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
