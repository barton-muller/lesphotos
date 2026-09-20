#!/usr/bin/env python3
"""
Location-aware alternative to reorganize.auto_generate_moments, for
regrouping a large/messy import (e.g. a Google Takeout dump) into moments
that better match how a trip actually felt: "two weeks in Barcelona" stays
one moment even through a quiet day with no photos, but a same-day flight
to a second city starts a new one even with no time gap at all.

Deliberately a standalone tool, NOT a change to reorganize.py or the live
/moments/auto-generate endpoint. Reasons, from the design conversation:
  - The right grouping is library-dependent in a way a live endpoint can't
    judge for you: an already-mostly-organized library (inherited photos,
    manually sorted years ago) doesn't want the same treatment as a raw
    Takeout dump.
  - It needs decent GPS coverage on at least some photos per time-cluster
    to do anything useful - a library with little/no GPS should just get
    plain time-gap grouping, not a half-broken location feature bolted
    onto the app UI for every user regardless of their library's GPS
    coverage.
  - This is inherently a "run it, look at the plan, adjust, run again"
    workflow, not a one-click background job - hence CLI, not API.

Algorithm:
  1. Every candidate file gets a capture time (reorganize's own
     read_capture_datetime/appdb-cached lookup - unchanged) and a city
     (via city_lookup.CityIndex, from GPS - see geocoding.read_gps).
  2. A photo with no GPS inherits its nearest-IN-TIME neighbor's city
     (only once neighbors already have one) - a phone with location
     services off for one afternoon mid-trip must not force a split.
  3. Sorted by time, split into a new moment whenever the gap to the
     previous photo exceeds `gap` OR the city changes from the previous
     photo. A library with no GPS at all degrades to step 3 acting on
     time gaps alone, since every photo's city is then the same "unknown"
     placeholder - unchanged from today's plain time-gap behavior.
  4. Any resulting moment with fewer than `min_moment_size` photos is
     NOT kept as its own moment - its photos are folded into a plain
     calendar-month folder ("YYYY-MM", no label/city) together with
     every other small moment and city-less photo from that month. This
     is deliberately unlabeled: a handful of stray photos doesn't
     deserve a city name implying it was a notable trip, and pooling
     them avoids a sparse old year fragmenting into dozens of
     single-digit-photo folders.
  5. Remaining (>= min_moment_size) moments get a real city label
     (majority_city across the moment's photos), or the literal string
     "moment" if none of them have a resolvable city at all.

Always prints the full plan and asks for confirmation before touching
the filesystem, same spirit as auto_generate_moments' dry_run - the
difference is this tool makes dry-run-then-review the default workflow,
not an option a caller has to remember to pass.

Usage (from a flat staging copy, e.g. import_takeout.py's output):
    python tools/auto_generate_moments_by_location.py \
        --library server/data/photos/testlib \
        --source _takeout_import_staging \
        [--gap-hours 48] [--min-moment-size 10] [--dry-run]

Usage (in place, straight from Takeout export folders - no staging copy;
run write_takeout_metadata_inplace.py on the same folders first):
    python tools/auto_generate_moments_by_location.py \
        --library server/data/photos/testlib \
        --source-root ~/Downloads/Takeout --source-root ~/Downloads/Takeout-2 \
        [--gap-hours 48] [--min-moment-size 10] [--dry-run]

--source-root walks the raw Takeout tree directly (files are still
matched by extension/exclusion, not by needing a "Google Photos"
subfolder specifically) and dedupes across all given roots by content
hash before building the plan, since the same photo can appear in more
than one split archive - see collect_candidates.
"""

import argparse
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from city_lookup import CityIndex

import config
import platform_hosts
import reorganize
from appdb import db
from geocoding import read_gps

DEFAULT_GAP_HOURS = 48
DEFAULT_MIN_MOMENT_SIZE = 10
UNKNOWN_CITY = None  # sentinel: no GPS anywhere nearby in time either


def _slug(text):
    import re
    import unicodedata

    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace(" ", "-")
    return re.sub(r"[^a-z0-9-]", "", text) or "unknown"


_COUNTRY_NAMES = None


def _country_name(cc):
    global _COUNTRY_NAMES
    if _COUNTRY_NAMES is None:
        # Imported lazily and only here: name_moments_by_city.py owns the
        # canonical table, reused rather than duplicated.
        from name_moments_by_city import _COUNTRY_NAMES as table

        _COUNTRY_NAMES = table
    return _COUNTRY_NAMES.get(cc, cc)


def city_label(city_index, lat, lon):
    """"city-country" string from a coordinate (or a custom_locations override), or None if unresolvable."""
    return city_index.city_label(lat, lon, _country_name, _slug)


def collect_candidates(library, source_dirs):
    """
    Same candidate walk as reorganize.auto_generate_moments, extended to
    accept MULTIPLE source directories (e.g. every Takeout-N export
    folder, when regrouping in place rather than from one flat staging
    copy) with a single content-hash dedup pass across all of them.

    Google re-exports the same photo into multiple album folders, which
    can land in different Takeout-N archives (Google's own split-zip
    convention) - without cross-root dedup here, a photo appearing in
    two archives would be moved twice, once per archive, producing a
    real duplicate file in the organized destination. First occurrence
    (by directory walk order) wins; a caller wanting a specific tie-break
    should pre-filter source_dirs' order.

    Recursive within each root; skips host bookkeeping dirs and anything
    already under a YYYY-MM-DD-prefixed (already-organized) folder,
    excludes a Live Photo's .MOV (it travels with its still via
    _expand_motion_pairs).
    """
    conn = db.connect(library)
    paired_clips = {
        r["motion_path"]
        for r in conn.execute(
            "SELECT motion_path FROM media WHERE motion_kind = 'apple' AND motion_path IS NOT NULL"
        )
    }
    indexed_dates = {
        r["path"]: r["taken_at"]
        for r in conn.execute(
            "SELECT p.path, m.taken_at FROM photos p JOIN media m ON m.content_hash = p.content_hash"
        )
    }
    conn.close()

    candidates = []
    seen_hashes = set()
    duplicate_count = 0
    for source_dir in source_dirs:
        for path in source_dir.rglob("*"):
            parts = path.relative_to(source_dir).parts
            if any(part.startswith(".") or part in platform_hosts.IGNORED_DIRNAMES for part in parts[:-1]):
                continue
            if not path.is_file() or path.suffix.lower() not in reorganize.GROUPABLE_EXTS:
                continue
            if str(path) in paired_clips:
                continue
            if any(reorganize._has_date_prefix(part) for part in parts[:-1]):
                continue

            content_hash = db.content_hash_of(path)
            if content_hash in seen_hashes:
                duplicate_count += 1
                continue
            seen_hashes.add(content_hash)
            candidates.append(path)

    if duplicate_count:
        print(f"  skipped {duplicate_count} duplicate(s) (same content hash seen in an earlier root)")

    return candidates, indexed_dates


def build_photo_records(candidates, indexed_dates, city_index):
    """
    One record per candidate: (path, when, lat, lon, city). city is None
    until _fill_missing_cities has run. Sorted by `when` ascending -
    everything downstream assumes this order.
    """
    records = []
    for path in candidates:
        when = reorganize._capture_datetime_cached(path, indexed_dates)
        lat, lon = (None, None)
        if path.suffix.lower() in reorganize.IMAGE_EXTS:
            lat, lon = read_gps(path)
        city = city_label(city_index, lat, lon) if lat is not None else None
        records.append({"path": path, "when": when, "lat": lat, "lon": lon, "city": city})
    records.sort(key=lambda r: r["when"])
    return records


def fill_missing_cities(records, max_distance):
    """
    A GPS-less photo inherits its nearest-IN-TIME neighbor's city (checking
    both directions, picking whichever neighbor is closer in time), so a
    stretch with location services off mid-trip doesn't force a spurious
    split - but ONLY if that neighbor is within max_distance (the same
    gap used for splitting). Without this cutoff, a GPS-less photo from
    2001 could inherit a city from the nearest GPS-tagged photo anywhere
    in the whole batch, even if that's a decade away - confirmed in
    testing: an 800-file batch spanning 2001-2015 had its first real GPS
    fix in 2012, and every earlier no-GPS record was inheriting that
    2012 city, fabricating a decade of false location labels. Beyond
    max_distance a record keeps city=None (UNKNOWN_CITY), and grouping
    falls back to the time gap alone for it - same as today's behaviour
    when a library has no GPS at all.
    """
    n = len(records)
    located = [i for i, r in enumerate(records) if r["city"] is not None]
    if not located:
        return  # nothing to inherit from - whole batch stays UNKNOWN_CITY

    for i, r in enumerate(records):
        if r["city"] is not None:
            continue
        # Nearest located index by position in the time-sorted list -
        # a linear scan outward is simplest and fast enough (records are
        # already sorted by time, so "nearest in time" and "nearest in
        # list position" are the same thing).
        left = next((j for j in range(i - 1, -1, -1) if records[j]["city"] is not None), None)
        right = next((j for j in range(i + 1, n) if records[j]["city"] is not None), None)

        d_left = (r["when"] - records[left]["when"]) if left is not None else None
        d_right = (records[right]["when"] - r["when"]) if right is not None else None
        if d_left is not None and d_left > max_distance:
            left = None
        if d_right is not None and d_right > max_distance:
            right = None

        if left is None and right is None:
            continue
        if left is None:
            r["city"] = records[right]["city"]
        elif right is None:
            r["city"] = records[left]["city"]
        else:
            r["city"] = records[left]["city"] if d_left <= d_right else records[right]["city"]


def split_into_moments(records, gap):
    """
    Time-sorted records -> list of clusters. A new cluster starts when the
    gap to the previous photo exceeds `gap` OR the city differs from the
    previous photo's - either condition alone is enough (see module
    docstring point 3). UNKNOWN_CITY (None) never counts as "different"
    from another UNKNOWN_CITY, so an all-no-GPS run only ever splits on
    the time gap, matching today's plain behaviour.
    """
    clusters = []
    current = []
    for r in records:
        if current:
            prev = current[-1]
            time_gap = (r["when"] - prev["when"]) > gap
            city_changed = r["city"] != prev["city"] and not (r["city"] is None and prev["city"] is None)
            if time_gap or city_changed:
                clusters.append(current)
                current = []
        current.append(r)
    if current:
        clusters.append(current)
    return clusters


def fold_small_clusters(clusters, min_size):
    """
    Splits clusters into (kept, to_fold): kept clusters have >= min_size
    photos and get a real dated+labeled moment; the rest are pooled per
    calendar month into a plain "YYYY-MM" folder - no city label, no
    per-cluster labeling at all - so a handful of day-trip/one-off
    photos scattered across a month become one small folder instead of
    many tiny ones, without implying more significance (a city name)
    than a few stray photos deserve.
    """
    kept = []
    month_pools = {}  # "YYYY-MM" -> list of records
    for cluster in clusters:
        if len(cluster) >= min_size:
            kept.append(cluster)
            continue
        for r in cluster:
            month_key = r["when"].strftime("%Y-%m")
            month_pools.setdefault(month_key, []).append(r)
    return kept, month_pools


def majority_city(records, default):
    """
    Majority city across a cluster's photos, ties broken by earliest
    photo. A kept moment must not label itself off its single earliest
    photo's city alone (that photo may happen to be the one with no GPS
    even though the rest of the cluster is fully located), so this always
    looks at every record. Only used for kept (>= min_size) moments -
    month-pool folders (see fold_small_clusters) get no city label at all.
    """
    cities = [r["city"] for r in records if r["city"] is not None]
    if not cities:
        return default
    counts = Counter(cities)
    best_count = max(counts.values())
    candidates = [c for c, n in counts.items() if n == best_count]
    if len(candidates) == 1:
        return candidates[0]
    # tie: whichever of the tied cities appears earliest in time
    for r in records:
        if r["city"] in candidates:
            return r["city"]
    return candidates[0]


def build_plan(library, kept_clusters, month_pools):
    """
    Destination folder per cluster/pool, following the existing
    <root>/<year>/YYYY-MM-DD-<label> convention for real moments, and
    a plain <root>/<year>/YYYY-MM for folded small clusters - no label,
    no city guess, just the month.
    """
    plan = []
    planned_dirs = set()

    for cluster in kept_clusters:
        first = cluster[0]
        year_dir = library.photos_root / str(first["when"].year)
        label = majority_city(cluster, default="moment")
        base_name = f"{first['when'].strftime('%Y-%m-%d')}-{label}"
        dest = reorganize._unique_dest_dir(year_dir, base_name, planned_dirs)
        planned_dirs.add(dest)
        plan.append({"dest": dest, "records": cluster, "count": len(cluster), "kind": "moment"})

    for month_key, records in sorted(month_pools.items()):
        year = month_key[:4]
        year_dir = library.photos_root / year
        existing = year_dir / month_key
        # A plain YYYY-MM folder from an earlier run of this tool (e.g.
        # re-running after recovering more files, like the webp-to-jpg
        # conversion pass) should be topped up, not sidestepped with a
        # "-2" sibling - _unique_dest_dir's collision avoidance is right
        # for two DIFFERENT clusters landing on the same name, but wrong
        # here since there's nothing to disambiguate: it's the same month.
        if existing.is_dir() and existing not in planned_dirs:
            dest = existing
        else:
            dest = reorganize._unique_dest_dir(year_dir, month_key, planned_dirs)
        planned_dirs.add(dest)
        plan.append({"dest": dest, "records": records, "count": len(records), "kind": "month"})

    return plan


def print_plan(plan):
    total = sum(p["count"] for p in plan)
    moments = [p for p in plan if p["kind"] == "moment"]
    month_folders = [p for p in plan if p["kind"] == "month"]
    print(f"{len(moments)} moment(s), {len(month_folders)} month folder(s), {total} photo(s) total\n")
    for p in sorted(plan, key=lambda p: p["dest"]):
        print(f"  {p['dest'].relative_to(p['dest'].parents[1])}  ({p['count']} photos)")


def execute_plan(library, plan):
    conn = db.connect(library)
    for p in plan:
        p["dest"].parent.mkdir(parents=True, exist_ok=True)
        paths = [r["path"] for r in p["records"]]
        reorganize._move_photos_no_scan(conn, paths, p["dest"])
    conn.commit()
    conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", required=True, help="Destination library root - files are MOVED here.")
    ap.add_argument("--source", action="append", dest="sources",
                     help="Folder (relative to --library) to regroup, e.g. _takeout_import_staging. "
                          "Repeatable. Mutually exclusive with --source-root.")
    ap.add_argument("--source-root", action="append", dest="source_roots",
                     help="Absolute folder to regroup files FROM (e.g. a Takeout export), independent "
                          "of --library - use this for in-place regrouping straight from Takeout "
                          "exports written by write_takeout_metadata_inplace.py. Repeatable.")
    ap.add_argument("--gap-hours", type=float, default=DEFAULT_GAP_HOURS)
    ap.add_argument("--min-moment-size", type=int, default=DEFAULT_MIN_MOMENT_SIZE)
    ap.add_argument("--dry-run", action="store_true", help="Print the plan without moving anything.")
    ap.add_argument("--yes", action="store_true", help="Skip the confirmation prompt (still prints the plan first).")
    args = ap.parse_args()

    if not args.sources and not args.source_roots:
        sys.exit("pass at least one --source or --source-root")
    if args.sources and args.source_roots:
        sys.exit("--source and --source-root are mutually exclusive")

    library = config.Library(args.library)

    if args.sources:
        source_dirs = [(library.photos_root / s).resolve() for s in args.sources]
    else:
        source_dirs = [Path(s).expanduser().resolve() for s in args.source_roots]
    missing = [d for d in source_dirs if not d.is_dir()]
    if missing:
        sys.exit(f"not a directory: {missing[0]}")

    print("Loading city index ...")
    city_index = CityIndex()

    print(f"Scanning {len(source_dirs)} source folder(s) ...")
    candidates, indexed_dates = collect_candidates(library, source_dirs)
    print(f"  {len(candidates)} candidate file(s)")

    print("Reading capture time + GPS for every candidate ...")
    records = build_photo_records(candidates, indexed_dates, city_index)

    gap = timedelta(hours=args.gap_hours)
    fill_missing_cities(records, max_distance=gap)
    clusters = split_into_moments(records, gap)
    kept, month_pools = fold_small_clusters(clusters, args.min_moment_size)
    plan = build_plan(library, kept, month_pools)

    print_plan(plan)

    if args.dry_run:
        print("\n(dry run - nothing moved)")
        return

    if not args.yes:
        answer = input("\nProceed with these moves? [y/N] ").strip().lower()
        if answer != "y":
            print("aborted")
            return

    execute_plan(library, plan)
    print("done")


if __name__ == "__main__":
    main()
