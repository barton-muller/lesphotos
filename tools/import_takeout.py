#!/usr/bin/env python3
"""
One-time migration: merge Google Takeout export(s) into a lesphotos library
by COPYING files into a flat staging folder, writing resolved metadata
(EXIF/mtime) into the copy. The Takeout export itself is left untouched.

For writing metadata directly onto the Takeout files in place (no copy,
then moving them straight into an organized tree) see
write_takeout_metadata_inplace.py and auto_generate_moments_by_location.py
instead - that pipeline skips the flat-staging-copy step entirely, which
matters when the export is large enough that a full copy is wasteful and
you're fine writing real metadata onto the original files (they're not
organized yet anyway, and redownloadable from Google if something goes
wrong).

Metadata resolution (sidecar JSON matching, filename/EXIF date fallback,
the actual EXIF/mtime writers) lives in takeout_meta.py, shared with the
in-place script so both follow identical rules - see that module's
docstring for the full per-file resolution story.

What THIS script adds on top of takeout_meta.py's per-file resolution:
  1. Dedupes by content hash (sha256) so Takeout's habit of the same
     photo appearing in multiple album export folders doesn't produce
     duplicate rows once indexed.
  2. Copies (never moves - the Takeout export is left untouched) into a
     flat staging folder under the target library root - or, for a file
     with no resolvable date at all, into a separate
     _takeout_import_unresolved_dates/ folder that auto_generate_moments
     is never pointed at, so it can't be silently misfiled.
  3. Writes <library>/.lesphotos/stars.json listing every favorited
     photo's (post-EXIF-write) content hash and destination path. This
     is a record for apply_stars.py to use, NOT the app's own stars
     mirror yet - nothing in the running app reads stars.json back into
     appdb (db.export_stars_json only ever WRITES it, after a favorite
     change through the app); appdb.sqlite3, populated by the indexer,
     is what the server actually reads. Marking these as real favorites
     needs photos rows to exist first, which means it has to happen
     after indexing - see apply_stars.py.

Pipeline after this script:
  1. reorganize.auto_generate_moments() on the staging folder, to fold
     it into YYYY-MM-DD-moment folders.
  2. The normal indexer, so appdb gets a row (and photo_id) per photo.
  3. tools/apply_stars.py, to turn this run's stars.json into real
     favorite=1 rows in appdb (see point 3 above).
  4. tools/name_moments_by_city.py, to auto-suggest city-level labels
     for moments with GPS.

Usage:
    python tools/import_takeout.py \
        --takeout ~/Downloads/Takeout --takeout ~/Downloads/Takeout-2 \
        --library server/data/photos/takeouttest \
        [--limit 200] [--dry-run]

Multiple --takeout roots are Google's own split-archive convention (one
export becomes Takeout, Takeout-2, Takeout-3, ... when it's too big for a
single zip) - all are merged into one staging pass with one dedupe set.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

import takeout_meta as tm

STAGING_DIRNAME = "_takeout_import_staging"
UNRESOLVED_DIRNAME = "_takeout_import_unresolved_dates"


def process_one(media_path, takeout_root, sidecar_index, dest_dir, unresolved_dir, seen_hashes, stats, dry_run):
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
        return None
    seen_hashes.add(content_hash)

    # Still no usable date from sidecar, filename, or EXIF: route to
    # unresolved_dir instead of guessing. auto_generate_moments never
    # sees this folder, so the file can't be silently misfiled into a
    # "today" moment - it's left for manual review/dating instead.
    target_dir = unresolved_dir if when is None else dest_dir

    dest_path = target_dir / media_path.name
    n = 2
    while dest_path.exists():
        dest_path = target_dir / f"{media_path.stem}-{n}{media_path.suffix}"
        n += 1

    if dry_run:
        stats["would_import"] += 1
        if when is None:
            stats["unresolved_date"] += 1
        return {
            "dest": dest_path, "when": when, "lat": lat, "lon": lon,
            "favorited": is_favorited, "content_hash": content_hash,
            "unresolved": when is None,
        }

    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(media_path, dest_path)

    if when is None:
        stats["unresolved_date"] += 1

    wrote_exif = tm.write_metadata(dest_path, when, lat, lon, stats)

    # content_hash here is the ORIGINAL file's hash (pre-write), but
    # stars.json keys on the hash of what actually gets indexed - any
    # metadata write changes the file's bytes, so appdb must be keyed on
    # the post-write hash instead.
    final_hash = tm.sha256_of(dest_path) if wrote_exif else content_hash

    stats["imported"] += 1
    return {
        "dest": dest_path, "when": when, "lat": lat, "lon": lon,
        "favorited": is_favorited, "content_hash": final_hash,
    }


def write_stars_json(library_root, starred_records, dry_run):
    """
    Same shape appdb.export_stars_json produces: {content_hash: {favorite,
    path}}. This is NOT yet the app's own favorites - the server reads
    appdb.sqlite3, and nothing imports stars.json back into it. It is
    apply_stars.py's input: run that after indexing to turn these entries
    into real favorite=1 rows.
    """
    if not starred_records:
        return
    sidecar = library_root / ".lesphotos"
    stars_path = sidecar / "stars.json"
    existing = {}
    if stars_path.exists():
        try:
            existing = json.loads(stars_path.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    for rec in starred_records:
        existing[rec["content_hash"]] = {
            "favorite": True,
            "path": str(rec["dest"].resolve()),
        }
    print(f"{'Would write' if dry_run else 'Writing'} {len(starred_records)} favorited entries to {stars_path}")
    if not dry_run:
        sidecar.mkdir(parents=True, exist_ok=True)
        stars_path.write_text(json.dumps(existing, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--takeout", action="append", required=True, dest="takeout_roots",
                     help="Path to an extracted Takeout folder. Repeat for split archives (Takeout, Takeout-2, ...).")
    ap.add_argument("--library", required=True, help="Target library root (e.g. server/data/photos/testlib).")
    ap.add_argument("--limit", type=int, default=None, help="Stop after N candidate files (for a test subset).")
    ap.add_argument("--dry-run", action="store_true", help="Report what would happen without writing anything.")
    args = ap.parse_args()

    if not tm.EXIFTOOL_BIN:
        print("! exiftool not found on PATH - HEIC files will get mtime-only dates, no GPS "
              "(brew install exiftool to fix)", file=sys.stderr)

    library_root = Path(args.library).resolve()
    staging_dir = library_root / STAGING_DIRNAME
    unresolved_dir = library_root / UNRESOLVED_DIRNAME

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

    stats = {"imported": 0, "duplicate": 0, "no_sidecar": 0, "skipped_ext": 0,
              "exif_write_failed": 0, "would_import": 0, "unresolved_date": 0,
              "fallback_date_used": 0}
    seen_hashes = set()
    starred = []

    total_seen = 0
    for takeout_root in takeout_roots:
        print(f"Scanning {takeout_root} ...")
        for media_path in tm.iter_media_files(takeout_root):
            if args.limit is not None and total_seen >= args.limit:
                break
            total_seen += 1
            result = process_one(media_path, takeout_root, sidecar_index, staging_dir, unresolved_dir, seen_hashes, stats, args.dry_run)
            if result and result["favorited"]:
                starred.append(result)
        if args.limit is not None and total_seen >= args.limit:
            break

    write_stars_json(library_root, starred, args.dry_run)

    print()
    print("Summary:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print(f"  starred: {len(starred)}")
    if not args.dry_run:
        print(f"\nStaged into: {staging_dir}")
        if stats["unresolved_date"]:
            print(f"{stats['unresolved_date']} file(s) with no usable date (sidecar, filename, "
                  f"or EXIF) copied to: {unresolved_dir}")
            print("These were NOT staged for auto_generate_moments - review and date them by hand,")
            print("then move them into the library wherever they belong.")
        print("Next: reorganize.auto_generate_moments() on this folder, then the")
        print("indexer, then tools/apply_stars.py, then tools/name_moments_by_city.py.")


if __name__ == "__main__":
    main()
