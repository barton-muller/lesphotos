#!/usr/bin/env python3
"""
Builds a fake /Photos-style library for testing PhotoView, using real sample
JPEGs (never modifying the originals) rather than synthetic images.

Source: a Google Photos Takeout export folder containing real photos plus
".supplemental-metadata.json" sidecars with real photoTakenTime timestamps.
Only files with a sidecar are used, so every fake photo gets a real EXIF date.

Output layout: server/data/photos/<user>/YYYY/YYYY-MM-DD-moment/IMG_xxxx.JPG
Moments are grouped from the real timestamps using the same gap logic as
initial_script.py (see grouping.py), with no year-boundary force-split.

Real timestamps only decide relative photo order; actual dates come from a
synthetic timeline (see apply_synthetic_timeline) built to deliberately
include one moment spanning a month boundary and one spanning a year
boundary, to verify PhotoView's timeline reads them as continuous single
albums rather than splitting at the boundary.
"""

import argparse
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import piexif

from grouping import group_into_moments

SOURCE_DIR = Path(
    "/path/to/sample-photos"
)
OUTPUT_ROOT = Path(__file__).resolve().parent.parent / "data" / "photos"
USERS = ["alice", "bob"]

# piexif only writes EXIF into JPEG containers, so only .jpg/.jpeg source
# files are usable here (a handful of PNG/HEIC files in the source folder
# are skipped).
JPEG_EXTS = {".jpg", ".jpeg"}


def load_source_photos():
    """
    Returns [(path, real_photoTakenTime)] for every JPEG source file that has
    a sidecar, sorted by that real timestamp. The real timestamp is only used
    to order photos plausibly (e.g. a burst stays a burst); actual dates
    written into the fake library come from apply_synthetic_timeline below,
    since the real photos span 2002-2026 with ~one photo per date and no
    natural clustering to test gap-based grouping against.
    """
    items = []
    for path in sorted(SOURCE_DIR.iterdir()):
        if path.name.endswith(".json") or not path.is_file():
            continue
        if path.suffix.lower() not in JPEG_EXTS:
            continue
        sidecar = SOURCE_DIR / (path.name + ".supplemental-metadata.json")
        if not sidecar.exists():
            continue
        try:
            data = json.loads(sidecar.read_text())
            ts = int(data["photoTakenTime"]["timestamp"])
        except (KeyError, ValueError, json.JSONDecodeError):
            continue
        items.append((path, datetime.fromtimestamp(ts)))

    items.sort(key=lambda pair: pair[1])
    return items


def apply_synthetic_timeline(items):
    """
    Re-maps the real, sorted photos onto a compact synthetic timeline built
    from named blocks, so the generated library has deliberate, legible
    clustering to test gap-based moment grouping against:

      - a few same-day bursts (multiple photos, <2 day gaps within)
      - a few multi-day gaps between distinct moments
      - one moment that spans a month boundary (no forced split)
      - one moment that spans a year boundary (no forced split)

    Keeps the real image bytes and their relative chronological order;
    only the assigned datetime is synthetic. Returns [(path, when)].
    """
    blocks = [
        # (start, [offsets in hours from start]) - each block's photos stay
        # within EVENT_GAP of each other, i.e. one intended moment.
        (datetime(2024, 3, 10, 9, 0), [0, 2, 5]),          # same-day burst
        (datetime(2024, 3, 14, 18, 0), [0, 26, 50]),        # multi-day trip
        (datetime(2024, 6, 1, 12, 0), [0]),                 # lone photo
        (datetime(2024, 6, 30, 20, 0), [0, 30, 55]),        # spans Jun->Jul boundary
        (datetime(2024, 9, 5, 8, 0), [0, 3]),                # same-day burst
        (datetime(2024, 12, 30, 15, 0), [0, 20, 44]),        # spans Dec->Jan (year) boundary
        (datetime(2025, 2, 14, 10, 0), [0]),                 # lone photo
        (datetime(2025, 5, 1, 9, 0), [0, 1, 2, 26]),         # burst + next day
    ]

    slots = []
    for start, offsets_hours in blocks:
        for offset in offsets_hours:
            slots.append(start + timedelta(hours=offset))

    # Cycle the synthetic slots if there are more real photos than slots,
    # spacing repeats a year apart so grouping stays deliberate rather than
    # colliding two unrelated photos onto the same moment.
    assigned = []
    for i, (path, _real_when) in enumerate(items):
        base_slot = slots[i % len(slots)]
        cycle = i // len(slots)
        when = base_slot.replace(year=base_slot.year + cycle)
        assigned.append((path, when))

    return assigned


def set_exif_date(dest_path, when):
    exif_dict = {"Exif": {}}
    date_str = when.strftime("%Y:%m:%d %H:%M:%S")
    exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = date_str.encode()
    exif_bytes = piexif.dump(exif_dict)
    piexif.insert(exif_bytes, str(dest_path))


def write_moment(user_root, moment, camera_names):
    first_when = moment[0][1]
    label = f"{first_when.strftime('%Y-%m-%d')}-moment"
    target_dir = user_root / str(first_when.year) / label
    target_dir.mkdir(parents=True, exist_ok=True)

    for src_path, when in moment:
        dest_name = next(camera_names)
        dest_path = target_dir / f"{dest_name}{src_path.suffix.upper()}"
        shutil.copyfile(src_path, dest_path)
        try:
            set_exif_date(dest_path, when)
        except Exception as e:
            print(f"  ! could not write EXIF for {dest_path.name}: {e}")

    print(f"  {user_root.name}/{first_when.year}/{label}  ({len(moment)} photos)")


def camera_name_generator():
    n = 1000
    while True:
        n += 1
        yield f"IMG_{n}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--clean", action="store_true", help="wipe server/data/photos before generating"
    )
    args = parser.parse_args()

    if args.clean and OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT)

    items = load_source_photos()
    if not items:
        print(f"No source JPEGs with sidecars found under {SOURCE_DIR}")
        return
    print(f"Loaded {len(items)} real JPEGs with sidecar timestamps.")

    items = apply_synthetic_timeline(items)
    moments = group_into_moments(items, when_key=lambda pair: pair[1])
    print(f"Grouped into {len(moments)} moments.\n")

    # Split moments round-robin between users so both accumulate a spread of dates.
    names = camera_name_generator()
    for i, moment in enumerate(moments):
        user = USERS[i % len(USERS)]
        user_root = OUTPUT_ROOT / user
        write_moment(user_root, moment, names)

    print(f"\nDone. Fake library written to {OUTPUT_ROOT}")


if __name__ == "__main__":
    main()
