#!/usr/bin/env python3
"""
Offline "nearest major city" lookup over GeoNames' cities15000.txt (every
place with population >= 15,000; ~34k rows, ~8.5MB, tab-separated).
Not committed to the repo (see .gitignore) - fetch it once with
`python tools/city_lookup.py --fetch`.

Why not reverse_geocoder's bundled cities1000.csv (population >= 1,000):
plain nearest-neighbor against that dataset resolves a Leeds photo to
"Chapel Allerton" and a Manchester photo to "Bowdon" - the literal
nearest GeoNames point, which is a real suburb but not the name a human
would give a moment folder. This module instead takes the K nearest
candidates within a radius and returns the highest-population one, so a
suburb loses to the major city it's actually part of. Verified against 6
known real-world coordinates - see tools/name_moments_by_city.py's test
notes.

Why not Nominatim's address hierarchy (city/town/village fields): that's
what geocoding.py already uses for street-level suggestions, but it's
network-bound at 1 req/sec, making a several-thousand-moment import take
hours. This is a local k-d tree query, ~instant per lookup, no network,
no rate limit.
"""

import csv
import json
import math
import sys
from pathlib import Path

from scipy.spatial import cKDTree

DATA_PATH = Path(__file__).resolve().parent / "data" / "cities15000.txt"
GEONAMES_URL = "https://download.geonames.org/export/dump/cities15000.zip"

# Places you live/visit often enough that the population-based "nearest
# BIG city" pick is wrong for them - a real, well-known city can still
# lose to an even bigger neighbour (Delft, ~100k people, lost to
# Rotterdam every time under the plain nearest_major_city lookup, since
# Rotterdam is both bigger and within the search radius), and a private
# place (a cottage, a specific house) was never going to be in GeoNames
# at all. Checked BEFORE the population lookup, so an entry here always
# wins within its radius regardless of what's nearby. Not committed with
# real coordinates filled in - see custom_locations.json's own template
# entries, which need editing to the user's actual coordinates.
CUSTOM_LOCATIONS_PATH = Path(__file__).resolve().parent / "custom_locations.json"

# How far to look for a bigger nearby city before giving up and returning
# the plain nearest entry. In degrees, not km - 0.5 deg is roughly 35-55km
# depending on latitude, comfortably covering a metro area without
# reaching into a neighbouring one for most of the world's cities.
DEFAULT_RADIUS_DEG = 0.5
DEFAULT_K = 20


def _haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km - accurate enough for a "within N km of home" check."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def load_custom_locations(path=CUSTOM_LOCATIONS_PATH):
    """[{"name", "lat", "lon", "radius_km"}, ...], or [] if the file is missing/empty."""
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def fetch_cities_dataset(dest=DATA_PATH):
    """
    Downloads and extracts cities15000.txt if it isn't already present.
    Not called automatically - run once, deliberately, since it's a
    network fetch: `python tools/city_lookup.py --fetch`.
    """
    import io
    import urllib.request
    import zipfile

    if dest.exists():
        print(f"already present: {dest}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {GEONAMES_URL} ...")
    req = urllib.request.Request(GEONAMES_URL, headers={"User-Agent": "lesphotos-import/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        with z.open("cities15000.txt") as src, open(dest, "wb") as out:
            out.write(src.read())
    print(f"wrote {dest} ({dest.stat().st_size / 1e6:.1f}MB)")


class CityIndex:
    """A k-d tree over cities15000.txt, queryable for the nearest sizeable city to a coordinate."""

    def __init__(self, data_path=DATA_PATH, custom_locations_path=CUSTOM_LOCATIONS_PATH):
        if not data_path.exists():
            raise FileNotFoundError(
                f"{data_path} not found - run `python tools/city_lookup.py --fetch` first "
                "(one-time ~3MB download of GeoNames' cities15000 dataset)"
            )
        self.rows = []  # (lat, lon, name, country_code, population)
        with open(data_path, encoding="utf-8") as f:
            for r in csv.reader(f, delimiter="\t"):
                self.rows.append((float(r[4]), float(r[5]), r[1], r[8], int(r[14])))
        coords = [(r[0], r[1]) for r in self.rows]
        self.tree = cKDTree(coords)
        self.custom_locations = load_custom_locations(custom_locations_path)

    def nearest_major_city(self, lat, lon, radius_deg=DEFAULT_RADIUS_DEG, k=DEFAULT_K):
        """
        Returns (name, country_code, population) for the highest-population
        city within radius_deg of (lat, lon), or the single nearest entry
        if nothing is that close (e.g. a remote area with no larger city
        nearby - the plain-nearest result is still the most useful answer
        there, same as reverse_geocoder's behaviour).

        Does NOT consult custom_locations - see city_label() for the
        override-aware entry point callers should normally use instead.
        """
        dist, idx = self.tree.query((lat, lon), k=k)
        # scipy returns a bare int/float instead of a length-1 array when k=1;
        # not reachable with our DEFAULT_K=20, but keep query() well-defined.
        if k == 1:
            idx, dist = [idx], [dist]
        candidates = [self.rows[i] for i, d in zip(idx, dist) if d <= radius_deg]
        if not candidates:
            return self.rows[idx[0]][2:]
        return max(candidates, key=lambda r: r[4])[2:]

    def custom_location_for(self, lat, lon):
        """
        The first custom_locations entry whose radius_km contains (lat,
        lon), or None. First-match order follows the JSON file's own
        order, so a caller who wants one entry to take priority over an
        overlapping one lists it first.
        """
        for loc in self.custom_locations:
            if _haversine_km(lat, lon, loc["lat"], loc["lon"]) <= loc["radius_km"]:
                return loc["name"]
        return None

    def city_label(self, lat, lon, country_namer, slugger):
        """
        The final, ready-to-use folder-name slug for (lat, lon): a
        custom_locations override if one matches (used AS-IS - entries
        in custom_locations.json are written already slugged, e.g.
        "delft-netherlands", so a caller doesn't re-slug them), else the
        population-based nearest major city, slugged via `slugger` (a
        text -> slug callable) and joined with its country name (via
        `country_namer`, a cc -> country-name callable - both owned by
        the caller, since the slug/country-name logic already existed
        independently in name_moments_by_city.py and
        auto_generate_moments_by_location.py before this method existed;
        passed in rather than duplicated here).
        Returns None if nothing resolves at all (only possible for the
        population branch: an empty cities15000.txt, never expected in
        practice).
        """
        custom = self.custom_location_for(lat, lon)
        if custom:
            return custom
        name, cc, _population = self.nearest_major_city(lat, lon)
        country = country_namer(cc)
        parts = [p for p in (name and slugger(name), country and slugger(country)) if p]
        return "-".join(parts) if parts else None


if __name__ == "__main__":
    if "--fetch" in sys.argv:
        fetch_cities_dataset()
    else:
        print("usage: python tools/city_lookup.py --fetch")
