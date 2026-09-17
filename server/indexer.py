#!/usr/bin/env python3
"""
First-party media indexer - replaces PhotoView's scan + derivative
generation (see docs/decisions/replacing-photoview.md).

Walks the library, and for each file that is new or whose (mtime_ns, size)
has changed, registers it in appdb and writes derivatives to
data/cache/<photo_id>/{thumb,preview}.webp.

Three properties matter more than raw speed here, because the target is a
DS124 (1GB RAM, ARM) holding a library heading for ~5TB:

  Incremental - the steady state is "has anything changed?", not a full
  index. An unchanged file costs one stat() and one indexed SELECT; it is
  never opened, hashed, or decoded. This is what makes a re-run seconds
  rather than hours, and it is the cost the day-to-day system actually pays.

  Memory-bounded - everything is a generator, end to end. Peak RSS is
  independent of library size: we never build a list of files, and never
  hold more than one decoded image. The single largest allocation in the
  process is one downscaled bitmap, not the library.

  Resumable - progress is committed in batches, and `derived` distinguishes
  "registered" from "derivatives on disk". A killed run restarts where it
  stopped instead of redoing everything.
"""

import argparse
import os
import resource
import re
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image

import config
import motion
import platform_hosts
import thumb_shards
from appdb import db
from moment_dates import is_month_bucket, parse_moment_date_from_parts, parse_moment_date_prefix

# HEIC is not decodable by Pillow alone. Every modern iPhone shoots it by
# default, so this is load-bearing rather than an edge case - 75 files in
# the current library and growing. Optional import so a machine without the
# wheel still indexes everything else instead of failing outright.
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIC_OK = True
except ImportError:
    HEIC_OK = False

# Roots are no longer module constants: each user has their own library,
# and its database/cache live inside it (config.Library). Passed in.

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
# Decoded in a short-lived subprocess rather than in-process - see
# _derive_heic_isolated. iPhone HEICs are 12-24MP and pillow-heif has no
# scaled-decode path (Image.draft() is a silent no-op for HEIC), so one
# decode costs 110-250MB. That memory is bounded and does plateau across
# many files, but it permanently raises this process's high-water mark,
# and this process has to coexist with the API server on a 1GB box.
# Isolating it means the peak dies with the child.
#
# ffmpeg was tried first and rejected: iPhone HEICs are stored as a grid of
# 512x512 tiles, which ffmpeg 8.0 exposes as dozens of separate streams
# rather than assembling, so -map 0:v:0 silently yields a single 512x512
# tile instead of the photo. Confirmed directly - the derivatives came out
# square. pillow-heif assembles the grid correctly.
HEIC_EXTS = {".heic", ".heif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".3gp", ".mkv", ".wmv", ".mpg", ".mpeg"}

THUMB_MAX = 400
# 1400, not 1600, and the difference is not cosmetic - it is the single
# biggest memory lever in this file. Image.draft() only downscales in
# powers of two, and only when the target is at least half the source. A
# 3072x4080 phone photo drafted to 1600 does NOT scale (1600 > 3072/2), so
# it decodes at full size: measured 193MB peak. Drafted to 1400 it decodes
# at 1/2 (1536x2040) instead. Measured directly on the real library, not
# reasoned about - see docs/decisions/replacing-photoview.md.
PREVIEW_MAX = 1400

# Batched rather than per-file: committing per photo is slow (an fsync per
# image), and accumulating the whole library is unbounded memory. 500 is the
# usual sweet spot and keeps a crash's lost work trivial.
COMMIT_BATCH = 500


def iter_media_files(root):
    """
    Yields (path, stat_result) for every media file under root.

    os.scandir rather than os.walk/glob: scandir's DirEntry carries the
    stat data from the directory read itself on most filesystems, so the
    incremental check costs no extra syscall per file. Recursive generator,
    so at 200k files this holds one directory's entries at a time rather
    than a 200k-element list.
    """
    try:
        entries = os.scandir(root)
    except (PermissionError, FileNotFoundError):
        return
    with entries:
        for entry in entries:
            name = entry.name
            # Dot-dirs are ours and the OS's (including the library's own
            # .lesphotos sidecar - without this the indexer walks into its
            # own output); IGNORED_DIRNAMES is host bookkeeping.
            if name.startswith(".") or name in platform_hosts.IGNORED_DIRNAMES:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    yield from iter_media_files(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    ext = os.path.splitext(name)[1].lower()
                    if ext in PHOTO_EXTS or ext in VIDEO_EXTS:
                        yield Path(entry.path), entry.stat()
            except OSError:
                continue


def exif_taken_at(img):
    """
    DateTimeOriginal (EXIF 0x9003), falling back to DateTime (0x0132).

    Read off the already-open image rather than a second parse of the file:
    the indexer has it open to derive from anyway, so this is free.
    """
    try:
        exif = img.getexif()
        if not exif:
            return None
        for tag in (0x9003, 0x0132):
            raw = exif.get(tag)
            if not raw:
                # DateTimeOriginal actually lives in the Exif sub-IFD on
                # most cameras, not the top-level IFD - the same gotcha
                # already hit in the metadata panel (see commit a28ee0d).
                try:
                    sub = exif.get_ifd(0x8769)
                    raw = sub.get(tag)
                except Exception:
                    raw = None
            if raw:
                # EXIF format is "YYYY:MM:DD HH:MM:SS".
                s = str(raw).strip()
                if len(s) >= 19:
                    return f"{s[0:4]}-{s[5:7]}-{s[8:10]}T{s[11:19]}"
    except Exception:
        pass
    return None


def _derive_one(src, box, dest, quality):
    """
    Decodes src no larger than needed for `box` and writes one derivative.

    Order matters for peak memory, and it is the opposite of the obvious
    one. Each of load/transpose/convert/thumbnail allocates a fresh buffer
    the size of the image it operates on, so doing the shrink LAST means
    every intermediate is full-size. Shrinking first makes the transpose and
    convert operate on a small image instead: measured 162MB -> 110MB peak
    across the same 15 real photos, purely from this reordering.

    draft() asks the JPEG decoder to downscale during decode via DCT
    scaling, so the full bitmap is never materialised at all. It only scales
    in powers of two, only when the target is at least half the source (see
    PREVIEW_MAX), and is a silent no-op for non-JPEG - safe, just not a
    saving there.
    """
    from PIL import ImageOps

    img = Image.open(src)
    try:
        img.draft("RGB", (box, box))
        img.thumbnail((box, box), Image.Resampling.BILINEAR)
        # Only convert when actually needed - a plain RGB JPEG is already in
        # the right mode, and an unconditional convert() would allocate a
        # second full buffer to produce an identical image.
        if img.mode != "RGB":
            img = img.convert("RGB")
        # EXIF orientation: phones store the sensor frame plus a rotation
        # flag, so without this portrait shots come out sideways - the same
        # class of bug as the video rotation handled in derive_video_thumb.
        # Cheap here because the image is already shrunk.
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        img.save(dest, "WEBP", quality=quality, method=0)
    finally:
        img.close()


def _derive_heic_isolated(src, out_dir):
    """
    Derives a HEIC's thumb+preview inside a throwaway subprocess.

    The decode itself is unavoidably large (see HEIC_EXTS), so rather than
    shrinking it, this confines it: the child's ~110-250MB peak is returned
    to the OS the instant it exits, leaving the indexer's own high-water
    mark untouched. Running the same work in-process measured 107MB -> 400MB
    peak on a folder with 78 HEICs.
    """
    code = (
        "import sys;from PIL import Image;import pillow_heif;"
        "pillow_heif.register_heif_opener();"
        "src,out=sys.argv[1],sys.argv[2];"
        "im=Image.open(src);im=im.convert('RGB');"
        "from PIL import ImageOps;im=ImageOps.exif_transpose(im);"
        f"im.thumbnail(({PREVIEW_MAX},{PREVIEW_MAX}),Image.Resampling.BILINEAR);"
        "im.save(out+'/preview.webp','WEBP',quality=82,method=0);"
        f"im.thumbnail(({THUMB_MAX},{THUMB_MAX}),Image.Resampling.BILINEAR);"
        "im.save(out+'/thumb.webp','WEBP',quality=80,method=0)"
    )
    r = subprocess.run(
        [sys.executable, "-c", code, str(src), str(out_dir)],
        capture_output=True, timeout=180,
    )
    if r.returncode != 0:
        raise RuntimeError(f"heic decode failed: {r.stderr.decode()[:200]}")


def derive_photo(src, out_dir):
    """
    Writes thumb + preview for a still image. Returns (width, height, taken_at).

    Deliberately decodes the source twice - once per output size - rather
    than decoding once and copy()ing. That sounds wasteful and is in fact
    cheaper: a shared decode has to be large enough for the biggest output,
    so the thumbnail would inherit the preview's bitmap. Decoding separately
    lets each pass draft down to what it actually needs, and the second
    decode is cheap precisely because it is itself drafted.

    width/height/taken_at always come from `src` itself, via a header-only
    read (measured 32MB peak vs. 235MB for a full HEIC decode on a real
    file) - never from a borrowed thumbnail below, which may be smaller
    than the original and (unlike the original) isn't guaranteed to carry
    the real EXIF block at all.
    """
    with Image.open(src) as probe:
        taken_at = exif_taken_at(probe)
        full_w, full_h = probe.size

    if src.suffix.lower() in HEIC_EXTS:
        # DSM's Image Assistant has often already paid this exact decode
        # cost (see platform_hosts.existing_photo_thumb) - reusing its
        # output skips the subprocess-isolated full HEIC decode entirely
        # for both thumb and preview, not just one of them. Falls through
        # to the real decode when no usable poster exists.
        poster = platform_hosts.existing_photo_thumb(src)
        if poster is not None:
            _derive_one(poster, PREVIEW_MAX, out_dir / "preview.webp", 82)
            _derive_one(poster, THUMB_MAX, out_dir / "thumb.webp", 80)
        else:
            _derive_heic_isolated(src, out_dir)
    else:
        _derive_one(src, PREVIEW_MAX, out_dir / "preview.webp", 82)
        _derive_one(src, THUMB_MAX, out_dir / "thumb.webp", 80)
    return full_w, full_h, taken_at


def read_photo_meta(src):
    """
    Returns (width, height, taken_at) without decoding pixels.

    Image.open only reads the header, so this costs one small read even
    for a 24MP HEIC - which is what makes the metadata-only pass
    (index(..., derive=False)) run at filesystem speed. The expensive
    part of indexing has always been the decode, never the metadata.
    """
    with Image.open(src) as probe:
        return probe.size[0], probe.size[1], exif_taken_at(probe)


def _remap(path, path_map):
    """
    Stored path -> this machine's path. Identity when path_map is None,
    which is every run on the machine that owns the library.
    """
    if not path_map:
        return path
    stored, local = path_map
    return Path(local + str(path)[len(stored):]) if str(path).startswith(stored) else path


def derive_pending(library, cache=None, limit=None, verbose=False, path_map=None):
    """
    Builds derivatives for rows registered but not yet thumbnailed
    (derived=0), newest first, up to `limit`.

    This is the throttle: run it with --limit 200 whenever convenient and
    it chips away at the backlog, resuming exactly where it stopped
    because `derived` is persisted per row. Newest-first because the
    timeline opens at the top - the photos someone actually sees get
    pixels first.
    """
    cache = Path(cache) if cache else library.cache_root
    conn = db.connect(library)
    cache.mkdir(parents=True, exist_ok=True)

    rows = conn.execute(
        "SELECT p.path, p.photo_id, p.content_hash, m.is_video "
        "FROM photos p JOIN media m ON m.content_hash = p.content_hash "
        "WHERE m.derived = 0 ORDER BY m.taken_at DESC" + (" LIMIT ?" if limit else ""),
        (limit,) if limit else (),
    ).fetchall()

    t0 = time.monotonic()
    done = failed = 0
    for r in rows:
        path = _remap(Path(r["path"]), path_map)
        if not path.exists():
            continue
        try:
            out_dir = cache / r["photo_id"]
            out_dir.mkdir(parents=True, exist_ok=True)
            if r["is_video"]:
                ok = derive_video_thumb(path, out_dir)
            else:
                derive_photo(path, out_dir)
                ok = True
            if ok:
                conn.execute(
                    "UPDATE media SET derived = 1 WHERE content_hash = ?",
                    (r["content_hash"],),
                )
                # Shard assignment always targets library.thumb_shards_dir,
                # not the (possibly remapped, see path_map/--cache above)
                # `cache` this derive run is writing thumbnails to - shards
                # are a pure cache-of-a-cache that belongs wherever the API
                # actually serves from, independent of where a remote
                # derive machine happened to write the underlying
                # thumb.webp files. thumb_path is read from `cache`
                # (this machine, right now) since that's where the bytes
                # this loop just wrote actually are. Photos and videos
                # both land at out_dir/thumb.webp (see derive_video_thumb),
                # so no is_video branch is needed here.
                thumb_shards.assign_and_append(
                    conn, library, r["content_hash"], r["photo_id"], out_dir / "thumb.webp",
                )
                done += 1
                if done % COMMIT_BATCH == 0:
                    conn.commit()
            else:
                # derive_video_thumb returns False rather than raising, so
                # without this the caller got a bare "failed=5" and no way
                # to learn why - which on a NAS with a broken ffmpeg means every
                # video fails silently and ~19% of the library is invisible.
                failed += 1
                print(f"  FAILED {path}: video thumbnail not produced "
                      f"(is ffmpeg present and working?)", file=sys.stderr)
            if verbose and (done + failed) % 25 == 0:
                print(f"  ...{done} derived, {failed} failed", file=sys.stderr)
        except Exception as e:
            failed += 1
            print(f"  FAILED {path}: {type(e).__name__}: {e}", file=sys.stderr)
    conn.commit()

    remaining = conn.execute("SELECT COUNT(*) c FROM media WHERE derived = 0").fetchone()["c"]
    conn.close()
    return {
        "derived": done, "failed": failed, "remaining": remaining,
        "elapsed_s": round(time.monotonic() - t0, 2),
    }


def _ffmpeg_scaled(src, box, dest_jpg, seek=None):
    """
    Decodes one frame of src, scaled to fit `box`, into dest_jpg.

    Writes JPEG, not WebP, deliberately: the webp encoder is not compiled
    into every ffmpeg build (confirmed - the dev machine's ffmpeg has only
    mjpeg and png), so requesting WebP here fails on some installs with an
    opaque "encoder selection failed". JPEG is universally present; the
    caller re-encodes to WebP with Pillow, which is cheap because the image
    is already small by then.

    -map 0:v:0 is required, not optional: HEIC containers carry multiple
    streams (thumbnails, depth, gain maps), and without an explicit stream
    selection ffmpeg builds a complex filtergraph and rejects the simple
    -vf with "Filtergraph was specified for a stream fed from a complex
    filtergraph".
    """
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if seek:
        # -ss before -i makes ffmpeg seek rather than decode from the start,
        # which matters on a weak CPU.
        cmd += ["-ss", seek]
    cmd += [
        "-i", str(src),
        "-map", "0:v:0",
        "-frames:v", "1",
        "-vf", f"scale='min({box},iw)':-2",
        "-q:v", "3",
        # Name the muxer instead of letting the .jpg extension choose it:
        # Synology's ffmpeg has the mjpeg ENCODER but not the image2 MUXER,
        # so extension-based selection fails with the misleading "Unable to
        # find a suitable output format for '...jpg'" before encoding is
        # even attempted. A single-frame mjpeg stream is a plain JPEG file.
        "-f", "mjpeg",
        str(dest_jpg),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
    except (subprocess.SubprocessError, OSError):
        return False
    return r.returncode == 0 and dest_jpg.exists() and dest_jpg.stat().st_size > 0


def probe_video(src):
    """
    Returns (width, height, duration, taken_at) by parsing `ffmpeg -i`.

    ffmpeg rather than ffprobe, deliberately: DSM ships /bin/ffmpeg with
    no ffprobe at all (confirmed on a DS124 - ffmpeg 4.1.9), and the
    target NAS has no package manager to add one. ffmpeg prints stream and
    format details to stderr even when producing no output, so "-f null -"
    yields everything needed without decoding the video.

    This used to shell out to ffprobe and swallow the resulting
    FileNotFoundError, which meant every video returned Nones, failed to
    derive, and became invisible in the timeline (which lists only derived
    rows) - 19% of the target library.

    One subprocess per video, at index time only - never at serve time.
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(src), "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        return None, None, None, None

    # ffmpeg exits non-zero for some inputs while still having printed
    # usable stream info, so parse stderr regardless of returncode.
    err = r.stderr
    w = h = dur = taken = None

    # The resolution sits after the pixel format in the Video: line, so
    # anchor on "Video:" rather than matching the first NxN in the output.
    m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", err)
    if m:
        w, h = int(m.group(1)), int(m.group(2))

    m = re.search(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", err)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    m = re.search(r"creation_time\s*:\s*([0-9T:\-]+)", err)
    if m:
        taken = m.group(1).replace("Z", "").split(".")[0]

    return w, h, dur, taken


def derive_video_thumb(src, out_dir):
    """
    Writes thumb + preview for a video.

    Prefers a poster frame the host has already decoded, falling back to
    extracting one with ffmpeg (see platform_hosts.existing_video_poster
    for which hosts leave one, and why). Some hosts can only do the
    first, dev machines only the second; both produce the same two files.

    autorotation is left ON in the ffmpeg path (no -noautorotate): the
    sampled iPhone clips store a landscape frame plus rotation=-90 side
    data, so ffmpeg's default rotation handling is what keeps thumbnails
    the right way up. Seeks to 1s first, falling back to the first frame
    for clips shorter than that.
    """
    poster = platform_hosts.existing_video_poster(src)
    if poster:
        try:
            with Image.open(poster) as im:
                im = im.convert("RGB")
                im.thumbnail((PREVIEW_MAX, PREVIEW_MAX), Image.Resampling.BILINEAR)
                im.save(out_dir / "preview.webp", "WEBP", quality=82, method=0)
                im.thumbnail((THUMB_MAX, THUMB_MAX), Image.Resampling.BILINEAR)
                im.save(out_dir / "thumb.webp", "WEBP", quality=80, method=0)
            return True
        except Exception:
            # A truncated or unreadable poster should fall through to
            # ffmpeg rather than failing the whole video.
            pass

    tmp = out_dir / ".frame.jpg"
    try:
        for seek in ("00:00:01", None):
            if _ffmpeg_scaled(src, PREVIEW_MAX, tmp, seek=seek):
                with Image.open(tmp) as im:
                    im.save(out_dir / "preview.webp", "WEBP", quality=82, method=0)
                    im.thumbnail((THUMB_MAX, THUMB_MAX), Image.Resampling.BILINEAR)
                    im.save(out_dir / "thumb.webp", "WEBP", quality=80, method=0)
                return True
        return False
    finally:
        tmp.unlink(missing_ok=True)


def link_motion_pairs(library, verbose=False, path_map=None):
    """
    Pairs Apple Live Photo stills with their .MOV halves.

    A second pass rather than inline work, because a .MOV is frequently
    walked before the still it belongs to - pairing during the walk would
    depend on directory order, which is not something to rely on.

    Only .MOV/.MP4 rows are scanned for identifiers, and only stills that
    have one are considered, so the expensive whole-file read
    (quicktime_content_id, ~26ms) happens once per video and never for
    ordinary photos.

    Marks the clip is_motion_clip=1 so the timeline can hide it. The file
    stays exactly where it is - it is still a real photo on disk
    (invariant #1), it just stops being its own timeline entry.

    path_map is the same (stored_prefix, local_prefix) pair derive_pending
    takes, for running this from a machine that mounts the library
    elsewhere. Lookups are remapped; what is written back is not.
    """
    conn = db.connect(library)

    rows = conn.execute(
        "SELECT p.path, p.content_hash, m.is_video, m.motion_kind FROM photos p "
        "JOIN media m ON m.content_hash = p.content_hash"
    ).fetchall()

    # Google motion is normally detected during the walk, but an unchanged
    # file takes the incremental skip path and never gets there - so a
    # library indexed before this existed would stay blank forever. Fill it
    # in here, where every row is visited anyway.
    google = 0
    for r in rows:
        if r["is_video"] or r["motion_kind"]:
            continue
        path = _remap(Path(r["path"]), path_map)
        if not path.exists():
            continue
        off = motion.google_motion_offset(path)
        if off is not None:
            conn.execute(
                "UPDATE media SET motion_kind = 'google', motion_offset = ? "
                "WHERE content_hash = ?",
                (off, r["content_hash"]),
            )
            google += 1

    stills, clips = {}, {}
    for r in rows:
        path = _remap(Path(r["path"]), path_map)
        if not path.exists():
            continue
        if r["is_video"]:
            if path.suffix.lower() in (".mov", ".mp4"):
                cid = motion.quicktime_content_id(path)
                if cid:
                    clips[cid] = r
        else:
            cid = motion.apple_content_id(path)
            if cid:
                stills[cid] = r

    linked = 0
    for cid, still in stills.items():
        clip = clips.get(cid)
        if not clip:
            continue
        conn.execute(
            "UPDATE media SET motion_kind = 'apple', motion_path = ? WHERE content_hash = ?",
            (clip["path"], still["content_hash"]),
        )
        conn.execute(
            "UPDATE media SET is_motion_clip = 1 WHERE content_hash = ?",
            (clip["content_hash"],),
        )
        linked += 1
        if verbose:
            print(f"  pair {Path(still['path']).name} + {Path(clip['path']).name}", file=sys.stderr)

    # A clip whose still is gone must become visible again, or deleting the
    # still would hide the video forever with no way to reach it.
    orphaned = conn.execute(
        "UPDATE media SET is_motion_clip = 0 WHERE is_motion_clip = 1 AND content_hash NOT IN ("
        "  SELECT m2.content_hash FROM media m2 JOIN photos p2 ON p2.content_hash = m2.content_hash"
        "  WHERE p2.path IN (SELECT motion_path FROM media WHERE motion_path IS NOT NULL))"
    ).rowcount
    conn.commit()
    conn.close()
    return {"linked": linked, "unhidden": max(0, orphaned), "google": google}


def prune(library, root=None, verbose=False):
    """
    Drop rows whose file is no longer on disk, returning how many went.

    The filesystem is the source of truth (invariant #1), so a file that has
    been deleted or moved outside the library has to stop being served -
    otherwise the timeline keeps offering it and every thumbnail request for
    it 404s. Without this the only ways to clean up were hand-written SQL or
    wiping the database and re-indexing the whole library.

    Scoped to `root` (defaulting to the whole library): rows for paths
    outside it are left alone, so pointing the indexer at a subdirectory
    can never delete the rest of the library.
    """
    root = Path(root) if root else library.photos_root
    conn = db.connect(library)
    prefix = str(root)
    rows = conn.execute(
        "SELECT content_hash, path FROM photos WHERE path LIKE ?",
        (prefix + "%",),
    ).fetchall()

    gone = [r for r in rows if not Path(r["path"]).exists()]
    for r in gone:
        if verbose:
            print(f"  prune {r['path']}", file=sys.stderr)
        # media first: photos holds the path this selected on.
        conn.execute("DELETE FROM media WHERE content_hash = ?", (r["content_hash"],))
        conn.execute("DELETE FROM photos WHERE content_hash = ?", (r["content_hash"],))
    conn.commit()

    # A file that failed to read (so never got a photos row at all) needs
    # its own existence check here - it would otherwise sit in failed_reads
    # forever once deleted, since it's invisible to the `photos` scan above.
    for r in conn.execute(
        "SELECT path FROM failed_reads WHERE path LIKE ?", (prefix + "%",)
    ).fetchall():
        if not Path(r["path"]).exists():
            if verbose:
                print(f"  prune (failed-read) {r['path']}", file=sys.stderr)
            db.clear_read_failure(conn, r["path"])

    return len(gone)


def rebalance_month_bucket_dates(conn, folder_paths):
    """
    Fixes moment_date for every photo in a bare "YYYY-MM" folder to the
    NEWEST taken_at date of any photo currently in it, not the 1st of the
    month.

    A day-level moment's folder name IS a date the user chose, so it can
    sort by that name alone. A month bucket has no such date - it's an
    interim holding pen the watcher creates before auto_generate_moments
    sweeps it into real moments - so parse_moment_date_prefix's day=1
    default was sorting a photo taken on the 17th as if from the 1st,
    pushing it behind older, fully-dated moments instead of near the top.
    Recomputed for every photo in the folder (not just the new arrival)
    because the "newest photo in this folder" fact changes for all of
    them whenever one more photo lands.

    `folder_paths` is deliberately a set of PARENT directories, not
    individual photo paths - callers pass the small number of folders
    actually touched by one indexing pass, not one row per photo, since
    the whole point is to correct every sibling with one query per folder.
    """
    for folder in {Path(f) for f in folder_paths}:
        if not is_month_bucket(folder.name, folder.parent.name):
            continue
        prefix = str(folder) + "/"
        row = conn.execute(
            "SELECT MAX(m.taken_at) newest FROM media m JOIN photos p ON p.content_hash = m.content_hash "
            "WHERE p.path LIKE ? || '%' AND instr(substr(p.path, length(?) + 1), '/') = 0",
            (prefix, prefix),
        ).fetchone()
        if not row["newest"]:
            continue
        newest_date = row["newest"][:10]
        conn.execute(
            "UPDATE media SET moment_date = ? WHERE content_hash IN ("
            "  SELECT p.content_hash FROM photos p WHERE p.path LIKE ? || '%'"
            "    AND instr(substr(p.path, length(?) + 1), '/') = 0"
            ")",
            (newest_date, prefix, prefix),
        )
    conn.commit()


def backfill_moment_dates(library, root=None, verbose=False):
    """
    Populates media.moment_date for rows indexed before that column existed.

    Deliberately not folded into the incremental scan: moment_date is
    derived from the path alone (which `photos` already has), not from file
    contents, so re-deriving it needs no stat/hash/decode - just a read of
    rows already in the database. Everything else in this module's "one
    pass must visit every row, or the incrementally-skipped ones stay blank
    forever" caution still applies: an unchanged file never reaches index()'s
    per-file body, so a column added after a library was first indexed would
    otherwise be NULL on every existing row until this runs once.
    """
    root = Path(root) if root else library.photos_root
    conn = db.connect(library)
    prefix = str(root)
    rows = conn.execute(
        "SELECT p.content_hash, p.path FROM photos p "
        "JOIN media m ON m.content_hash = p.content_hash "
        "WHERE p.path LIKE ? AND m.moment_date IS NULL",
        (prefix + "%",),
    ).fetchall()

    updated = 0
    touched_folders = set()
    for r in rows:
        path = Path(r["path"])
        moment_date = parse_moment_date_from_parts(path.parent.name, path.parent.parent.name)
        if moment_date is None:
            continue
        conn.execute(
            "UPDATE media SET moment_date = ? WHERE content_hash = ?",
            (moment_date.isoformat(), r["content_hash"]),
        )
        updated += 1
        touched_folders.add(path.parent)
        if verbose and updated % 200 == 0:
            print(f"  ...{updated} backfilled", file=sys.stderr)
    conn.commit()
    # A previously-backfilled month bucket that gains new siblings on a
    # later run also needs rebalancing, not just brand-new rows - a
    # NULL-only WHERE above would otherwise never revisit it.
    rebalance_month_bucket_dates(conn, touched_folders)
    return {"seen": len(rows), "updated": updated}


def index(library, root=None, cache=None, limit=None, verbose=False,
          progress_every=200, derive=True):
    """
    Walks `root` (default: the whole library), registering files and
    building their derivatives.

    derive=False registers metadata only - dimensions, capture time,
    hash - and leaves `derived` at 0, writing no thumbnails. That is the
    cheap pass: it is what the timeline actually needs to render, and it
    runs at filesystem speed instead of decode speed. Pixels can then be
    filled in afterwards by derive_pending(), a batch at a time, or on
    another machine entirely. Without this split, a first run on a large
    library is one all-or-nothing multi-hour decode.
    """
    root = Path(root) if root else library.photos_root
    cache = Path(cache) if cache else library.cache_root
    conn = db.connect(library)
    cache.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    seen = skipped = indexed = failed = 0
    pending = 0
    touched_folders = set()

    for path, st in iter_media_files(root):
        if limit and seen >= limit:
            break
        seen += 1

        rel = str(path)
        existing = conn.execute(
            "SELECT m.mtime_ns, m.size, m.derived FROM photos p "
            "JOIN media m ON m.content_hash = p.content_hash WHERE p.path = ?",
            (rel,),
        ).fetchone()

        # The incremental fast path, and the whole point of the design: an
        # unchanged, already-derived file costs one stat (already done by
        # scandir) and this one indexed lookup. No open, no hash, no decode.
        if (existing and existing["mtime_ns"] == st.st_mtime_ns
                and existing["size"] == st.st_size
                and (existing["derived"] or not derive)):
            skipped += 1
            if verbose and seen % progress_every == 0:
                print(f"  ...{seen} seen, {skipped} unchanged, {indexed} indexed", file=sys.stderr)
            continue

        try:
            # Hashing is the expensive part of registration, so it only
            # happens for files that actually changed - never on a re-run.
            chash = db.content_hash_of(path)
            db.upsert_photo(conn, chash, path)
            row = conn.execute("SELECT photo_id FROM photos WHERE content_hash = ?", (chash,)).fetchone()
            photo_id = row["photo_id"]

            ext = path.suffix.lower()
            out_dir = cache / photo_id
            # Only when something is actually going to be written into it -
            # a metadata-only pass would otherwise litter the cache with
            # thousands of empty directories.
            if derive:
                out_dir.mkdir(parents=True, exist_ok=True)

            is_video = ext in VIDEO_EXTS
            if is_video:
                # probe_video is an ffmpeg metadata read, cheap; the
                # thumbnail is the expensive decode, so only that is gated.
                w, h, dur, taken = probe_video(path)
                ok = derive_video_thumb(path, out_dir) if derive else False
            else:
                dur = None
                if derive:
                    w, h, taken = derive_photo(path, out_dir)
                    ok = True
                else:
                    w, h, taken = read_photo_meta(path)
                    ok = False

            # Never null, so the timeline's ORDER BY has no missing sort key
            # to special-case.
            if not taken:
                taken = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime))

            # Motion: a Google file carries its clip inside itself, so it
            # can be resolved here. Apple pairing needs both halves and is
            # done by link_motion_pairs() once the whole walk is done -
            # a .MOV is often reached before its still.
            m_kind = m_offset = None
            if not is_video:
                m_offset = motion.google_motion_offset(path)
                if m_offset is not None:
                    m_kind = "google"

            moment_date = parse_moment_date_from_parts(path.parent.name, path.parent.parent.name)
            moment_date = moment_date.isoformat() if moment_date else None

            conn.execute(
                "INSERT INTO media (content_hash, mtime_ns, size, taken_at, width, height, is_video, duration, derived, motion_kind, motion_offset, moment_date) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(content_hash) DO UPDATE SET "
                "mtime_ns=excluded.mtime_ns, size=excluded.size, taken_at=excluded.taken_at, "
                "width=excluded.width, height=excluded.height, is_video=excluded.is_video, "
                "duration=excluded.duration, derived=excluded.derived, "
                "motion_kind=excluded.motion_kind, motion_offset=excluded.motion_offset, "
                "moment_date=excluded.moment_date",
                (chash, st.st_mtime_ns, st.st_size, taken, w, h, int(is_video), dur, int(ok),
                 m_kind, m_offset, moment_date),
            )
            db.clear_read_failure(conn, path)
            indexed += 1
            pending += 1
            touched_folders.add(path.parent)
            if pending >= COMMIT_BATCH:
                conn.commit()
                pending = 0
        except Exception as e:
            failed += 1
            db.record_read_failure(conn, path, f"{type(e).__name__}: {e}")
            print(f"  FAILED {path}: {type(e).__name__}: {e}", file=sys.stderr)

        if verbose and seen % progress_every == 0:
            print(f"  ...{seen} seen, {skipped} unchanged, {indexed} indexed", file=sys.stderr)

    conn.commit()
    # A YYYY-MM bucket has no date of its own (unlike a real moment, whose
    # folder name the user chose), so every photo in it needs re-sorting to
    # the folder's newest arrival whenever this run added anything there -
    # not just the new file, since "newest in this folder" changed for
    # every sibling too. Cheap: one query per distinct folder touched, not
    # per photo.
    rebalance_month_bucket_dates(conn, touched_folders)
    conn.close()

    elapsed = time.monotonic() - t0
    # ru_maxrss is bytes on macOS, kilobytes on Linux - the DS124 is Linux,
    # dev is a Mac, so normalise rather than reporting a 1024x-wrong number
    # on one of them.
    scale = (1024 * 1024) if sys.platform == "darwin" else 1024
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale
    # Reported separately because they are different capacity questions.
    # RUSAGE_CHILDREN is dominated by ffmpeg decoding HEVC, which is
    # transient (one child at a time, exiting between files) and tunable by
    # not running video derivation concurrently. The indexer's own footprint
    # is what has to coexist with the API server and everything else on the
    # NAS, and it is far smaller - conflating the two makes the process look
    # ~4x heavier than it is.
    child_mb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / scale

    return {
        "seen": seen, "skipped": skipped, "indexed": indexed, "failed": failed,
        "elapsed_s": round(elapsed, 2),
        "peak_rss_mb": round(peak_mb, 1),
        "peak_child_rss_mb": round(child_mb, 1),
        "files_per_s": round(seen / elapsed, 1) if elapsed > 0 else 0,
    }


def main():
    ap = argparse.ArgumentParser(
        description="Index one user's media into their library's appdb + derivative cache",
    )
    ap.add_argument(
        "--user",
        help="index this user's library (looked up in the auth DB). "
             "Either --user or --root is required.",
    )
    ap.add_argument(
        "--root", type=Path,
        help="index this photos folder directly, without an account. Its "
             "database and cache live in <root>/.lesphotos/.",
    )
    ap.add_argument(
        "--only", type=Path,
        help="restrict the walk to this subdirectory of the library "
             "(still writes to the library's own database)",
    )
    ap.add_argument("--cache", type=Path, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument(
        "--no-derive", action="store_true",
        help="register metadata only, write no thumbnails. Fast first pass: "
             "the timeline works, pixels arrive later via --derive-pending.",
    )
    ap.add_argument(
        "--derive-pending", action="store_true",
        help="build derivatives for already-registered photos that have "
             "none yet, newest first. Combine with --limit to do a batch "
             "at a time; safe to re-run, it resumes where it left off.",
    )
    ap.add_argument(
        "--db", type=Path, default=None,
        help="use this database file instead of the library's own. For "
             "deriving on a second machine: SQLite over SMB/NFS cannot lock "
             "correctly, so copy the sidecar db local, work against it, and "
             "copy it back.",
    )
    ap.add_argument(
        "--path-prefix", default=None, metavar="STORED=LOCAL",
        help="remap stored paths for this machine, e.g. "
             "/volume1/homes/Bob/Photos=/Volumes/homes/Bob/Photos. "
             "Only affects where files are read; rows are written unchanged.",
    )
    ap.add_argument(
        "--prune", action="store_true",
        help="also drop rows whose file no longer exists",
    )
    ap.add_argument(
        "--status", action="store_true",
        help="print how many photos are registered and how many still need "
             "derivatives, then exit",
    )
    ap.add_argument(
        "--backfill-shards", action="store_true",
        help="one-time catch-up: assign a thumb-zip shard (see thumb_shards.py) "
             "to every already-derived photo that predates the shard system. "
             "Safe to interrupt and re-run.",
    )
    args = ap.parse_args()

    if not args.user and not args.root:
        ap.error("one of --user or --root is required")
    if args.user and args.root:
        ap.error("--user and --root are mutually exclusive")

    if args.user:
        import auth
        conn = auth.connect()
        row = auth.get_user_by_name(conn, args.user)
        if row is None:
            ap.error(f"no such user: {args.user!r} (see: python3 manage.py list-users)")
        library = auth.library_for(row)
        if args.db:
            library = config.Library(library.photos_root, db_path=args.db)
    else:
        library = config.Library(args.root, db_path=args.db)

    if not library.photos_root.is_dir():
        ap.error(f"photos root does not exist: {library.photos_root}")

    if args.status:
        conn = db.connect(library)
        total = conn.execute("SELECT COUNT(*) c FROM photos").fetchone()["c"]
        pending = conn.execute("SELECT COUNT(*) c FROM media WHERE derived = 0").fetchone()["c"]
        failures = db.list_read_failures(conn)
        print(f"library:    {library.photos_root}")
        print(f"registered: {total}")
        print(f"thumbnailed:{total - pending}")
        print(f"pending:    {pending}")
        print(f"unreadable: {len(failures)}")
        for r in failures:
            print(f"  {r['path']}: {r['error']} (since {r['first_seen_at']})", file=sys.stderr)
        return

    if not HEIC_OK:
        print("note: pillow-heif not installed - .heic/.heif files will fail", file=sys.stderr)

    path_map = None
    if args.path_prefix:
        if "=" not in args.path_prefix:
            ap.error("--path-prefix must look like STORED=LOCAL")
        stored, _, local = args.path_prefix.partition("=")
        path_map = (stored.rstrip("/"), local.rstrip("/"))

    if args.derive_pending:
        print(f"deriving pending thumbnails in {library.photos_root}", file=sys.stderr)
        stats = derive_pending(
            library, cache=args.cache, limit=args.limit,
            verbose=args.verbose, path_map=path_map,
        )
        print(
            f"\nderived={stats['derived']} failed={stats['failed']} "
            f"remaining={stats['remaining']}  elapsed={stats['elapsed_s']}s",
            file=sys.stderr,
        )
        return

    if args.backfill_shards:
        print(f"backfilling thumb-zip shards for {library.photos_root}", file=sys.stderr)
        conn = db.connect(library)
        stats = thumb_shards.backfill(conn, library, verbose=args.verbose)
        print(
            f"\nbackfilled={stats['backfilled']} "
            f"skipped(no thumb on disk)={stats['skipped_no_thumb']} "
            f"of {stats['total_candidates']} candidates",
            file=sys.stderr,
        )
        return

    walk_root = args.only or library.photos_root
    print(f"indexing {walk_root} -> {args.cache or library.cache_root}", file=sys.stderr)
    stats = index(
        library, root=walk_root, cache=args.cache, limit=args.limit,
        verbose=args.verbose, derive=not args.no_derive,
    )
    print(
        f"\nseen={stats['seen']} unchanged={stats['skipped']} indexed={stats['indexed']} "
        f"failed={stats['failed']}\n"
        f"elapsed={stats['elapsed_s']}s  {stats['files_per_s']} files/s\n"
        f"peak_rss={stats['peak_rss_mb']}MB (self)  "
        f"{stats['peak_child_rss_mb']}MB (ffmpeg, transient)",
        file=sys.stderr,
    )
    if stats["failed"]:
        print(
            f"see --status for the unreadable file list (persists across runs)",
            file=sys.stderr,
        )
    # After the walk, not during: a .MOV is often reached before the still
    # it belongs to, so pairing has to see the whole library.
    pairs = link_motion_pairs(library, verbose=args.verbose, path_map=path_map)
    if pairs["linked"] or pairs["unhidden"] or pairs["google"]:
        print(
            f"motion: {pairs['linked']} live-photo pairs linked"
            + (f", {pairs['google']} motion photos" if pairs["google"] else "")
            + (f", {pairs['unhidden']} clips unhidden" if pairs["unhidden"] else ""),
            file=sys.stderr,
        )

    # Cheap even on an already-backfilled library (an indexed WHERE
    # moment_date IS NULL, no rows to touch) - unconditional so a database
    # from before this column existed catches up on every run, not just
    # once someone remembers to ask for it.
    backfill = backfill_moment_dates(library, root=walk_root, verbose=args.verbose)
    if backfill["updated"]:
        print(f"moment_date: backfilled {backfill['updated']} rows", file=sys.stderr)

    if args.no_derive:
        print(
            "registered metadata only. Build thumbnails with:\n"
            "  python3 indexer.py --user <name> --derive-pending --limit 200",
            file=sys.stderr,
        )

    if args.prune:
        removed = prune(library, root=args.only, verbose=args.verbose)
        print(f"pruned={removed} rows whose file is gone", file=sys.stderr)


if __name__ == "__main__":
    main()
