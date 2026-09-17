#!/usr/bin/env python3
"""
Client-initiated moment reorganization: rename a moment, merge two or more
moments, or move individual photos between moments (which also covers
"adjust a moment's date boundary" - moving the boundary is just moving the
photos on one side of it into the adjacent moment).

Why this exists: PhotoView identifies albums/media purely by
path_hash = MD5(filesystem path) (confirmed via source inspection - see
plan.md), with no fallback to content/hash/mtime. A raw filesystem move or
rename is therefore always seen by PhotoView as delete-old-row +
create-new-row, which silently loses any favorite tied to the old row -
confirmed by direct testing (both single-file and whole-folder rename).

The fix isn't to avoid the filesystem move (the filesystem must stay the
source of truth per the project's design principles - a "rename" has to be
a real rename) or to patch PhotoView's own database directly (that would
make PhotoView's disposable DB load-bearing, and couples us to an
undocumented schema across PhotoView upgrades). Instead: server/appdb is the
actual source of truth for favorites, keyed by content hash rather than
PhotoView's path-derived id. Every operation here:
  1. moves the real files on disk
  2. updates appdb's records for the moved content hashes immediately -
     app state is correct the instant the move completes, independent of
     PhotoView entirely
  3. triggers a PhotoView rescan so it picks up the new paths
  4. reconciles PhotoView's own `favorite` flag on the new media rows, so
     PhotoView's UI/API (myTimeline(onlyFavorites), etc.) stays a useful
     read-optimization without being the source of truth

If step 3/4 fails or is skipped, no data is lost - appdb still has the
correct favorite state and a later scan + reconcile will catch PhotoView up.
"""

import shutil
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image

# Without this, Image.open() cannot read a .heic at all and
# read_capture_datetime silently falls back to file mtime - filing every
# iPhone photo under the date it was COPIED rather than when it was shot.
# indexer.py registers this too; reorganize.py did not, and the failure is
# invisible because the fallback always succeeds.
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass

import platform_hosts
from appdb import db
from moment_dates import (
    date_prefix_str,
    has_date_prefix,
    has_day_level_prefix,
    parse_moment_date_prefix,
)

# Every path the client sees is expressed relative to the user's own
# library root, prefixed with "/photos". That prefix is a leftover of
# PhotoView's container mount, kept only because the client already parses
# it (client/web/src/api.ts toApiPath) - but it now means "the root of
# whichever library you are logged into", which is also what makes it
# impossible for one user's paths to name another's files.
CLIENT_PATH_PREFIX = "/photos"


def client_path(library, host_path):
    """Absolute host path -> the library-relative form the client uses."""
    host_path = Path(host_path).resolve()
    return f"{CLIENT_PATH_PREFIX}/{host_path.relative_to(library.photos_root)}"


def set_favorite(conn, path, favorite):
    """
    Toggles a favorite in appdb, which is the source of truth (invariant #2).

    Used to also mirror the flag into PhotoView via favoriteMedia, so
    PhotoView's own UI would agree. Nothing reads that mirror now - the app
    serves its own timeline - so the mirror, the per-photo id lookup it
    needed, and the reconciliation that repaired it after every move are all
    """
    path = Path(path)
    content_hash = db.content_hash_of(path)
    db.upsert_photo(conn, content_hash, path, favorite=favorite)


def _register_photo(conn, path):
    """Computes/updates a photo's appdb row for its current path, preserving favorite state."""
    content_hash = db.content_hash_of(path)
    existing = db.get_photo_by_hash(conn, content_hash)
    favorite = existing["favorite"] if existing else 0
    db.upsert_photo(conn, content_hash, path, favorite=favorite)
    return content_hash


def _iter_moment_files(moment_path):
    """
    A moment folder's real contents, skipping host bookkeeping like
    Synology's @eaDir thumbnail cache (a directory, not a photo) - the same
    filter auto_generate_moments applies to iter_media_files candidates.
    Plain iterdir() here crashed content_hash_of with IsADirectoryError the
    first time a rename/merge touched a moment old enough to have one.
    """
    return [
        f for f in moment_path.iterdir()
        if f.is_file() and f.name not in platform_hosts.IGNORED_DIRNAMES
    ]


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic"}
# Auto-grouping has to consider videos as well, or a standalone clip is
# left behind in the source tree while every photo around it moves - the
# folder looks emptied but is not. A Live Photo's .MOV is handled
# separately (it travels with its still via _expand_motion_pairs) and must
# NOT also be grouped on its own, or the pair is split across two moments.
GROUPABLE_EXTS = IMAGE_EXTS | {".mov", ".mp4", ".m4v", ".avi", ".3gp", ".mkv"}

# Gap-based grouping, adapted from initial_script.py's split_into_events -
# see docs/decisions/auto-generate-moments.md for what changed and why
# (mainly: no forced year-boundary split, per this project's own explicit
# design decision - CLAUDE.md's "Pure gap-based, no year split").
DEFAULT_EVENT_GAP = timedelta(days=2)


def _capture_datetime_cached(path, indexed_dates):
    """
    taken_at from appdb where the indexer already recorded it, else read
    the file.

    The fallback matters: this function must keep working on a tree that
    has not been indexed, which is a supported way to use auto-generate.
    A stored value that cannot be parsed also falls through rather than
    raising, since one bad row must not abort grouping the whole library.
    """
    stored = indexed_dates.get(str(path))
    if stored:
        try:
            return datetime.fromisoformat(stored)
        except (TypeError, ValueError):
            pass
    return read_capture_datetime(path)


def read_capture_datetime(path):
    """
    EXIF DateTimeOriginal (36867) or DateTimeDigitized (36868) from the Exif
    sub-IFD, falling back to the file's own mtime if EXIF is
    missing/unreadable.

    Deliberately does NOT fall back to top-level DateTime (306) the way
    initial_script.py does: 306 is "last modified", not "when shot". An
    exported photo carried 306 = its export date (2025-12-21) while its
    real capture date was 2025-08-07, and trusting 306 filed it into a
    December moment sitting in the middle of July. Never raises: a single
    corrupt/unreadable/HEIC-without-a-registered-opener file must not abort
    grouping the rest of a large batch, matching this function's "robust"
    requirement - unreadable EXIF silently falls through to mtime, which is
    always readable for any file that passed the earlier os.path.isfile check.
    """
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if exif:
                # DateTimeOriginal/DateTimeDigitized live in the Exif
                # SUB-IFD (0x8769), not the top-level IFD that getexif()
                # returns. Reading exif.get(36867) directly never finds
                # them - measured: all 2288 photos in the test library
                # appeared to have no DateTimeOriginal, so every moment was
                # actually grouped by file mtime rather than capture date.
                sub = exif.get_ifd(0x8769)
                date_str = sub.get(36867) or sub.get(36868)
                if date_str:
                    try:
                        return datetime.strptime(str(date_str)[:19], "%Y:%m:%d %H:%M:%S")
                    except ValueError:
                        pass
    except Exception:
        pass
    return datetime.fromtimestamp(path.stat().st_mtime)


def ensure_all_photos_registered(library):
    """
    Backfills appdb (and each photo's stable photo_id, see
    docs/decisions/stable-photo-id.md) for every photo in the library, not
    just ones already touched by a favorite/rename/merge/create call - appdb
    is otherwise populated lazily and most of the library would have no
    photo_id at all. Only hashes files whose *current path* isn't already
    known to appdb (db.get_photo_by_path), so a library that's already fully
    registered costs one query per photo, not a re-hash of every file - the
    per-file hash (a full read of the file's bytes) only happens once, ever,
    per path.
    """
    conn = db.connect(library)
    for path in library.photos_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTS:
            continue
        if db.get_photo_by_path(conn, path):
            continue
        _register_photo(conn, path)
    return conn


def _split_into_gap_events(dated_photos, gap):
    """
    Sorts by capture time, starts a new batch whenever the gap to the
    previous photo exceeds `gap`. Deliberately does NOT also force a split
    at year boundaries - initial_script.py's split_into_events does, but
    this project explicitly chose pure gap-based grouping instead (see
    CLAUDE.md: "if a moment spills over then its fine to be in that same
    folder", and the "Pure gap-based, no year split" decision in plan.md).
    A moment starting Dec 30 and running into January stays one moment here.
    """
    dated_photos = sorted(dated_photos, key=lambda dp: dp[1])
    batches = []
    current = []
    for path, when in dated_photos:
        if current and (when - current[-1][1]) > gap:
            batches.append(current)
            current = []
        current.append((path, when))
    if current:
        batches.append(current)
    return batches


def _unique_dest_dir(parent, base_name, already_planned):
    """
    Appends -2, -3, ... if base_name is already taken - two gap-batches
    landing on the same calendar date (e.g. two short, separate trips both
    starting 2024-06-01) need distinct folders, not a silent merge or a
    move_photos collision. Checks both the real filesystem (candidate.exists())
    and `already_planned` (dest dirs chosen earlier in this same
    auto_generate_moments call, which won't exist on disk yet in a dry run
    or before the moves actually happen) - without the latter, two batches
    landing on the same date in the same run would both compute the exact
    same "not taken yet" answer and collide with each other.
    """
    candidate = parent / base_name
    n = 2
    while candidate.exists() or candidate in already_planned:
        candidate = parent / f"{base_name}-{n}"
        n += 1
    return candidate


def auto_generate_moments(library, source_dir, gap=DEFAULT_EVENT_GAP, dry_run=False):
    """
    Regroups a flat folder of photos (e.g. <root>/<user>/<year>/<month>/, as
    produced by a raw phone-backup import) into gap-based moment folders,
    reusing dad's initial_script.py's core event-splitting idea but adapted
    to this project's own conventions - see
    docs/decisions/auto-generate-moments.md.

    Robust by design for a messy/partial source tree:
    - Walks recursively, so it works whether photos sit directly in
      source_dir or in nested YYYY/MM-style subfolders under it.
    - Skips any file already sitting under a folder that looks like an
      already-organized moment (a day-level YYYY-MM-DD-prefixed directory
      name anywhere in its path - a coarser YYYY-MM month bucket does NOT
      count as organized, since that's exactly the intermediate shape this
      function exists to sweep up) - makes this safe to re-run without
      re-shuffling moments a previous run (or the user) already created,
      and safe to point at a root that's a mix of already-organized and
      not-yet-organized photos.
    - A single file with unreadable/missing EXIF never aborts the whole
      operation (read_capture_datetime always returns something, falling
      back to mtime).
    - Moves are done in one pass across all computed batches, with exactly
      one scanAll + wait at the end (not one per batch) - scanAll is a full
      library rescan (confirmed directly - see plan.md), so doing it once
      per batch would make regrouping a large flat folder take
      minutes-per-batch instead of minutes total.
    - dry_run=True computes and returns the planned batches/destinations
      without touching the filesystem, so a caller (the API layer) can show
      a preview before committing to a - potentially large - reorganization.
    """
    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        raise FileNotFoundError(f"source directory does not exist: {source_dir}")

    # Live Photo clips are excluded as candidates in their own right: each
    # travels with its still (_expand_motion_pairs), and grouping it
    # separately could file the two halves into different moments - the
    # pair would survive on disk but be split, which is worse than either
    # moving or not moving it.
    conn = db.connect(library)
    paired_clips = {
        r["motion_path"]
        for r in conn.execute(
            "SELECT motion_path FROM media WHERE motion_kind = 'apple' AND motion_path IS NOT NULL"
        )
    }
    # The indexer already extracted taken_at from every file's EXIF, using
    # the same rules read_capture_datetime applies. Reading it back is a
    # single query; re-deriving it means opening every candidate file,
    # which on a NAS with a spinning disk is minutes for a few thousand
    # photos (measured at ~12 header reads/s on the target hardware).
    indexed_dates = {
        r["path"]: r["taken_at"]
        for r in conn.execute(
            "SELECT p.path, m.taken_at FROM photos p "
            "JOIN media m ON m.content_hash = p.content_hash"
        )
    }
    conn.close()

    candidates = []
    for path in source_dir.rglob("*"):
        # Skip host bookkeeping and our own sidecar, exactly as
        # indexer.iter_media_files does. Without this, rglob descends into
        # @eaDir and .lesphotos/cache and treats their contents as photos
        # to be filed: on a real library that was 19,962 "candidates"
        # against 4,458 actual media files, and it would have moved
        # Synology's own thumbnails into the user's moment folders.
        parts = path.relative_to(source_dir).parts
        if any(part.startswith(".") or part in platform_hosts.IGNORED_DIRNAMES
               for part in parts[:-1]):
            continue
        if not path.is_file() or path.suffix.lower() not in GROUPABLE_EXTS:
            continue
        if str(path) in paired_clips:
            continue
        # A day-level prefix (or fuller) means "already a real moment" and
        # is skipped; a month-only YYYY-MM prefix (e.g. what the watcher
        # files new uploads under before this function ever sees them) is
        # NOT treated as organized - that's an intermediate state this
        # function's whole job is to sweep into real moments.
        if any(has_day_level_prefix(part) for part in path.relative_to(source_dir).parts[:-1]):
            continue
        candidates.append(path)

    # Destination moments are filed under <library root>/<year>/, matching
    # this project's YYYY/YYYY-MM-DD-label convention (see CLAUDE.md).
    # Previously this recovered a "user root" as the first path component
    # under a single shared PHOTOS_ROOT (e.g. "bob"); each user now has
    # their own library, so the library root IS the user root.
    user_root = library.photos_root

    dated = [(p, _capture_datetime_cached(p, indexed_dates)) for p in candidates]
    batches = _split_into_gap_events(dated, gap)

    plan = []
    planned_dirs = set()
    for batch in batches:
        paths = [p for p, _ in batch]
        first_when = batch[0][1]
        year_dir = user_root / str(first_when.year)
        base_name = f"{first_when.strftime('%Y-%m-%d')}-moment"
        dest_dir = _unique_dest_dir(year_dir, base_name, planned_dirs)
        planned_dirs.add(dest_dir)
        plan.append({"dest": dest_dir, "paths": paths, "count": len(paths)})

    if dry_run:
        return plan

    conn = db.connect(library)
    all_moved = []
    source_parents = {p.parent for p in candidates}
    for item in plan:
        item["dest"].parent.mkdir(parents=True, exist_ok=True)
        moved = _move_photos_no_scan(conn, item["paths"], item["dest"])
        all_moved.extend(moved)

    # Prune emptied source folders (e.g. a flat scaletest-06/ month folder
    # with nothing left in it after every photo moved into a real moment) -
    # matches merge_moments' own cleanup of its drained source folders.
    # rmdir only succeeds on a genuinely empty directory, so this safely
    # no-ops for any source folder that still has non-image files in it
    # (or a nested @eaDir-style thumbnail cache dir) rather than needing to
    # check emptiness itself.
    for parent in source_parents:
        try:
            parent.rmdir()
        except OSError:
            pass

    if all_moved:
        # No timeout handling any more: this used to wrap a global PhotoView
        # scanAll whose duration scaled with LIBRARY size (~90s/1000 photos
        # measured), so it could legitimately outlast its wait window after
        # the moves had already succeeded - hence the plan-carrying
        # TimeoutError this once raised. _reindex_moved is a bounded number
        # of indexed UPDATEs proportional to the photos actually moved, so
        # there is nothing left to time out.
        _reindex_moved(conn, all_moved)

    return plan


def _with_date_prefix(old_name, new_label):
    """
    Keeps a moment's date prefix when the new label doesn't carry one.

    The rename UI pre-fills its input with the full folder name, so the
    normal edit-the-label-only flow already includes the prefix - but
    clearing the field entirely (easy to do, and what a user typing a fresh
    name would naturally do) would otherwise produce a prefix-less folder,
    silently breaking the chronological sort that every other part of the
    system relies on (moment ordering, merge_moments' "earliest date wins",
    create_moment's date_prefix derivation). Re-attaching the original
    date here means a rename can never lose it, while a label that already
    has its own prefix is passed through untouched.
    """
    if has_date_prefix(new_label) or not has_date_prefix(old_name):
        return new_label
    return f"{date_prefix_str(old_name)}-{new_label}"



def _reindex_moved(conn, moved_paths):
    """
    Brings the media table in line after a move, replacing the
    scanAll + poll + per-photo favorite reconciliation this used to do.

    That old path existed only because PhotoView identifies media by
    MD5(path), so any move destroyed and recreated its rows under new ids,
    taking the favorite mirror with them. It cost a FULL-library rescan plus
    one whole-album-tree query per moved photo - measured at 3 minutes and
    then a timeout on a 2,330-photo library, and it got worse with library
    size, not with the number of photos actually moved.

    None of that is needed now. photo_id is stable across moves by
    construction and favorites live in appdb, so a move changes exactly one
    thing: the path. The derivatives are keyed by photo_id, so they are
    still valid and are not regenerated.

    Cost is proportional to the photos actually moved, not to library size.
    """
    for path in moved_paths:
        row = conn.execute("SELECT content_hash FROM photos WHERE path = ?", (str(path),)).fetchone()
        if not row:
            continue
        st = Path(path).stat()
        # A move changes the containing folder, which is exactly what
        # moment_date is derived from - stale otherwise, e.g. a photo
        # merged into a differently-dated moment keeping its old sort key.
        moment_date = parse_moment_date_prefix(Path(path).parent.name)
        conn.execute(
            "UPDATE media SET mtime_ns = ?, size = ?, moment_date = ? WHERE content_hash = ?",
            (st.st_mtime_ns, st.st_size, moment_date.isoformat() if moment_date else None, row["content_hash"]),
        )
    conn.commit()
    _repair_motion_paths(conn, moved_paths)

def rename_moment(library, moment_path, new_label, new_date=None):
    """
    Rename a moment's label and, optionally, its date - e.g.
    "2024-03-10-moment" -> "2024-03-10-LeedsWithDad", or with new_date="2024-07-04"
    -> "2024-07-04-LeedsWithDad". A new_label with no date prefix of its own
    inherits the moment's current one (see _with_date_prefix) unless new_date
    is given, in which case that becomes the prefix instead.

    A date change can move the moment into a different year: moments are
    filed under <library root>/<year>/ (see auto_generate_moments), and
    nothing else re-derives that directory from the folder's own name, so
    a changed year is relocated here rather than left stranded under its
    old year folder.
    """
    moment_path = Path(moment_path)
    if new_date is not None:
        try:
            datetime.strptime(new_date, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"new_date must be YYYY-MM-DD, got {new_date!r}")
        new_name = f"{new_date}-{new_label}" if new_label else new_date
        new_year_dir = moment_path.parent.parent / new_date[:4]
    else:
        new_name = _with_date_prefix(moment_path.name, new_label)
        new_year_dir = moment_path.parent
    new_path = new_year_dir / new_name

    if not moment_path.is_dir():
        raise FileNotFoundError(f"moment folder no longer exists: {moment_path}")
    if new_path.exists():
        raise FileExistsError(f"a moment already exists at the new name: {new_path}")

    new_year_dir.mkdir(parents=True, exist_ok=True)

    conn = db.connect(library)
    files_before = _iter_moment_files(moment_path)
    for f in files_before:
        db.upsert_photo(conn, db.content_hash_of(f), f, favorite=None)

    moment_path.rename(new_path)

    # A date change that crosses a year boundary can leave the old year
    # folder empty - prune it, matching auto_generate_moments' own cleanup
    # of emptied source folders. rmdir only succeeds when genuinely empty.
    if moment_path.parent != new_year_dir:
        try:
            moment_path.parent.rmdir()
        except OSError:
            pass

    # A folder rename moves both halves of every pair inside it together,
    # so pairing survives - but motion_path is absolute and now points at
    # the old folder. Rewrite the prefix rather than re-pairing: the files
    # are untouched, only their parent's name changed.
    conn.execute(
        "UPDATE media SET motion_path = ? || substr(motion_path, ?) "
        "WHERE motion_path LIKE ?",
        (str(new_path), len(str(moment_path)) + 1, str(moment_path) + "%"),
    )
    conn.commit()

    moved_paths = []
    for f in files_before:
        new_file_path = new_path / f.name
        _register_photo(conn, new_file_path)
        moved_paths.append(new_file_path)

    moment_id = None
    existing_moment = db.get_moment_by_path(conn, moment_path)
    moment_id = existing_moment["moment_id"] if existing_moment else str(uuid.uuid4())
    db.upsert_moment(conn, moment_id, new_path, new_label)

    _reindex_moved(conn, moved_paths)
    return new_path


def merge_moments(library, moment_paths, new_label):
    """
    Combines two or more moment folders into one, e.g. "2024-06-10-madrid" +
    "2024-06-14-barcelona" -> "2024-06-10-spain-holiday". All files move into
    a new folder named after the earliest moment's date; the emptied source
    folders are removed.

    PhotoView's per-user root-path isolation means cross-user moves would
    need to physically cross filesystem roots, which is a different and
    much bigger operation than reconciling favorites.
    """
    moment_paths = [Path(p) for p in moment_paths]
    if len(moment_paths) < 2:
        raise ValueError("merge_moments needs at least 2 moments")

    missing = [str(p) for p in moment_paths if not p.is_dir()]
    if missing:
        raise FileNotFoundError(f"{len(missing)} moment folder(s) no longer exist: {missing}")

    earliest = min(moment_paths, key=lambda p: p.name)
    date_prefix = date_prefix_str(earliest.name)
    dest = earliest.parent / f"{date_prefix}-{new_label}"
    dest.mkdir(exist_ok=True)

    conn = db.connect(library)
    moved_paths = []
    for src_moment in moment_paths:
        for f in _iter_moment_files(src_moment):
            db.upsert_photo(conn, db.content_hash_of(f), f, favorite=None)
            dest_file = dest / f.name
            shutil.move(str(f), str(dest_file))
            _register_photo(conn, dest_file)
            moved_paths.append(dest_file)
        # Host bookkeeping (e.g. Synology's @eaDir) left behind by the
        # filtered iteration above would make a plain rmdir() fail on a
        # non-empty directory - shutil.rmtree tolerates it the same way
        # DSM itself regenerates the cache for the surviving files.
        shutil.rmtree(src_moment, ignore_errors=True)

    moment_id = str(uuid.uuid4())
    db.upsert_moment(conn, moment_id, dest, dest.name)
    for src_moment in moment_paths:
        conn.execute("DELETE FROM moments WHERE path = ?", (str(src_moment),))
    conn.commit()

    _reindex_moved(conn, moved_paths)
    return dest


def _expand_motion_pairs(conn, photo_paths):
    """
    Adds the .MOV half of any Live Photo in the list.

    A Live Photo is two files that must never be separated: the pairing is
    recorded in appdb, but the still's own row points at an absolute path,
    so moving one without the other leaves a row pointing at a file that
    is no longer beside it. The clip is also hidden from the timeline, so
    a user has no way to notice or fix it by hand - it would simply be
    lost from view while still occupying disk.

    Silent about clips already in the list, so a caller that passed both
    halves explicitly does not get a duplicate move.
    """
    paths = [Path(p) for p in photo_paths]
    known = {str(p.resolve()) for p in paths}
    extra = []
    for p in paths:
        row = conn.execute(
            "SELECT m.motion_kind, m.motion_path FROM photos ph "
            "JOIN media m ON m.content_hash = ph.content_hash WHERE ph.path = ?",
            (str(p),),
        ).fetchone()
        if not row or row["motion_kind"] != "apple" or not row["motion_path"]:
            continue
        clip = Path(row["motion_path"])
        if str(clip.resolve()) not in known and clip.is_file():
            extra.append(clip)
            known.add(str(clip.resolve()))
    return paths + extra


def _repair_motion_paths(conn, moved_paths):
    """
    Rewrites motion_path for pairs whose clip just moved.

    Cheaper and more direct than re-running the whole pairing pass: the
    clip is always beside its still, so the new path is derivable from
    where the still landed.
    """
    for f in moved_paths:
        row = conn.execute(
            "SELECT ph.content_hash, m.motion_kind, m.motion_path FROM photos ph "
            "JOIN media m ON m.content_hash = ph.content_hash WHERE ph.path = ?",
            (str(f),),
        ).fetchone()
        if not row or row["motion_kind"] != "apple" or not row["motion_path"]:
            continue
        new_clip = f.parent / Path(row["motion_path"]).name
        if new_clip.is_file() and str(new_clip) != row["motion_path"]:
            conn.execute(
                "UPDATE media SET motion_path = ? WHERE content_hash = ?",
                (str(new_clip), row["content_hash"]),
            )
    conn.commit()


def _move_photos_no_scan(conn, photo_paths, dest_moment_path):
    """
    The filesystem-move + appdb-update half of move_photos, without
    triggering scanAll - split out so a caller moving many batches (e.g.
    auto_generate_moments) can do all the moves first and pay the cost of a
    full PhotoView rescan exactly once at the end, not once per batch.
    scanAll is a *global* rescan (confirmed directly: it walks the whole
    library, not just the changed paths), so calling it once per batch
    while auto-generating dozens of moments from a single flat folder would
    be minutes-per-batch instead of minutes total.
    """
    # A Live Photo's clip travels with its still, always - see
    # _expand_motion_pairs.
    photo_paths = _expand_motion_pairs(conn, photo_paths)
    dest_moment_path = Path(dest_moment_path)

    missing = [str(f) for f in photo_paths if not f.is_file()]
    if missing:
        raise FileNotFoundError(f"{len(missing)} photo(s) no longer exist on disk: {missing}")

    dest_moment_path.mkdir(exist_ok=True)

    moved_paths = []
    for f in photo_paths:
        db.upsert_photo(conn, db.content_hash_of(f), f, favorite=None)
        dest_file = dest_moment_path / f.name
        shutil.move(str(f), str(dest_file))
        _register_photo(conn, dest_file)
        moved_paths.append(dest_file)
    _repair_motion_paths(conn, moved_paths)
    return moved_paths


def move_photos(library, photo_paths, dest_moment_path):
    """
    Moves individual photos into a different (existing) moment - the
    building block for "adjust a moment's date boundary": splitting a moment
    is moving the photos on one side of the new boundary into a
    newly-created adjacent moment folder.

    Validates everything exists before touching anything. The caller's
    photo list (e.g. the client's IndexedDB cache, or a stale PhotoView
    listing) can legitimately drift from the real filesystem - confirmed in
    practice, not hypothetical: a raw shutil.move mid-batch on a missing
    file crashed with an uncaught FileNotFoundError, leaving no useful error
    and a possibly-half-completed move. Fail clearly and atomically instead
    of partway through.
    """
    conn = db.connect(library)
    moved_paths = _move_photos_no_scan(conn, photo_paths, dest_moment_path)
    _reindex_moved(conn, moved_paths)
    return moved_paths
