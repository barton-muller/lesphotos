#!/usr/bin/env python3
"""
Turns a stars.json written by import_takeout.py into real favorite=1 rows
in appdb.sqlite3.

Run this AFTER the indexer has processed the library at least once -
appdb only has a `photos` row (and a photo_id) for a file once something
has registered it, and db.upsert_photo needs an existing or new row to
set favorite on. Running before indexing silently mints rows with no
media/thumbnail data behind them yet, which the indexer then has to
reconcile - simpler to just index first.

Looks up each photo by content_hash (db.get_photo_by_hash) rather than
trusting stars.json's recorded path: by the time this runs,
auto_generate_moments has already moved the file out of the staging
folder into its real moment directory, and upsert_photo would otherwise
overwrite appdb's already-correct current path with the stale staging
one on every ON CONFLICT update.

Safe to re-run: upsert_photo is idempotent per content_hash.

Usage:
    python tools/apply_stars.py --library server/data/photos/testlib
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import config
from appdb import db


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    library = config.Library(args.library)
    stars_path = library.stars_json_path
    if not stars_path.exists():
        print(f"no stars.json at {stars_path}, nothing to do")
        return

    entries = json.loads(stars_path.read_text())
    print(f"{len(entries)} entries in {stars_path}")

    conn = db.connect(library)
    applied, missing = 0, 0
    for content_hash, entry in entries.items():
        row = db.get_photo_by_hash(conn, content_hash)
        if row:
            current_path = Path(row["path"])
        else:
            # Not registered by the indexer yet (or moved before indexing
            # ever ran) - fall back to the recorded staging path, which
            # only works if it still exists.
            current_path = Path(entry["path"])
        if not current_path.exists():
            missing += 1
            print(f"  ! file not found at {current_path}, skipping")
            continue
        print(f"  {'would star' if args.dry_run else 'starring'} {current_path.name}")
        if not args.dry_run:
            db.upsert_photo(conn, content_hash, current_path, favorite=1)
        applied += 1

    print(f"\n{'Would apply' if args.dry_run else 'Applied'}: {applied}, missing files: {missing}")


if __name__ == "__main__":
    main()
