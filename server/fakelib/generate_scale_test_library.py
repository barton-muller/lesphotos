#!/usr/bin/env python3
"""
Scale-test variant of generate_library.py: copies real, full-size phone
photos (not the small ~200-400KB archival samples the main generator uses)
to test PhotoView's scan-time memory peak against realistic file sizes and
a much larger library (~2000 photos vs. 97).

Source: a real phone camera-roll backup, already organized as
YYYY/MM/PXL_*.jpg with real EXIF - no synthetic timeline needed here, the
real capture dates and their real clustering are used directly. Originals
are never modified, only copied.

Output: server/data/photos/scaletest/<original YYYY>/<original MM>/, i.e.
flat month folders rather than gap-grouped moments - this test is about
PhotoView's scan/memory behavior at volume, not the moment-grouping logic
(already covered by generate_library.py), so there's no need to run the
gap-grouping step here.
"""

import argparse
import shutil
from pathlib import Path

SOURCE_DIR = Path(
    "/Volumes/home/Photos/MobileBackup/namasenda/DCIM/Camera"
)
OUTPUT_ROOT = Path(__file__).resolve().parent.parent / "data" / "photos" / "scaletest"
JPEG_EXTS = {".jpg", ".jpeg"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=2000)
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()

    if args.clean and OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    sources = sorted(
        p for p in SOURCE_DIR.rglob("*") if p.suffix.lower() in JPEG_EXTS and p.is_file()
    )
    if not sources:
        print(f"No JPEGs found under {SOURCE_DIR}")
        return
    sources = sources[: args.count]
    print(f"Copying {len(sources)} real photos from {SOURCE_DIR}...")

    total_bytes = 0
    for i, src in enumerate(sources):
        # Preserve the source's own YYYY/MM grouping, from its path
        # (.../Camera/YYYY/MM/file.jpg) - keeps real EXIF and real
        # clustering, no synthetic timeline needed for this test.
        try:
            year, month = src.parent.parent.name, src.parent.name
        except IndexError:
            year, month = "unknown", "unknown"
        dest_dir = OUTPUT_ROOT / year / month
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        shutil.copyfile(src, dest)
        total_bytes += dest.stat().st_size
        if (i + 1) % 200 == 0:
            print(f"  {i + 1}/{len(sources)} copied ({total_bytes / 1e9:.2f} GB so far)")

    print(f"\nDone. {len(sources)} photos, {total_bytes / 1e9:.2f} GB, written to {OUTPUT_ROOT}")


if __name__ == "__main__":
    main()
