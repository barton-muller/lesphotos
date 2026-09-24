#!/usr/bin/env python3
"""
Auto-suggests city-level labels for moment folders that auto_generate_moments
left as "YYYY-MM-DD-moment" (its generic placeholder label), using each
moment's photos' GPS.

Deliberately broad, recognizable city names, not geocoding.py's
street.city.cc form - that granularity is right for a single-user's own
"where was I" browsing but produces noisy, near-duplicate moment names
across a Takeout import spanning years and countries. This trades
precision for a name a human would actually pick: "leeds-united-kingdom",
not "chapel-allerton-united-kingdom" (a real GeoNames-listed suburb that
happens to be the closest point to a Leeds photo) - see city_lookup.py
for how the "broad" city is chosen (nearest sizeable population centre,
not just nearest point).

Offline and instant: city_lookup.CityIndex is a k-d tree over a local
GeoNames extract (tools/data/cities15000.txt, fetched once via
`python tools/city_lookup.py --fetch`), not geocoding.py's Nominatim -
no network, no 1 req/sec rate limit, so a several-thousand-moment import
resolves in well under a second instead of hours.

Never overwrites a label a user (or an earlier run of this script) already
set - it only touches folders still named "YYYY-MM-DD-moment", matching
rename_moment's own "caller decides whether to apply a suggestion" contract
that geocoding.py documents.

Usage:
    python tools/city_lookup.py --fetch   # once
    python tools/name_moments_by_city.py --library server/data/photos/testlib [--dry-run]
"""

import argparse
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from city_lookup import CityIndex

import config
import geocoding
import reorganize

_MOMENT_PLACEHOLDER = re.compile(r"^\d{4}-\d{2}-\d{2}-moment(-\d+)?$")


def _slug(text):
    """
    Lowercases, transliterates accented Latin to plain ASCII (NFKD
    decompose + drop combining marks - "Västerbotten" -> "vasterbotten",
    not geocoding.py's plain regex-strip, which silently drops the a
    entirely and produces "vsterbotten"), then collapses to [a-z0-9-].
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace(" ", "-")
    return re.sub(r"[^a-z0-9-]", "", text) or "unknown"


def suggest_city_label(city_index, lat, lon):
    """
    "city-country" (e.g. "leeds-united-kingdom"), or a custom_locations
    override, or None if unresolvable. COUNTRY_NAMES lives in
    server/geocoding.py (also used by the web app's /moments/suggest-name,
    a scipy-free port of this same CityIndex for the always-running
    server - see its module comment) rather than duplicated here.
    """
    return city_index.city_label(lat, lon, lambda cc: geocoding.COUNTRY_NAMES.get(cc, cc), _slug)


def _moment_gps(moment_path):
    """First usable GPS fix among the moment's files - one call is enough since a moment is one trip/day."""
    from geocoding import read_gps

    for f in reorganize._iter_moment_files(moment_path):
        if f.suffix.lower() not in reorganize.IMAGE_EXTS:
            continue
        lat, lon = read_gps(f)
        if lat is not None and lon is not None:
            return lat, lon
    return None, None


def find_unnamed_moments(library_root):
    for year_dir in sorted(library_root.iterdir()):
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        for moment_dir in sorted(year_dir.iterdir()):
            if moment_dir.is_dir() and _MOMENT_PLACEHOLDER.match(moment_dir.name):
                yield moment_dir


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    library = config.Library(args.library)
    city_index = CityIndex()

    renamed, no_gps = 0, 0
    for moment_dir in find_unnamed_moments(library.photos_root):
        lat, lon = _moment_gps(moment_dir)
        if lat is None:
            no_gps += 1
            print(f"  (no GPS) {moment_dir.name}")
            continue
        label = suggest_city_label(city_index, lat, lon)
        if not label:
            no_gps += 1
            print(f"  (geocode failed) {moment_dir.name}")
            continue
        print(f"  {moment_dir.name} -> {label}  ({lat:.3f},{lon:.3f})")
        if not args.dry_run:
            reorganize.rename_moment(library, moment_dir, label)
        renamed += 1

    print(f"\n{'Would rename' if args.dry_run else 'Renamed'}: {renamed}, no GPS/unresolved: {no_gps}")


if __name__ == "__main__":
    main()
