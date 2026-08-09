from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from .enums import (
    AlertDeliveryStatus,
    AlertType,
    DataIssueCode,
    DataIssueSeverity,
    FundingRateSource,
    SignalState,
    StrategyDirection,
    YieldBase,
)

# ---------------------------------------------------------------------
# Service / cycle models
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class CycleContext:
    """
    Context for one polling cycle.

    cycle_id is used as correlation id in logs, snapshots and alerts.
    """

    cycle_id: str
    started_at_ms: int
    config_version: str | None = None
    calculation_version: str | None = None


# ---------------------------------------------------------------------
# Data quality models
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class DataIssue:
    """
    One data quality issue.

    Examples:
    - stale snapshot
    - spot/perp timestamp mismatch
    - spread too wide
    - funding unknown
    """

    code: DataIssueCode
    severity: DataIssueSeverity
    message: str
    field_name: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class QualityReport:
    """
    Aggregated data quality result for one symbol/cycle.
    """

    is_ok: bool
    checked_at_ms: int
    errors: tuple[DataIssue, ...] = ()
    warnings: tuple[DataIssue, ...] = ()

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0


# ---------------------------------------------------------------------
# Instrument
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class CarryInstrument:
    """
    Domain representation of one monitored cash-and-carry instrument.

    Example:
    name = BTC_CARRY
    spot_symbol = BTC/USDT
    perp_symbol = BTC/USDT:USDT
    direction = long_spot_short_perp
    """

    name: str
    exchange: str
    base: str
    quote: str
    spot_symbol: str
    perp_symbol: str
    direction: StrategyDirection
    notional_usd: Decimal
    enabled: bool = True


# ---------------------------------------------------------------------
# Market data models
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class SpotTicker:
    """
    Normalized spot ticker.

    All prices are quote currency per base currency.
    Example: BTC/USDT ask = 65000.5 USDT per BTC.
    """

    symbol: str
    bid: Decimal
    ask: Decimal
    timestamp_ms: int

    last: Decimal | None = None

    quote_volume_24h: Decimal | None = None
    base_volume_24h: Decimal | None = None

    bid_quantity: Decimal | None = None
    ask_quantity: Decimal | None = None

    raw: Mapping[str, Any] | None = None

    @property
    def mid_price(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread_abs(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True, slots=True, kw_only=True)
class PerpTicker:
    """
    Normalized perpetual ticker.

    mark_price and index_price are optional but useful for
    funding analysis and sanity checks.
    """

    symbol: str
    bid: Decimal
    ask: Decimal
    timestamp_ms: int

    last: Decimal | None = None
    mark_price: Decimal | None = None
    index_price: Decimal | None = None

    quote_volume_24h: Decimal | None = None
    base_volume_24h: Decimal | None = None
    open_interest: Decimal | None = None

    bid_quantity: Decimal | None = None
    ask_quantity: Decimal | None = None

    raw: Mapping[str, Any] | None = None

    @property
    def mid_price(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread_abs(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketSnapshot:
    """
    Combined spot + perp market snapshot for one carry instrument.
    """

    cycle_id: str
    symbol_name: str
    spot: SpotTicker
    perp: PerpTicker
    received_at_ms: int

    @property
    def spot_perp_time_diff_ms(self) -> int:
        return abs(self.spot.timestamp_ms - self.perp.timestamp_ms)


# ---------------------------------------------------------------------
# Funding models
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class FundingSnapshot:
    """
    Normalized funding snapshot.

    effective_funding_rate is the rate selected for yield calculation.

    If use_predicted_funding is enabled and predicted funding is available,
    effective_funding_rate should be equal to predicted_funding_rate.
    Otherwise it may fall back to last_funding_rate.
    """

    cycle_id: str
    symbol_name: str

    effective_funding_rate: Decimal
    funding_rate_source: FundingRateSource

    funding_interval_hours: Decimal
    received_at_ms: int

    last_funding_rate: Decimal | None = None
    predicted_funding_rate: Decimal | None = None

    last_funding_timestamp_ms: int | None = None
    next_funding_timestamp_ms: int | None = None

    raw: Mapping[str, Any] | None = None

    @property
    def has_predicted_funding(self) -> bool:
        return self.predicted_funding_rate is not None

    @property
    def funding_interval_ms(self) -> int:
        return int(self.funding_interval_hours * Decimal("3600000"))


# ---------------------------------------------------------------------
# Calculated metric models
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class BasisMetrics:
    """
    Basis metrics.

    All values are decimal fractions.

    Example:
    basis_entry = Decimal("0.0004") means 0.04%.

    spot_spread and perp_spread are also decimal fractions:
    spread = ask / bid - 1
    """

    basis_mid: Decimal
    basis_entry: Decimal

    spot_mid: Decimal
    perp_mid: Decimal

    spot_spread: Decimal
    perp_spread: Decimal

    calculated_at_ms: int


@dataclass(frozen=True, slots=True, kw_only=True)
class FundingYieldMetrics:
    """
    Funding yield metrics.

    funding_rate_per_interval is decimal per funding interval.
    funding_annual is decimal annualized yield.

    Example:
    funding_rate_per_interval = Decimal("0.0001")
    funding_annual = Decimal("0.1095")  # 10.95%
    """

    funding_rate_per_interval: Decimal
    funding_interval_hours: Decimal

    periods_per_year: Decimal
    funding_annual: Decimal

    holding_hours: Decimal
    pro_rata_funding_events: Decimal
    funding_horizon: Decimal

    calculated_at_ms: int


@dataclass(frozen=True, slots=True, kw_only=True)
class CostMetrics:
    """
    Cost metrics.

    All values are decimal fractions.

    one_time_costs includes:
    - round-trip spot fees
    - round-trip perp fees
    - slippage round trip
    - spread buffer

    total_costs_horizon includes:
    - one_time_costs
    - borrow cost for horizon
    - opportunity cost for horizon
    """

    spot_fee_round_trip: Decimal
    perp_fee_round_trip: Decimal

    slippage_round_trip: Decimal
    spread_buffer: Decimal

    one_time_costs: Decimal

    borrow_cost_horizon: Decimal
    opportunity_cost_horizon: Decimal

    total_costs_horizon: Decimal

    one_time_costs_annualized: Decimal
    total_costs_annualized: Decimal

    cost_amortization_hours: Decimal

    calculated_at_ms: int


@dataclass(frozen=True, slots=True, kw_only=True)
class NetYieldMetrics:
    """
    Final net yield metrics.

    Conservative Stage 1 mode:
    - basis_convergence_pnl = 0
    - gross_horizon = funding_horizon
    - net_horizon = funding_horizon - total_costs_horizon
    """

    holding_hours: Decimal
    cost_amortization_hours: Decimal

    funding_horizon: Decimal
    basis_convergence_pnl: Decimal
    gross_horizon: Decimal

    total_costs_horizon: Decimal

    net_horizon: Decimal
    net_annual: Decimal

    include_basis_convergence: bool
    yield_base: YieldBase

    calculated_at_ms: int

    # Optional for future Stage 2 / equity analysis.
    net_annual_on_equity: Decimal | None = None


# ---------------------------------------------------------------------
# Signal models
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalMetricsSummary:
    """
    Compact summary of metrics used for signal decision and alert text.
    """

    funding_rate_per_interval: Decimal
    funding_annual: Decimal
    funding_horizon: Decimal

    basis_entry: Decimal

    one_time_costs: Decimal
    total_costs_horizon: Decimal

    net_horizon: Decimal
    net_annual: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalDecision:
    """
    Result of signal engine evaluation for one symbol/cycle.
    """

    cycle_id: str
    symbol_name: str
    timestamp_ms: int

    state: SignalState
    should_alert: bool

    reasons: tuple[str, ...] = ()
    passed_checks: tuple[str, ...] = ()

    consecutive_confirmations: int = 0
    cooldown_remaining_sec: int | None = None

    metrics: SignalMetricsSummary | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AlertState:
    """
    Current signal/alert state for one symbol.

    This object is immutable. Signal state manager should create
    a new AlertState instance on every update.
    """

    symbol_name: str
    state: SignalState

    consecutive_confirmations: int

    last_alert_ts_ms: int | None
    last_signal_started_ts_ms: int | None

    last_net_annual: Decimal | None
    last_reasons: tuple[str, ...]

    updated_at_ms: int


# ---------------------------------------------------------------------
# Alert / audit models
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class AlertRecord:
    """
    Stored alert record for audit and duplicate protection.
    """

    alert_id: str
    cycle_id: str
    symbol_name: str

    alert_type: AlertType
    delivery_status: AlertDeliveryStatus

    created_at_ms: int
    sent_at_ms: int | None = None

    message_payload: Mapping[str, Any] | str | None = None
    error_message: str | None = None
