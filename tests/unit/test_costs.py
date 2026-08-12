"""
Tests for monitor.calculators.costs
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from monitor.calculators.costs import (
    calc_borrow_cost_horizon,
    calc_cost_metrics,
    calc_effective_cost_amortization_hours,
    calc_effective_holding_hours,
    calc_one_time_costs,
    calc_one_time_costs_annualized,
    calc_opportunity_cost_horizon,
    calc_round_trip_fees,
    calc_slippage_round_trip,
    calc_total_costs_annualized,
    calc_total_costs_horizon,
)


# ---------------------------------------------------------------------
# calc_effective_holding_hours
# ---------------------------------------------------------------------
class TestCalcEffectiveHoldingHours:
    def test_holding_above_minimum(self):
        result = calc_effective_holding_hours(
            holding_hours=Decimal("168"),
            min_cost_amortization_hours=Decimal("24"),
        )
        assert result == Decimal("168")

    def test_holding_below_minimum_capped(self):
        result = calc_effective_holding_hours(
            holding_hours=Decimal("4"),
            min_cost_amortization_hours=Decimal("24"),
        )
        assert result == Decimal("24")

    def test_equal_values(self):
        result = calc_effective_holding_hours(
            holding_hours=Decimal("24"),
            min_cost_amortization_hours=Decimal("24"),
        )
        assert result == Decimal("24")


# ---------------------------------------------------------------------
# calc_effective_cost_amortization_hours
# ---------------------------------------------------------------------
class TestCalcEffectiveCostAmortization:
    def test_normal_case(self):
        result = calc_effective_cost_amortization_hours(
            holding_hours=Decimal("168"),
            cost_amortization_hours=Decimal("168"),
            min_cost_amortization_hours=Decimal("24"),
        )
        assert result == Decimal("168")

    def test_amort_below_minimum_capped(self):
        result = calc_effective_cost_amortization_hours(
            holding_hours=Decimal("168"),
            cost_amortization_hours=Decimal("4"),
            min_cost_amortization_hours=Decimal("24"),
        )
        assert result == Decimal("24")

    def test_amort_above_holding_capped(self):
        result = calc_effective_cost_amortization_hours(
            holding_hours=Decimal("48"),
            cost_amortization_hours=Decimal("168"),
            min_cost_amortization_hours=Decimal("24"),
        )
        assert result == Decimal("48")


# ---------------------------------------------------------------------
# calc_round_trip_fees
# ---------------------------------------------------------------------
class TestCalcRoundTripFees:
    def test_basic(self):
        # 2 * 0.001 + 2 * 0.0005 = 0.003
        result = calc_round_trip_fees(
            spot_fee=Decimal("0.001"),
            perp_fee=Decimal("0.0005"),
        )
        assert result == Decimal("0.003")

    def test_zero_fees(self):
        result = calc_round_trip_fees(
            spot_fee=Decimal("0"),
            perp_fee=Decimal("0"),
        )
        assert result == Decimal("0")


# ---------------------------------------------------------------------
# calc_slippage_round_trip
# ---------------------------------------------------------------------
class TestCalcSlippageRoundTrip:
    def test_basic(self):
        result = calc_slippage_round_trip(
            slippage_entry=Decimal("0.0002"),
            slippage_exit=Decimal("0.0002"),
        )
        assert result == Decimal("0.0004")


# ---------------------------------------------------------------------
# calc_one_time_costs
# ---------------------------------------------------------------------
class TestCalcOneTimeCosts:
    def test_basic(self):
        # 2*0.001 + 2*0.0005 + 0.0002 + 0.0002 + 0.0002 = 0.0036
        result = calc_one_time_costs(
            spot_fee=Decimal("0.001"),
            perp_fee=Decimal("0.0005"),
            slippage_entry=Decimal("0.0002"),
            slippage_exit=Decimal("0.0002"),
            spread_buffer=Decimal("0.0002"),
        )
        assert result == Decimal("0.0036")


# ---------------------------------------------------------------------
# calc_borrow_cost_horizon
# ---------------------------------------------------------------------
class TestCalcBorrowCostHorizon:
    def test_basic(self):
        # 0.05 * 168 / 8760
        result = calc_borrow_cost_horizon(
            borrow_rate_annual=Decimal("0.05"),
            holding_hours=Decimal("168"),
        )
        expected = Decimal("0.05") * Decimal("168") / Decimal("8760")
        assert result == expected

    def test_zero_rate(self):
        result = calc_borrow_cost_horizon(
            borrow_rate_annual=Decimal("0"),
            holding_hours=Decimal("168"),
        )
        assert result == Decimal("0")


# ---------------------------------------------------------------------
# calc_opportunity_cost_horizon
# ---------------------------------------------------------------------
class TestCalcOpportunityCostHorizon:
    def test_basic(self):
        result = calc_opportunity_cost_horizon(
            opportunity_cost_annual=Decimal("0.03"),
            holding_hours=Decimal("168"),
        )
        expected = Decimal("0.03") * Decimal("168") / Decimal("8760")
        assert result == expected


# ---------------------------------------------------------------------
# calc_total_costs_horizon
# ---------------------------------------------------------------------
class TestCalcTotalCostsHorizon:
    def test_basic(self):
        one_time = Decimal("0.0036")
        borrow = Decimal("0.000959")
        opportunity = Decimal("0.000575")
        result = calc_total_costs_horizon(
            one_time_costs=one_time,
            borrow_cost_horizon=borrow,
            opportunity_cost_horizon=opportunity,
        )
        assert result == one_time + borrow + opportunity


# ---------------------------------------------------------------------
# calc_one_time_costs_annualized
# ---------------------------------------------------------------------
class TestCalcOneTimeCostsAnnualized:
    def test_basic(self):
        # 0.0036 * 8760 / 168 = 0.187714...
        result = calc_one_time_costs_annualized(
            one_time_costs=Decimal("0.0036"),
            cost_amortization_hours=Decimal("168"),
        )
        expected = Decimal("0.0036") * Decimal("8760") / Decimal("168")
        assert result == expected

    def test_shorter_amortization_inflates_cost(self):
        result_short = calc_one_time_costs_annualized(
            one_time_costs=Decimal("0.0036"),
            cost_amortization_hours=Decimal("24"),
        )
        result_long = calc_one_time_costs_annualized(
            one_time_costs=Decimal("0.0036"),
            cost_amortization_hours=Decimal("168"),
        )
        assert result_short > result_long


# ---------------------------------------------------------------------
# calc_total_costs_annualized
# ---------------------------------------------------------------------
class TestCalcTotalCostsAnnualized:
    def test_basic(self):
        result = calc_total_costs_annualized(
            one_time_costs_annualized=Decimal("0.1877"),
            borrow_rate_annual=Decimal("0.05"),
            opportunity_cost_annual=Decimal("0.03"),
        )
        assert result == Decimal("0.2677")


# ---------------------------------------------------------------------
# calc_cost_metrics (integration)
# ---------------------------------------------------------------------
class TestCalcCostMetrics:
    def test_full_metrics(self):
        result = calc_cost_metrics(
            spot_fee=Decimal("0.001"),
            perp_fee=Decimal("0.0005"),
            slippage_entry=Decimal("0.0002"),
            slippage_exit=Decimal("0.0002"),
            spread_buffer=Decimal("0.0002"),
            borrow_rate_annual=Decimal("0"),
            opportunity_cost_annual=Decimal("0"),
            holding_hours=Decimal("168"),
            cost_amortization_hours=Decimal("168"),
            calculated_at_ms=1710000000500,
        )
        assert result.spot_fee_round_trip == Decimal("0.002")
        assert result.perp_fee_round_trip == Decimal("0.001")
        assert result.slippage_round_trip == Decimal("0.0004")
        assert result.spread_buffer == Decimal("0.0002")
        assert result.one_time_costs == Decimal("0.0036")
        assert result.borrow_cost_horizon == Decimal("0")
        assert result.opportunity_cost_horizon == Decimal("0")
        assert result.total_costs_horizon == Decimal("0.0036")
        assert result.cost_amortization_hours == Decimal("168")
        assert result.calculated_at_ms == 1710000000500

    def test_negative_fee_rejected(self):
        with pytest.raises(ValueError, match="spot_fee must be >= 0"):
            calc_cost_metrics(
                spot_fee=Decimal("-0.001"),
                perp_fee=Decimal("0.0005"),
                slippage_entry=Decimal("0"),
                slippage_exit=Decimal("0"),
                spread_buffer=Decimal("0"),
                borrow_rate_annual=Decimal("0"),
                opportunity_cost_annual=Decimal("0"),
                holding_hours=Decimal("168"),
                cost_amortization_hours=Decimal("168"),
            )