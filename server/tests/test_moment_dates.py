"""
Unit tests for moment_dates.py's date parsing - the shared parser that
replaced six independent, inconsistently-strict re-derivations (see the
module docstring). Pure functions, no filesystem or app state, so these
run in milliseconds and belong in the automated suite rather than behind
a running server.
"""

from datetime import date

import moment_dates as md


def test_day_level_prefix():
    assert md.parse_moment_date_prefix("2026-03-15-Skiing") == date(2026, 3, 15)
    assert md.parse_moment_date_prefix("2026-03-15") == date(2026, 3, 15)
    assert md.has_day_level_prefix("2026-03-15-Skiing")
    assert not md.has_day_level_prefix("2026-03")


def test_month_only_prefix():
    assert md.parse_moment_date_prefix("2026-08") == date(2026, 8, 1)
    assert md.is_month_only_prefix("2026-08")
    # A label makes it a real moment, not a bucket - see the module's own
    # comment on why is_month_only_prefix must NOT accept "2026-08-London".
    assert not md.is_month_only_prefix("2026-08-London")
    assert md.has_date_prefix("2026-08-London")


def test_invalid_dates_rejected_not_crashed():
    # "9999-99-99" is 10 chars with dashes in the right places - the exact
    # shape every ad-hoc check this module replaced let through silently.
    assert md.parse_moment_date_prefix("9999-99-99") is None
    assert md.parse_moment_date_prefix("2026-13-01") is None
    assert md.parse_moment_date_prefix("2026-02-30") is None
    assert md.parse_moment_date_prefix("not-a-date-at-all") is None


def test_raw_phone_backup_layout():
    # <root>/YYYY/MM/photo.jpg - two path components, neither alone a date.
    assert md.parse_moment_date_from_parts("09", "2024") == date(2024, 9, 1)
    assert md.is_month_bucket("09", "2024")
    # A watcher-created YYYY-MM bucket, single component.
    assert md.parse_moment_date_from_parts("2026-08", None) == date(2026, 8, 1)
    assert md.is_month_bucket("2026-08", None)
    # A bare "09" NOT nested under a bare year is never misread as a month.
    assert md.parse_moment_date_from_parts("09", "not-a-year") is None
    assert not md.is_month_bucket("09", "not-a-year")


def test_date_prefix_str_round_trips():
    assert md.date_prefix_str("2024-03-10-LeedsWithDad") == "2024-03-10"
    assert md.date_prefix_str("2026-08") == "2026-08"
    # No date prefix at all - name passes through unchanged, used as the
    # base for building a new date-prefixed folder name.
    assert md.date_prefix_str("RandomFolder") == "RandomFolder"
