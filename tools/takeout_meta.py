#!/usr/bin/env python3
"""
Shared Google Takeout metadata-resolution logic: sidecar JSON lookup,
capture-time/GPS/favorited extraction, filename/EXIF date fallbacks, and
the actual EXIF/mtime writers.

Split out of import_takeout.py so it can be reused by both that script
(copies into a library, writes metadata into the copy) and
write_takeout_metadata_inplace.py (writes metadata directly onto the
Takeout export's own files, no copy) without duplicating the sidecar-
matching and date-fallback logic - both need EXACTLY the same rules, and
drift between two copies of this logic would be the kind of bug that's
invisible until a specific edge-case file hits the one that didn't get
the fix.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import piexif
from PIL import Image

MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".gif", ".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv"}
PIEXIF_WRITABLE_EXTS = {".jpg", ".jpeg"}  # piexif only understands JPEG containers
HEIC_EXTS = {".heic"}  # written via exiftool instead - see _write_heic_exif

EXIFTOOL_BIN = shutil.which("exiftool")


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _google_photos_relpath(media_path, takeout_root):
    """Path relative to "Google Photos/" (e.g. "Photos from 2024/foo.jpg"), the key stable across split archives."""
    google_photos = takeout_root / "Google Photos"
    base = google_photos if google_photos.is_dir() else takeout_root
    return media_path.relative_to(base)


class SidecarIndex:
    """Sidecar lookup by exact (dir, filename), plus a per-directory list for the truncated-name prefix scan."""

    def __init__(self, exact, by_dir):
        self.exact = exact
        self.by_dir = by_dir

    def __len__(self):
        return len(self.exact)

    def get(self, key):
        return self.exact.get(key)

    def candidates_in_dir(self, rel_dir):
        return self.by_dir.get(rel_dir, [])


def build_sidecar_index(takeout_roots):
    """
    Maps every sidecar JSON found across ALL takeout roots to the
    "Google Photos"-relative directory it lives under and its base name,
    so a media file's sidecar can be found even when Google split the
    file and its JSON into two different archive parts (Takeout vs.
    Takeout-8, say) - confirmed on a real export: 20240714_021207.jpg
    landed in Takeout/Photos from 2024/ while its
    ...supplemental-metadata.json landed in Takeout-8/Photos from
    2024/. A per-root, per-directory lookup misses this entirely.

    Keyed by (relative_dir, sidecar filename) so multiple same-named
    subfolders across roots (every split shares "Photos from 2024/")
    don't collide with each other.
    """
    index = {}
    by_dir = {}
    for takeout_root in takeout_roots:
        google_photos = takeout_root / "Google Photos"
        walk_root = google_photos if google_photos.is_dir() else takeout_root
        for json_path in walk_root.rglob("*.json"):
            rel_dir = json_path.parent.relative_to(walk_root)
            index.setdefault((rel_dir, json_path.name), json_path)
            by_dir.setdefault(rel_dir, []).append(json_path)
    return SidecarIndex(index, by_dir)


def find_sidecar(media_path, takeout_root, sidecar_index):
    """
    Google's supplemental-metadata JSON naming, in the order it's tried:

    1. `<name><ext>.supplemental-metadata.json` - the common case.
    2. For a media file whose own name already carries Google's "(N)"
       duplicate suffix (e.g. "DSCN3172(1).JPG"), Google moves the "(N)"
       to sit *after* .supplemental-metadata rather than duplicating the
       whole sidecar name: "DSCN3172.JPG.supplemental-metadata(1).json".
       Confirmed against a real export (2009 folder): DSCN3172.JPG pairs
       with DSCN3172.JPG.supplemental-metadata.json, and DSCN3172(1).JPG
       pairs with DSCN3172.JPG.supplemental-metadata(1).json - guess #1
       alone silently mismatches the two.
    3. Google also truncates the sidecar's base name (not the media
       file's) to fit a ~46-char limit before appending
       ".supplemental-metadata.json" when the combined name would
       otherwise exceed the OS/zip path-length Google exports against.
       Tried last since it's the rarest case and needs a prefix scan
       rather than a direct lookup.

    All three are looked up against sidecar_index (built by
    build_sidecar_index across every takeout root), not just
    media_path's own directory, since the matching sidecar can live in a
    sibling split archive.
    """
    rel_dir = _google_photos_relpath(media_path, takeout_root).parent
    name = media_path.name

    direct = sidecar_index.get((rel_dir, f"{name}.supplemental-metadata.json"))
    if direct:
        return direct

    # Duplicate-suffix case: split "stem(N).ext" -> stem, N, ext
    m = re.match(r"^(.*)\((\d+)\)(\.[^.]+)$", name)
    if m:
        stem, n, ext = m.groups()
        swapped = sidecar_index.get((rel_dir, f"{stem}{ext}.supplemental-metadata({n}).json"))
        if swapped:
            return swapped

    truncated_prefix = name[:47]
    for path in sidecar_index.candidates_in_dir(rel_dir):
        if path.name.startswith(truncated_prefix) and "supplemental-metadata" in path.name:
            return path

    return None


def load_sidecar(sidecar_path):
    try:
        with open(sidecar_path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def photo_taken_at(meta):
    """UTC datetime from photoTakenTime, or None if absent/malformed."""
    ts = meta.get("photoTakenTime", {}).get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def geo(meta):
    """(lat, lon) or (None, None) - Takeout emits 0.0/0.0 for "no GPS", not absence."""
    g = meta.get("geoData") or {}
    lat, lon = g.get("latitude"), g.get("longitude")
    if not lat and not lon:
        return None, None
    return lat, lon


def favorited(meta):
    return bool(meta.get("favorited"))


# Matches YYYYMMDD embedded in a filename, dash/underscore-separated or
# not: IMG_20180712_174801683.jpg, 20240714_021207.jpg,
# IMG-20220721-WA0108.jpg, bereal-2023-12-28-1011.jpeg. Deliberately does
# NOT match a bare run of 8 digits with no separator hint of being a date
# in an implausible position - old digital-camera names like DSCN3172.JPG
# have no date to find and correctly return no match, falling through to
# a real EXIF read instead (see fallback_taken_at). Verified against 5000
# real filenames from this export: 3748 matched, all with dates that
# agreed with their containing "Photos from YYYY" folder; 0 false positives
# spot-checked among the 1252 non-matches (all pre-smartphone camera names).
_FILENAME_DATE_RE = re.compile(
    r"(?P<y>19\d{2}|20\d{2})[-_]?(?P<m>0[1-9]|1[0-2])[-_]?(?P<d>0[1-9]|[12]\d|3[01])"
)


def date_from_filename(name):
    m = _FILENAME_DATE_RE.search(name)
    if not m:
        return None
    try:
        return datetime(int(m["y"]), int(m["m"]), int(m["d"]))
    except ValueError:
        return None


def date_from_real_exif(path):
    """
    Reads the SOURCE file's own EXIF DateTimeOriginal/DateTimeDigitized -
    the same tags/sub-IFD reorganize.read_capture_datetime looks at. Some
    Takeout entries have no sidecar JSON at all but still carry their
    original camera EXIF untouched (verified: IMG_20200718_120157510~2
    -edited.jpg has no sidecar in any of the 17 archives, but its own
    EXIF DateTimeOriginal is 2020:07:18 12:01:57, matching its filename
    exactly) - reading it directly is more reliable than re-deriving from
    the filename alone and works for files whose name has no date at all.
    """
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if exif:
                sub = exif.get_ifd(0x8769)
                date_str = sub.get(36867) or sub.get(36868)
                if date_str:
                    return datetime.strptime(str(date_str)[:19], "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return None


def fallback_taken_at(media_path):
    """
    Tried only when the sidecar JSON is missing or has no usable
    photoTakenTime - never overrides real sidecar data. Real EXIF first:
    it's authoritative (exact time, not just a date) when present, and
    trusting a filename-parsed date over it could clobber a correct
    time-of-day with a guessed midnight. Filename date is the fallback
    for files whose EXIF was stripped but whose name still encodes when
    they were taken (e.g. WhatsApp/BeReal exports). Returns None if
    neither yields anything, meaning the caller has no usable date at all.
    """
    from_exif = date_from_real_exif(media_path)
    if from_exif:
        return from_exif
    return date_from_filename(media_path.name)


def resolve_metadata(media_path, takeout_root, sidecar_index):
    """
    The full per-file resolution pipeline: sidecar -> filename/EXIF
    fallback. Returns (when, lat, lon, is_favorited, had_sidecar,
    used_fallback). Never writes anything - pure lookup, safe to call
    on a file that will only be metadata-written in place, never moved.
    """
    sidecar_path = find_sidecar(media_path, takeout_root, sidecar_index)
    meta = load_sidecar(sidecar_path) if sidecar_path else None
    had_sidecar = meta is not None

    when = photo_taken_at(meta) if meta else None
    lat, lon = geo(meta) if meta else (None, None)
    is_favorited = favorited(meta) if meta else False

    used_fallback = False
    if when is None:
        when = fallback_taken_at(media_path)
        used_fallback = when is not None

    return when, lat, lon, is_favorited, had_sidecar, used_fallback


def deg_to_dms_rational(deg):
    deg = abs(deg)
    d = int(deg)
    m_full = (deg - d) * 60
    m = int(m_full)
    s = round((m_full - m) * 60 * 100)
    return ((d, 1), (m, 1), (s, 100))


def write_jpeg_exif(path, when, lat, lon):
    """
    Writes DateTimeOriginal/DateTimeDigitized and GPS IFD directly into
    the JPEG, matching where reorganize.read_capture_datetime (Exif
    sub-IFD 0x8769, tags 36867/36868) and geocoding.read_gps (GPS IFD
    0x8825) already look - no lesphotos code needs to change to pick
    these up.

    Drops any embedded EXIF thumbnail before dumping, and the MakerNote
    tag (37500) proactively. piexif.dump() re-validates/re-segments
    exif_dict["thumbnail"] as JPEG data, and a real-world file can have a
    genuinely valid main image with a corrupt/truncated embedded
    thumbnail - confirmed on real Takeout files from an OPPO A94 5G
    (MediaTek camera firmware): piexif.load() succeeds, the file opens
    and displays fine, but piexif.dump() raised "Given data isn't JPEG"
    purely because of the thumbnail bytes (41 files hit this in one real
    export). MakerNote is vendor-specific binary that piexif sometimes
    mis-decodes on load (as a tuple of ints instead of bytes) and then
    can't re-encode on dump - confirmed on a real Google Pixel photo.
    Nothing in lesphotos reads either of these, so dropping them is
    lossless for anything that matters here.

    Falls back to retrying dump() with every OTHER tag also stripped
    from whichever IFD raised, one at a time, for any other vendor
    tag-encoding quirk piexif can't round-trip - seen once on a real
    photo (SceneType, tag 41729, encoded as a bare int instead of
    bytes). This is a last resort, not the common path: only reached if
    dropping thumbnail+MakerNote wasn't enough.
    """
    try:
        exif_dict = piexif.load(str(path))
        exif_dict["thumbnail"] = None
        exif_dict.get("Exif", {}).pop(37500, None)  # MakerNote
    except Exception:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

    if when is not None:
        date_str = when.strftime("%Y:%m:%d %H:%M:%S").encode()
        exif_dict.setdefault("Exif", {})
        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = date_str
        exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = date_str
        exif_dict.setdefault("0th", {})
        exif_dict["0th"][piexif.ImageIFD.DateTime] = date_str

    if lat is not None and lon is not None:
        exif_dict.setdefault("GPS", {})
        exif_dict["GPS"][piexif.GPSIFD.GPSLatitudeRef] = "N" if lat >= 0 else "S"
        exif_dict["GPS"][piexif.GPSIFD.GPSLatitude] = deg_to_dms_rational(lat)
        exif_dict["GPS"][piexif.GPSIFD.GPSLongitudeRef] = "E" if lon >= 0 else "W"
        exif_dict["GPS"][piexif.GPSIFD.GPSLongitude] = deg_to_dms_rational(lon)

    try:
        exif_bytes = piexif.dump(exif_dict)
    except ValueError as e:
        # e.g. '"dump" got wrong type of exif value.\n41729 in Exif IFD.
        # Got as <class \'int\'>.' - the tag number is the last
        # whitespace-separated token on the second line. Strip it from
        # every IFD it could plausibly be in and retry once; if the tag
        # can't be identified from the message, let the exception
        # propagate so the caller's existing fallback (mtime-only) kicks in.
        tag_id = _extract_offending_tag(str(e))
        if tag_id is None:
            raise
        for ifd_name in ("0th", "Exif", "GPS", "1st"):
            exif_dict.get(ifd_name, {}).pop(tag_id, None)
        exif_bytes = piexif.dump(exif_dict)

    piexif.insert(exif_bytes, str(path))


def _extract_offending_tag(message):
    """Pulls the tag id out of piexif's 'X in Y IFD. Got as ...' ValueError text."""
    m = re.search(r"^(\d+) in \w+ IFD\.", message, re.MULTILINE)
    return int(m.group(1)) if m else None


def write_heic_exif(path, when, lat, lon):
    """
    Same fields as write_jpeg_exif, but shelled out to exiftool: piexif
    can't parse HEIC's ISOBMFF container, and pillow_heif's own writer
    only offers a full re-encode (verified: a 3.1MB test file came back
    5x larger and re-compressed - unacceptable for invariant #2).
    exiftool patches the metadata box in place - verified a real file's
    size moved by ~130 bytes, and that reorganize.read_capture_datetime /
    geocoding.read_gps (both go through pillow_heif's registered PIL
    opener) read back exactly what was written.
    """
    args = [EXIFTOOL_BIN, "-overwrite_original", "-q"]
    if when is not None:
        date_str = when.strftime("%Y:%m:%d %H:%M:%S")
        args += [f"-DateTimeOriginal={date_str}", f"-CreateDate={date_str}"]
    if lat is not None and lon is not None:
        args += [
            f"-GPSLatitude={abs(lat)}", f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
            f"-GPSLongitude={abs(lon)}", f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}",
        ]
    if len(args) == 3:  # nothing to write
        return
    args.append(str(path))
    subprocess.run(args, check=True, capture_output=True)


def set_mtime(path, when):
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def _looks_like_jpeg(path):
    """
    Real content sniff (magic bytes), not the extension - a file named
    *.HEIC can genuinely be a JPEG. Confirmed on real Takeout files: 18
    iPhone-12-shot photos across two archives were named IMG_3009.HEIC
    etc. but are actually JPEG (JFIF) end to end, complete with real
    EXIF/GPS already embedded - some export step renamed them without
    converting. exiftool correctly refuses these ("Not a valid HEIC -
    looks more like a JPEG") rather than corrupting them, which is the
    right call, but it means routing by extension alone sent them to
    the wrong writer. JPEG always starts with the two bytes 0xFFD8.
    """
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"\xff\xd8"
    except OSError:
        return False


def write_metadata(path, when, lat, lon, stats):
    """
    Writes what resolve_metadata found onto `path` itself - EXIF for
    JPEG/HEIC, filesystem mtime for anything else (PNG/GIF/video, or
    HEIC with no exiftool on PATH). Mutates `stats` (a dict of counters)
    in place for exif_write_failed. Returns True if EXIF bytes were
    written (meaning the file's content hash changed and must be
    recomputed by the caller), False otherwise.

    Routing checks real content (_looks_like_jpeg), not just the
    extension in EITHER direction: a *.HEIC file can legitimately need
    the JPEG writer (see that function's docstring), and a *.jpg/*.jpeg
    file can legitimately NOT be JPEG at all - confirmed on real Takeout
    files: several *.jpg exports (screen-recording/Snapchat-style names)
    were actually PNG end to end. Without this check they hit
    write_jpeg_exif, which raised piexif.insert's InvalidImageDataError
    with no message text (logged as "EXIF write failed for X: ", blank)
    while still correctly falling through to the mtime-only branch -
    just noisier than it needed to be, and now avoided outright by not
    attempting the JPEG writer on non-JPEG bytes in the first place.
    """
    ext = path.suffix.lower()
    is_jpeg = (ext in PIEXIF_WRITABLE_EXTS or ext in HEIC_EXTS) and _looks_like_jpeg(path)
    wrote_exif = False

    if is_jpeg:
        try:
            write_jpeg_exif(path, when, lat, lon)
            wrote_exif = when is not None or lat is not None
        except Exception as e:
            stats["exif_write_failed"] += 1
            print(f"  ! EXIF write failed for {path.name}: {e}")
            if when is not None:
                set_mtime(path, when)
    elif ext in HEIC_EXTS and EXIFTOOL_BIN:
        try:
            write_heic_exif(path, when, lat, lon)
            wrote_exif = when is not None or lat is not None
        except Exception as e:
            stats["exif_write_failed"] += 1
            print(f"  ! HEIC EXIF write failed for {path.name}: {e}")
            if when is not None:
                set_mtime(path, when)
    elif when is not None:
        set_mtime(path, when)

    return wrote_exif


def iter_media_files(takeout_root):
    google_photos = takeout_root / "Google Photos"
    walk_root = google_photos if google_photos.is_dir() else takeout_root
    for path in walk_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() == ".json":
            continue
        if path.suffix.lower() not in MEDIA_EXTS:
            continue
        yield path
