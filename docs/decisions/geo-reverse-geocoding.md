# Decision: reverse geocoding approach for auto moment naming

## Context

PhotoView exposes raw GPS coordinates (`MediaEXIF.coordinates.{latitude,longitude}`) but does
no reverse geocoding itself (confirmed via source inspection — see the archived plan.md). To auto-name
moments by location ("Spain Holiday" instead of "2024-06-10-moment"), we need our own
reverse-geocoding step, the way dad's `initial_script.py` already did with Nominatim.

## Options considered

**A. Nominatim (OpenStreetMap), as dad's script used.**
- Free, no API key, good coverage.
- Hard usage policy: max 1 request/second, requires attribution, requires a real User-Agent,
  and their public instance can rate-limit or block abusive usage.
- Requires internet access from wherever the geocoding runs (the DS124, in production).
- dad's script already has working, tested code for this (`get_location_details` in
  `initial_script.py`) — reusable almost as-is.

**B. A commercial geocoding API (Google, Mapbox, etc.)**
- Faster, higher rate limits, generally better address data.
- Costs money past a free tier, requires an API key/account, adds an external paid dependency
  to a project whose whole design philosophy is avoiding dependence on external services long
  after this codebase might be gone (see the original design doc's own principles).
- Rejected for that reason — inconsistent with the project's stated goals.

**C. Offline reverse geocoding (a local dataset, e.g. `reverse_geocoder` Python package using
GeoNames data, or a bundled city/country lookup table).**
- No network dependency, no rate limit, fast, works even if NAS has no internet.
- Coarser results — typically nearest-city-level, not street-level (dad's script's street-level
  detail would be lost).
- No usage-policy risk at all — genuinely "run this as often as you want."

## Decision: **A, with a cache, matching dad's approach — not C**

Reasoning: moment naming happens rarely (once per new moment during a scan/ingest pass, not
per-photo-per-browse), so Nominatim's 1 req/sec limit is a non-issue in practice — a scan
ingesting even 50 new moments in one run is under a minute of geocoding calls, and results are
cached by rounded coordinate (matching dad's `GEO_PRECISION`/`_geo_cache` pattern) so a moment's
location is only ever looked up once. Street-level detail (dad's script's `street, city,
country` breakdown) is worth keeping for the auto-suggested name, since the user can always
edit it — losing detail to gain speed we don't need is the wrong tradeoff here. Revisit if
real-world testing shows Nominatim's public instance rate-limiting or blocking becomes a
practical problem (at which point running a local Nominatim instance, or switching to C, both
stay open options — nothing here is hard to reverse).

## What got built

`server/geocoding.py` — adapted from `initial_script.py`'s `get_location_details`/`_slug`, as
a standalone reusable module (not copy-pasted inline into `reorganize.py`), with the same
coordinate-rounding cache. Wired into moment creation as a *suggested* label, never silently
overwriting a name the user (or a prior auto-suggestion) already set — matches the "app should
suggest a geo name, user can always override" idea from the original conversation.
