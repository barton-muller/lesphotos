"""
Gap-based event grouping, adapted from initial_script.py's split_into_events.

Unlike the original, there is no forced split at year boundaries: a moment
that spans New Year's stays one folder, filed under the year of its first
photo. See plan.md for why.
"""

from datetime import timedelta

DEFAULT_EVENT_GAP = timedelta(days=2)


def group_into_moments(items, when_key, event_gap=DEFAULT_EVENT_GAP):
    """
    Sorts items by when_key(item) and starts a new moment whenever the gap
    since the previous item exceeds event_gap. No year-boundary split.
    """
    items = sorted(items, key=when_key)

    moments = []
    current = []

    for item in items:
        if not current:
            current = [item]
            continue

        previous_when = when_key(current[-1])
        if (when_key(item) - previous_when) > event_gap:
            moments.append(current)
            current = [item]
        else:
            current.append(item)

    if current:
        moments.append(current)

    return moments
