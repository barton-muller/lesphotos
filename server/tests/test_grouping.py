"""
Unit tests for fakelib/grouping.py's gap-based event grouping - the same
logic initial_script.py used to build moments from raw timestamps.
"""

from datetime import datetime, timedelta

from fakelib.grouping import group_into_moments


def test_splits_on_gap_exceeding_threshold():
    base = datetime(2026, 1, 1, 12, 0)
    items = [base, base + timedelta(hours=1), base + timedelta(days=5)]
    moments = group_into_moments(items, when_key=lambda d: d)
    assert len(moments) == 2
    assert moments[0] == [base, base + timedelta(hours=1)]
    assert moments[1] == [base + timedelta(days=5)]


def test_no_split_within_gap():
    base = datetime(2026, 1, 1)
    items = [base, base + timedelta(days=1), base + timedelta(days=1, hours=23)]
    moments = group_into_moments(items, when_key=lambda d: d)
    assert len(moments) == 1
    assert len(moments[0]) == 3


def test_no_forced_split_at_year_boundary():
    # Deliberately different from the original split_into_events - a
    # moment spanning New Year's must stay one folder. See the module
    # docstring.
    dec31 = datetime(2025, 12, 31, 23, 0)
    jan1 = datetime(2026, 1, 1, 1, 0)
    moments = group_into_moments([dec31, jan1], when_key=lambda d: d)
    assert len(moments) == 1


def test_unsorted_input_is_sorted_first():
    base = datetime(2026, 1, 1)
    items = [base + timedelta(hours=2), base, base + timedelta(hours=1)]
    moments = group_into_moments(items, when_key=lambda d: d)
    assert len(moments) == 1
    assert moments[0] == sorted(items)


def test_empty_input():
    assert group_into_moments([], when_key=lambda d: d) == []
