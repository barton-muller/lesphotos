#!/usr/bin/env python3
"""
One-off: converts a folder of standalone .webp files (recovered from
takeout quarantine - .webp was never in MEDIA_EXTS/GROUPABLE_EXTS, so
these never went through the normal Takeout pipeline at all, not
because they lacked a date) to .jpg, writes EXIF DateTimeOriginal from
the filename's embedded timestamp (these have no sidecar JSON - they're
past the quarantine stage, already separated from their archives - and
no pre-existing EXIF; the filename is the only source), and leaves the
.jpg files ready for auto_generate_moments_by_location.py to pick up
via --source-root.

RGBA source frames are flattened onto white before JPEG encode (JPEG
has no alpha channel) - these are phone-camera photos, not graphics
with meaningful transparency, so white is a reasonable and unremarkable
background pixel value in the rare case any survives to be visible.

Usage:
    python tools/convert_webp_to_jpeg.py --dir ~/Downloads/takeout-undateable --dest ~/Downloads/takeout-webp-converted
"""

import argparse
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import takeout_meta as tm


def convert_one(src, dest_dir):
    when = tm.date_from_filename(src.name)
    dest = dest_dir / (src.stem + ".jpg")

    img = Image.open(src)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    img.save(dest, "JPEG", quality=92)

    if when is not None:
        tm.write_jpeg_exif(dest, when, None, None)
    return dest, when


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, type=Path, help="Folder containing .webp files to convert.")
    ap.add_argument("--dest", required=True, type=Path, help="Folder to write converted .jpg files into.")
    args = ap.parse_args()

    src_dir = args.dir.expanduser().resolve()
    dest_dir = args.dest.expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    converted, no_date = 0, 0
    for src in sorted(src_dir.glob("*.webp")):
        dest, when = convert_one(src, dest_dir)
        status = when.isoformat() if when else "NO DATE"
        print(f"  {src.name} -> {dest.name}  ({status})")
        converted += 1
        if when is None:
            no_date += 1

    print(f"\nconverted: {converted}, no resolvable date: {no_date}")
    print(f"output: {dest_dir}")


if __name__ == "__main__":
    main()
