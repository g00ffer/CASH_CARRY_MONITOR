from __future__ import annotations

from decimal import Decimal

from monitor.domain import (
    BasisMetrics,
    CostMetrics,
    FundingSnapshot,
    MarketSnapshot,
    NetYieldMetrics,
    QualityReport,
    SignalDecision,
)
from monitor.utils import (
    decimal_to_pct,
    format_decimal,
    hours_until,
    ms_to_iso,
)

# ---------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------


def _fmt_pct(
    value: Decimal | int | float | str | None,
    places: int = 2,
) -> str:
    """
    Format decimal fraction as percent string.

    Example:
        Decimal("0.1095") -> "10.95%"
    """

    if value is None:
        return "n/a"

    try:
        pct_value = decimal_to_pct(value)
        return f"{format_decimal(pct_value, places)}%"
    except Exception:
        return "invalid"


def _fmt_price(
    value: Decimal | int | float | str | None,
    places: int = 2,
) -> str:
    """
    Format price.
    """

    if value is None:
        return "n/a"

    try:
        return format_decimal(value, places)
    except Exception:
        return "invalid"


def _fmt_decimal(
    value: Decimal | int | float | str | None,
    places: int = 6,
) -> str:
    """
    Format generic decimal value.
    """

    if value is None:
        return "n/a"

    try:
        return format_decimal(value, places)
    except Exception:
        return "invalid"


def _fmt_timestamp(timestamp_ms: int | None) -> str:
    """
    Format UTC timestamp in ms.
    """

    if timestamp_ms is None:
        return "n/a"

    try:
        return ms_to_iso(timestamp_ms)
    except Exception:
        return "invalid"


# ---------------------------------------------------------------------
# SIGNAL
# ---------------------------------------------------------------------


def format_signal_message(
    *,
    symbol_name: str,
    decision: SignalDecision,
    market_snapshot: MarketSnapshot | None = None,
    funding_snapshot: FundingSnapshot | None = None,
    basis_metrics: BasisMetrics | None = None,
    cost_metrics: CostMetrics | None = None,
    net_yield_metrics: NetYieldMetrics | None = None,
    quality_report: QualityReport | None = None,
    config_version: str | None = None,
) -> str:
    """
    Format human-readable trading signal message.

    This formatter intentionally uses plain text.
    It is safer than HTML/Markdown for Stage 1.
    """

    lines: list[str] = []

    lines.append("🚨 CARRY SIGNAL")
    lines.append(f"Symbol: {symbol_name}")
    lines.append(f"Time: {_fmt_timestamp(decision.timestamp_ms)} UTC")
    lines.append(f"State: {decision.state.value}")
    lines.append(f"Confirmations: {decision.consecutive_confirmations}")
    lines.append("")

    # ------------------------------------------------------------------
    # Market
    # ------------------------------------------------------------------

    if market_snapshot is not None:
        lines.append(
            "Spot bid/ask: "
            f"{_fmt_price(market_snapshot.spot.bid)} / "
            f"{_fmt_price(market_snapshot.spot.ask)}"
        )

        lines.append(
            "Perp bid/ask: "
            f"{_fmt_price(market_snapshot.perp.bid)} / "
            f"{_fmt_price(market_snapshot.perp.ask)}"
        )

        if market_snapshot.perp.mark_price is not None:
            lines.append(
                "Mark price: "
                f"{_fmt_price(market_snapshot.perp.mark_price)}"
            )

        snapshot_age_ms = (
            int(decision.timestamp_ms)
            - int(market_snapshot.received_at_ms)
        )
        lines.append(f"Snapshot age: {snapshot_age_ms} ms")

        lines.append("")

    # ------------------------------------------------------------------
    # Basis
    # ------------------------------------------------------------------

    if basis_metrics is not None:
        lines.append(
            "Entry basis: "
            f"{_fmt_pct(basis_metrics.basis_entry, places=4)}"
        )
        lines.append(
            "Mid basis: "
            f"{_fmt_pct(basis_metrics.basis_mid, places=4)}"
        )
        lines.append("")

    # ------------------------------------------------------------------
    # Funding
    # ------------------------------------------------------------------

    if funding_snapshot is not None:
        interval_hours = funding_snapshot.funding_interval_hours

        lines.append(
            "Funding rate: "
            f"{_fmt_pct(funding_snapshot.effective_funding_rate, places=4)} "
            f"per {_fmt_decimal(interval_hours, places=0)}h"
        )

        lines.append(
            f"Funding source: {funding_snapshot.funding_rate_source.value}"
        )

        if funding_snapshot.predicted_funding_rate is not None:
            lines.append(
                "Predicted funding: "
                f"{_fmt_pct(funding_snapshot.predicted_funding_rate, places=4)}"
            )

        if funding_snapshot.next_funding_timestamp_ms is not None:
            lines.append(
                "Next funding: "
                f"{_fmt_timestamp(funding_snapshot.next_funding_timestamp_ms)} UTC"
            )

            hours_to_funding = hours_until(
                funding_snapshot.next_funding_timestamp_ms,
                decision.timestamp_ms,
            )

            lines.append(
                f"Hours to funding: {_fmt_decimal(hours_to_funding, places=2)}h"
            )

        lines.append("")

    # ------------------------------------------------------------------
    # Yield summary
    # ------------------------------------------------------------------

    if decision.metrics is not None:
        lines.append(
            "Funding annualized: "
            f"{_fmt_pct(decision.metrics.funding_annual, places=2)}"
        )

    if cost_metrics is not None:
        total_fees_round_trip = (
            cost_metrics.spot_fee_round_trip
            + cost_metrics.perp_fee_round_trip
        )

        lines.append(
            "Fees round-trip: "
            f"{_fmt_pct(total_fees_round_trip, places=3)}"
        )

        lines.append(
            "Slippage round-trip: "
            f"{_fmt_pct(cost_metrics.slippage_round_trip, places=3)}"
        )

        lines.append(
            "Spread buffer: "
            f"{_fmt_pct(cost_metrics.spread_buffer, places=3)}"
        )

        lines.append(
            "One-time costs: "
            f"{_fmt_pct(cost_metrics.one_time_costs, places=3)}"
        )

        lines.append(
            "Total costs horizon: "
            f"{_fmt_pct(cost_metrics.total_costs_horizon, places=3)}"
        )

    if net_yield_metrics is not None:
        lines.append("")
        lines.append(
            "Net horizon: "
            f"{_fmt_pct(net_yield_metrics.net_horizon, places=3)}"
        )
        lines.append(
            "Net annual: "
            f"{_fmt_pct(net_yield_metrics.net_annual, places=2)}"
        )
        lines.append(
            "Holding: "
            f"{_fmt_decimal(net_yield_metrics.holding_hours, places=0)}h"
        )
    elif decision.metrics is not None:
        lines.append("")
        lines.append(
            "Net horizon: "
            f"{_fmt_pct(decision.metrics.net_horizon, places=3)}"
        )
        lines.append(
            "Net annual: "
            f"{_fmt_pct(decision.metrics.net_annual, places=2)}"
        )

    # ------------------------------------------------------------------
    # Warnings / metadata
    # ------------------------------------------------------------------

    warning_messages: list[str] = []

    if quality_report is not None:
        for issue in quality_report.warnings:
            warning_messages.append(issue.message)

    lines.append("")

    if warning_messages:
        lines.append(f"Warnings: {'; '.join(warning_messages)}")
    else:
        lines.append("Warnings: none")

    if config_version is not None:
        lines.append(f"Config: {config_version}")

    return "\n".join(lines)


# ---------------------------------------------------------------------
# WARNING / ERROR / HEARTBEAT
# ---------------------------------------------------------------------


def format_warning_message(
    *,
    message: str,
    now_ms: int,
    symbol_name: str | None = None,
    cycle_id: str | None = None,
    details: str | None = None,
    config_version: str | None = None,
) -> str:
    """
    Format warning message.

    Examples:
    - repeated data quality issues
    - stale data
    - funding timestamp issues
    """

    lines: list[str] = []

    lines.append("⚠️ WARNING")
    lines.append(f"Time: {_fmt_timestamp(now_ms)} UTC")

    if symbol_name is not None:
        lines.append(f"Symbol: {symbol_name}")

    if cycle_id is not None:
        lines.append(f"Cycle: {cycle_id}")

    lines.append(f"Message: {message}")

    if details is not None:
        lines.append(f"Details: {details}")

    if config_version is not None:
        lines.append(f"Config: {config_version}")

    return "\n".join(lines)


def format_error_message(
    *,
    message: str,
    now_ms: int,
    symbol_name: str | None = None,
    cycle_id: str | None = None,
    details: str | None = None,
    config_version: str | None = None,
) -> str:
    """
    Format error message.

    Examples:
    - exchange request failure
    - Telegram delivery failure
    - unexpected application error
    """

    lines: list[str] = []

    lines.append("❌ ERROR")
    lines.append(f"Time: {_fmt_timestamp(now_ms)} UTC")

    if symbol_name is not None:
        lines.append(f"Symbol: {symbol_name}")

    if cycle_id is not None:
        lines.append(f"Cycle: {cycle_id}")

    lines.append(f"Message: {message}")

    if details is not None:
        lines.append(f"Details: {details}")

    if config_version is not None:
        lines.append(f"Config: {config_version}")

    return "\n".join(lines)


def format_heartbeat_message(
    *,
    now_ms: int,
    service_name: str = "cash-carry-monitor",
    symbols_count: int | None = None,
    alerts_sent_count: int | None = None,
    last_error: str | None = None,
    config_version: str | None = None,
) -> str:
    """
    Format heartbeat message.
    """

    lines: list[str] = []

    lines.append("💓 HEARTBEAT")
    lines.append(f"Service: {service_name}")
    lines.append(f"Time: {_fmt_timestamp(now_ms)} UTC")

    if symbols_count is not None:
        lines.append(f"Symbols monitored: {symbols_count}")

    if alerts_sent_count is not None:
        lines.append(f"Alerts sent: {alerts_sent_count}")

    if last_error is not None:
        lines.append(f"Last error: {last_error}")
    else:
        lines.append("Last error: none")

    if config_version is not None:
        lines.append(f"Config: {config_version}")

    return "\n".join(lines)
