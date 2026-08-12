from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Sequence

from monitor.calculators import (
    calc_basis_metrics,
    calc_cost_metrics,
    calc_funding_yield_metrics,
    calc_net_yield_metrics,
)
from monitor.config import Settings
from monitor.data import (
    QualityParams,
    check_symbol_quality,
    fetch_funding_snapshot,
    fetch_market_snapshot,
    quality_report_from_errors,
)
from monitor.domain import (
    AlertDeliveryStatus,
    AlertRecord,
    AlertType,
    CarryInstrument,
    ExpectedExitBasisMode,
    MarketSnapshot,
    SignalDecision,
    SignalState,
    YieldBase,
    QualityReport,
)
from monitor.exchanges import ExchangeClient
from monitor.notifications import (
    NotificationResult,
    TelegramNotifier,
    format_heartbeat_message,
    format_signal_message,
    format_warning_message,
)
from monitor.persistence import (
    AlertRepository,
    Database,
    SnapshotRepository,
    get_logger,
    log_event,
    new_alert_id,
)
from monitor.signals import (
    InMemorySignalStateStore,
    SignalEngineParams,
    SignalEvaluationInput,
    evaluate_signal,
    update_alert_state,
)
from monitor.signals.cooldown import calc_cooldown_remaining_sec
from monitor.utils import (
    next_tick_delay_ms,
    utc_now_ms,
    wait_for_stop,
)

# ---------------------------------------------------------------------
# App-level params
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class CostParams:
    """
    Cost parameters prepared by bootstrap.

    All values are decimal fractions.
    """

    spot_fee: Decimal
    perp_fee: Decimal
    slippage_entry: Decimal
    slippage_exit: Decimal
    spread_buffer: Decimal
    borrow_rate_annual: Decimal
    opportunity_cost_annual: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class YieldParams:
    """
    Yield model parameters prepared by bootstrap.
    """

    holding_hours: Decimal
    cost_amortization_hours: Decimal
    include_basis_convergence: bool
    basis_haircut: Decimal
    expected_exit_basis_mode: ExpectedExitBasisMode
    yield_base: YieldBase


# ---------------------------------------------------------------------
# Monitor application
# ---------------------------------------------------------------------


class MonitorApp:
    """
    Main monitoring application for Stage 1.

    Responsibilities:
    - polling cycle orchestration
    - data fetching
    - quality checks
    - metric calculation
    - signal evaluation
    - Telegram notifications
    - persistence
    - heartbeat
    - graceful shutdown
    """

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        exchange_client: ExchangeClient,
        notifier: TelegramNotifier,
        instruments: Sequence[CarryInstrument],
        quality_params: QualityParams,
        signal_params: SignalEngineParams,
        cost_params: CostParams,
        yield_params: YieldParams,
        state_store: InMemorySignalStateStore,
        snapshot_repository: SnapshotRepository,
        alert_repository: AlertRepository,
    ) -> None:
        self._settings = settings
        self._database = database
        self._exchange_client = exchange_client
        self._notifier = notifier

        self._instruments = list(instruments)
        self._enabled_instruments = [
            instrument
            for instrument in self._instruments
            if instrument.enabled
        ]

        self._quality_params = quality_params
        self._signal_params = signal_params
        self._cost_params = cost_params
        self._yield_params = yield_params

        self._state_store = state_store
        self._snapshot_repository = snapshot_repository
        self._alert_repository = alert_repository

        self._logger = get_logger("monitor.app")

        self._stop_event = asyncio.Event()

        self._previous_market_snapshots: dict[str, MarketSnapshot] = {}
        self._last_quality_warning_ms: dict[str, int] = {}

        self._alerts_sent_count = 0
        self._last_error_message: str | None = None

        self._last_heartbeat_ms = 0
        self._last_cleanup_ms = 0

        # Local anti-spam cooldown for data quality warnings.
        self._quality_warning_cooldown_ms = 15 * 60 * 1000

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def request_shutdown(self) -> None:
        """
        Request graceful shutdown.

        Usually called from signal handler.
        """

        self._logger.info("shutdown requested")
        self._stop_event.set()

    async def close(self) -> None:
        """
        Close external clients and database.
        """

        await self._exchange_client.close()
        await self._notifier.close()
        self._database.close()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Run monitoring loop until shutdown.
        """

        log_event(
            self._logger,
            event="app_started",
            payload={
                "config_version": self._settings.meta.config_version,
                "enabled_symbols": len(self._enabled_instruments),
                "market_interval_ms": self._settings.polling.market_interval_ms,
            },
        )

        while not self._stop_event.is_set():
            cycle_started_ms = utc_now_ms()
            cycle_id = self._new_cycle_id()

            try:
                await self._process_cycle(cycle_id)
            except Exception as exc:
                self._last_error_message = str(exc)
                self._logger.exception("polling cycle failed")

            await self._maybe_send_heartbeat()
            await self._maybe_cleanup()

            delay_ms = next_tick_delay_ms(
                interval_ms=self._settings.polling.market_interval_ms,
                started_at_ms=cycle_started_ms,
            )

            await wait_for_stop(
                stop_event=self._stop_event,
                timeout_ms=delay_ms,
            )

        log_event(
            self._logger,
            event="app_stopped",
        )

    # ------------------------------------------------------------------
    # Cycle processing
    # ------------------------------------------------------------------

    async def _process_cycle(self, cycle_id: str) -> None:
        """
        Process one polling cycle for all enabled instruments.
        """

        log_event(
            self._logger,
            event="cycle_started",
            cycle_id=cycle_id,
            payload={
                "symbols": [
                    instrument.name
                    for instrument in self._enabled_instruments
                ],
            },
        )

        for instrument in self._enabled_instruments:
            try:
                await self._process_symbol(
                    instrument=instrument,
                    cycle_id=cycle_id,
                )
            except Exception as exc:
                self._last_error_message = str(exc)

                log_event(
                    self._logger,
                    event="symbol_processing_failed",
                    level=logging.ERROR,
                    cycle_id=cycle_id,
                    symbol_name=instrument.name,
                    payload={
                        "error": str(exc),
                    },
                    exc_info=True,
                )

        log_event(
            self._logger,
            event="cycle_finished",
            cycle_id=cycle_id,
        )

    # ------------------------------------------------------------------
    # Symbol processing
    # ------------------------------------------------------------------

    async def _process_symbol(
        self,
        *,
        instrument: CarryInstrument,
        cycle_id: str,
    ) -> None:
        """
        Process one symbol:

        1. fetch market/funding
        2. quality check
        3. persist snapshots
        4. calculate metrics
        5. evaluate signal
        6. update state
        7. send alert if needed
        """

        now_ms = utc_now_ms()

        previous_market_snapshot = self._previous_market_snapshots.get(
            instrument.name,
        )

        market_snapshot = None
        funding_snapshot = None
        fetch_errors: list[str] = []

        # --------------------------------------------------------------
        # Fetch market snapshot
        # --------------------------------------------------------------

        try:
            market_snapshot = await fetch_market_snapshot(
                client=self._exchange_client,
                instrument=instrument,
                cycle_id=cycle_id,
                now_ms=now_ms,
            )
        except Exception as exc:
            message = f"market fetch failed: {exc}"
            fetch_errors.append(message)
            self._last_error_message = message

            log_event(
                self._logger,
                event="market_fetch_failed",
                level=logging.WARNING,
                cycle_id=cycle_id,
                symbol_name=instrument.name,
                payload={
                    "error": str(exc),
                },
            )

        # --------------------------------------------------------------
        # Fetch funding snapshot
        # --------------------------------------------------------------

        try:
            funding_snapshot = await fetch_funding_snapshot(
                client=self._exchange_client,
                instrument=instrument,
                cycle_id=cycle_id,
                use_predicted_funding=(
                    self._settings.yield_model.use_predicted_funding
                ),
                default_funding_interval_hours=Decimal(
                    str(
                        self._settings.yield_model.default_funding_interval_hours
                    ),
                ),
                now_ms=now_ms,
            )
        except Exception as exc:
            message = f"funding fetch failed: {exc}"
            fetch_errors.append(message)
            self._last_error_message = message

            log_event(
                self._logger,
                event="funding_fetch_failed",
                level=logging.WARNING,
                cycle_id=cycle_id,
                symbol_name=instrument.name,
                payload={
                    "error": str(exc),
                },
            )

        # --------------------------------------------------------------
        # Quality check
        # --------------------------------------------------------------
        if fetch_errors:
            quality_report = quality_report_from_errors(
                messages=fetch_errors,
                checked_at_ms=now_ms,
            )
        else:
            quality_report = check_symbol_quality(
                market_snapshot=market_snapshot,
                funding_snapshot=funding_snapshot,
                params=self._quality_params,
                now_ms=now_ms,
                previous_market_snapshot=previous_market_snapshot,
            )

        await asyncio.to_thread(
            self._snapshot_repository.save_quality_report,
            cycle_id=cycle_id,
            symbol_name=instrument.name,
            quality_report=quality_report,
        )

        # --------------------------------------------------------------
        # Persist snapshots (always save to DB for audit trail)
        # NOTE: _previous_market_snapshots is NOT updated here.
        # It is updated only after quality gate passes, to prevent
        # false PRICE_JUMP warnings on the next cycle. [C4 fix]
        # --------------------------------------------------------------
        if market_snapshot is not None:
            await asyncio.to_thread(
                self._snapshot_repository.save_market_snapshot,
                market_snapshot,
            )
        if funding_snapshot is not None:
            await asyncio.to_thread(
                self._snapshot_repository.save_funding_snapshot,
                funding_snapshot,
            )

        # --------------------------------------------------------------
        # If data is invalid, do not calculate metrics/signals
        # --------------------------------------------------------------
        if (
            not quality_report.is_ok
            or market_snapshot is None
            or funding_snapshot is None
        ):
            await self._handle_bad_data(
                instrument=instrument,
                cycle_id=cycle_id,
                now_ms=now_ms,
                quality_report=quality_report,
            )
            return

        # --------------------------------------------------------------
        # Update previous snapshot reference ONLY after quality check
        # passes. This prevents price_jump check from comparing
        # against bad data. [C4 fix]
        # --------------------------------------------------------------
        self._previous_market_snapshots[instrument.name] = market_snapshot

        # --------------------------------------------------------------
        # Calculate metrics
        # --------------------------------------------------------------

        calculated_at_ms = utc_now_ms()

        basis_metrics = calc_basis_metrics(
            market_snapshot=market_snapshot,
            calculated_at_ms=calculated_at_ms,
        )

        funding_yield_metrics = calc_funding_yield_metrics(
            funding_snapshot=funding_snapshot,
            holding_hours=self._yield_params.holding_hours,
            calculated_at_ms=calculated_at_ms,
        )

        cost_metrics = calc_cost_metrics(
            spot_fee=self._cost_params.spot_fee,
            perp_fee=self._cost_params.perp_fee,
            slippage_entry=self._cost_params.slippage_entry,
            slippage_exit=self._cost_params.slippage_exit,
            spread_buffer=self._cost_params.spread_buffer,
            borrow_rate_annual=self._cost_params.borrow_rate_annual,
            opportunity_cost_annual=(
                self._cost_params.opportunity_cost_annual
            ),
            holding_hours=self._yield_params.holding_hours,
            cost_amortization_hours=(
                self._yield_params.cost_amortization_hours
            ),
            calculated_at_ms=calculated_at_ms,
        )

        net_yield_metrics = calc_net_yield_metrics(
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            basis_metrics=basis_metrics,
            include_basis_convergence=(
                self._yield_params.include_basis_convergence
            ),
            basis_haircut=self._yield_params.basis_haircut,
            expected_exit_basis_mode=(
                self._yield_params.expected_exit_basis_mode
            ),
            historical_median_basis=None,
            yield_base=self._yield_params.yield_base,
            calculated_at_ms=calculated_at_ms,
        )

        await asyncio.to_thread(
            self._snapshot_repository.save_metrics,
            cycle_id=cycle_id,
            symbol_name=instrument.name,
            basis_metrics=basis_metrics,
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=net_yield_metrics,
            calculated_at_ms=calculated_at_ms,
        )

        log_event(
            self._logger,
            event="metrics_calculated",
            cycle_id=cycle_id,
            symbol_name=instrument.name,
            payload={
                "basis_entry": str(basis_metrics.basis_entry),
                "funding_annual": str(funding_yield_metrics.funding_annual),
                "one_time_costs": str(cost_metrics.one_time_costs),
                "net_horizon": str(net_yield_metrics.net_horizon),
                "net_annual": str(net_yield_metrics.net_annual),
            },
        )

        # --------------------------------------------------------------
        # Evaluate signal
        # --------------------------------------------------------------

        evaluation_input = SignalEvaluationInput(
            cycle_id=cycle_id,
            symbol_name=instrument.name,
            now_ms=utc_now_ms(),
            quality_report=quality_report,
            market_snapshot=market_snapshot,
            funding_snapshot=funding_snapshot,
            basis_metrics=basis_metrics,
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=net_yield_metrics,
        )

        current_state = self._state_store.get(instrument.name)

        decision = evaluate_signal(
            evaluation_input=evaluation_input,
            current_state=current_state,
            params=self._signal_params,
        )

        await asyncio.to_thread(
            self._snapshot_repository.save_signal_decision,
            decision,
        )

        log_event(
            self._logger,
            event="signal_decision",
            cycle_id=cycle_id,
            symbol_name=instrument.name,
            payload={
                "state": decision.state.value,
                "should_alert": decision.should_alert,
                "confirmations": decision.consecutive_confirmations,
                "cooldown_remaining_sec": decision.cooldown_remaining_sec,
                "reasons": decision.reasons,
            },
        )

        # --------------------------------------------------------------
        # Send alert if needed
        # --------------------------------------------------------------

        state_decision = decision

        if decision.should_alert:
            notification_result = await self._send_signal_alert(
                instrument=instrument,
                cycle_id=cycle_id,
                decision=decision,
                market_snapshot=market_snapshot,
                funding_snapshot=funding_snapshot,
                basis_metrics=basis_metrics,
                cost_metrics=cost_metrics,
                net_yield_metrics=net_yield_metrics,
                quality_report=quality_report,
                now_ms=utc_now_ms(),
            )

            if notification_result.delivered:
                state_decision = decision
            elif notification_result.status == AlertDeliveryStatus.FAILED:
                # Keep watching and allow retry on next cycle.
                state_decision = replace(
                    decision,
                    should_alert=False,
                    state=SignalState.WATCHING,
                    reasons=(*decision.reasons, "notification_failed"),
                )
            else:
                # Suppressed by Telegram settings/rate limiter.
                # Move to cooldown-like state to avoid retry flooding.
                state_decision = replace(
                    decision,
                    should_alert=False,
                    state=SignalState.COOLDOWN,
                    reasons=(*decision.reasons, "notification_suppressed"),
                )

        # --------------------------------------------------------------
        # Update alert state
        # --------------------------------------------------------------

        new_state = update_alert_state(
            current=current_state,
            decision=state_decision,
            now_ms=utc_now_ms(),
        )

        self._state_store.upsert(new_state)

    # ------------------------------------------------------------------
    # Bad data handling
    # ------------------------------------------------------------------

    async def _handle_bad_data(
        self,
        *,
        instrument: CarryInstrument,
        cycle_id: str,
        now_ms: int,
        quality_report: QualityReport,
    ) -> None:
        """
        Handle invalid/missing data.

        Signal engine is not called.
        State is reset to DATA_INVALID.
        """

        reasons = tuple(
            issue.message
            for issue in quality_report.errors
        )

        current_state = self._state_store.get(instrument.name)

        cooldown_remaining_sec = calc_cooldown_remaining_sec(
            last_alert_ts_ms=(
                current_state.last_alert_ts_ms
                if current_state is not None
                else None
            ),
            now_ms=now_ms,
            cooldown_sec=self._signal_params.cooldown_sec,
        )

        decision = SignalDecision(
            cycle_id=cycle_id,
            symbol_name=instrument.name,
            timestamp_ms=now_ms,
            state=SignalState.DATA_INVALID,
            should_alert=False,
            reasons=reasons,
            passed_checks=(),
            consecutive_confirmations=0,
            cooldown_remaining_sec=cooldown_remaining_sec,
            metrics=None,
        )

        await asyncio.to_thread(
            self._snapshot_repository.save_signal_decision,
            decision,
        )

        new_state = update_alert_state(
            current=current_state,
            decision=decision,
            now_ms=now_ms,
        )

        self._state_store.upsert(new_state)

        log_event(
            self._logger,
            event="data_invalid",
            level=logging.WARNING,
            cycle_id=cycle_id,
            symbol_name=instrument.name,
            payload={
                "reasons": reasons,
            },
        )

        await self._maybe_send_quality_warning(
            instrument=instrument,
            cycle_id=cycle_id,
            now_ms=now_ms,
            quality_report=quality_report,
        )

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    async def _send_signal_alert(
        self,
        *,
        instrument: CarryInstrument,
        cycle_id: str,
        decision: SignalDecision,
        market_snapshot,
        funding_snapshot,
        basis_metrics,
        cost_metrics,
        net_yield_metrics,
        quality_report,
        now_ms: int,
    ) -> NotificationResult:
        """
        Send trading signal to Telegram.
        """

        message = format_signal_message(
            symbol_name=instrument.name,
            decision=decision,
            market_snapshot=market_snapshot,
            funding_snapshot=funding_snapshot,
            basis_metrics=basis_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=net_yield_metrics,
            quality_report=quality_report,
            config_version=self._settings.meta.config_version,
        )

        result = await self._notifier.send_signal(
            text=message,
            now_ms=now_ms,
        )

        if result.status != AlertDeliveryStatus.SUPPRESSED:
            await self._save_notification_alert(
                cycle_id=cycle_id,
                symbol_name=instrument.name,
                alert_type=AlertType.SIGNAL,
                message=message,
                result=result,
                now_ms=now_ms,
            )

        if result.delivered:
            self._alerts_sent_count += 1

            log_event(
                self._logger,
                event="signal_alert_sent",
                cycle_id=cycle_id,
                symbol_name=instrument.name,
            )
        else:
            if result.error_message:
                self._last_error_message = result.error_message

            log_event(
                self._logger,
                event="signal_alert_not_delivered",
                level=logging.WARNING,
                cycle_id=cycle_id,
                symbol_name=instrument.name,
                payload={
                    "status": result.status.value,
                    "suppressed_reason": result.suppressed_reason,
                    "error_message": result.error_message,
                },
            )

        return result

    async def _maybe_send_quality_warning(
        self,
        *,
        instrument: CarryInstrument,
        cycle_id: str,
        now_ms: int,
        quality_report: QualityReport,  # ← ДОБАВЛЕНО
    ) -> None:
        """
        Send data quality warning if enabled and local cooldown passed.
        """

        if not self._settings.telegram.send_data_errors:
            return

        last_warning_ms = self._last_quality_warning_ms.get(
            instrument.name,
            0,
        )

        if now_ms - last_warning_ms < self._quality_warning_cooldown_ms:
            return

        details = "; ".join(
            issue.message
            for issue in quality_report.errors
        )[:1000]

        message = format_warning_message(
            message="data quality check failed",
            now_ms=now_ms,
            symbol_name=instrument.name,
            cycle_id=cycle_id,
            details=details,
            config_version=self._settings.meta.config_version,
        )

        result = await self._notifier.send_warning(
            text=message,
            now_ms=now_ms,
        )

        if result.status != AlertDeliveryStatus.SUPPRESSED:
            await self._save_notification_alert(
                cycle_id=cycle_id,
                symbol_name=instrument.name,
                alert_type=AlertType.WARNING,
                message=message,
                result=result,
                now_ms=now_ms,
            )

        if result.delivered:
            self._last_quality_warning_ms[instrument.name] = now_ms
        elif result.status == AlertDeliveryStatus.SUPPRESSED:
            # Avoid trying to send the same warning every cycle
            # when Telegram limiter/settings suppress it.
            self._last_quality_warning_ms[instrument.name] = now_ms
        else:
            if result.error_message:
                self._last_error_message = result.error_message

            log_event(
                self._logger,
                event="quality_warning_not_delivered",
                level=logging.WARNING,
                cycle_id=cycle_id,
                symbol_name=instrument.name,
                payload={
                    "status": result.status.value,
                    "error_message": result.error_message,
                },
            )

    async def _maybe_send_heartbeat(self) -> None:
        """
        Send heartbeat if heartbeat interval elapsed.
        """

        if not self._settings.telegram.send_heartbeat:
            return

        now_ms = utc_now_ms()

        if (
            self._last_heartbeat_ms > 0
            and now_ms - self._last_heartbeat_ms
            < self._settings.polling.heartbeat_interval_ms
        ):
            return

        message = format_heartbeat_message(
            now_ms=now_ms,
            service_name="cash-carry-monitor",
            symbols_count=len(self._enabled_instruments),
            alerts_sent_count=self._alerts_sent_count,
            last_error=self._last_error_message,
            config_version=self._settings.meta.config_version,
        )

        result = await self._notifier.send_heartbeat(
            text=message,
            now_ms=now_ms,
        )

        # For heartbeat we update last attempt time even if failed/suppressed
        # to avoid heartbeat spam on transient issues.
        self._last_heartbeat_ms = now_ms

        if result.delivered:
            log_event(
                self._logger,
                event="heartbeat_sent",
            )
        else:
            if result.error_message:
                self._last_error_message = result.error_message

            log_event(
                self._logger,
                event="heartbeat_not_delivered",
                level=logging.WARNING,
                payload={
                    "status": result.status.value,
                    "suppressed_reason": result.suppressed_reason,
                    "error_message": result.error_message,
                },
            )

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    async def _save_notification_alert(
        self,
        *,
        cycle_id: str,
        symbol_name: str,
        alert_type: AlertType,
        message: str,
        result: NotificationResult,
        now_ms: int,
    ) -> None:
        """
        Save notification attempt into alert repository.
        """
        alert_record = AlertRecord(
            alert_id=new_alert_id(),
            cycle_id=cycle_id,
            symbol_name=symbol_name,
            alert_type=alert_type,
            delivery_status=result.status,
            created_at_ms=now_ms,
            sent_at_ms=now_ms if result.delivered else None,
            message_payload=message,
            error_message=result.error_message,
        )
        await asyncio.to_thread(
            self._alert_repository.save_alert,
            alert_record,
        )

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    async def _maybe_cleanup(self) -> None:
        """
        Periodically remove old SQLite records.
        """

        now_ms = utc_now_ms()
        cleanup_interval_ms = 24 * 60 * 60 * 1000

        if (
            self._last_cleanup_ms > 0
            and now_ms - self._last_cleanup_ms < cleanup_interval_ms
        ):
            return

        try:
            await asyncio.to_thread(
                self._database.cleanup_old_records,
                retention_days=self._settings.storage.retention_days,
            )

            self._last_cleanup_ms = now_ms

            log_event(
                self._logger,
                event="database_cleanup_done",
                payload={
                    "retention_days": self._settings.storage.retention_days,
                },
            )
        except Exception as exc:
            self._last_error_message = str(exc)
            self._logger.exception("database cleanup failed")

    # ------------------------------------------------------------------
    # Utils
    # ------------------------------------------------------------------

    @staticmethod
    def _new_cycle_id() -> str:
        return str(uuid.uuid4())
