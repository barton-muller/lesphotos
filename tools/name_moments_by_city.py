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


# ISO 3166-1 alpha-2 -> name, only for the codes GeoNames actually uses in
# cities15000.txt. A static table rather than a pycountry dependency: it's
# a closed, essentially-frozen list (new country codes are a once-a-decade
# event) so there's nothing here that benefits from being data-driven.
_COUNTRY_NAMES = {
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


def suggest_city_label(city_index, lat, lon):
    """"city-country" (e.g. "leeds-united-kingdom"), or a custom_locations override, or None if unresolvable."""
    return city_index.city_label(lat, lon, lambda cc: _COUNTRY_NAMES.get(cc, cc), _slug)


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
