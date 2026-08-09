from __future__ import annotations

from decimal import Decimal

from monitor.domain import FundingSnapshot, FundingYieldMetrics
from monitor.utils import (
    HOURS_PER_YEAR,
    ZERO,
    DecimalLike,
    to_decimal,
    utc_now_ms,
)


def calc_periods_per_year(
    funding_interval_hours: DecimalLike,
) -> Decimal:
    """
    Number of funding periods per year.

        periods_per_year = 8760 / funding_interval_hours
    """

    interval_decimal = to_decimal(funding_interval_hours)

    if interval_decimal <= ZERO:
        return ZERO

    return HOURS_PER_YEAR / interval_decimal


def calc_funding_annual(
    funding_rate: DecimalLike,
    funding_interval_hours: DecimalLike,
) -> Decimal:
    """
    Annualized funding yield.

        funding_annual = funding_rate * (8760 / funding_interval_hours)
    """

    funding_rate_decimal = to_decimal(funding_rate)
    periods = calc_periods_per_year(funding_interval_hours)

    return funding_rate_decimal * periods


def calc_pro_rata_funding_events(
    holding_hours: DecimalLike,
    funding_interval_hours: DecimalLike,
) -> Decimal:
    """
    Pro-rata number of funding events over holding horizon.

        n_events = holding_hours / funding_interval_hours
    """

    holding_decimal = to_decimal(holding_hours)
    interval_decimal = to_decimal(funding_interval_hours)

    if holding_decimal <= ZERO or interval_decimal <= ZERO:
        return ZERO

    return holding_decimal / interval_decimal


def calc_funding_horizon(
    funding_rate: DecimalLike,
    holding_hours: DecimalLike,
    funding_interval_hours: DecimalLike,
) -> Decimal:
    """
    Funding yield over holding horizon.

        funding_horizon = funding_rate * holding_hours / funding_interval_hours
    """

    funding_rate_decimal = to_decimal(funding_rate)
    events = calc_pro_rata_funding_events(
        holding_hours=holding_hours,
        funding_interval_hours=funding_interval_hours,
    )

    return funding_rate_decimal * events


def calc_funding_yield_metrics(
    funding_snapshot: FundingSnapshot,
    holding_hours: DecimalLike,
    calculated_at_ms: int | None = None,
) -> FundingYieldMetrics:
    """
    Calculate funding yield metrics from funding snapshot.

    holding_hours should normally be the effective holding horizon:

        effective_holding_hours = max(
            planned_holding_hours,
            min_cost_amortization_hours,
        )
    """

    calculated_at = (
        utc_now_ms()
        if calculated_at_ms is None
        else int(calculated_at_ms)
    )

    funding_rate = funding_snapshot.effective_funding_rate
    funding_interval = funding_snapshot.funding_interval_hours

    holding_decimal = to_decimal(holding_hours)

    if holding_decimal < ZERO:
        raise ValueError("holding_hours must be >= 0")

    periods_per_year = calc_periods_per_year(funding_interval)
    funding_annual = funding_rate * periods_per_year

    pro_rata_events = calc_pro_rata_funding_events(
        holding_hours=holding_decimal,
        funding_interval_hours=funding_interval,
    )

    funding_horizon = funding_rate * pro_rata_events

    return FundingYieldMetrics(
        funding_rate_per_interval=funding_rate,
        funding_interval_hours=funding_interval,
        periods_per_year=periods_per_year,
        funding_annual=funding_annual,
        holding_hours=holding_decimal,
        pro_rata_funding_events=pro_rata_events,
        funding_horizon=funding_horizon,
        calculated_at_ms=calculated_at,
    )
