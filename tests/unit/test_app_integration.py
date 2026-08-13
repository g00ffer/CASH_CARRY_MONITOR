"""
Integration tests for MonitorApp._process_symbol.
Real SQLite + real calculators/quality/signal engine,
mock ExchangeClient + mock notifier.
"""
from __future__ import annotations

import sqlite3
import types
from dataclasses import dataclass
from decimal import Decimal

import pytest

from monitor.app import CostParams, MonitorApp, YieldParams
from monitor.config.schema import Settings, SymbolConfig
from monitor.domain import (
    AlertDeliveryStatus,
    ExpectedExitBasisMode,
    FundingSnapshot,
    PerpTicker,
    SignalState,
    SpotTicker,
    YieldBase,
)
from monitor.domain.enums import FundingRateSource
from monitor.exchanges import ExchangeRequestError
from monitor.persistence import (
    AlertRepository,
    Database,
    SnapshotRepository,
)
from monitor.signals import InMemorySignalStateStore
from monitor.utils import utc_now_ms


# ======================================================================
# Fakes
# ======================================================================

class MockExchangeClient:
    """In-memory ExchangeClient (same contract as in test_data_services)."""

    def __init__(
        self,
        *,
        spot: SpotTicker | None = None,
        perp: PerpTicker | None = None,
        funding: FundingSnapshot | None = None,
        spot_error: Exception | None = None,
        funding_error: Exception | None = None,
    ) -> None:
        self._spot = spot
        self._perp = perp
        self._funding = funding
        self._spot_error = spot_error
        self._funding_error = funding_error

    async def close(self) -> None:
        return None

    async def fetch_exchange_time_ms(self) -> int:
        return utc_now_ms()

    async def fetch_spot_ticker(self, symbol: str) -> SpotTicker:
        if self._spot_error is not None:
            raise self._spot_error
        assert self._spot is not None
        return self._spot

    async def fetch_perp_ticker(self, symbol: str) -> PerpTicker:
        assert self._perp is not None
        return self._perp

    async def fetch_funding_snapshot(
        self,
        *,
        cycle_id: str,
        symbol_name: str,
        perp_symbol: str,
        use_predicted_funding: bool,
        default_funding_interval_hours: Decimal,
    ) -> FundingSnapshot:
        if self._funding_error is not None:
            raise self._funding_error
        assert self._funding is not None
        return self._funding


@dataclass
class FakeNotificationResult:
    status: AlertDeliveryStatus
    delivered: bool
    error_message: str | None = None
    suppressed_reason: str | None = None


class FakeNotifier:
    """Records outgoing messages instead of hitting Telegram."""

    def __init__(
        self,
        status: AlertDeliveryStatus = AlertDeliveryStatus.SENT,
    ) -> None:
        self._status = status
        self.signals: list[str] = []
        self.warnings: list[str] = []
        self.heartbeats: list[str] = []

    def _result(self) -> FakeNotificationResult:
        return FakeNotificationResult(
            status=self._status,
            delivered=self._status == AlertDeliveryStatus.SENT,
            error_message=(
                "delivery failed"
                if self._status == AlertDeliveryStatus.FAILED
                else None
            ),
        )

    async def send_signal(self, *, text, now_ms=None, **kwargs):
        self.signals.append(text)
        return self._result()

    async def send_warning(self, *, text, now_ms=None, **kwargs):
        self.warnings.append(text)
        return self._result()

    async def send_heartbeat(self, *, text, now_ms=None, **kwargs):
        self.heartbeats.append(text)
        return self._result()

    async def close(self) -> None:
        return None


# ======================================================================
# Fresh market data builders (timestamps relative to real now)
# ======================================================================

def _make_spot(now_ms: int) -> SpotTicker:
    return SpotTicker(
        symbol="BTC/USDT",
        bid=Decimal("65000"),
        ask=Decimal("65001"),
        timestamp_ms=now_ms - 500,
        last=Decimal("65000.5"),
        quote_volume_24h=Decimal("803702067"),
        base_volume_24h=Decimal("12345.67"),
    )


def _make_perp(now_ms: int) -> PerpTicker:
    return PerpTicker(
        symbol="BTC/USDT:USDT",
        bid=Decimal("65030"),
        ask=Decimal("65032"),
        timestamp_ms=now_ms - 300,
        last=Decimal("65031"),
        mark_price=Decimal("65031"),
        index_price=Decimal("65025"),
        quote_volume_24h=Decimal("6432587654"),
        base_volume_24h=Decimal("98765.43"),
    )


def _make_funding(now_ms: int, rate: Decimal) -> FundingSnapshot:
    return FundingSnapshot(
        cycle_id="integration",
        symbol_name="BTC_CARRY",
        effective_funding_rate=rate,
        funding_rate_source=FundingRateSource.PREDICTED,
        funding_interval_hours=Decimal("8"),
        received_at_ms=now_ms - 400,
        last_funding_rate=rate,
        predicted_funding_rate=rate,
        # 4 hours ahead — outside suppression window
        next_funding_timestamp_ms=now_ms + 4 * 3600 * 1000,
    )


def _count(db_path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.close()
    return count


# ======================================================================
# App builder
# ======================================================================

def _build_app(
    tmp_path,
    client,
    notifier,
    quality_params,
    signal_params,
    carry_instrument,
):
    db_path = tmp_path / "integration.sqlite"
    database = Database(
        types.SimpleNamespace(
            mode="sqlite",
            sqlite_path=str(db_path),
            save_raw_responses=True,
            retention_days=90,
            save_snapshots=True,
            save_alerts=True,
        ),
    )
    settings = Settings(
        symbols=[
            SymbolConfig(
                name="BTC_CARRY",
                base="BTC",
                quote="USDT",
                spot_symbol="BTC/USDT",
                perp_symbol="BTC/USDT:USDT",
            ),
        ],
    )
    app = MonitorApp(
        settings=settings,
        database=database,
        exchange_client=client,
        notifier=notifier,
        instruments=[carry_instrument],
        quality_params=quality_params,
        signal_params=signal_params,
        cost_params=CostParams(
            spot_fee=Decimal("0.001"),
            perp_fee=Decimal("0.0005"),
            slippage_entry=Decimal("0.0002"),
            slippage_exit=Decimal("0.0002"),
            spread_buffer=Decimal("0.0002"),
            borrow_rate_annual=Decimal("0"),
            opportunity_cost_annual=Decimal("0"),
        ),
        yield_params=YieldParams(
            holding_hours=Decimal("720"),
            cost_amortization_hours=Decimal("720"),
            include_basis_convergence=False,
            basis_haircut=Decimal("0"),
            expected_exit_basis_mode=ExpectedExitBasisMode.ENTRY,
            yield_base=YieldBase.NOTIONAL,
        ),
        state_store=InMemorySignalStateStore(),
        snapshot_repository=SnapshotRepository(database),
        alert_repository=AlertRepository(database),
    )
    return app, db_path, database


# ======================================================================
# Integration scenarios
# ======================================================================

class TestProcessSymbolIntegration:
    @pytest.mark.asyncio
    async def test_full_cycle_no_signal(
        self, tmp_path, quality_params, signal_params, carry_instrument,
    ):
        """
        Funding 0.01%/8h -> annual 10.95%, costs 4.38% (720h)
        -> net_annual ~6.57% < 8% -> no alert, everything persisted.
        """
        now = utc_now_ms()
        client = MockExchangeClient(
            spot=_make_spot(now),
            perp=_make_perp(now),
            funding=_make_funding(now, Decimal("0.0001")),
        )
        notifier = FakeNotifier()
        app, db_path, database = _build_app(
            tmp_path, client, notifier,
            quality_params, signal_params, carry_instrument,
        )
        try:
            await app._process_symbol(
                instrument=carry_instrument, cycle_id="c-1",
            )

            assert _count(db_path, "market_snapshots") == 1
            assert _count(db_path, "funding_snapshots") == 1
            assert _count(db_path, "quality_reports") == 1
            assert _count(db_path, "metrics") == 1
            assert _count(db_path, "signal_decisions") == 1
            assert _count(db_path, "alerts") == 0
            assert notifier.signals == []

            state = app._state_store.get("BTC_CARRY")
            assert state is not None
            assert state.state in (
                SignalState.NORMAL,
                SignalState.WATCHING,
            )
        finally:
            database.close()

    @pytest.mark.asyncio
    async def test_signal_after_three_confirmations(
        self, tmp_path, quality_params, signal_params, carry_instrument,
    ):
        """
        Funding 0.03%/8h -> annual ~32.8% -> net ~28% > 8%.
        After 3 consecutive cycles: SIGNAL_ACTIVE + 1 delivered alert.
        """
        now = utc_now_ms()
        client = MockExchangeClient(
            spot=_make_spot(now),
            perp=_make_perp(now),
            funding=_make_funding(now, Decimal("0.0003")),
        )
        notifier = FakeNotifier()
        app, db_path, database = _build_app(
            tmp_path, client, notifier,
            quality_params, signal_params, carry_instrument,
        )
        try:
            for i in range(1, 4):
                await app._process_symbol(
                    instrument=carry_instrument, cycle_id=f"c-{i}",
                )

            state = app._state_store.get("BTC_CARRY")
            assert state is not None
            assert state.state == SignalState.SIGNAL_ACTIVE
            assert state.consecutive_confirmations == 3
            assert state.last_alert_ts_ms is not None

            # Exactly one alert delivered
            assert len(notifier.signals) == 1
            assert "BTC_CARRY" in notifier.signals[0]
            assert _count(db_path, "alerts") == 1
        finally:
            database.close()

    @pytest.mark.asyncio
    async def test_bad_data_path(
        self, tmp_path, quality_params, signal_params, carry_instrument,
    ):
        """
        Market fetch fails -> DATA_INVALID, warning sent, no metrics.
        """
        now = utc_now_ms()
        client = MockExchangeClient(
            spot_error=ExchangeRequestError("spot down"),
            funding=_make_funding(now, Decimal("0.0001")),
        )
        notifier = FakeNotifier()
        app, db_path, database = _build_app(
            tmp_path, client, notifier,
            quality_params, signal_params, carry_instrument,
        )
        try:
            await app._process_symbol(
                instrument=carry_instrument, cycle_id="c-bad",
            )

            state = app._state_store.get("BTC_CARRY")
            assert state is not None
            assert state.state == SignalState.DATA_INVALID

            assert _count(db_path, "metrics") == 0
            assert _count(db_path, "signal_decisions") == 1
            # Quality warning delivered + saved
            assert len(notifier.warnings) == 1
            assert _count(db_path, "alerts") == 1
        finally:
            database.close()

    @pytest.mark.asyncio
    async def test_failed_notification_keeps_watching(
        self, tmp_path, quality_params, signal_params, carry_instrument,
    ):
        """
        Signal conditions met, but Telegram delivery FAILED
        -> state stays WATCHING with 'notification_failed' reason.
        """
        now = utc_now_ms()
        client = MockExchangeClient(
            spot=_make_spot(now),
            perp=_make_perp(now),
            funding=_make_funding(now, Decimal("0.0003")),
        )
        notifier = FakeNotifier(status=AlertDeliveryStatus.FAILED)
        app, db_path, database = _build_app(
            tmp_path, client, notifier,
            quality_params, signal_params, carry_instrument,
        )
        try:
            for i in range(1, 4):
                await app._process_symbol(
                    instrument=carry_instrument, cycle_id=f"c-{i}",
                )

            state = app._state_store.get("BTC_CARRY")
            assert state is not None
            assert state.state == SignalState.WATCHING
            assert "notification_failed" in state.last_reasons

            # Alert was attempted (3rd cycle) but saved as failed
            assert len(notifier.signals) == 1
            assert _count(db_path, "alerts") == 1
        finally:
            database.close()