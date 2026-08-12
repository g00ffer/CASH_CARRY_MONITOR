"""Tests for monitor.utils.decimal (remaining branches)"""
from __future__ import annotations

from decimal import Decimal, getcontext

import pytest

from monitor.utils.decimal import (
    HUNDRED,
    TEN_THOUSAND,
    ZERO,
    annualize_value,
    bps_to_decimal,
    decimal_to_bps,
    decimal_to_pct,
    format_decimal,
    pct_to_decimal,
    pro_rata_factor,
    round_decimal,
    safe_div,
    set_decimal_context,
    to_decimal,
    to_decimal_or_default,
)


class TestSetDecimalContext:
    def test_sets_precision(self):
        set_decimal_context(precision=50)
        assert getcontext().prec == 50
        # Restore
        set_decimal_context(precision=28)


class TestToDecimal:
    def test_decimal_passthrough(self):
        assert to_decimal(Decimal("1.5")) == Decimal("1.5")

    def test_int(self):
        assert to_decimal(42) == Decimal("42")

    def test_float_via_str(self):
        result = to_decimal(0.1)
        assert result == Decimal("0.1")

    def test_string(self):
        assert to_decimal("3.14") == Decimal("3.14")

    def test_bool_raises(self):
        with pytest.raises(TypeError, match="bool cannot be converted"):
            to_decimal(True)

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="empty string"):
            to_decimal("")

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError, match="cannot convert"):
            to_decimal("abc")

    def test_infinity_raises(self):
        with pytest.raises(ValueError, match="non-finite"):
            to_decimal(float("inf"))

    def test_nan_raises(self):
        with pytest.raises(ValueError, match="non-finite"):
            to_decimal(float("nan"))

    def test_unsupported_type_raises(self):
        with pytest.raises(TypeError, match="unsupported type"):
            to_decimal([1, 2, 3])


class TestToDecimalOrDefault:
    def test_none_returns_default(self):
        assert to_decimal_or_default(None) == ZERO

    def test_custom_default(self):
        assert to_decimal_or_default(None, default="5") == Decimal("5")

    def test_invalid_returns_default(self):
        assert to_decimal_or_default("abc", default="0") == ZERO

    def test_valid_value(self):
        assert to_decimal_or_default("3.14") == Decimal("3.14")


class TestConversions:
    def test_pct_to_decimal(self):
        assert pct_to_decimal(0.10) == Decimal("0.001")

    def test_bps_to_decimal(self):
        assert bps_to_decimal(2) == Decimal("0.0002")

    def test_decimal_to_pct(self):
        assert decimal_to_pct(Decimal("0.001")) == Decimal("0.10")

    def test_decimal_to_bps(self):
        assert decimal_to_bps(Decimal("0.0002")) == Decimal("2")


class TestSafeDiv:
    def test_normal(self):
        assert safe_div(10, 2) == Decimal("5")

    def test_zero_denominator(self):
        assert safe_div(10, 0) == ZERO

    def test_custom_default(self):
        assert safe_div(10, 0, default="99") == Decimal("99")


class TestAnnualizeValue:
    def test_normal(self):
        result = annualize_value(Decimal("0.003"), 168)
        expected = Decimal("0.003") * Decimal("8760") / Decimal("168")
        assert result == expected

    def test_zero_hours(self):
        assert annualize_value(Decimal("0.003"), 0) == ZERO

    def test_negative_hours(self):
        assert annualize_value(Decimal("0.003"), -1) == ZERO


class TestProRataFactor:
    def test_normal(self):
        assert pro_rata_factor(168, 8) == Decimal("21")

    def test_zero_interval(self):
        assert pro_rata_factor(168, 0) == ZERO


class TestRoundDecimal:
    def test_normal(self):
        result = round_decimal(Decimal("3.14159"), places=2)
        assert result == Decimal("3.14")

    def test_negative_places_raises(self):
        with pytest.raises(ValueError, match="places cannot be negative"):
            round_decimal(Decimal("3.14"), places=-1)

    def test_non_finite_raises(self):
        with pytest.raises(ValueError, match="non-finite"):
            round_decimal(float("inf"), places=2)


class TestFormatDecimal:
    def test_normal(self):
        result = format_decimal(Decimal("3.14159"), places=2)
        assert result == "3.14"

    def test_zero(self):
        assert format_decimal(ZERO, places=2) == "0.00"