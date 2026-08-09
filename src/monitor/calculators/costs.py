from __future__ import annotations

from decimal import Decimal

from monitor.domain import CostMetrics
from monitor.utils import (
    HOURS_PER_YEAR,
    TWO,
    ZERO,
    DecimalLike,
    to_decimal,
    utc_now_ms,
)

# ---------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------


def _require_non_negative(
    value: DecimalLike,
    name: str,
) -> Decimal:
    decimal_value = to_decimal(value)

    if decimal_value < ZERO:
        raise ValueError(f"{name} must be >= 0")

    return decimal_value


def _require_positive(
    value: DecimalLike,
    name: str,
) -> Decimal:
    decimal_value = to_decimal(value)

    if decimal_value <= ZERO:
        raise ValueError(f"{name} must be > 0")

    return decimal_value


# ---------------------------------------------------------------------
# Effective horizon helpers
# ---------------------------------------------------------------------


def calc_effective_holding_hours(
    holding_hours: DecimalLike,
    min_cost_amortization_hours: DecimalLike,
) -> Decimal:
    """
    Effective holding horizon.

    This protects against artificial annualization over extremely short
    horizons.

        effective_holding_hours = max(
            holding_hours,
            min_cost_amortization_hours,
        )
    """

    holding_decimal = _require_positive(
        holding_hours,
        "holding_hours",
    )
    min_amortization_decimal = _require_positive(
        min_cost_amortization_hours,
        "min_cost_amortization_hours",
    )

    return max(holding_decimal, min_amortization_decimal)


def calc_effective_cost_amortization_hours(
    holding_hours: DecimalLike,
    cost_amortization_hours: DecimalLike,
    min_cost_amortization_hours: DecimalLike,
) -> Decimal:
    """
    Effective cost amortization horizon.

    Conservative behavior:
    - cannot be less than min_cost_amortization_hours;
    - cannot be greater than effective holding horizon.

    If cost_amortization_hours is greater than effective holding horizon,
    it is capped by effective holding horizon to avoid underestimating
    annualized costs.
    """

    effective_holding_decimal = calc_effective_holding_hours(
        holding_hours=holding_hours,
        min_cost_amortization_hours=min_cost_amortization_hours,
    )

    cost_amortization_decimal = _require_positive(
        cost_amortization_hours,
        "cost_amortization_hours",
    )

    min_amortization_decimal = _require_positive(
        min_cost_amortization_hours,
        "min_cost_amortization_hours",
    )

    if cost_amortization_decimal < min_amortization_decimal:
        return min_amortization_decimal

    if cost_amortization_decimal > effective_holding_decimal:
        return effective_holding_decimal

    return cost_amortization_decimal


# ---------------------------------------------------------------------
# Fee / slippage / spread helpers
# ---------------------------------------------------------------------


def calc_round_trip_fees(
    spot_fee: DecimalLike,
    perp_fee: DecimalLike,
) -> Decimal:
    """
    Round-trip trading fees.

    Entry:
    - buy spot
    - sell perp

    Exit:
    - sell spot
    - buy perp

        fee_round_trip = 2 * spot_fee + 2 * perp_fee

    spot_fee and perp_fee are decimal fractions per trade.
    Example: 0.001 = 0.10%
    """

    spot_fee_decimal = _require_non_negative(spot_fee, "spot_fee")
    perp_fee_decimal = _require_non_negative(perp_fee, "perp_fee")

    return TWO * spot_fee_decimal + TWO * perp_fee_decimal


def calc_slippage_round_trip(
    slippage_entry: DecimalLike,
    slippage_exit: DecimalLike,
) -> Decimal:
    """
    Round-trip slippage buffer.

        slippage_round_trip = slippage_entry + slippage_exit
    """

    slippage_entry_decimal = _require_non_negative(
        slippage_entry,
        "slippage_entry",
    )
    slippage_exit_decimal = _require_non_negative(
        slippage_exit,
        "slippage_exit",
    )

    return slippage_entry_decimal + slippage_exit_decimal


def calc_one_time_costs(
    spot_fee: DecimalLike,
    perp_fee: DecimalLike,
    slippage_entry: DecimalLike,
    slippage_exit: DecimalLike,
    spread_buffer: DecimalLike,
) -> Decimal:
    """
    Total one-time entry/exit costs.

        one_time_costs =
            2 * spot_fee
          + 2 * perp_fee
          + slippage_entry
          + slippage_exit
          + spread_buffer
    """

    fees_round_trip = calc_round_trip_fees(
        spot_fee=spot_fee,
        perp_fee=perp_fee,
    )

    slippage_round_trip_decimal = calc_slippage_round_trip(
        slippage_entry=slippage_entry,
        slippage_exit=slippage_exit,
    )

    spread_buffer_decimal = _require_non_negative(
        spread_buffer,
        "spread_buffer",
    )

    return fees_round_trip + slippage_round_trip_decimal + spread_buffer_decimal


# ---------------------------------------------------------------------
# Horizon cost helpers
# ---------------------------------------------------------------------


def calc_borrow_cost_horizon(
    borrow_rate_annual: DecimalLike,
    holding_hours: DecimalLike,
) -> Decimal:
    """
    Borrow cost over holding horizon.

        borrow_cost_horizon =
            borrow_rate_annual * holding_hours / 8760
    """

    borrow_rate_decimal = _require_non_negative(
        borrow_rate_annual,
        "borrow_rate_annual",
    )
    holding_decimal = _require_positive(
        holding_hours,
        "holding_hours",
    )

    return borrow_rate_decimal * holding_decimal / HOURS_PER_YEAR


def calc_opportunity_cost_horizon(
    opportunity_cost_annual: DecimalLike,
    holding_hours: DecimalLike,
) -> Decimal:
    """
    Opportunity cost of own capital over holding horizon.

        opportunity_cost_horizon =
            opportunity_cost_annual * holding_hours / 8760
    """

    opportunity_rate_decimal = _require_non_negative(
        opportunity_cost_annual,
        "opportunity_cost_annual",
    )
    holding_decimal = _require_positive(
        holding_hours,
        "holding_hours",
    )

    return opportunity_rate_decimal * holding_decimal / HOURS_PER_YEAR


def calc_total_costs_horizon(
    one_time_costs: DecimalLike,
    borrow_cost_horizon: DecimalLike,
    opportunity_cost_horizon: DecimalLike,
) -> Decimal:
    """
    Total costs over holding horizon.

        total_costs_horizon =
            one_time_costs
          + borrow_cost_horizon
          + opportunity_cost_horizon
    """

    one_time_costs_decimal = _require_non_negative(
        one_time_costs,
        "one_time_costs",
    )
    borrow_cost_decimal = _require_non_negative(
        borrow_cost_horizon,
        "borrow_cost_horizon",
    )
    opportunity_cost_decimal = _require_non_negative(
        opportunity_cost_horizon,
        "opportunity_cost_horizon",
    )

    return one_time_costs_decimal + borrow_cost_decimal + opportunity_cost_decimal


# ---------------------------------------------------------------------
# Annualized cost helpers
# ---------------------------------------------------------------------


def calc_one_time_costs_annualized(
    one_time_costs: DecimalLike,
    cost_amortization_hours: DecimalLike,
) -> Decimal:
    """
    Annualized one-time costs.

        one_time_costs_annualized =
            one_time_costs * 8760 / cost_amortization_hours
    """

    one_time_costs_decimal = _require_non_negative(
        one_time_costs,
        "one_time_costs",
    )
    amortization_decimal = _require_positive(
        cost_amortization_hours,
        "cost_amortization_hours",
    )

    return one_time_costs_decimal * HOURS_PER_YEAR / amortization_decimal


def calc_total_costs_annualized(
    one_time_costs_annualized: DecimalLike,
    borrow_rate_annual: DecimalLike,
    opportunity_cost_annual: DecimalLike,
) -> Decimal:
    """
    Total annualized costs.

        total_costs_annualized =
            one_time_costs_annualized
          + borrow_rate_annual
          + opportunity_cost_annual
    """

    one_time_costs_annualized_decimal = _require_non_negative(
        one_time_costs_annualized,
        "one_time_costs_annualized",
    )
    borrow_rate_decimal = _require_non_negative(
        borrow_rate_annual,
        "borrow_rate_annual",
    )
    opportunity_rate_decimal = _require_non_negative(
        opportunity_cost_annual,
        "opportunity_cost_annual",
    )

    return (
        one_time_costs_annualized_decimal
        + borrow_rate_decimal
        + opportunity_rate_decimal
    )


# ---------------------------------------------------------------------
# Cost metrics builder
# ---------------------------------------------------------------------


def calc_cost_metrics(
    *,
    spot_fee: DecimalLike,
    perp_fee: DecimalLike,
    slippage_entry: DecimalLike,
    slippage_exit: DecimalLike,
    spread_buffer: DecimalLike,
    borrow_rate_annual: DecimalLike,
    opportunity_cost_annual: DecimalLike,
    holding_hours: DecimalLike,
    cost_amortization_hours: DecimalLike,
    calculated_at_ms: int | None = None,
) -> CostMetrics:
    """
    Build full cost metrics.

    Expected input units:
    - spot_fee: decimal fraction per trade, e.g. 0.001 = 0.10%
    - perp_fee: decimal fraction per trade
    - slippage_entry: decimal fraction
    - slippage_exit: decimal fraction
    - spread_buffer: decimal fraction
    - borrow_rate_annual: decimal annual rate
    - opportunity_cost_annual: decimal annual rate
    - holding_hours: effective holding horizon
    - cost_amortization_hours: effective cost amortization horizon
    """

    calculated_at = (
        utc_now_ms()
        if calculated_at_ms is None
        else int(calculated_at_ms)
    )

    spot_fee_decimal = _require_non_negative(spot_fee, "spot_fee")
    perp_fee_decimal = _require_non_negative(perp_fee, "perp_fee")

    slippage_entry_decimal = _require_non_negative(
        slippage_entry,
        "slippage_entry",
    )
    slippage_exit_decimal = _require_non_negative(
        slippage_exit,
        "slippage_exit",
    )
    spread_buffer_decimal = _require_non_negative(
        spread_buffer,
        "spread_buffer",
    )

    borrow_rate_decimal = _require_non_negative(
        borrow_rate_annual,
        "borrow_rate_annual",
    )
    opportunity_rate_decimal = _require_non_negative(
        opportunity_cost_annual,
        "opportunity_cost_annual",
    )

    holding_decimal = _require_positive(
        holding_hours,
        "holding_hours",
    )
    amortization_decimal = _require_positive(
        cost_amortization_hours,
        "cost_amortization_hours",
    )

    spot_fee_round_trip = TWO * spot_fee_decimal
    perp_fee_round_trip = TWO * perp_fee_decimal

    slippage_round_trip_decimal = (
        slippage_entry_decimal + slippage_exit_decimal
    )

    one_time_costs_decimal = (
        spot_fee_round_trip
        + perp_fee_round_trip
        + slippage_round_trip_decimal
        + spread_buffer_decimal
    )

    borrow_cost_horizon_decimal = (
        borrow_rate_decimal * holding_decimal / HOURS_PER_YEAR
    )

    opportunity_cost_horizon_decimal = (
        opportunity_rate_decimal * holding_decimal / HOURS_PER_YEAR
    )

    total_costs_horizon_decimal = (
        one_time_costs_decimal
        + borrow_cost_horizon_decimal
        + opportunity_cost_horizon_decimal
    )

    one_time_costs_annualized_decimal = (
        one_time_costs_decimal
        * HOURS_PER_YEAR
        / amortization_decimal
    )

    total_costs_annualized_decimal = (
        one_time_costs_annualized_decimal
        + borrow_rate_decimal
        + opportunity_rate_decimal
    )

    return CostMetrics(
        spot_fee_round_trip=spot_fee_round_trip,
        perp_fee_round_trip=perp_fee_round_trip,
        slippage_round_trip=slippage_round_trip_decimal,
        spread_buffer=spread_buffer_decimal,
        one_time_costs=one_time_costs_decimal,
        borrow_cost_horizon=borrow_cost_horizon_decimal,
        opportunity_cost_horizon=opportunity_cost_horizon_decimal,
        total_costs_horizon=total_costs_horizon_decimal,
        one_time_costs_annualized=one_time_costs_annualized_decimal,
        total_costs_annualized=total_costs_annualized_decimal,
        cost_amortization_hours=amortization_decimal,
        calculated_at_ms=calculated_at,
    )
