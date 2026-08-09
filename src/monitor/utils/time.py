from __future__ import annotations

import datetime as dt
from decimal import Decimal

from .decimal import (
    MS_PER_HOUR_DECIMAL,
    MS_PER_MINUTE_DECIMAL,
    MS_PER_SECOND_DECIMAL,
    ZERO,
    to_decimal,
)

# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------

MS_PER_SECOND = 1000
MS_PER_MINUTE = 60_000
MS_PER_HOUR = 3_600_000


# ---------------------------------------------------------------------
# Current time
# ---------------------------------------------------------------------


def utc_now() -> dt.datetime:
    """
    Current UTC datetime with timezone.
    """

    return dt.datetime.now(dt.timezone.utc)


def utc_now_ms() -> int:
    """
    Current UTC timestamp in milliseconds.
    """

    return int(utc_now().timestamp() * 1000)


def utc_now_iso() -> str:
    """
    Current UTC datetime in ISO format with millisecond precision.
    """

    return utc_now().isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------


def ms_to_datetime(timestamp_ms: int) -> dt.datetime:
    """
    Convert UTC milliseconds to timezone-aware datetime.
    """

    return dt.datetime.fromtimestamp(
        timestamp_ms / 1000,
        tz=dt.timezone.utc,
    )


def datetime_to_ms(value: dt.datetime) -> int:
    """
    Convert datetime to UTC milliseconds.

    If datetime is naive, it is treated as UTC.
    """

    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)

    return int(value.timestamp() * 1000)


def ms_to_iso(timestamp_ms: int) -> str:
    """
    Convert UTC milliseconds to ISO string.
    """

    return ms_to_datetime(timestamp_ms).isoformat(timespec="milliseconds")


def ms_to_hours(ms: int | float | Decimal) -> Decimal:
    """
    Convert milliseconds to Decimal hours.
    """

    return to_decimal(ms) / MS_PER_HOUR_DECIMAL


def hours_to_ms(hours: Decimal | int | float | str) -> int:
    """
    Convert Decimal hours to integer milliseconds.
    """

    return int(to_decimal(hours) * MS_PER_HOUR_DECIMAL)


# ---------------------------------------------------------------------
# Age / staleness helpers
# ---------------------------------------------------------------------


def age_ms(
    timestamp_ms: int,
    now_ms: int | None = None,
) -> int:
    """
    Age of timestamp in milliseconds.

    Result can be negative if timestamp is in the future.
    """

    now = utc_now_ms() if now_ms is None else int(now_ms)
    return now - int(timestamp_ms)


def is_stale(
    timestamp_ms: int | None,
    max_age_ms: int,
    now_ms: int | None = None,
) -> bool:
    """
    Return True if timestamp is missing or older than max_age_ms.
    """

    if timestamp_ms is None:
        return True

    return age_ms(timestamp_ms, now_ms) > int(max_age_ms)


def is_future(
    timestamp_ms: int | None,
    now_ms: int | None = None,
    tolerance_ms: int = 0,
) -> bool:
    """
    Return True if timestamp is in the future beyond tolerance.
    """

    if timestamp_ms is None:
        return False

    now = utc_now_ms() if now_ms is None else int(now_ms)
    return int(timestamp_ms) > now + int(tolerance_ms)


def timestamp_diff_ms(
    first_timestamp_ms: int,
    second_timestamp_ms: int,
) -> int:
    """
    Absolute difference between two timestamps in milliseconds.
    """

    return abs(int(first_timestamp_ms) - int(second_timestamp_ms))


# ---------------------------------------------------------------------
# Time-until helpers
# ---------------------------------------------------------------------


def ms_until(
    timestamp_ms: int | None,
    now_ms: int | None = None,
) -> int:
    """
    Milliseconds until timestamp.

    Returns 0 if timestamp is missing or already in the past.
    """

    if timestamp_ms is None:
        return 0

    now = utc_now_ms() if now_ms is None else int(now_ms)
    return max(0, int(timestamp_ms) - now)


def seconds_until(
    timestamp_ms: int | None,
    now_ms: int | None = None,
) -> Decimal:
    """
    Decimal seconds until timestamp.
    """

    return Decimal(ms_until(timestamp_ms, now_ms)) / MS_PER_SECOND_DECIMAL


def minutes_until(
    timestamp_ms: int | None,
    now_ms: int | None = None,
) -> Decimal:
    """
    Decimal minutes until timestamp.
    """

    return Decimal(ms_until(timestamp_ms, now_ms)) / MS_PER_MINUTE_DECIMAL


def hours_until(
    timestamp_ms: int | None,
    now_ms: int | None = None,
) -> Decimal:
    """
    Decimal hours until timestamp.
    """

    return Decimal(ms_until(timestamp_ms, now_ms)) / MS_PER_HOUR_DECIMAL


def hours_since(
    timestamp_ms: int | None,
    now_ms: int | None = None,
) -> Decimal:
    """
    Decimal hours since timestamp.

    Returns 0 if timestamp is missing or in the future.
    """

    if timestamp_ms is None:
        return ZERO

    age = max(0, age_ms(timestamp_ms, now_ms))
    return Decimal(age) / MS_PER_HOUR_DECIMAL
