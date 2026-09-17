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
