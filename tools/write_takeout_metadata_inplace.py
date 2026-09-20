#!/usr/bin/env python3
"""
Writes resolved Takeout metadata (EXIF date+GPS for JPEG/HEIC, mtime for
everything else) directly onto the files inside one or more Takeout
export folders - no copy, no move. The files stay exactly where they are;
only their bytes/mtime change.

This is the first half of the "organize in place" pipeline: run this,
then auto_generate_moments_by_location.py --source-roots (pointed at the
same Takeout folders) does the actual grouping and MOVES files out into
an organized tree. Splitting the two matters because this step's cost is
mostly I/O (one open+write per file) while the grouping step's cost is
mostly the geocoding/clustering logic - keeping them separate means a
grouping-logic bug doesn't require re-writing metadata into 26,000 files
to fix.

Only use this if you are fine with the Takeout export's own files being
modified. It is NOT a copy - if something goes wrong, the fix is
redownloading from Google Takeout, same as import_takeout.py's copy
mode is unnecessary if you'd rather that. See takeout_meta.py for the
shared metadata-resolution rules (identical to import_takeout.py's).

Any file with no resolvable date at all (no sidecar, no filename date,
no real EXIF - mostly downloaded memes/reposts/screenshots, not real
photos) is left with no metadata written and, if --quarantine-dir is
given, MOVED there instead of left scattered across whichever of the 17
export folders it happened to land in. Without --quarantine-dir it's
just left in place and printed as "unresolved", matching
import_takeout.py's _takeout_import_unresolved_dates/ naming.

Usage:
    python tools/write_takeout_metadata_inplace.py \
        --takeout ~/Downloads/Takeout --takeout ~/Downloads/Takeout-2 \
        --quarantine-dir ~/Downloads/takeout-undateable \
        [--limit 200] [--dry-run]
"""

import argparse
import shutil
import sys
from pathlib import Path

import takeout_meta as tm


def process_one(media_path, takeout_root, sidecar_index, seen_hashes, stats, dry_run, quarantine_dir):
    ext = media_path.suffix.lower()
    if ext not in tm.MEDIA_EXTS:
        stats["skipped_ext"] += 1
        return None

    when, lat, lon, is_favorited, had_sidecar, used_fallback = tm.resolve_metadata(
        media_path, takeout_root, sidecar_index
    )
    if not had_sidecar:
        stats["no_sidecar"] += 1
    if used_fallback:
        stats["fallback_date_used"] += 1

    content_hash = tm.sha256_of(media_path)
    if content_hash in seen_hashes:
        stats["duplicate"] += 1
        return {"path": media_path, "when": when, "lat": lat, "lon": lon,
                "favorited": is_favorited, "content_hash": content_hash, "duplicate": True}
    seen_hashes.add(content_hash)

    if when is None:
        stats["unresolved_date"] += 1
        if quarantine_dir is not None:
            if dry_run:
                stats["would_quarantine"] += 1
            else:
                quarantine_dir.mkdir(parents=True, exist_ok=True)
                dest = quarantine_dir / media_path.name
                n = 2
                while dest.exists():
                    dest = quarantine_dir / f"{media_path.stem}-{n}{media_path.suffix}"
                    n += 1
                shutil.move(str(media_path), str(dest))
                stats["quarantined"] += 1
        else:
            print(f"  ! no usable date for {media_path}", file=sys.stderr)
        return {"path": media_path, "when": None, "lat": lat, "lon": lon,
                "favorited": is_favorited, "content_hash": content_hash, "unresolved": True}

    if dry_run:
        stats["would_write"] += 1
        return {"path": media_path, "when": when, "lat": lat, "lon": lon,
                "favorited": is_favorited, "content_hash": content_hash}

    tm.write_metadata(media_path, when, lat, lon, stats)
    stats["written"] += 1

    # Bytes changed for any file that got real EXIF written (JPEG/HEIC) -
    # the content_hash callers see afterward (e.g. for dedup bookkeeping
    # or a stars.json entry) must reflect the file as it now sits on disk.
    final_hash = tm.sha256_of(media_path)

    return {"path": media_path, "when": when, "lat": lat, "lon": lon,
            "favorited": is_favorited, "content_hash": final_hash}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--takeout", action="append", required=True, dest="takeout_roots",
                     help="Path to an extracted Takeout folder. Repeat for split archives.")
    ap.add_argument("--quarantine-dir", type=Path, default=None,
                     help="Move files with no resolvable date here instead of leaving them in place.")
    ap.add_argument("--limit", type=int, default=None, help="Stop after N candidate files (for a test subset).")
    ap.add_argument("--dry-run", action="store_true", help="Report what would happen without writing anything.")
    args = ap.parse_args()

    quarantine_dir = args.quarantine_dir.expanduser().resolve() if args.quarantine_dir else None

    if not tm.EXIFTOOL_BIN:
        print("! exiftool not found on PATH - HEIC files will get mtime-only dates, no GPS "
              "(brew install exiftool to fix)", file=sys.stderr)

    takeout_roots = []
    for r in args.takeout_roots:
        r = Path(r).expanduser().resolve()
        if not r.is_dir():
            print(f"! not a directory, skipping: {r}", file=sys.stderr)
            continue
        takeout_roots.append(r)

    print(f"Indexing sidecar JSON across {len(takeout_roots)} root(s) ...")
    sidecar_index = tm.build_sidecar_index(takeout_roots)
    print(f"  {len(sidecar_index)} sidecar files found")

    # Duplicates are tracked for visibility (Takeout re-exports the same
    # photo into multiple album folders) but NOT skipped here - every
    # in-place file still needs its own correct EXIF/mtime, since the
    # grouping step that follows moves files by their actual path, not
    # by a staging-copy identity. Deduping which COPY survives only makes
    # sense when there's a copy step; here there isn't one.
    seen_hashes = set()
    stats = {"written": 0, "duplicate": 0, "no_sidecar": 0, "skipped_ext": 0,
              "exif_write_failed": 0, "would_write": 0, "unresolved_date": 0,
              "fallback_date_used": 0, "quarantined": 0, "would_quarantine": 0}

    total_seen = 0
    for takeout_root in takeout_roots:
        print(f"Scanning {takeout_root} ...")
        for media_path in tm.iter_media_files(takeout_root):
            if args.limit is not None and total_seen >= args.limit:
                break
            total_seen += 1
            process_one(media_path, takeout_root, sidecar_index, seen_hashes, stats, args.dry_run, quarantine_dir)
        if args.limit is not None and total_seen >= args.limit:
            break

    print()
    print("Summary:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    if stats["unresolved_date"]:
        if quarantine_dir:
            verb = "Would move" if args.dry_run else "Moved"
            print(f"\n{verb} {stats['unresolved_date']} file(s) with no usable date to: {quarantine_dir}")
        else:
            print(f"\n{stats['unresolved_date']} file(s) have no usable date and were left untouched -")
            print("see the '! no usable date for ...' lines above. Pass --quarantine-dir next time")
            print("to collect them in one place instead.")


if __name__ == "__main__":
    main()
