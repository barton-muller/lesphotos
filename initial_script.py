#!/usr/bin/env python3
# =============================================================================
# --- 1 initial clone-and-sort implementation (dump copied into a separate tree)
# --- 2 in-place sort: the dump IS the target. Photos are MOVED, not copied.
#
#       Before : <root>/2024/03/IMG_1234.jpg
#                <root>/2024/03/@eaDir/IMG_1234.jpg/SYNOFILE_THUMB_M.jpg
#
#       After  : <root>/2024/2024-03-15-amsterdam/
#                    2024-03-15 alice grote-markt.amsterdam.nl ~IMG_1234.1~.jpg
#                <root>/2024/2024-03-15-amsterdam/@eaDir/
#                    2024-03-15 alice grote-markt.amsterdam.nl ~IMG_1234.1~.jpg/
#                        SYNOFILE_THUMB_M.jpg
#
#       Because this rewrites the library rather than duplicating it, v2 adds:
#         - an idempotency guard, so a second run cannot re-process its own output
#         - an undo log written as each move completes
#         - DRY_RUN defaulting to True
# =============================================================================

import os
import re
import shutil
from collections import Counter
from datetime import datetime, timedelta
from PIL import Image
from pillow_heif import register_heif_opener
from geopy.geocoders import Nominatim
import time

# HEIC files are opened only for their embedded metadata; the pixels are never
# decoded, so this stays cheap even on a slow ARM NAS with no HEVC codec.
register_heif_opener()

# ----------------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------------
PHOTO_ROOT = "/volume1/Photos/PhotoDump"   # the dump, sorted in place
USER_FIRST_NAME = "alice"                  # the %user% field in the filename

# An event ends when the gap to the next photo exceeds this. Two days keeps a
# holiday together across overnight gaps; drop it to hours to split a trip into
# individual days.
EVENT_GAP = timedelta(days=2)

SEPARATOR = "."           # between street / city / country
NAME_WRAP = "~"           # brackets the original name and its sequence number

DRY_RUN = True            # True = report only, move nothing
REMOVE_EMPTY_MONTH_DIRS = False   # prune drained MM folders once a run completes
UNDO_LOG = "/volume1/Photos/photo_sort_undo.tsv"

# Reverse geocoding is the slowest part of a run. Photos from one event share a
# location, so results are cached against coordinates rounded to this many
# decimal places (3 dp is roughly 110 m). The walk stays O(n) over the photos,
# but tens of thousands of network round-trips collapse to a few dozen.
GEO_PRECISION = 3
_geo_cache = {}

geolocator = Nominatim(user_agent="synology_photo_inplace_sort")

# A month folder in the dump: "3", "03", "12". Nothing else is touched.
MONTH_DIR = re.compile(r"^\d{1,2}$")

# A folder this script has already created. The guard that stops run two from
# eating run one's output.
SORTED_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}-")


# ----------------------------------------------------------------------------
# --- 2.1 top-level coordinator
# ----------------------------------------------------------------------------
def main():
    """
    Runs the four stages of an in-place sort:
      1. find photos sitting in YYYY/MM month folders (and nowhere else)
      2. read each timestamp and GPS fix, resolve coordinates to a place name
      3. cut the chronological run into events on time gaps and year boundaries
      4. move each event's files, dragging their thumbnail folders along
    """

    photo_paths = scan_dump_tree(PHOTO_ROOT)
    if not photo_paths:
        print("No unsorted photos found under", PHOTO_ROOT)
        return
    print(f"Found {len(photo_paths)} unsorted photos. Reading metadata...")

    all_photos = [describe_photo(p) for p in photo_paths]

    batches = split_into_events(all_photos)
    print(f"Grouped into {len(batches)} event folders.\n")

    # The log is opened before the first move so that a crash mid-run still
    # leaves a complete record of everything that had already been relocated.
    log = None if DRY_RUN else open(UNDO_LOG, "a", encoding="utf-8")
    try:
        for batch in batches:
            move_batch(batch, log)
    finally:
        if log:
            log.close()

    if REMOVE_EMPTY_MONTH_DIRS and not DRY_RUN:
        prune_empty_dirs(PHOTO_ROOT)

    if DRY_RUN:
        print("\nDRY RUN - nothing was moved. Set DRY_RUN = False to commit.")
    else:
        print(f"\nDone. Undo log appended to {UNDO_LOG}")


# ----------------------------------------------------------------------------
# --- 2.2 finds photos still sitting in the YYYY/MM dump
# ----------------------------------------------------------------------------
def scan_dump_tree(root):
    """
    Collects photos from month folders only.

    Two exclusions matter, and both are load-bearing:

    @eaDir - Synology stores generated thumbnails in hidden @eaDir folders
    beside every image. Those hold real JPEGs, so an unguarded recursive scan
    would ingest thumbnails as though they were photographs.

    Sorted folders - because the dump is now also the destination, a second run
    would otherwise re-process its own output, wrapping each name inside the
    previous one. Restricting the walk to folders named like a month makes the
    script safely re-runnable: anything already moved is invisible to it.
    """
    extensions = {".jpg", ".jpeg", ".heic", ".png"}
    found = []

    for dirpath, dirnames, filenames in os.walk(root):
        # Editing dirnames in place stops os.walk descending further
        dirnames[:] = [d for d in dirnames
                       if d != "@eaDir" and not SORTED_DIR.match(d)]

        # Only harvest files from a month folder; year folders and the root
        # itself are traversed but never read
        if not MONTH_DIR.match(os.path.basename(dirpath)):
            continue

        for name in filenames:
            if os.path.splitext(name)[1].lower() in extensions:
                found.append(os.path.join(dirpath, name))

    return found


# ----------------------------------------------------------------------------
# --- 2.3 reads timestamp and GPS from one photo
# ----------------------------------------------------------------------------
def get_photo_data(image_path):
    """
    Extracts capture datetime and decimal GPS coordinates.

    Returns (datetime, latitude, longitude); the coordinates are None when the
    photo carries no GPS fix. Falls back to file modification time when EXIF
    has no DateTimeOriginal.
    """
    capture_dt = None
    lat_deg, lon_deg = None, None

    try:
        with Image.open(image_path) as img:
            exif = img.getexif()

            if exif:
                # 36867 = DateTimeOriginal (when the shutter fired)
                # 306   = DateTime (last modified) - a weaker fallback
                date_str = exif.get(36867) or exif.get(306)
                if date_str:
                    try:
                        capture_dt = datetime.strptime(date_str[:19], "%Y:%m:%d %H:%M:%S")
                    except ValueError:
                        pass

            if not capture_dt:
                capture_dt = datetime.fromtimestamp(os.path.getmtime(image_path))

            gps = exif.get_ifd(0x8825) if exif else None
            if gps:
                lat_deg = _dms_to_decimal(gps.get(2), gps.get(1))
                lon_deg = _dms_to_decimal(gps.get(4), gps.get(3))

    except Exception as e:
        print(f"  ! metadata unreadable for {os.path.basename(image_path)}: {e}")

    if not capture_dt:
        capture_dt = datetime.now()

    return capture_dt, lat_deg, lon_deg


# ----------------------------------------------------------------------------
# --- 1.3.1 (removed in 1.3.2) the original conversion was mathematically wrong
# def to_deg(v):
#     return float(v)/float(v) if isinstance(v, tuple) else float(v)
# lat_deg = to_deg(lat) + to_deg(lat)/60.0 + to_deg(lat)/3600.0
#
# float(v)/float(v) is identically 1.0, and the whole (d, m, s) tuple was passed
# three times instead of indexed. Every photo resolved to 1 + 1/60 + 1/3600 =
# 1.0169 degrees in both axes - a point in the Gulf of Guinea.
# ----------------------------------------------------------------------------
def _dms_to_decimal(dms, ref):
    """
    Converts EXIF degrees/minutes/seconds plus a hemisphere reference into a
    signed decimal degree. EXIF stores each component as a rational, which
    float() resolves directly.
    """
    if not dms or not ref:
        return None
    try:
        degrees = float(dms[0]) + float(dms[1]) / 60.0 + float(dms[2]) / 3600.0
        # South of the equator and west of Greenwich are negative
        if ref in ("S", "W"):
            degrees = -degrees
        return degrees
    except (TypeError, IndexError, ValueError):
        return None


# ----------------------------------------------------------------------------
# --- 2.4 resolves coordinates to "street.city.cc", with caching
# ----------------------------------------------------------------------------
def get_location_details(lat, lon):
    """
    Reverse-geocodes a coordinate to a filename-safe location string.
    Returns None when the photo has no GPS fix, so callers can tell
    "nowhere recorded" apart from "lookup failed".
    """
    if lat is None or lon is None:
        return None

    key = (round(lat, GEO_PRECISION), round(lon, GEO_PRECISION))
    if key in _geo_cache:
        return _geo_cache[key]

    result = SEPARATOR.join(["unknown", "unknown", "xx"])
    try:
        time.sleep(1)  # Nominatim usage policy: at most one request per second
        location = geolocator.reverse(key, timeout=10, language="en")

        if location and "address" in location.raw:
            address = location.raw["address"]
            street = address.get("road") or address.get("suburb") or "unknown"
            city = (address.get("city") or address.get("town")
                    or address.get("village") or "unknown")
            cc = address.get("country_code", "xx").lower()
            if cc == "gb":
                cc = "uk"   # the postal convention people actually recognise
            result = SEPARATOR.join([_slug(street), _slug(city), cc])

    except Exception as e:
        print(f"  ! geocoding failed for {key}: {e}")

    _geo_cache[key] = result
    return result


# ----------------------------------------------------------------------------
# --- 2.5 makes a text fragment safe and consistent inside a filename
# ----------------------------------------------------------------------------
def _slug(text):
    """
    Lowercases, collapses whitespace to hyphens and strips anything outside
    [a-z0-9-]. Guarantees pure ASCII output whatever Nominatim returns, so a
    Greek or Cyrillic street name cannot smuggle non-ASCII into a filename.
    """
    text = text.lower().replace(" ", "-")
    return re.sub(r"[^a-z0-9-]", "", text) or "unknown"


# ----------------------------------------------------------------------------
# --- 2.6 bundles one photo's metadata into a record
# ----------------------------------------------------------------------------
def describe_photo(path):
    """Collects everything the later stages need about a single photo."""
    when, lat, lon = get_photo_data(path)
    return {
        "path": path,
        "when": when,
        "loc": get_location_details(lat, lon),
    }


# ----------------------------------------------------------------------------
# --- 2.7 cuts the chronological run into event batches
# ----------------------------------------------------------------------------
def split_into_events(photos):
    """
    Sorts all photos by capture time and starts a new batch when either:
      - the gap since the previous photo exceeds EVENT_GAP
      - the calendar year changes (a year boundary always ends a folder)

    Single pass over a sorted list, so O(n log n) overall, dominated by the
    sort. Location plays no part in grouping: an event crossing a town boundary
    stays one event, and the place name only reaches the name.

    Note this works from EXIF dates, not the MM folder a photo happens to sit
    in. A photo filed under 2024/01 but shot on 31 December 2023 will move into
    the 2023 tree, which is the correct outcome and worth expecting.
    """
    photos.sort(key=lambda p: p["when"])

    batches = []
    current = []

    for item in photos:
        if not current:
            current = [item]
            continue

        previous = current[-1]["when"]
        gap_too_large = (item["when"] - previous) > EVENT_GAP
        year_changed = item["when"].year != previous.year

        if gap_too_large or year_changed:
            batches.append(current)
            current = [item]
        else:
            current.append(item)

    if current:
        batches.append(current)

    return batches


# ----------------------------------------------------------------------------
# --- 2.8 builds the destination filename
# ----------------------------------------------------------------------------
def get_unique_filename(target_dir, base_name, extension):
    """
    Returns a path whose sequence number is always present, starting at 1 and
    rising only if that exact name is already taken.
    """
    counter = 1
    while True:
        candidate = os.path.join(target_dir, f"{base_name}.{counter}{NAME_WRAP}{extension}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def build_base_name(record):
    """
    Assembles everything left of the sequence number:
        2024-03-15 alice grote-markt.amsterdam.nl ~IMG_1234
    The closing wrapper is added by get_unique_filename once the number is known.
    """
    date_part = record["when"].strftime("%Y-%m-%d")
    place = record["loc"] or "no-geo"
    original = os.path.splitext(os.path.basename(record["path"]))[0]
    return f"{date_part} {USER_FIRST_NAME} {place} {NAME_WRAP}{original}"


# ----------------------------------------------------------------------------
# --- 2.9 moves one event's photos into their dated folder
# ----------------------------------------------------------------------------
def move_batch(batch, log):
    """
    Creates <root>/YYYY/YYYY-MM-DD-city/ and moves the batch into it.

    The folder is named for the first photo's date and the batch's most common
    city, so a touring day lands under wherever most of it happened. Rename the
    folder freely afterwards - the re-run guard keys on the YYYY-MM-DD- prefix
    only, so any suffix you like is safe.

    Within one volume shutil.move degrades to os.rename, which is a metadata
    operation: no bytes are copied and no extra space is needed, however large
    the library.
    """
    first = batch[0]["when"]
    year_dir = os.path.join(PHOTO_ROOT, str(first.year))

    cities = [p["loc"].split(SEPARATOR)[1] for p in batch if p["loc"]]
    label = Counter(cities).most_common(1)[0][0] if cities else "no-geo"

    target_dir = os.path.join(year_dir, f"{first.strftime('%Y-%m-%d')}-{label}")
    print(f"{os.path.basename(target_dir)}  ({len(batch)} photos)")

    if not DRY_RUN:
        os.makedirs(target_dir, exist_ok=True)

    for record in batch:
        src = record["path"]
        extension = os.path.splitext(src)[1].lower()
        dest = get_unique_filename(target_dir, build_base_name(record), extension)

        if DRY_RUN:
            thumbs = "" if has_thumbnail(src) else "   [no thumbnail cache]"
            print(f"    {os.path.basename(dest)}{thumbs}")
            continue

        try:
            # Photo first: if this fails nothing has changed. Only once it has
            # succeeded and been logged do we chase the thumbnail folder, so a
            # thumbnail failure can never leave an unrecorded photo move.
            shutil.move(src, dest)
            log.write(f"{src}\t{dest}\n")
            log.flush()
            move_thumbnail(src, dest)
        except Exception as e:
            print(f"  ! move failed for {src}: {e}")


# ----------------------------------------------------------------------------
# --- 2.10 carries the Synology thumbnail cache across, renamed to match
# ----------------------------------------------------------------------------
def thumbnail_dir_for(path):
    """DSM keeps thumbnails at <dir>/@eaDir/<filename>/SYNOFILE_THUMB_*.jpg"""
    return os.path.join(os.path.dirname(path), "@eaDir", os.path.basename(path))


def has_thumbnail(path):
    return os.path.isdir(thumbnail_dir_for(path))


def move_thumbnail(src, dest):
    """
    Relocates and renames the per-file thumbnail folder so it continues to
    match its photo.

    This matters more than it looks. DSM names that folder after the image
    exactly, so renaming the photo without renaming the folder orphans the
    cache - and since DSM 7.2.2 dropped the HEVC codec, the NAS can no longer
    regenerate HEIC thumbnails. For those files an orphaned cache is a
    permanent loss, not a slow rebuild.
    """
    src_thumbs = thumbnail_dir_for(src)
    if not os.path.isdir(src_thumbs):
        return

    dest_thumbs = thumbnail_dir_for(dest)
    os.makedirs(os.path.dirname(dest_thumbs), exist_ok=True)
    shutil.move(src_thumbs, dest_thumbs)


# ----------------------------------------------------------------------------
# --- 2.11 removes month folders left empty by the sort
# ----------------------------------------------------------------------------
def prune_empty_dirs(root):
    """
    Walks bottom-up so a month folder is only considered after its own @eaDir
    has had a chance to be removed. Anything still holding a file is left
    alone, so this can never delete data.
    """
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        name = os.path.basename(dirpath)
        if not (MONTH_DIR.match(name) or name == "@eaDir"):
            continue
        try:
            os.rmdir(dirpath)      # fails harmlessly if not empty
            removed += 1
        except OSError:
            pass
    print(f"Pruned {removed} empty folders.")


if __name__ == "__main__":
    main()


# Run this on ray on a dayly basis and your folder will be sorted and the original dump backup will be a backup. The current structure can always be recovered from this dump back up, but any changes you make cannot. I suggest we backup the organised store to Stan