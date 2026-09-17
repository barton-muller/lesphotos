"""
One shared parser for the date a moment folder's name encodes.

Before this, six call sites (reorganize.py's _has_date_prefix/
_with_date_prefix/merge_moments, api.py's create_moment/suggest_name, and
/timeline's own SQL CASE) each re-derived this independently, at three
different strictness levels - some validated nothing beyond "10 characters
and a dash in the right place", so "9999-99-99" passed. A raw phone-backup
layout (<root>/YYYY/MM/) or an intermediate watcher-created <root>/YYYY/
YYYY-MM/ folder failed every one of them identically, since none accepted
anything shorter than a full YYYY-MM-DD.
"""

import re
from datetime import date

# Ordered most-specific first: a name is tried against each pattern in turn
# and the first match wins. "YYYY-MM-DD" is deliberately before "YYYY-MM" so
# a real day-level prefix is never mistaken for a shorter month-only one.
# Group 1 in each pattern is the literal date-prefix substring (used by
# date_prefix_str), independent of however many digit groups it captures.
_PATTERNS = (
    re.compile(r"^((\d{4})-(\d{2})-(\d{2}))(?:-.*)?$"),
    re.compile(r"^((\d{4})-(\d{2}))$"),
)

_BARE_YEAR = re.compile(r"^(\d{4})$")
_BARE_MONTH = re.compile(r"^(\d{2})$")


def parse_moment_date_prefix(name):
    """
    The real calendar date a moment folder's name starts with, or None.

    Accepts "YYYY-MM-DD-Label", "YYYY-MM-DD", and "YYYY-MM" (day defaults to
    1). Validates the digits form an actual date - "9999-99-99" is rejected,
    unlike every one of the ad-hoc checks this replaces.
    """
    for pattern in _PATTERNS:
        m = pattern.match(name)
        if not m:
            continue
        digit_groups = m.groups()[1:]
        year, month = int(digit_groups[0]), int(digit_groups[1])
        day = int(digit_groups[2]) if len(digit_groups) > 2 else 1
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def has_date_prefix(name):
    """True if `name` starts with a real date in any accepted shape."""
    return parse_moment_date_prefix(name) is not None


def has_day_level_prefix(name):
    """
    True only for "YYYY-MM-DD" or "YYYY-MM-DD-Label" - not the coarser
    "YYYY-MM". Distinguishes a real, already-organized moment from an
    intermediate month-only bucket (e.g. what a watcher files new uploads
    under before something sweeps them into real moments) - callers that
    need "is this folder done, or still raw?" want this, not
    has_date_prefix, which also accepts the coarser shape.
    """
    return _PATTERNS[0].match(name) is not None


def is_month_only_prefix(name):
    """
    True only for the coarser "YYYY-MM" shape - not a day-level moment.
    A month bucket has no single real date of its own (unlike a real
    moment, whose folder name IS a date the user chose), so its
    moment_date needs to track the newest photo actually inside it,
    recomputed as photos arrive - see
    indexer.py's rebalance_month_bucket_dates.
    """
    return _PATTERNS[1].match(name) is not None


def is_month_bucket(parent_name, grandparent_name):
    """
    True for any folder that holds a month's worth of undated photos and
    nothing more specific - a watcher-created "YYYY-MM", OR a bare "MM"
    directly under a bare "YYYY" (a raw phone-backup layout, e.g.
    <root>/2024/09/). Both need moment_date to track their newest photo
    rather than default to day=1 - see rebalance_month_bucket_dates.
    """
    if is_month_only_prefix(parent_name):
        return True
    return bool(_BARE_MONTH.match(parent_name) and grandparent_name and _BARE_YEAR.match(grandparent_name))


def parse_moment_date_from_parts(parent_name, grandparent_name):
    """
    Like parse_moment_date_prefix, but also recognizes a raw phone-backup
    layout that splits the date across two path components -
    <root>/YYYY/MM/photo.jpg - which a single folder name alone can never
    match, since "MM" on its own (e.g. "09") isn't a date.

    Tries `parent_name` alone first (the normal case: a real moment or a
    watcher-created YYYY-MM bucket), and only falls back to combining
    grandparent+parent when the parent is a bare two-digit month AND the
    grandparent is a bare four-digit year - deliberately narrow, so a
    folder that happens to be named "09" for some other reason (not
    nested under a plain "YYYY" year folder) is never misread as a month.
    """
    direct = parse_moment_date_prefix(parent_name)
    if direct is not None:
        return direct
    month_match = _BARE_MONTH.match(parent_name)
    year_match = _BARE_YEAR.match(grandparent_name) if grandparent_name else None
    if month_match and year_match:
        return parse_moment_date_prefix(f"{year_match.group(1)}-{month_match.group(1)}")
    return None


def date_prefix_str(name):
    """
    The literal date-prefix substring of `name` (e.g. "2024-03-10" out of
    "2024-03-10-LeedsWithDad"), or `name` itself if it has no date prefix.

    For building a new folder name from an existing one's prefix - callers
    that need the *parsed* date (for sorting/validation) want
    parse_moment_date_prefix instead.
    """
    if not has_date_prefix(name):
        return name
    for pattern in _PATTERNS:
        m = pattern.match(name)
        if m:
            return m.group(1)
    return name
