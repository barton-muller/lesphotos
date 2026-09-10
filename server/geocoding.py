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

import re
import time

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
