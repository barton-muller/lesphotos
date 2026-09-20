#!/usr/bin/env python3
"""
Recovers Google Photos "favorited" status into appdb favorite=1 rows,
for a library built via the IN-PLACE pipeline (write_takeout_metadata_
inplace.py + auto_generate_moments_by_location.py) rather than
import_takeout.py's copy-based one.

Why this exists, not apply_stars.py: apply_stars.py reads a stars.json
written by import_takeout.py's copy step. The in-place pipeline never
copies (it writes metadata onto the Takeout export's own files, then
MOVES them straight into the organized tree), so nothing ever recorded
which photos were favorited - the sidecar JSONs holding that flag stay
behind in the (now largely emptied) Takeout export folders, never
touched by the move.

What this does instead: scans every remaining supplemental-metadata
JSON with "favorited": true, recovers the media filename it originally
described (undoing Google's "(N)" duplicate-suffix swap - see
takeout_meta.find_sidecar's docstring for the same transform in
reverse), and searches the ALREADY-ORGANIZED library for a file with
that exact name. A unique filename match -> that file's current
content hash -> db.upsert_photo(favorite=1). An ambiguous match
(same filename appears more than once in the library, ~85 cases in a
real 26k-photo run) is reported rather than guessed.

Usage:
    python tools/apply_takeout_stars.py \
        --library ~/Downloads/lesphotos-organized \
        --takeout ~/Downloads/Takeout --takeout ~/Downloads/Takeout-2 ... \
        [--dry-run]
"""

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import config
from appdb import db

_DUP_SUFFIX_RE = re.compile(r"^(.*)\.supplemental-metadata(?:\((\d+)\))?\.json$")


def recover_media_name(sidecar_name):
    """
    Reverses find_sidecar's two naming transforms:
      "name.ext.supplemental-metadata.json"       -> "name.ext"
      "name.ext.supplemental-metadata(N).json"     -> "name(N).ext"
    (Google moves the "(N)" duplicate suffix to sit after
    ".supplemental-metadata" rather than duplicating the whole sidecar
    name - see takeout_meta.find_sidecar's docstring point 2.)
    Returns None if the sidecar name doesn't match either shape.
    """
    m = _DUP_SUFFIX_RE.match(sidecar_name)
    if not m:
        return None
    base, dup_n = m.groups()
    if dup_n is None:
        return base
    stem, ext = base.rsplit(".", 1) if "." in base else (base, "")
    return f"{stem}({dup_n}).{ext}" if ext else f"{stem}({dup_n})"


def build_filename_index(library_root):
    """filename -> [Path, ...] for every file in the organized library (excluding .lesphotos)."""
    sidecar_dir = library_root / ".lesphotos"
    index = defaultdict(list)
    for path in library_root.rglob("*"):
        if not path.is_file():
            continue
        if sidecar_dir in path.parents:
            continue
        index[path.name].append(path)
    return index


def find_favorited_sidecars(takeout_roots):
    for root in takeout_roots:
        google_photos = root / "Google Photos"
        walk_root = google_photos if google_photos.is_dir() else root
        for json_path in walk_root.rglob("*.json"):
            try:
                import json as json_module
                meta = json_module.loads(json_path.read_text())
            except (OSError, ValueError):
                continue
            if meta.get("favorited"):
                yield json_path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", required=True)
    ap.add_argument("--takeout", action="append", required=True, dest="takeout_roots")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    library = config.Library(args.library)
    takeout_roots = [Path(r).expanduser().resolve() for r in args.takeout_roots]

    print("Indexing organized library filenames ...")
    filename_index = build_filename_index(library.photos_root)
    print(f"  {sum(len(v) for v in filename_index.values())} files, {len(filename_index)} distinct names")

    conn = db.connect(library)
    starred, ambiguous, not_found, no_match_name = 0, 0, 0, 0

    for sidecar_path in find_favorited_sidecars(takeout_roots):
        media_name = recover_media_name(sidecar_path.name)
        if media_name is None:
            no_match_name += 1
            print(f"  ! couldn't parse sidecar name: {sidecar_path.name}", file=sys.stderr)
            continue

        candidates = filename_index.get(media_name, [])

        # The original source file (still sitting beside this sidecar,
        # since only files that MOVED are gone from here) tells us its
        # real content hash directly - the authoritative disambiguator
        # for both the "no exact-name match" and "multiple same-name
        # matches" cases below, rather than guessing off a name alone.
        source_media_path = sidecar_path.parent / media_name
        source_hash = db.content_hash_of(source_media_path) if source_media_path.is_file() else None

        if not candidates:
            # A collision at move time can append "-2"/"-3" (import_takeout.py
            # style) or "(N)" (the plain filesystem-collision path some
            # movers use) to the destination name - confirmed on real
            # files: IMG_4491.JPG moved to IMG_4491(1).JPG because another
            # file already claimed the plain name. Try likely variants; if
            # the source file is still here, verify by content hash. If
            # it's NOT still here (the "sidecar orphaned by cross-archive
            # dedup" case - confirmed on IMG-20190706-WA0007.jpg, whose
            # sidecar exists in Takeout/ but the file itself only ever
            # lived in Takeout-11/) there's nothing to hash-verify against,
            # so a variant match is accepted only if it's the SOLE variant
            # found across every candidate name - multiple would be a real
            # ambiguity, not something to silently pick one of.
            stem, dot, ext = media_name.rpartition(".")
            variant_names = [f"{stem}({n}){dot}{ext}" for n in range(1, 10)] + \
                             [f"{stem}-{n}{dot}{ext}" for n in range(2, 10)]
            variant_matches = []
            for variant in variant_names:
                variant_matches.extend(filename_index.get(variant, []))

            if source_hash:
                verified = [c for c in variant_matches if db.content_hash_of(c) == source_hash]
                if verified:
                    candidates = verified[:1]
            elif len(variant_matches) == 1:
                candidates = variant_matches

        if not candidates:
            not_found += 1
            print(f"  ! no organized file named {media_name!r} (from {sidecar_path})", file=sys.stderr)
            continue

        if len(candidates) > 1:
            if source_hash:
                exact = [c for c in candidates if db.content_hash_of(c) == source_hash]
                if len(exact) == 1:
                    candidates = exact
        if len(candidates) > 1:
            ambiguous += 1
            print(f"  ! ambiguous: {len(candidates)} files named {media_name!r} - skipping, star by hand:",
                  file=sys.stderr)
            for c in candidates:
                print(f"      {c}", file=sys.stderr)
            continue

        target = candidates[0]
        content_hash = db.content_hash_of(target)
        print(f"  {'would star' if args.dry_run else 'starring'} {target.relative_to(library.photos_root)}")
        if not args.dry_run:
            db.upsert_photo(conn, content_hash, target, favorite=1)
        starred += 1

    print()
    print("Summary:")
    print(f"  starred: {starred}")
    print(f"  ambiguous (skipped, needs manual star): {ambiguous}")
    print(f"  not found in organized library: {not_found}")
    print(f"  unparseable sidecar name: {no_match_name}")


if __name__ == "__main__":
    main()
