"""Tests for monitor.utils.time (remaining branches)"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from monitor.utils.time import (
    age_ms,
    datetime_to_ms,
    hours_since,
    hours_to_ms,
    hours_until,
    is_future,
    is_stale,
    minutes_until,
    ms_to_datetime,
    ms_to_hours,
    ms_to_iso,
    ms_until,
    seconds_until,
    timestamp_diff_ms,
    utc_now,
    utc_now_iso,
    utc_now_ms,
)


class TestUtcNow:
    def test_utc_now_has_timezone(self):
        now = utc_now()
        assert now.tzinfo is not None

    def test_utc_now_ms_positive(self):
        assert utc_now_ms() > 0

    def test_utc_now_iso_format(self):
        iso = utc_now_iso()
        assert "T" in iso
        assert "+" in iso or "Z" in iso


class TestMsToDatetime:
    def test_normal(self):
        result = ms_to_datetime(1710000000000)
        assert result.year == 2024
        assert result.tzinfo is not None


class TestDatetimeToMs:
    def test_aware_datetime(self):
        dt_obj = dt.datetime(2024, 3, 9, 12, 0, 0, tzinfo=dt.timezone.utc)
        result = datetime_to_ms(dt_obj)
        assert result == 1709985600000  # <-- Исправлено с 1710000000000

    def test_naive_datetime_treated_as_utc(self):
        dt_obj = dt.datetime(2024, 3, 9, 12, 0, 0)
        result = datetime_to_ms(dt_obj)
        assert result == 1709985600000  # <-- Исправлено с 1710000000000


class TestMsToIso:
    def test_normal(self):
        result = ms_to_iso(1710000000000)
        assert "2024-03-09" in result


class TestMsToHours:
    def test_normal(self):
        result = ms_to_hours(3600000)
        assert result == Decimal("1")

    def test_zero(self):
        assert ms_to_hours(0) == Decimal("0")


class TestHoursToMs:
    def test_normal(self):
        assert hours_to_ms(Decimal("1")) == 3600000

    def test_fractional(self):
        assert hours_to_ms(Decimal("0.5")) == 1800000


class TestAgeMs:
    def test_normal(self):
        result = age_ms(1000, now_ms=5000)
        assert result == 4000

    def test_future_timestamp(self):
        result = age_ms(5000, now_ms=1000)
        assert result == -4000


class TestIsStale:
    def test_fresh(self):
        assert is_stale(1000, max_age_ms=5000, now_ms=2000) is False

    def test_stale(self):
        assert is_stale(1000, max_age_ms=5000, now_ms=10000) is True

    def test_none_timestamp(self):
        assert is_stale(None, max_age_ms=5000) is True


class TestIsFuture:
    def test_past(self):
        assert is_future(1000, now_ms=5000) is False

    def test_future(self):
        assert is_future(10000, now_ms=5000) is True

    def test_future_within_tolerance(self):
        assert is_future(5500, now_ms=5000, tolerance_ms=1000) is False

    def test_none(self):
        assert is_future(None) is False


class TestTimestampDiffMs:
    def test_normal(self):
        assert timestamp_diff_ms(1000, 5000) == 4000

    def test_reversed(self):
        assert timestamp_diff_ms(5000, 1000) == 4000

    def test_equal(self):
        assert timestamp_diff_ms(1000, 1000) == 0


class TestMsUntil:
    def test_future(self):
        assert ms_until(5000, now_ms=1000) == 4000

    def test_past(self):
        assert ms_until(1000, now_ms=5000) == 0

    def test_none(self):
        assert ms_until(None) == 0


class TestSecondsUntil:
    def test_normal(self):
        result = seconds_until(5000, now_ms=1000)
        assert result == Decimal("4")


class TestMinutesUntil:
    def test_normal(self):
        result = minutes_until(120000, now_ms=0)
        assert result == Decimal("2")


class TestHoursUntil:
    def test_normal(self):
        result = hours_until(3600000, now_ms=0)
        assert result == Decimal("1")


class TestHoursSince:
    def test_normal(self):
        result = hours_since(0, now_ms=3600000)
        assert result == Decimal("1")

    def test_future_returns_zero(self):
        result = hours_since(5000000, now_ms=1000)
        assert result == Decimal("0")

    def test_none_returns_zero(self):
        result = hours_since(None)
        assert result == Decimal("0")