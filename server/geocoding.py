#!/usr/bin/env python3
"""
Reverse geocoding for auto-suggested moment names. Adapted from dad's
initial_script.py (get_location_details/_slug) as a standalone reusable
module - see docs/decisions/geo-reverse-geocoding.md for why Nominatim was
kept over a commercial API or an offline dataset.

This never overwrites a moment label the user (or an earlier auto-suggest)
already set - callers decide whether to apply a suggestion, this module
only produces one.
"""

import csv
import json
import math
import re
import time
import unicodedata
from pathlib import Path

from geopy.geocoders import Nominatim
from PIL import Image

SEPARATOR = "."

# Reverse geocoding is the slowest part of a scan. Photos from one moment
# share a location, so results are cached against coordinates rounded to
# this many decimal places (3 dp is roughly 110m) - see the decision doc
# for why this makes Nominatim's 1 req/sec policy a non-issue in practice.
GEO_PRECISION = 3
_geo_cache = {}

_geolocator = Nominatim(user_agent="lesphotos")


def _slug(text):
    """Lowercases, collapses whitespace to hyphens, strips anything outside [a-z0-9-]."""
    text = text.lower().replace(" ", "-")
    return re.sub(r"[^a-z0-9-]", "", text) or "unknown"


def suggest_location_label(lat, lon):
    """
    Reverse-geocodes a coordinate to a filename-safe "street.city.cc" style
    label, or None if there's no GPS fix. Never raises - a lookup failure
    degrades to None rather than blocking whatever moment-creation flow
    called this.
    """
    if lat is None or lon is None:
        return None

    key = (round(lat, GEO_PRECISION), round(lon, GEO_PRECISION))
    if key in _geo_cache:
        return _geo_cache[key]

    result = None
    try:
        time.sleep(1)  # Nominatim usage policy: at most one request per second
        location = _geolocator.reverse(key, timeout=10, language="en")

        if location and "address" in location.raw:
            address = location.raw["address"]
            street = address.get("road") or address.get("suburb")
            city = address.get("city") or address.get("town") or address.get("village")
            cc = address.get("country_code", "").lower()
            if cc == "gb":
                cc = "uk"  # the postal convention people actually recognise
            parts = [p for p in (street and _slug(street), city and _slug(city), cc) if p]
            if parts:
                result = SEPARATOR.join(parts)

    except Exception as e:
        print(f"  ! geocoding failed for {key}: {e}")

    _geo_cache[key] = result
    return result


def _dms_to_decimal(dms, ref):
    """Same conversion as initial_script.py's _dms_to_decimal - EXIF stores each DMS component as a rational."""
    if not dms or not ref:
        return None
    try:
        degrees = float(dms[0]) + float(dms[1]) / 60.0 + float(dms[2]) / 3600.0
        if ref in ("S", "W"):
            degrees = -degrees
        return degrees
    except (TypeError, IndexError, ValueError):
        return None


def read_gps(image_path):
    """Returns (lat, lon), either of which may be None if the photo has no GPS fix or is unreadable."""
    try:
        with Image.open(image_path) as img:
            exif = img.getexif()
            gps = exif.get_ifd(0x8825) if exif else None
            if gps:
                lat = _dms_to_decimal(gps.get(2), gps.get(1))
                lon = _dms_to_decimal(gps.get(4), gps.get(3))
                return lat, lon
    except Exception:
        pass
    return None, None


def suggest_moment_label(photo_gps_list):
    """
    Given a list of (lat, lon) pairs for a moment's photos (some may be
    (None, None) for photos with no GPS fix), returns a single suggested
    location label for the whole moment, or None if no photo had a fix.
    Uses the first photo with a GPS fix - photos in one moment are assumed
    to share roughly one location, matching dad's script's approach of
    labeling a whole event by its most common city rather than per-photo.
    """
    for lat, lon in photo_gps_list:
        label = suggest_location_label(lat, lon)
        if label:
            return label
    return None


# --- Coarse "nearest major city" naming (server-side, no scipy) ----------
#
# tools/name_moments_by_city.py's CityIndex (used for the Takeout import)
# gets its speed from a scipy k-d tree, but scipy is deliberately kept out
# of the always-running server (import-tools extra in pyproject.toml) -
# on the DS124 (1GB RAM total, ~300MB actually free) `import scipy.spatial`
# alone costs ~52MB resident, permanently, for the life of the process,
# measured via tools/benchmark_city_lookup.py (since deleted - a one-off
# check, not a fixture) on real NAS hardware. A linear scan of the ~34k-row
# GeoNames dataset was measured on that same hardware at ~62ms/query - slow
# next to the k-d tree's ~0.2ms, but this endpoint is a single opt-in
# button click, not a bulk import, so the 62ms is imperceptible and not
# worth a permanent 52MB tax. See docs/known-issues.md's now-resolved
# "Suggest name gives an overly specific geopoint name" entry for why this
# exists at all: geocoding.py's Nominatim path is deliberately street-level
# (see docs/decisions/geo-reverse-geocoding.md), which is too noisy for a
# quick "roughly where was this" suggestion - this gives the same coarse
# "city-country" style name the Takeout import already used.
CITIES_DATA_PATH = Path(__file__).resolve().parent.parent / "tools" / "data" / "cities15000.txt"
CUSTOM_LOCATIONS_PATH = Path(__file__).resolve().parent.parent / "tools" / "custom_locations.json"

# How far to look for a bigger nearby city before falling back to the
# plain nearest entry - matches tools/city_lookup.py's DEFAULT_RADIUS_DEG.
CITY_RADIUS_DEG = 0.5

# ISO 3166-1 alpha-2 -> name, only for the codes GeoNames actually uses in
# cities15000.txt. Kept here (not in tools/) since tools/name_moments_by_city.py
# is the one-directional importer of server/ code, never the reverse - this
# table is shared by both.
COUNTRY_NAMES = {
    "AD": "Andorra", "AE": "United Arab Emirates", "AF": "Afghanistan", "AG": "Antigua and Barbuda",
    "AI": "Anguilla", "AL": "Albania", "AM": "Armenia", "AO": "Angola", "AR": "Argentina", "AS": "American Samoa",
    "AT": "Austria", "AU": "Australia", "AW": "Aruba", "AZ": "Azerbaijan", "BA": "Bosnia and Herzegovina",
    "BB": "Barbados", "BD": "Bangladesh", "BE": "Belgium", "BF": "Burkina Faso", "BG": "Bulgaria",
    "BH": "Bahrain", "BI": "Burundi", "BJ": "Benin", "BM": "Bermuda", "BN": "Brunei", "BO": "Bolivia",
    "BR": "Brazil", "BS": "Bahamas", "BT": "Bhutan", "BW": "Botswana", "BY": "Belarus", "BZ": "Belize",
    "CA": "Canada", "CD": "DR Congo", "CF": "Central African Republic", "CG": "Congo", "CH": "Switzerland",
    "CI": "Ivory Coast", "CL": "Chile", "CM": "Cameroon", "CN": "China", "CO": "Colombia", "CR": "Costa Rica",
    "CU": "Cuba", "CV": "Cape Verde", "CW": "Curacao", "CY": "Cyprus", "CZ": "Czechia", "DE": "Germany",
    "DJ": "Djibouti", "DK": "Denmark", "DM": "Dominica", "DO": "Dominican Republic", "DZ": "Algeria",
    "EC": "Ecuador", "EE": "Estonia", "EG": "Egypt", "ER": "Eritrea", "ES": "Spain", "ET": "Ethiopia",
    "FI": "Finland", "FJ": "Fiji", "FM": "Micronesia", "FO": "Faroe Islands", "FR": "France", "GA": "Gabon",
    "GB": "United Kingdom", "GD": "Grenada", "GE": "Georgia", "GF": "French Guiana", "GG": "Guernsey",
    "GH": "Ghana", "GI": "Gibraltar", "GL": "Greenland", "GM": "Gambia", "GN": "Guinea", "GP": "Guadeloupe",
    "GQ": "Equatorial Guinea", "GR": "Greece", "GT": "Guatemala", "GU": "Guam", "GW": "Guinea-Bissau",
    "GY": "Guyana", "HK": "Hong Kong", "HN": "Honduras", "HR": "Croatia", "HT": "Haiti", "HU": "Hungary",
    "ID": "Indonesia", "IE": "Ireland", "IL": "Israel", "IM": "Isle of Man", "IN": "India", "IQ": "Iraq",
    "IR": "Iran", "IS": "Iceland", "IT": "Italy", "JE": "Jersey", "JM": "Jamaica", "JO": "Jordan",
    "JP": "Japan", "KE": "Kenya", "KG": "Kyrgyzstan", "KH": "Cambodia", "KI": "Kiribati", "KM": "Comoros",
    "KN": "Saint Kitts and Nevis", "KP": "North Korea", "KR": "South Korea", "KW": "Kuwait",
    "KY": "Cayman Islands", "KZ": "Kazakhstan", "LA": "Laos", "LB": "Lebanon", "LC": "Saint Lucia",
    "LI": "Liechtenstein", "LK": "Sri Lanka", "LR": "Liberia", "LS": "Lesotho", "LT": "Lithuania",
    "LU": "Luxembourg", "LV": "Latvia", "LY": "Libya", "MA": "Morocco", "MC": "Monaco", "MD": "Moldova",
    "ME": "Montenegro", "MF": "Saint Martin", "MG": "Madagascar", "MH": "Marshall Islands",
    "MK": "North Macedonia", "ML": "Mali", "MM": "Myanmar", "MN": "Mongolia", "MO": "Macau", "MP": "Northern Mariana Islands",
    "MQ": "Martinique", "MR": "Mauritania", "MS": "Montserrat", "MT": "Malta", "MU": "Mauritius",
    "MV": "Maldives", "MW": "Malawi", "MX": "Mexico", "MY": "Malaysia", "MZ": "Mozambique", "NA": "Namibia",
    "NC": "New Caledonia", "NE": "Niger", "NG": "Nigeria", "NI": "Nicaragua", "NL": "Netherlands",
    "NO": "Norway", "NP": "Nepal", "NR": "Nauru", "NZ": "New Zealand", "OM": "Oman", "PA": "Panama",
    "PE": "Peru", "PF": "French Polynesia", "PG": "Papua New Guinea", "PH": "Philippines", "PK": "Pakistan",
    "PL": "Poland", "PR": "Puerto Rico", "PS": "Palestine", "PT": "Portugal", "PW": "Palau", "PY": "Paraguay",
    "QA": "Qatar", "RE": "Reunion", "RO": "Romania", "RS": "Serbia", "RU": "Russia", "RW": "Rwanda",
    "SA": "Saudi Arabia", "SB": "Solomon Islands", "SC": "Seychelles", "SD": "Sudan", "SE": "Sweden",
    "SG": "Singapore", "SI": "Slovenia", "SK": "Slovakia", "SL": "Sierra Leone", "SM": "San Marino",
    "SN": "Senegal", "SO": "Somalia", "SR": "Suriname", "SS": "South Sudan", "ST": "Sao Tome and Principe",
    "SV": "El Salvador", "SX": "Sint Maarten", "SY": "Syria", "SZ": "Eswatini", "TC": "Turks and Caicos Islands",
    "TD": "Chad", "TG": "Togo", "TH": "Thailand", "TJ": "Tajikistan", "TL": "Timor-Leste", "TM": "Turkmenistan",
    "TN": "Tunisia", "TO": "Tonga", "TR": "Turkey", "TT": "Trinidad and Tobago", "TW": "Taiwan",
    "TZ": "Tanzania", "UA": "Ukraine", "UG": "Uganda", "US": "United States", "UY": "Uruguay",
    "UZ": "Uzbekistan", "VC": "Saint Vincent and the Grenadines", "VE": "Venezuela", "VG": "British Virgin Islands",
    "VI": "US Virgin Islands", "VN": "Vietnam", "VU": "Vanuatu", "WS": "Samoa", "XK": "Kosovo",
    "YE": "Yemen", "YT": "Mayotte", "ZA": "South Africa", "ZM": "Zambia", "ZW": "Zimbabwe",
}

_city_rows = None  # lazily loaded (lat, lon, name, country_code, population) tuples
_custom_locations = None  # lazily loaded [{"name", "lat", "lon", "radius_km"}, ...]


def _city_slug(text):
    """
    Lowercases, transliterates accented Latin to plain ASCII (NFKD
    decompose + drop combining marks), then collapses to [a-z0-9-]. Same
    approach as tools/name_moments_by_city.py's _slug.
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace(" ", "-")
    return re.sub(r"[^a-z0-9-]", "", text) or "unknown"


def _load_city_rows():
    global _city_rows
    if _city_rows is None:
        rows = []
        with open(CITIES_DATA_PATH, encoding="utf-8") as f:
            for r in csv.reader(f, delimiter="\t"):
                rows.append((float(r[4]), float(r[5]), r[1], r[8], int(r[14])))
        _city_rows = rows
    return _city_rows


def _load_custom_locations():
    global _custom_locations
    if _custom_locations is None:
        try:
            _custom_locations = json.loads(CUSTOM_LOCATIONS_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            _custom_locations = []
    return _custom_locations


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _custom_location_for(lat, lon):
    for loc in _load_custom_locations():
        if _haversine_km(lat, lon, loc["lat"], loc["lon"]) <= loc["radius_km"]:
            return loc["name"]
    return None


def _nearest_major_city(lat, lon, radius_deg=CITY_RADIUS_DEG):
    """
    Plain linear scan (no k-d tree - see the module-level comment above for
    why) over cities15000.txt: the highest-population city within
    radius_deg of (lat, lon), or the single nearest entry if nothing is
    that close. Same selection rule as tools/city_lookup.py's
    nearest_major_city, just O(n) instead of O(log n).
    """
    rows = _load_city_rows()
    candidates = [r for r in rows if abs(r[0] - lat) <= radius_deg and abs(r[1] - lon) <= radius_deg]
    if not candidates:
        best = min(rows, key=lambda r: (r[0] - lat) ** 2 + (r[1] - lon) ** 2)
        return best[2:]
    return max(candidates, key=lambda r: r[4])[2:]


def suggest_city_label(lat, lon):
    """
    Coarse "city-country" label (e.g. "leeds-united-kingdom") for a
    coordinate - a custom_locations.json override if one matches, else the
    nearest sizeable city. Returns None if lat/lon is missing or nothing
    resolves (only possible if cities15000.txt is missing/empty).
    """
    if lat is None or lon is None:
        return None
    if not CITIES_DATA_PATH.exists():
        return None

    custom = _custom_location_for(lat, lon)
    if custom:
        return custom
    name, cc, _population = _nearest_major_city(lat, lon)
    country = COUNTRY_NAMES.get(cc, cc)
    parts = [p for p in (name and _city_slug(name), country and _city_slug(country)) if p]
    return "-".join(parts) if parts else None


def suggest_city_moment_label(photo_gps_list):
    """Same contract as suggest_moment_label, but coarse city-level naming instead of Nominatim street-level."""
    for lat, lon in photo_gps_list:
        label = suggest_city_label(lat, lon)
        if label:
            return label
    return None
