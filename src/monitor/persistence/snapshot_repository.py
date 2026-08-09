from __future__ import annotations

from dataclasses import asdict

from monitor.domain import (
    BasisMetrics,
    CostMetrics,
    FundingSnapshot,
    FundingYieldMetrics,
    MarketSnapshot,
    NetYieldMetrics,
    QualityReport,
    SignalDecision,
)
from monitor.utils import utc_now_ms

from .database import (
    Database,
    to_json,
)


class SnapshotRepository:
    """
    Repository for market snapshots, funding snapshots, quality reports,
    calculated metrics and signal decisions.
    """

    def __init__(
        self,
        database: Database,
        save_raw_responses: bool = True,
    ) -> None:
        self._db = database
        self._save_raw_responses = save_raw_responses

    # ------------------------------------------------------------------
    # Market snapshots
    # ------------------------------------------------------------------

    def save_market_snapshot(
        self,
        market_snapshot: MarketSnapshot,
    ) -> int:
        """
        Save market snapshot.
        """

        payload = asdict(market_snapshot)

        if not self._save_raw_responses:
            if "spot" in payload:
                payload["spot"].pop("raw", None)

            if "perp" in payload:
                payload["perp"].pop("raw", None)

        return self._db.execute(
            """
            INSERT INTO market_snapshots (
                cycle_id,
                symbol_name,
                received_at_ms,
                spot_bid,
                spot_ask,
                perp_bid,
                perp_ask,
                payload,
                created_at_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market_snapshot.cycle_id,
                market_snapshot.symbol_name,
                market_snapshot.received_at_ms,
                str(market_snapshot.spot.bid),
                str(market_snapshot.spot.ask),
                str(market_snapshot.perp.bid),
                str(market_snapshot.perp.ask),
                to_json(payload),
                utc_now_ms(),
            ),
        )

    # ------------------------------------------------------------------
    # Funding snapshots
    # ------------------------------------------------------------------

    def save_funding_snapshot(
        self,
        funding_snapshot: FundingSnapshot,
    ) -> int:
        """
        Save funding snapshot.
        """

        payload = asdict(funding_snapshot)

        if not self._save_raw_responses:
            payload.pop("raw", None)

        return self._db.execute(
            """
            INSERT INTO funding_snapshots (
                cycle_id,
                symbol_name,
                received_at_ms,
                effective_funding_rate,
                funding_interval_hours,
                next_funding_timestamp_ms,
                payload,
                created_at_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                funding_snapshot.cycle_id,
                funding_snapshot.symbol_name,
                funding_snapshot.received_at_ms,
                str(funding_snapshot.effective_funding_rate),
                str(funding_snapshot.funding_interval_hours),
                funding_snapshot.next_funding_timestamp_ms,
                to_json(payload),
                utc_now_ms(),
            ),
        )

    # ------------------------------------------------------------------
    # Quality reports
    # ------------------------------------------------------------------

    def save_quality_report(
        self,
        *,
        cycle_id: str,
        symbol_name: str,
        quality_report: QualityReport,
    ) -> int:
        """
        Save quality report.
        """

        payload = asdict(quality_report)

        return self._db.execute(
            """
            INSERT INTO quality_reports (
                cycle_id,
                symbol_name,
                checked_at_ms,
                is_ok,
                payload,
                created_at_ms
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_id,
                symbol_name,
                quality_report.checked_at_ms,
                int(quality_report.is_ok),
                to_json(payload),
                utc_now_ms(),
            ),
        )

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def save_metrics(
        self,
        *,
        cycle_id: str,
        symbol_name: str,
        basis_metrics: BasisMetrics | None = None,
        funding_yield_metrics: FundingYieldMetrics | None = None,
        cost_metrics: CostMetrics | None = None,
        net_yield_metrics: NetYieldMetrics | None = None,
        calculated_at_ms: int | None = None,
    ) -> int:
        """
        Save calculated metrics.
        """

        calculated_at = (
            calculated_at_ms
            if calculated_at_ms is not None
            else (
                net_yield_metrics.calculated_at_ms
                if net_yield_metrics is not None
                else (
                    basis_metrics.calculated_at_ms
                    if basis_metrics is not None
                    else utc_now_ms()
                )
            )
        )

        payload = {
            "basis_metrics": (
                asdict(basis_metrics)
                if basis_metrics is not None
                else None
            ),
            "funding_yield_metrics": (
                asdict(funding_yield_metrics)
                if funding_yield_metrics is not None
                else None
            ),
            "cost_metrics": (
                asdict(cost_metrics)
                if cost_metrics is not None
                else None
            ),
            "net_yield_metrics": (
                asdict(net_yield_metrics)
                if net_yield_metrics is not None
                else None
            ),
        }

        basis_entry = (
            str(basis_metrics.basis_entry)
            if basis_metrics is not None
            else None
        )

        funding_annual = (
            str(funding_yield_metrics.funding_annual)
            if funding_yield_metrics is not None
            else None
        )

        net_horizon = (
            str(net_yield_metrics.net_horizon)
            if net_yield_metrics is not None
            else None
        )

        net_annual = (
            str(net_yield_metrics.net_annual)
            if net_yield_metrics is not None
            else None
        )

        return self._db.execute(
            """
            INSERT INTO metrics (
                cycle_id,
                symbol_name,
                calculated_at_ms,
                basis_entry,
                funding_annual,
                net_horizon,
                net_annual,
                payload,
                created_at_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_id,
                symbol_name,
                calculated_at,
                basis_entry,
                funding_annual,
                net_horizon,
                net_annual,
                to_json(payload),
                utc_now_ms(),
            ),
        )

    # ------------------------------------------------------------------
    # Signal decisions
    # ------------------------------------------------------------------

    def save_signal_decision(
        self,
        signal_decision: SignalDecision,
    ) -> int:
        """
        Save signal decision.
        """

        payload = asdict(signal_decision)

        return self._db.execute(
            """
            INSERT INTO signal_decisions (
                cycle_id,
                symbol_name,
                decision_timestamp_ms,
                state,
                should_alert,
                consecutive_confirmations,
                payload,
                created_at_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_decision.cycle_id,
                signal_decision.symbol_name,
                signal_decision.timestamp_ms,
                signal_decision.state.value,
                int(signal_decision.should_alert),
                signal_decision.consecutive_confirmations,
                to_json(payload),
                utc_now_ms(),
            ),
        )
