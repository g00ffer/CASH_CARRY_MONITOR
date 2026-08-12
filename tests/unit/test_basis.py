"""
Tests for monitor.calculators.basis
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from monitor.calculators.basis import (
    calc_basis_entry,
    calc_basis_mid,
    calc_basis_metrics,
    calc_mid_price,
    calc_spread_ratio,
    select_expected_exit_basis,
)
from monitor.domain import MarketSnapshot, PerpTicker, SpotTicker
from monitor.domain.enums import ExpectedExitBasisMode


# ---------------------------------------------------------------------
# calc_mid_price
# ---------------------------------------------------------------------
class TestCalcMidPrice:
    def test_normal(self):
        result = calc_mid_price(Decimal("100"), Decimal("102"))
        assert result == Decimal("101")

    def test_zero_bid_returns_zero(self):
        result = calc_mid_price(Decimal("0"), Decimal("100"))
        assert result == Decimal("0")

    def test_negative_ask_returns_zero(self):
        result = calc_mid_price(Decimal("100"), Decimal("-1"))
        assert result == Decimal("0")

    def test_equal_prices(self):
        result = calc_mid_price(Decimal("50"), Decimal("50"))
        assert result == Decimal("50")


# ---------------------------------------------------------------------
# calc_spread_ratio
# ---------------------------------------------------------------------
class TestCalcSpreadRatio:
    def test_normal(self):
        # spread = ask / bid - 1 = 100.10 / 100 - 1 = 0.001
        result = calc_spread_ratio(Decimal("100"), Decimal("100.10"))
        assert result == Decimal("0.001")

    def test_zero_bid_returns_zero(self):
        result = calc_spread_ratio(Decimal("0"), Decimal("100"))
        assert result == Decimal("0")

    def test_equal_prices_returns_zero(self):
        result = calc_spread_ratio(Decimal("100"), Decimal("100"))
        assert result == Decimal("0")

    def test_wide_spread(self):
        # spread = 101 / 100 - 1 = 0.01 = 1%
        result = calc_spread_ratio(Decimal("100"), Decimal("101"))
        assert result == Decimal("0.01")


# ---------------------------------------------------------------------
# calc_basis_mid
# ---------------------------------------------------------------------
class TestCalcBasisMid:
    def test_positive_basis(self):
        # perp_mid = 65031, spot_mid = 65000.5
        # basis_mid = 65031 / 65000.5 - 1 ≈ 0.000469
        result = calc_basis_mid(Decimal("65000.5"), Decimal("65031"))
        assert result > Decimal("0")
        assert result < Decimal("0.01")

    def test_zero_spot_returns_zero(self):
        result = calc_basis_mid(Decimal("0"), Decimal("65000"))
        assert result == Decimal("0")

    def test_equal_prices_returns_zero(self):
        result = calc_basis_mid(Decimal("65000"), Decimal("65000"))
        assert result == Decimal("0")

    def test_negative_basis_backwardation(self):
        # perp < spot -> backwardation
        result = calc_basis_mid(Decimal("65000"), Decimal("64900"))
        assert result < Decimal("0")


# ---------------------------------------------------------------------
# calc_basis_entry
# ---------------------------------------------------------------------
class TestCalcBasisEntry:
    def test_positive_entry_basis(self):
        # basis_entry = perp_bid / spot_ask - 1
        # = 65030 / 65001 - 1 ≈ 0.000446
        result = calc_basis_entry(Decimal("65001"), Decimal("65030"))
        assert result > Decimal("0")
        assert result < Decimal("0.01")

    def test_negative_entry_basis(self):
        # perp_bid < spot_ask -> backwardation
        result = calc_basis_entry(Decimal("65050"), Decimal("65000"))
        assert result < Decimal("0")

    def test_zero_spot_ask_returns_zero(self):
        result = calc_basis_entry(Decimal("0"), Decimal("65000"))
        assert result == Decimal("0")

    def test_zero_perp_bid_returns_zero(self):
        result = calc_basis_entry(Decimal("65000"), Decimal("0"))
        assert result == Decimal("0")


# ---------------------------------------------------------------------
# select_expected_exit_basis
# ---------------------------------------------------------------------
class TestSelectExpectedExitBasis:
    def test_entry_mode(self):
        result = select_expected_exit_basis(
            mode=ExpectedExitBasisMode.ENTRY,
            basis_entry=Decimal("0.0004"),
        )
        assert result == Decimal("0.0004")

    def test_zero_mode(self):
        result = select_expected_exit_basis(
            mode=ExpectedExitBasisMode.ZERO,
            basis_entry=Decimal("0.0004"),
        )
        assert result == Decimal("0")

    def test_historical_median_with_value(self):
        result = select_expected_exit_basis(
            mode=ExpectedExitBasisMode.HISTORICAL_MEDIAN,
            basis_entry=Decimal("0.0004"),
            historical_median_basis=Decimal("0.0002"),
        )
        assert result == Decimal("0.0002")

    def test_historical_median_fallback_to_entry(self):
        result = select_expected_exit_basis(
            mode=ExpectedExitBasisMode.HISTORICAL_MEDIAN,
            basis_entry=Decimal("0.0004"),
            historical_median_basis=None,
        )
        assert result == Decimal("0.0004")


# ---------------------------------------------------------------------
# calc_basis_metrics (integration)
# ---------------------------------------------------------------------
class TestCalcBasisMetrics:
    def test_metrics_structure(self, market_snapshot: MarketSnapshot):
        result = calc_basis_metrics(
            market_snapshot,
            calculated_at_ms=1710000000300,
        )
        assert result.basis_mid > Decimal("0")
        assert result.basis_entry > Decimal("0")
        assert result.spot_mid == Decimal("65000.5")
        assert result.perp_mid == Decimal("65031")
        assert result.spot_spread > Decimal("0")
        assert result.perp_spread > Decimal("0")
        assert result.calculated_at_ms == 1710000000300

    def test_entry_basis_less_than_mid(self, market_snapshot: MarketSnapshot):
        """
        Entry basis uses worse prices (perp_bid / spot_ask),
        so it should be less than mid basis (perp_mid / spot_mid).
        """
        result = calc_basis_metrics(market_snapshot)
        assert result.basis_entry < result.basis_mid

    def test_backwardation_produces_negative_basis(self):
        """When perp < spot, basis should be negative."""
        spot = SpotTicker(
            symbol="BTC/USDT",
            bid=Decimal("65000"),
            ask=Decimal("65001"),
            timestamp_ms=1710000000000,
        )
        perp = PerpTicker(
            symbol="BTC/USDT:USDT",
            bid=Decimal("64900"),
            ask=Decimal("64902"),
            timestamp_ms=1710000000200,
        )
        snapshot = MarketSnapshot(
            cycle_id="test",
            symbol_name="BTC_CARRY",
            spot=spot,
            perp=perp,
            received_at_ms=1710000000300,
        )
        result = calc_basis_metrics(snapshot)
        assert result.basis_mid < Decimal("0")
        assert result.basis_entry < Decimal("0")