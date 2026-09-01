"""Cron parser surface: parse_cron acceptance/rejection + parse_iso_at."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agentos.scheduler.parser import CronParseError, parse_cron, parse_iso_at

# --- parse_cron ----------------------------------------------------------


def test_parse_cron_accepts_basic_five_field() -> None:
    assert parse_cron("*/5 * * * *").raw == "*/5 * * * *"


def test_parse_cron_accepts_named_dow_and_month() -> None:
    assert parse_cron("0 9 * * 1-5").raw == "0 9 * * 1-5"
    assert parse_cron("30 8 1 jan *").raw == "30 8 1 jan *"


def test_parse_cron_names_are_case_insensitive() -> None:
    # POSIX: month and day-of-week names are case-insensitive. The parser used
    # to substitute only all-lowercase and all-uppercase spellings, so the
    # common "Mon-Fri" business-hours schedule was rejected outright.
    assert parse_cron("0 9 * * Mon-Fri").day_of_week.values == frozenset({1, 2, 3, 4, 5})
    assert parse_cron("0 9 * * MON-FRI").day_of_week.values == frozenset({1, 2, 3, 4, 5})
    assert parse_cron("0 0 * * Mon,Wed,Fri").day_of_week.values == frozenset({1, 3, 5})
    assert parse_cron("0 9 * Jan *").month.values == frozenset({1})
    assert parse_cron("0 9 * JAN *").month.values == frozenset({1})
    assert parse_cron("0 0 * Jan-Mar *").month.values == frozenset({1, 2, 3})
    assert parse_cron("0 0 * JAN-MAR/2 *").month.values == frozenset({1, 3})


def test_parse_cron_accepts_preset_alias() -> None:
    assert parse_cron("@hourly").raw == "0 * * * *"


def test_parse_cron_rejects_wrong_field_count() -> None:
    with pytest.raises(CronParseError, match="Expected 5 fields"):
        parse_cron("0 9 * *")


def test_parse_cron_rejects_out_of_range_value() -> None:
    with pytest.raises(CronParseError, match="out of range"):
        parse_cron("0 25 * * *")


def test_parse_cron_rejects_garbage() -> None:
    with pytest.raises(CronParseError):
        parse_cron("not-a-cron")


def test_parse_cron_accepts_dow_7_as_sunday() -> None:
    # POSIX permits either 0 or 7 to mean Sunday in the day-of-week field.
    expr = parse_cron("0 0 * * 7")
    assert expr.day_of_week.values == frozenset({0})


def test_parse_cron_dow_ranges_may_end_at_7() -> None:
    # With Sunday spellable as 7, a "WED-SUN" style range is valid and must
    # resolve to the same weekday set as its 0-terminated equivalent.
    expr = parse_cron("0 0 * * WED-7")
    assert expr.day_of_week.values == frozenset({0, 3, 4, 5, 6})


def test_parse_cron_dow_7_dedups_with_0_and_names() -> None:
    assert parse_cron("0 0 * * 0,7").day_of_week.values == frozenset({0})
    assert parse_cron("0 0 * * MON,7").day_of_week.values == frozenset({0, 1})


def test_parse_cron_dow_7_matches_sunday_not_monday() -> None:
    expr = parse_cron("0 0 * * 7")
    sunday = datetime(2026, 8, 30, 0, 0)  # a Sunday
    monday = datetime(2026, 8, 31, 0, 0)  # the next Monday
    assert expr.matches(sunday)
    assert not expr.matches(monday)


def test_parse_cron_rejects_unknown_preset() -> None:
    with pytest.raises(CronParseError, match="Unknown preset"):
        parse_cron("@bogus")


def test_parse_cron_rejects_reversed_range_with_step() -> None:
    # A reversed range in the step branch used to parse into an *empty* field
    # set, so the expression validated, stored, and then matched nothing —
    # _next_run would burn through its whole scan window and raise
    # "No valid next run found" at job creation. Reject it up front like the
    # plain-range branch already does.
    with pytest.raises(CronParseError, match="Range start > end"):
        parse_cron("5-3/2 * * * *")
    with pytest.raises(CronParseError, match="Range start > end"):
        parse_cron("0 0 * * FRI-TUE/2")


# ---------------------------------------------------------------------------
# CronExpression.matches — day-of-month / day-of-week OR semantics (#660)
# ---------------------------------------------------------------------------


def test_matches_ors_dom_and_dow_when_both_restricted() -> None:
    """POSIX: when both day-of-month and day-of-week are restricted,
    the job fires when EITHER matches."""
    expr = parse_cron("0 0 1,15 * 5")

    # Friday (day=7, not 1 or 15) → should match via day-of-week
    friday = datetime(2026, 8, 7, 0, 0, tzinfo=UTC)
    assert expr.matches(friday), "Friday should match via day-of-week"

    # 1st (Saturday) → should match via day-of-month
    first = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    assert expr.matches(first), "1st should match via day-of-month"

    # 15th (Saturday) → should match via day-of-month
    fifteenth = datetime(2026, 8, 15, 0, 0, tzinfo=UTC)
    assert expr.matches(fifteenth), "15th should match via day-of-month"

    # Monday 3rd (not 1st/15th, not Friday) → should NOT match
    monday_3rd = datetime(2026, 8, 3, 0, 0, tzinfo=UTC)
    assert not expr.matches(monday_3rd), "Monday 3rd should NOT match"


def test_matches_ands_dom_and_dow_when_one_is_wild() -> None:
    """When either day-of-month or day-of-week is a wildcard, AND semantics
    apply as normal."""
    # DOM restricted, DOW wildcard → only 1st and 15th
    dom_only = parse_cron("0 0 1,15 * *")
    friday_7th = datetime(2026, 8, 7, 0, 0, tzinfo=UTC)
    assert not dom_only.matches(friday_7th), "DOM-only should not match Friday 7th"
    assert dom_only.matches(datetime(2026, 8, 1, 0, 0, tzinfo=UTC)), "1st should match"

    # DOW restricted, DOM wildcard → only Fridays
    dow_only = parse_cron("0 0 * * 5")
    sat_1st = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    assert not dow_only.matches(sat_1st), "DOW-only should not match Saturday 1st"
    assert dow_only.matches(friday_7th), "DOW-only should match Friday"


def test_matches_ands_dom_and_dow_when_both_wild() -> None:
    """Both wildcards → every day."""
    every = parse_cron("0 0 * * *")
    assert every.matches(datetime(2026, 8, 3, 0, 0, tzinfo=UTC))
    with pytest.raises(CronParseError, match="Range start > end"):
        parse_cron("0 0 * dec-feb/2 *")


# --- parse_iso_at --------------------------------------------------------


def test_parse_iso_at_accepts_offset() -> None:
    dt = parse_iso_at("2026-05-15T09:00:00+08:00")
    assert dt.tzinfo is not None
    assert dt.year == 2026 and dt.hour == 9


def test_parse_iso_at_accepts_z_suffix() -> None:
    dt = parse_iso_at("2026-05-15T01:00:00Z")
    assert dt.tzinfo is not None
    assert dt.astimezone(UTC) == datetime(2026, 5, 15, 1, 0, tzinfo=UTC)


def test_parse_iso_at_rejects_naive_datetime() -> None:
    with pytest.raises(CronParseError, match="timezone"):
        parse_iso_at("2026-05-15T09:00:00")


def test_parse_iso_at_rejects_garbage() -> None:
    with pytest.raises(CronParseError, match="Invalid ISO-8601"):
        parse_iso_at("not-a-timestamp")


def test_parse_iso_at_rejects_empty() -> None:
    with pytest.raises(CronParseError, match="must not be empty"):
        parse_iso_at("   ")


def test_parse_iso_at_rejects_non_string() -> None:
    with pytest.raises(CronParseError, match="Expected ISO-8601 string"):
        parse_iso_at(12345)  # type: ignore[arg-type]
