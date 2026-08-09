from __future__ import annotations

from decimal import Decimal

from monitor.domain import (
    BasisMetrics,
    CostMetrics,
    FundingYieldMetrics,
    NetYieldMetrics,
)
from monitor.domain.enums import (
    ExpectedExitBasisMode,
    YieldBase,
)
from monitor.utils import (
    ONE,
    ZERO,
    DecimalLike,
    annualize_value,
    to_decimal,
    utc_now_ms,
)

from .basis import select_expected_exit_basis

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
# Basis convergence
# ---------------------------------------------------------------------


def calc_basis_convergence_pnl(
    *,
    include_basis_convergence: bool,
    basis_haircut: DecimalLike,
    basis_entry: DecimalLike,
    expected_exit_basis_mode: ExpectedExitBasisMode,
    historical_median_basis: DecimalLike | None = None,
) -> Decimal:
    """
    Calculate optional basis convergence PnL.

    Conservative Stage 1 default:

        include_basis_convergence = false
        basis_haircut = 0
        expected_exit_basis_mode = ENTRY

    In that case basis_convergence_pnl = 0.

    If enabled:

        basis_convergence_pnl =
            basis_haircut * (basis_entry - expected_exit_basis)
    """

    if not include_basis_convergence:
        return ZERO

    haircut_decimal = _require_non_negative(
        basis_haircut,
        "basis_haircut",
    )

    if haircut_decimal > ONE:
        raise ValueError("basis_haircut must be <= 1")

    if haircut_decimal == ZERO:
        return ZERO

    basis_entry_decimal = to_decimal(basis_entry)

    expected_exit_basis_decimal = select_expected_exit_basis(
        mode=expected_exit_basis_mode,
        basis_entry=basis_entry_decimal,
        historical_median_basis=historical_median_basis,
    )

    return haircut_decimal * (
        basis_entry_decimal - expected_exit_basis_decimal
    )


# ---------------------------------------------------------------------
# Gross / net horizon helpers
# ---------------------------------------------------------------------


def calc_gross_horizon(
    funding_horizon: DecimalLike,
    basis_convergence_pnl: DecimalLike,
) -> Decimal:
    """
    Gross horizon yield.

        gross_horizon =
            funding_horizon
          + basis_convergence_pnl
    """

    funding_horizon_decimal = to_decimal(funding_horizon)
    basis_convergence_pnl_decimal = to_decimal(basis_convergence_pnl)

    return funding_horizon_decimal + basis_convergence_pnl_decimal


def calc_net_horizon(
    gross_horizon: DecimalLike,
    total_costs_horizon: DecimalLike,
) -> Decimal:
    """
    Net horizon yield.

        net_horizon =
            gross_horizon
          - total_costs_horizon
    """

    gross_horizon_decimal = to_decimal(gross_horizon)
    total_costs_horizon_decimal = to_decimal(total_costs_horizon)

    return gross_horizon_decimal - total_costs_horizon_decimal


def calc_net_annual_from_horizon(
    net_horizon: DecimalLike,
    annualization_hours: DecimalLike,
) -> Decimal:
    """
    Annualize net horizon yield.

        net_annual = net_horizon * 8760 / annualization_hours
    """

    annualization_decimal = _require_positive(
        annualization_hours,
        "annualization_hours",
    )

    return annualize_value(
        net_horizon,
        annualization_decimal,
    )


# ---------------------------------------------------------------------
# Net yield metrics builder
# ---------------------------------------------------------------------


def calc_net_yield_metrics(
    *,
    funding_yield_metrics: FundingYieldMetrics,
    cost_metrics: CostMetrics,
    basis_metrics: BasisMetrics | None,
    include_basis_convergence: bool,
    basis_haircut: DecimalLike,
    expected_exit_basis_mode: ExpectedExitBasisMode,
    historical_median_basis: DecimalLike | None = None,
    yield_base: YieldBase = YieldBase.NOTIONAL,
    calculated_at_ms: int | None = None,
) -> NetYieldMetrics:
    """
    Build final net yield metrics.

    For Stage 1 conservative mode:

        include_basis_convergence = false
        basis_haircut = 0
        expected_exit_basis_mode = ENTRY

    Net annual is calculated as:

        net_annual =
            funding_annual
          + basis_convergence_annual
          - total_costs_annualized

    This respects separate cost amortization horizon and avoids
    directly annualizing a very short net horizon without cost control.
    """

    calculated_at = (
        utc_now_ms()
        if calculated_at_ms is None
        else int(calculated_at_ms)
    )

    if basis_metrics is None:
        basis_convergence_pnl_decimal = ZERO
    else:
        basis_convergence_pnl_decimal = calc_basis_convergence_pnl(
            include_basis_convergence=include_basis_convergence,
            basis_haircut=basis_haircut,
            basis_entry=basis_metrics.basis_entry,
            expected_exit_basis_mode=expected_exit_basis_mode,
            historical_median_basis=historical_median_basis,
        )

    gross_horizon_decimal = calc_gross_horizon(
        funding_horizon=funding_yield_metrics.funding_horizon,
        basis_convergence_pnl=basis_convergence_pnl_decimal,
    )

    net_horizon_decimal = calc_net_horizon(
        gross_horizon=gross_horizon_decimal,
        total_costs_horizon=cost_metrics.total_costs_horizon,
    )

    holding_hours_decimal = funding_yield_metrics.holding_hours

    if holding_hours_decimal > ZERO:
        basis_convergence_annual_decimal = annualize_value(
            basis_convergence_pnl_decimal,
            holding_hours_decimal,
        )
    else:
        basis_convergence_annual_decimal = ZERO

    net_annual_decimal = (
        funding_yield_metrics.funding_annual
        + basis_convergence_annual_decimal
        - cost_metrics.total_costs_annualized
    )

    return NetYieldMetrics(
        holding_hours=holding_hours_decimal,
        cost_amortization_hours=cost_metrics.cost_amortization_hours,
        funding_horizon=funding_yield_metrics.funding_horizon,
        basis_convergence_pnl=basis_convergence_pnl_decimal,
        gross_horizon=gross_horizon_decimal,
        total_costs_horizon=cost_metrics.total_costs_horizon,
        net_horizon=net_horizon_decimal,
        net_annual=net_annual_decimal,
        include_basis_convergence=include_basis_convergence,
        yield_base=yield_base,
        calculated_at_ms=calculated_at,
        net_annual_on_equity=None,
    )
