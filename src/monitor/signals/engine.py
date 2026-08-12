from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from monitor.domain import (
    AlertState,
    BasisMetrics,
    CostMetrics,
    FundingSnapshot,
    FundingYieldMetrics,
    MarketSnapshot,
    NetYieldMetrics,
    QualityReport,
    SignalDecision,
    SignalMetricsSummary,
    SignalState,
)
from monitor.utils import (
    ZERO,
    to_decimal,
)

from .cooldown import (
    calc_cooldown_remaining_sec,
    hysteresis_threshold,
)

# ---------------------------------------------------------------------
# Engine parameters
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalEngineParams:
    """
    Signal engine thresholds.

    All yield/rate/spread values are decimal fractions.

    Examples:
        min_net_annual = Decimal("0.08")      # 8%
        min_net_horizon = Decimal("0.001")    # 0.1%
        min_funding_rate_per_interval = Decimal("0.00005")  # 0.005%
        max_spread = Decimal("0.001")         # 0.10% = 10 bps
    """

    min_net_annual: Decimal
    min_net_horizon: Decimal
    min_funding_rate_per_interval: Decimal

    require_positive_funding: bool
    require_predicted_funding: bool

    min_consecutive_confirmations: int
    cooldown_sec: int
    hysteresis: Decimal

    max_snapshot_age_ms: int
    max_spread: Decimal

    suppress_minutes_before_funding: int
    suppress_minutes_after_funding: int

    # If True, repeated alerts may be sent while signal remains active
    # after cooldown expires.
    #
    # For Stage 1 conservative default is False:
    # alert once when signal becomes active.
    repeat_alert_while_active: bool = False

    def __post_init__(self) -> None:
        if self.min_consecutive_confirmations < 1:
            raise ValueError("min_consecutive_confirmations must be >= 1")

        if self.cooldown_sec < 0:
            raise ValueError("cooldown_sec must be >= 0")

        if self.max_snapshot_age_ms <= 0:
            raise ValueError("max_snapshot_age_ms must be > 0")

        if self.suppress_minutes_before_funding < 0:
            raise ValueError("suppress_minutes_before_funding must be >= 0")

        if self.suppress_minutes_after_funding < 0:
            raise ValueError("suppress_minutes_after_funding must be >= 0")

        if to_decimal(self.min_net_annual) < ZERO:
            raise ValueError("min_net_annual must be >= 0")

        if to_decimal(self.min_net_horizon) < ZERO:
            raise ValueError("min_net_horizon must be >= 0")

        if to_decimal(self.min_funding_rate_per_interval) < ZERO:
            raise ValueError("min_funding_rate_per_interval must be >= 0")

        if to_decimal(self.hysteresis) < ZERO:
            raise ValueError("hysteresis must be >= 0")

        if to_decimal(self.max_spread) < ZERO:
            raise ValueError("max_spread must be >= 0")


# ---------------------------------------------------------------------
# Engine input
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalEvaluationInput:
    """
    Full input required to evaluate one signal.

    This object should be produced by application/data layer after:
    - exchange fetch
    - quality checks
    - calculators
    """

    cycle_id: str
    symbol_name: str
    now_ms: int

    quality_report: QualityReport

    market_snapshot: MarketSnapshot
    funding_snapshot: FundingSnapshot

    basis_metrics: BasisMetrics
    funding_yield_metrics: FundingYieldMetrics
    cost_metrics: CostMetrics
    net_yield_metrics: NetYieldMetrics


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def build_signal_metrics_summary(
    evaluation_input: SignalEvaluationInput,
) -> SignalMetricsSummary:
    """
    Build compact metric summary for signal decision/alert.
    """

    return SignalMetricsSummary(
        funding_rate_per_interval=(
            evaluation_input.funding_snapshot.effective_funding_rate
        ),
        funding_annual=(
            evaluation_input.funding_yield_metrics.funding_annual
        ),
        funding_horizon=(
            evaluation_input.funding_yield_metrics.funding_horizon
        ),
        basis_entry=evaluation_input.basis_metrics.basis_entry,
        one_time_costs=evaluation_input.cost_metrics.one_time_costs,
        total_costs_horizon=(
            evaluation_input.cost_metrics.total_costs_horizon
        ),
        net_horizon=evaluation_input.net_yield_metrics.net_horizon,
        net_annual=evaluation_input.net_yield_metrics.net_annual,
    )


def _funding_suppression_reason(
    *,
    next_funding_timestamp_ms: int | None,
    now_ms: int,
    suppress_minutes_before_funding: int,
    suppress_minutes_after_funding: int,
) -> str | None:
    """
    Optional suppression around funding timestamp.

    This protects against entering too close to funding settlement.
    """

    if next_funding_timestamp_ms is None:
        return None

    before_ms = int(suppress_minutes_before_funding) * 60_000
    after_ms = int(suppress_minutes_after_funding) * 60_000

    if before_ms > 0:
        if now_ms < next_funding_timestamp_ms:
            time_until_funding_ms = next_funding_timestamp_ms - now_ms

            if time_until_funding_ms <= before_ms:
                return "funding_imminent"

    if after_ms > 0:
        if now_ms >= next_funding_timestamp_ms:
            time_since_funding_ms = now_ms - next_funding_timestamp_ms

            if time_since_funding_ms <= after_ms:
                return "funding_just_passed"

    return None


# ---------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------


def evaluate_signal(
    *,
    evaluation_input: SignalEvaluationInput,
    current_state: AlertState | None,
    params: SignalEngineParams,
) -> SignalDecision:
    """
    Evaluate signal for one symbol/cycle.

    The engine does not send Telegram messages.
    It only returns SignalDecision.
    """

    reasons: list[str] = []
    passed_checks: list[str] = []

    now_ms = int(evaluation_input.now_ms)

    previous_state = (
        current_state.state
        if current_state is not None
        else SignalState.NORMAL
    )

    was_active = previous_state in (
        SignalState.SIGNAL_ACTIVE,
        SignalState.COOLDOWN,
    )

    cooldown_remaining_sec = calc_cooldown_remaining_sec(
        last_alert_ts_ms=(
            current_state.last_alert_ts_ms
            if current_state is not None
            else None
        ),
        now_ms=now_ms,
        cooldown_sec=params.cooldown_sec,
    )

    # ------------------------------------------------------------------
    # 1. Data quality gate
    # ------------------------------------------------------------------

    if not evaluation_input.quality_report.is_ok:
        for issue in evaluation_input.quality_report.errors:
            reasons.append(f"quality:{issue.code.value}")

        return SignalDecision(
            cycle_id=evaluation_input.cycle_id,
            symbol_name=evaluation_input.symbol_name,
            timestamp_ms=now_ms,
            state=SignalState.DATA_INVALID,
            should_alert=False,
            reasons=tuple(reasons),
            passed_checks=(),
            consecutive_confirmations=0,
            cooldown_remaining_sec=cooldown_remaining_sec,
            metrics=None,
        )

    passed_checks.append("quality_ok")

    # ------------------------------------------------------------------
    # 2. Snapshot age
    # ------------------------------------------------------------------

    snapshot_age_ms = (
        now_ms - evaluation_input.market_snapshot.received_at_ms
    )

    if snapshot_age_ms < 0:
        reasons.append("snapshot_in_future")
    elif snapshot_age_ms <= params.max_snapshot_age_ms:
        passed_checks.append("snapshot_fresh")
    else:
        reasons.append("snapshot_stale")

    # ------------------------------------------------------------------
    # 3. Spread check
    # ------------------------------------------------------------------

    spot_spread = evaluation_input.basis_metrics.spot_spread
    perp_spread = evaluation_input.basis_metrics.perp_spread

    if spot_spread < ZERO or perp_spread < ZERO:
        reasons.append("invalid_spread")
    elif spot_spread <= params.max_spread and perp_spread <= params.max_spread:
        passed_checks.append("spread_ok")
    else:
        reasons.append("spread_too_wide")

    # ------------------------------------------------------------------
    # 4. Funding interval
    # ------------------------------------------------------------------

    if evaluation_input.funding_snapshot.funding_interval_hours > ZERO:
        passed_checks.append("funding_interval_known")
    else:
        reasons.append("funding_interval_unknown")

    # ------------------------------------------------------------------
    # 5. Predicted funding availability
    # ------------------------------------------------------------------

    if params.require_predicted_funding:
        if evaluation_input.funding_snapshot.predicted_funding_rate is not None:
            passed_checks.append("predicted_funding_available")
        else:
            reasons.append("predicted_funding_missing")

    # ------------------------------------------------------------------
    # 6. Funding sign / minimum funding rate
    # ------------------------------------------------------------------

    funding_rate = evaluation_input.funding_snapshot.effective_funding_rate

    if params.require_positive_funding:
        if funding_rate > ZERO:
            passed_checks.append("funding_positive")
        else:
            reasons.append("funding_not_positive")

    if funding_rate >= params.min_funding_rate_per_interval:
        passed_checks.append("funding_rate_ok")
    else:
        reasons.append("funding_rate_below_threshold")

    # ------------------------------------------------------------------
    # 7. Net horizon yield (with hysteresis)
    # ------------------------------------------------------------------
    effective_net_horizon_threshold = hysteresis_threshold(
        base_threshold=params.min_net_horizon,
        hysteresis=params.hysteresis,
        is_active=was_active,
    )
    if (
        evaluation_input.net_yield_metrics.net_horizon
        >= effective_net_horizon_threshold
    ):
        passed_checks.append("net_horizon_ok")
    else:
        reasons.append("net_horizon_below_threshold")

    # ------------------------------------------------------------------
    # 8. Net annual yield with hysteresis
    # ------------------------------------------------------------------

    effective_net_annual_threshold = hysteresis_threshold(
        base_threshold=params.min_net_annual,
        hysteresis=params.hysteresis,
        is_active=was_active,
    )

    if (
        evaluation_input.net_yield_metrics.net_annual
        >= effective_net_annual_threshold
    ):
        passed_checks.append("net_annual_ok")
    else:
        reasons.append("net_annual_below_threshold")

    # ------------------------------------------------------------------
    # 9. Funding timing suppression
    # ------------------------------------------------------------------

    funding_suppression_reason = _funding_suppression_reason(
        next_funding_timestamp_ms=(
            evaluation_input.funding_snapshot.next_funding_timestamp_ms
        ),
        now_ms=now_ms,
        suppress_minutes_before_funding=(
            params.suppress_minutes_before_funding
        ),
        suppress_minutes_after_funding=(
            params.suppress_minutes_after_funding
        ),
    )

    if funding_suppression_reason is None:
        passed_checks.append("funding_window_ok")
    else:
        reasons.append(funding_suppression_reason)

    # ------------------------------------------------------------------
    # 10. Confirmations and state transition
    # ------------------------------------------------------------------

    condition_met = len(reasons) == 0

    previous_confirmations = 0

    if (
        current_state is not None
        and current_state.state != SignalState.DATA_INVALID
    ):
        previous_confirmations = current_state.consecutive_confirmations

    if condition_met:
        consecutive_confirmations = previous_confirmations + 1
    else:
        consecutive_confirmations = 0

    signal_active = (
        condition_met
        and consecutive_confirmations >= params.min_consecutive_confirmations
    )

    if not condition_met:
        new_state = SignalState.NORMAL
    elif signal_active:
        if cooldown_remaining_sec > 0:
            new_state = SignalState.COOLDOWN
        else:
            new_state = SignalState.SIGNAL_ACTIVE
    else:
        new_state = SignalState.WATCHING

    # ------------------------------------------------------------------
    # 11. Alert decision
    # ------------------------------------------------------------------

    should_alert = False

    if signal_active and cooldown_remaining_sec == 0:
        if not was_active:
            should_alert = True
        elif params.repeat_alert_while_active:
            should_alert = True

    if should_alert:
        new_state = SignalState.SIGNAL_ACTIVE

    # ------------------------------------------------------------------
    # 12. Metrics summary
    # ------------------------------------------------------------------

    metrics_summary = build_signal_metrics_summary(evaluation_input)

    return SignalDecision(
        cycle_id=evaluation_input.cycle_id,
        symbol_name=evaluation_input.symbol_name,
        timestamp_ms=now_ms,
        state=new_state,
        should_alert=should_alert,
        reasons=tuple(reasons),
        passed_checks=tuple(passed_checks),
        consecutive_confirmations=consecutive_confirmations,
        cooldown_remaining_sec=cooldown_remaining_sec,
        metrics=metrics_summary,
    )
