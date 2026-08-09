from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, getcontext
from typing import Union

DecimalLike = Union[Decimal, int, float, str]


# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------

ZERO = Decimal("0")
ONE = Decimal("1")
TWO = Decimal("2")
THREE = Decimal("3")
FOUR = Decimal("4")

HUNDRED = Decimal("100")
THOUSAND = Decimal("1000")
TEN_THOUSAND = Decimal("10000")

HOURS_PER_YEAR = Decimal("8760")

MS_PER_SECOND_DECIMAL = Decimal("1000")
MS_PER_MINUTE_DECIMAL = Decimal("60000")
MS_PER_HOUR_DECIMAL = Decimal("3600000")


# ---------------------------------------------------------------------
# Decimal context
# ---------------------------------------------------------------------

getcontext().prec = 28
getcontext().rounding = ROUND_HALF_UP


def set_decimal_context(precision: int = 28) -> None:
    """
    Configure global decimal context.

    This is useful for tests or for overriding precision at bootstrap time.
    """

    getcontext().prec = precision
    getcontext().rounding = ROUND_HALF_UP


# ---------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------


def to_decimal(value: DecimalLike) -> Decimal:
    """
    Convert int / float / str / Decimal into Decimal.

    Float values are converted through str() to avoid binary float
    representation issues.
    """

    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Decimal value must be finite")
        return value

    if isinstance(value, bool):
        raise TypeError("bool cannot be converted to Decimal")

    if isinstance(value, (int, float, str)):
        text = str(value).strip()

        if not text:
            raise ValueError("cannot convert empty string to Decimal")

        try:
            decimal_value = Decimal(text)
        except Exception as exc:
            raise ValueError(f"cannot convert {value!r} to Decimal") from exc

        if not decimal_value.is_finite():
            raise ValueError(f"cannot convert non-finite value {value!r} to Decimal")

        return decimal_value

    raise TypeError(f"unsupported type for Decimal conversion: {type(value).__name__}")


def to_decimal_or_default(
    value: DecimalLike | None,
    default: DecimalLike = ZERO,
) -> Decimal:
    """
    Convert value to Decimal, returning default on None or conversion error.
    """

    if value is None:
        return to_decimal(default)

    try:
        return to_decimal(value)
    except (TypeError, ValueError):
        return to_decimal(default)


def pct_to_decimal(value: DecimalLike) -> Decimal:
    """
    Convert percent value into decimal fraction.

    Example:
    0.10 pct -> 0.0010
    8.0 pct  -> 0.08
    """

    return to_decimal(value) / HUNDRED


def bps_to_decimal(value: DecimalLike) -> Decimal:
    """
    Convert basis points into decimal fraction.

    Example:
    1 bp  -> 0.0001
    2 bps -> 0.0002
    """

    return to_decimal(value) / TEN_THOUSAND


def decimal_to_pct(value: DecimalLike) -> Decimal:
    """
    Convert decimal fraction into percent.

    Example:
    0.0010 -> 0.10
    0.08   -> 8.0
    """

    return to_decimal(value) * HUNDRED


def decimal_to_bps(value: DecimalLike) -> Decimal:
    """
    Convert decimal fraction into basis points.

    Example:
    0.0001 -> 1
    0.0002 -> 2
    """

    return to_decimal(value) * TEN_THOUSAND


# ---------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------


def safe_div(
    numerator: DecimalLike,
    denominator: DecimalLike,
    default: DecimalLike = ZERO,
) -> Decimal:
    """
    Divide numerator by denominator.

    If denominator is zero, return default instead of raising ZeroDivisionError.
    """

    numerator_decimal = to_decimal(numerator)
    denominator_decimal = to_decimal(denominator)

    if denominator_decimal == ZERO:
        return to_decimal(default)

    return numerator_decimal / denominator_decimal


def annualize_value(
    value: DecimalLike,
    hours: DecimalLike,
) -> Decimal:
    """
    Annualize a horizon value.

    Example:
    net_horizon = 0.003
    hours = 168

    annualized = 0.003 * 8760 / 168
    """

    hours_decimal = to_decimal(hours)

    if hours_decimal <= ZERO:
        return ZERO

    return safe_div(
        to_decimal(value) * HOURS_PER_YEAR,
        hours_decimal,
        default=ZERO,
    )


def pro_rata_factor(
    holding_hours: DecimalLike,
    interval_hours: DecimalLike,
) -> Decimal:
    """
    Calculate pro-rata number of intervals.

    Example:
    holding_hours = 168
    interval_hours = 8

    factor = 21
    """

    return safe_div(
        to_decimal(holding_hours),
        to_decimal(interval_hours),
        default=ZERO,
    )


# ---------------------------------------------------------------------
# Rounding / formatting
# ---------------------------------------------------------------------


def round_decimal(
    value: DecimalLike,
    places: int = 12,
    rounding: str = ROUND_HALF_UP,
) -> Decimal:
    """
    Round Decimal to fixed number of decimal places.
    """

    if places < 0:
        raise ValueError("places cannot be negative")

    decimal_value = to_decimal(value)

    if not decimal_value.is_finite():
        raise ValueError("cannot round non-finite Decimal")

    exponent = Decimal(1).scaleb(-places)
    return decimal_value.quantize(exponent, rounding=rounding)


def format_decimal(
    value: DecimalLike,
    places: int = 8,
) -> str:
    """
    Format Decimal as plain fixed-point string.
    """

    rounded = round_decimal(value, places=places)
    return format(rounded, "f")
