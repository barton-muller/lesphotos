#!/usr/bin/env python3
"""
Sparse historical backfill: a few small photos per year going back to 2000,
so the timeline scrubber can be developed/tested against a realistic
25-year date range without the scan/thumbnail cost of a dense library.

Why this exists separately from generate_library.py / generate_scale_test_
library.py: those build *dense* libraries from real source photos to test
PhotoView's scan and memory behaviour at volume. This one has the opposite
goal - the smallest possible number of bytes that still produces a real,
correctly-dated 25-year spread, because what's being tested is the
scrubber's date spacing/compression, not throughput. Photos are tiny
generated JPEGs (a few KB), not copies of real photos.

Output layout matches the rest of the app's convention:
  server/data/photos/<user>/YYYY/YYYY-MM-DD-moment/hist_YYYYMMDD_N.jpg
"""

import argparse
import random
from datetime import datetime, timedelta
from pathlib import Path

import piexif
from PIL import Image

PHOTOS_ROOT = Path(__file__).resolve().parent.parent / "data" / "photos"


def make_tiny_jpeg(path: Path, when: datetime, size=(160, 120)):
    """A small solid-colour JPEG carrying a real EXIF DateTimeOriginal."""
    # Colour derived from the date so different years look visibly distinct
    # when eyeballing the grid - purely a debugging convenience.
    rng = random.Random(when.toordinal())
    img = Image.new("RGB", size, (rng.randint(40, 220), rng.randint(40, 220), rng.randint(40, 220)))
    exif_date = when.strftime("%Y:%m:%d %H:%M:%S")
    exif = {
        "0th": {piexif.ImageIFD.DateTime: exif_date},
        "Exif": {
            piexif.ExifIFD.DateTimeOriginal: exif_date,
            piexif.ExifIFD.DateTimeDigitized: exif_date,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=60, exif=piexif.dump(exif))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default="bob")
    parser.add_argument("--start-year", type=int, default=2000)
    parser.add_argument("--end-year", type=int, default=2023,
                        help="Inclusive. Defaults to 2023 so it doesn't collide with the "
                             "existing dense 2024-2025 real-photo library.")
    parser.add_argument("--moments-per-year", type=int, default=4)
    parser.add_argument("--photos-per-moment", type=int, default=3)
    parser.add_argument("--clean", action="store_true",
                        help="Remove previously-generated hist_* moments first.")
    args = parser.parse_args()

    user_root = PHOTOS_ROOT / args.user
    if args.clean:
        removed = 0
        for d in sorted(user_root.glob("*/*-hist")):
            for f in d.iterdir():
                f.unlink()
            d.rmdir()
            removed += 1
        print(f"removed {removed} previously-generated hist moments")

    rng = random.Random(20000101)
    total_photos = 0
    total_moments = 0

    for year in range(args.start_year, args.end_year + 1):
        # Spread moments unevenly through the year rather than on fixed
        # dates - even spacing would make the scrubber's compression look
        # better-behaved than it really is on real data.
        months = sorted(rng.sample(range(1, 13), args.moments_per_year))
        for month in months:
            day = rng.randint(1, 28)
            base = datetime(year, month, day, rng.randint(9, 18), rng.randint(0, 59))
            # "-hist" suffix so --clean can find exactly what this script
            # made, without touching real moments.
            moment_dir = user_root / str(year) / f"{base:%Y-%m-%d}-hist"
            for n in range(args.photos_per_moment):
                when = base + timedelta(minutes=n * 7)
                make_tiny_jpeg(moment_dir / f"hist_{when:%Y%m%d}_{n}.jpg", when)
                total_photos += 1
            total_moments += 1

    print(f"created {total_moments} moments / {total_photos} photos "
          f"across {args.start_year}-{args.end_year} under {user_root}")


if __name__ == "__main__":
    main()
