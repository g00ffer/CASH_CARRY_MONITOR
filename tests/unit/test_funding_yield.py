"""
Tests for monitor.calculators.funding_yield
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from monitor.calculators.funding_yield import (
    calc_funding_annual,
    calc_funding_horizon,
    calc_funding_yield_metrics,
    calc_periods_per_year,
    calc_pro_rata_funding_events,
)
from monitor.domain import FundingSnapshot
from monitor.domain.enums import FundingRateSource


# ---------------------------------------------------------------------
# calc_periods_per_year
# ---------------------------------------------------------------------
class TestCalcPeriodsPerYear:
    def test_8h_interval(self):
        # 8760 / 8 = 1095
        result = calc_periods_per_year(Decimal("8"))
        assert result == Decimal("1095")

    def test_1h_interval(self):
        result = calc_periods_per_year(Decimal("1"))
        assert result == Decimal("8760")

    def test_4h_interval(self):
        result = calc_periods_per_year(Decimal("4"))
        assert result == Decimal("2190")

    def test_zero_interval_returns_zero(self):
        result = calc_periods_per_year(Decimal("0"))
        assert result == Decimal("0")

    def test_negative_interval_returns_zero(self):
        result = calc_periods_per_year(Decimal("-8"))
        assert result == Decimal("0")


# ---------------------------------------------------------------------
# calc_funding_annual
# ---------------------------------------------------------------------
class TestCalcFundingAnnual:
    def test_positive_rate_8h(self):
        # 0.0001 * (8760 / 8) = 0.0001 * 1095 = 0.1095 = 10.95%
        result = calc_funding_annual(Decimal("0.0001"), Decimal("8"))
        assert result == Decimal("0.1095")

    def test_negative_rate(self):
        result = calc_funding_annual(Decimal("-0.0001"), Decimal("8"))
        assert result == Decimal("-0.1095")

    def test_zero_rate(self):
        result = calc_funding_annual(Decimal("0"), Decimal("8"))
        assert result == Decimal("0")

    def test_typical_btc_funding(self):
        """
        Typical BTC funding: 0.01% per 8h = 0.0001
        Annual: 0.0001 * 1095 = 10.95%
        """
        result = calc_funding_annual(Decimal("0.0001"), Decimal("8"))
        assert result == Decimal("0.1095")


# ---------------------------------------------------------------------
# calc_pro_rata_funding_events
# ---------------------------------------------------------------------
class TestCalcProRataFundingEvents:
    def test_exact_multiple(self):
        # 168h / 8h = 21 events
        result = calc_pro_rata_funding_events(
            holding_hours=Decimal("168"),
            funding_interval_hours=Decimal("8"),
        )
        assert result == Decimal("21")

    def test_fractional(self):
        # 12h / 8h = 1.5 events
        result = calc_pro_rata_funding_events(
            holding_hours=Decimal("12"),
            funding_interval_hours=Decimal("8"),
        )
        assert result == Decimal("1.5")

    def test_zero_holding_returns_zero(self):
        result = calc_pro_rata_funding_events(
            holding_hours=Decimal("0"),
            funding_interval_hours=Decimal("8"),
        )
        assert result == Decimal("0")

    def test_720h_holding(self):
        # 720h / 8h = 90 events
        result = calc_pro_rata_funding_events(
            holding_hours=Decimal("720"),
            funding_interval_hours=Decimal("8"),
        )
        assert result == Decimal("90")


# ---------------------------------------------------------------------
# calc_funding_horizon
# ---------------------------------------------------------------------
class TestCalcFundingHorizon:
    def test_basic(self):
        # rate = 0.0001, holding = 168h, interval = 8h
        # events = 21, horizon = 0.0001 * 21 = 0.0021
        result = calc_funding_horizon(
            funding_rate=Decimal("0.0001"),
            holding_hours=Decimal("168"),
            funding_interval_hours=Decimal("8"),
        )
        assert result == Decimal("0.0021")

    def test_short_holding(self):
        # rate = 0.0001, holding = 4h, interval = 8h
        # events = 0.5, horizon = 0.00005
        result = calc_funding_horizon(
            funding_rate=Decimal("0.0001"),
            holding_hours=Decimal("4"),
            funding_interval_hours=Decimal("8"),
        )
        assert result == Decimal("0.00005")

    def test_zero_holding(self):
        result = calc_funding_horizon(
            funding_rate=Decimal("0.0001"),
            holding_hours=Decimal("0"),
            funding_interval_hours=Decimal("8"),
        )
        assert result == Decimal("0")


# ---------------------------------------------------------------------
# calc_funding_yield_metrics (integration)
# ---------------------------------------------------------------------
class TestCalcFundingYieldMetrics:
    def test_metrics_structure(self, funding_snapshot: FundingSnapshot):
        result = calc_funding_yield_metrics(
            funding_snapshot=funding_snapshot,
            holding_hours=Decimal("168"),
            calculated_at_ms=1710000000500,
        )
        assert result.funding_rate_per_interval == Decimal("0.0001")
        assert result.funding_interval_hours == Decimal("8")
        assert result.periods_per_year == Decimal("1095")
        assert result.funding_annual == Decimal("0.1095")
        assert result.holding_hours == Decimal("168")
        assert result.pro_rata_funding_events == Decimal("21")
        assert result.funding_horizon == Decimal("0.0021")

    def test_negative_holding_raises(self, funding_snapshot: FundingSnapshot):
        with pytest.raises(ValueError, match="holding_hours must be >= 0"):
            calc_funding_yield_metrics(
                funding_snapshot=funding_snapshot,
                holding_hours=Decimal("-1"),
            )

    def test_720h_holding(self, funding_snapshot: FundingSnapshot):
        result = calc_funding_yield_metrics(
            funding_snapshot=funding_snapshot,
            holding_hours=Decimal("720"),
        )
        assert result.pro_rata_funding_events == Decimal("90")
        assert result.funding_horizon == Decimal("0.009")