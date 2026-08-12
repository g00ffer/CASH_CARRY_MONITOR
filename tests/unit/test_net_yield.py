"""
Tests for monitor.calculators.net_yield
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from monitor.calculators.basis import calc_basis_metrics
from monitor.calculators.costs import calc_cost_metrics
from monitor.calculators.funding_yield import calc_funding_yield_metrics
from monitor.calculators.net_yield import (
    calc_basis_convergence_pnl,
    calc_gross_horizon,
    calc_net_annual_from_horizon,
    calc_net_horizon,
    calc_net_yield_metrics,
)
from monitor.domain import (
    FundingSnapshot,
    MarketSnapshot,
    PerpTicker,
    SpotTicker,
)
from monitor.domain.enums import (
    ExpectedExitBasisMode,
    FundingRateSource,
    YieldBase,
)


# ---------------------------------------------------------------------
# calc_basis_convergence_pnl
# ---------------------------------------------------------------------
class TestCalcBasisConvergencePnl:
    def test_disabled_returns_zero(self):
        result = calc_basis_convergence_pnl(
            include_basis_convergence=False,
            basis_haircut=Decimal("0.5"),
            basis_entry=Decimal("0.001"),
            expected_exit_basis_mode=ExpectedExitBasisMode.ENTRY,
        )
        assert result == Decimal("0")

    def test_zero_haircut_returns_zero(self):
        result = calc_basis_convergence_pnl(
            include_basis_convergence=True,
            basis_haircut=Decimal("0"),
            basis_entry=Decimal("0.001"),
            expected_exit_basis_mode=ExpectedExitBasisMode.ENTRY,
        )
        assert result == Decimal("0")

    def test_entry_mode_returns_zero(self):
        # ENTRY: expected_exit = basis_entry, pnl = haircut * (entry - entry) = 0
        result = calc_basis_convergence_pnl(
            include_basis_convergence=True,
            basis_haircut=Decimal("0.5"),
            basis_entry=Decimal("0.001"),
            expected_exit_basis_mode=ExpectedExitBasisMode.ENTRY,
        )
        assert result == Decimal("0")

    def test_zero_mode_positive_pnl(self):
        # ZERO: expected_exit = 0, pnl = 0.5 * (0.001 - 0) = 0.0005
        result = calc_basis_convergence_pnl(
            include_basis_convergence=True,
            basis_haircut=Decimal("0.5"),
            basis_entry=Decimal("0.001"),
            expected_exit_basis_mode=ExpectedExitBasisMode.ZERO,
        )
        assert result == Decimal("0.0005")

    def test_haircut_above_one_rejected(self):
        with pytest.raises(ValueError, match="basis_haircut must be <= 1"):
            calc_basis_convergence_pnl(
                include_basis_convergence=True,
                basis_haircut=Decimal("1.5"),
                basis_entry=Decimal("0.001"),
                expected_exit_basis_mode=ExpectedExitBasisMode.ZERO,
            )


# ---------------------------------------------------------------------
# calc_gross_horizon
# ---------------------------------------------------------------------
class TestCalcGrossHorizon:
    def test_without_convergence(self):
        result = calc_gross_horizon(
            funding_horizon=Decimal("0.0021"),
            basis_convergence_pnl=Decimal("0"),
        )
        assert result == Decimal("0.0021")

    def test_with_convergence(self):
        result = calc_gross_horizon(
            funding_horizon=Decimal("0.0021"),
            basis_convergence_pnl=Decimal("0.0005"),
        )
        assert result == Decimal("0.0026")


# ---------------------------------------------------------------------
# calc_net_horizon
# ---------------------------------------------------------------------
class TestCalcNetHorizon:
    def test_basic(self):
        result = calc_net_horizon(
            gross_horizon=Decimal("0.0021"),
            total_costs_horizon=Decimal("0.0006"),
        )
        assert result == Decimal("0.0015")

    def test_costs_exceed_returns(self):
        result = calc_net_horizon(
            gross_horizon=Decimal("0.001"),
            total_costs_horizon=Decimal("0.003"),
        )
        assert result == Decimal("-0.002")


# ---------------------------------------------------------------------
# calc_net_annual_from_horizon
# ---------------------------------------------------------------------
class TestCalcNetAnnualFromHorizon:
    def test_basic(self):
        # 0.0015 * 8760 / 168 = 0.0782...
        result = calc_net_annual_from_horizon(
            net_horizon=Decimal("0.0015"),
            annualization_hours=Decimal("168"),
        )
        expected = Decimal("0.0015") * Decimal("8760") / Decimal("168")
        assert result == expected


# ---------------------------------------------------------------------
# calc_net_yield_metrics (full integration)
# ---------------------------------------------------------------------
class TestCalcNetYieldMetrics:
    def _make_inputs(self):
        funding_snapshot = FundingSnapshot(
            cycle_id="test",
            symbol_name="BTC_CARRY",
            effective_funding_rate=Decimal("0.0001"),
            funding_rate_source=FundingRateSource.PREDICTED,
            funding_interval_hours=Decimal("8"),
            received_at_ms=1710000000300,
        )
        funding_yield = calc_funding_yield_metrics(
            funding_snapshot=funding_snapshot,
            holding_hours=Decimal("168"),
        )
        cost_metrics = calc_cost_metrics(
            spot_fee=Decimal("0.001"),
            perp_fee=Decimal("0.0005"),
            slippage_entry=Decimal("0.0002"),
            slippage_exit=Decimal("0.0002"),
            spread_buffer=Decimal("0.0002"),
            borrow_rate_annual=Decimal("0"),
            opportunity_cost_annual=Decimal("0"),
            holding_hours=Decimal("168"),
            cost_amortization_hours=Decimal("168"),
        )
        spot = SpotTicker(
            symbol="BTC/USDT",
            bid=Decimal("65000"),
            ask=Decimal("65001"),
            timestamp_ms=1710000000000,
        )
        perp = PerpTicker(
            symbol="BTC/USDT:USDT",
            bid=Decimal("65030"),
            ask=Decimal("65032"),
            timestamp_ms=1710000000200,
        )
        market_snapshot = MarketSnapshot(
            cycle_id="test",
            symbol_name="BTC_CARRY",
            spot=spot,
            perp=perp,
            received_at_ms=1710000000300,
        )
        basis_metrics = calc_basis_metrics(market_snapshot)
        return funding_yield, cost_metrics, basis_metrics

    def test_conservative_mode(self):
        funding_yield, cost_metrics, basis_metrics = self._make_inputs()
        result = calc_net_yield_metrics(
            funding_yield_metrics=funding_yield,
            cost_metrics=cost_metrics,
            basis_metrics=basis_metrics,
            include_basis_convergence=False,
            basis_haircut=Decimal("0"),
            expected_exit_basis_mode=ExpectedExitBasisMode.ENTRY,
            yield_base=YieldBase.NOTIONAL,
        )
        # funding_horizon = 0.0021
        # basis_convergence_pnl = 0 (conservative)
        # gross_horizon = 0.0021
        # total_costs_horizon = 0.0036
        # net_horizon = 0.0021 - 0.0036 = -0.0015
        assert result.funding_horizon == Decimal("0.0021")
        assert result.basis_convergence_pnl == Decimal("0")
        assert result.gross_horizon == Decimal("0.0021")
        assert result.total_costs_horizon == Decimal("0.0036")
        assert result.net_horizon == Decimal("-0.0015")
        assert result.include_basis_convergence is False
        assert result.yield_base == YieldBase.NOTIONAL

    def test_net_annual_negative_in_conservative_mode(self):
        """
        net_annual = funding_annual + basis_convergence_annual - total_costs_annualized
        = 0.1095 + 0 - 0.187714... < 0
        """
        funding_yield, cost_metrics, basis_metrics = self._make_inputs()
        result = calc_net_yield_metrics(
            funding_yield_metrics=funding_yield,
            cost_metrics=cost_metrics,
            basis_metrics=basis_metrics,
            include_basis_convergence=False,
            basis_haircut=Decimal("0"),
            expected_exit_basis_mode=ExpectedExitBasisMode.ENTRY,
        )
        assert result.net_annual < Decimal("0")

    def test_none_basis_metrics_convergence_zero(self):
        """If basis_metrics is None, convergence PnL should be 0."""
        funding_yield, cost_metrics, _ = self._make_inputs()
        result = calc_net_yield_metrics(
            funding_yield_metrics=funding_yield,
            cost_metrics=cost_metrics,
            basis_metrics=None,
            include_basis_convergence=True,
            basis_haircut=Decimal("0.5"),
            expected_exit_basis_mode=ExpectedExitBasisMode.ZERO,
        )
        assert result.basis_convergence_pnl == Decimal("0")