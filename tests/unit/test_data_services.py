"""Tests for monitor.data services with mock ExchangeClient"""
from __future__ import annotations

from decimal import Decimal

import pytest

from monitor.data import fetch_funding_snapshot, fetch_market_snapshot
from monitor.domain import (
    FundingSnapshot,
    MarketSnapshot,
    PerpTicker,
    SpotTicker,
)
from monitor.exchanges import ExchangeRequestError


# ======================================================================
# Mock ExchangeClient (implements the exact public async API)
# ======================================================================

class MockExchangeClient:
    """In-memory ExchangeClient for data service tests."""

    def __init__(
        self,
        *,
        spot: SpotTicker | None = None,
        perp: PerpTicker | None = None,
        funding: FundingSnapshot | None = None,
        spot_error: Exception | None = None,
        perp_error: Exception | None = None,
        funding_error: Exception | None = None,
        exchange_time_ms: int = 1710000001000,
    ) -> None:
        self._spot = spot
        self._perp = perp
        self._funding = funding
        self._spot_error = spot_error
        self._perp_error = perp_error
        self._funding_error = funding_error
        self._exchange_time_ms = exchange_time_ms
        self.spot_calls: list[str] = []
        self.perp_calls: list[str] = []
        self.funding_calls: list[dict] = []

    async def close(self) -> None:
        return None

    async def fetch_exchange_time_ms(self) -> int:
        return self._exchange_time_ms

    async def fetch_spot_ticker(self, symbol: str) -> SpotTicker:
        self.spot_calls.append(symbol)
        if self._spot_error is not None:
            raise self._spot_error
        assert self._spot is not None
        return self._spot

    async def fetch_perp_ticker(self, symbol: str) -> PerpTicker:
        self.perp_calls.append(symbol)
        if self._perp_error is not None:
            raise self._perp_error
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
        self.funding_calls.append(
            {
                "cycle_id": cycle_id,
                "symbol_name": symbol_name,
                "perp_symbol": perp_symbol,
                "use_predicted_funding": use_predicted_funding,
                "default_funding_interval_hours": (
                    default_funding_interval_hours
                ),
            },
        )
        if self._funding_error is not None:
            raise self._funding_error
        assert self._funding is not None
        return self._funding


# ======================================================================
# fetch_market_snapshot
# ======================================================================

class TestFetchMarketSnapshot:
    @pytest.mark.asyncio
    async def test_returns_combined_snapshot(
        self, carry_instrument, spot_ticker, perp_ticker,
    ):
        client = MockExchangeClient(spot=spot_ticker, perp=perp_ticker)
        result = await fetch_market_snapshot(
            client=client,
            instrument=carry_instrument,
            cycle_id="test-cycle",
            now_ms=1710000001000,
        )
        assert isinstance(result, MarketSnapshot)
        assert result.cycle_id == "test-cycle"
        assert result.symbol_name == carry_instrument.name
        assert result.spot == spot_ticker
        assert result.perp == perp_ticker
        assert result.received_at_ms > 0

    @pytest.mark.asyncio
    async def test_calls_correct_symbols(
        self, carry_instrument, spot_ticker, perp_ticker,
    ):
        client = MockExchangeClient(spot=spot_ticker, perp=perp_ticker)
        await fetch_market_snapshot(
            client=client,
            instrument=carry_instrument,
            cycle_id="test-cycle",
        )
        assert client.spot_calls == [carry_instrument.spot_symbol]
        assert client.perp_calls == [carry_instrument.perp_symbol]

    @pytest.mark.asyncio
    async def test_spot_failure_raises(
        self, carry_instrument, perp_ticker,
    ):
        client = MockExchangeClient(
            spot_error=ExchangeRequestError("spot down"),
            perp=perp_ticker,
        )
        with pytest.raises(ExchangeRequestError):
            await fetch_market_snapshot(
                client=client,
                instrument=carry_instrument,
                cycle_id="test-cycle",
            )

    @pytest.mark.asyncio
    async def test_perp_failure_raises(
        self, carry_instrument, spot_ticker,
    ):
        client = MockExchangeClient(
            perp_error=ExchangeRequestError("perp down"),
            spot=spot_ticker,
        )
        with pytest.raises(ExchangeRequestError):
            await fetch_market_snapshot(
                client=client,
                instrument=carry_instrument,
                cycle_id="test-cycle",
            )


# ======================================================================
# fetch_funding_snapshot
# ======================================================================

class TestFetchFundingSnapshot:
    @pytest.mark.asyncio
    async def test_returns_funding_snapshot(
        self, carry_instrument, funding_snapshot,
    ):
        client = MockExchangeClient(funding=funding_snapshot)
        result = await fetch_funding_snapshot(
            client=client,
            instrument=carry_instrument,
            cycle_id="test-cycle",
            use_predicted_funding=True,
            default_funding_interval_hours=Decimal("8"),
        )
        assert isinstance(result, FundingSnapshot)
        assert result.symbol_name == funding_snapshot.symbol_name
        assert len(client.funding_calls) == 1

    @pytest.mark.asyncio
    async def test_passes_correct_arguments(
        self, carry_instrument, funding_snapshot,
    ):
        client = MockExchangeClient(funding=funding_snapshot)
        await fetch_funding_snapshot(
            client=client,
            instrument=carry_instrument,
            cycle_id="test-cycle",
            use_predicted_funding=False,
            default_funding_interval_hours=Decimal("4"),
        )
        call = client.funding_calls[0]
        assert call["cycle_id"] == "test-cycle"
        assert call["symbol_name"] == carry_instrument.name
        assert call["perp_symbol"] == carry_instrument.perp_symbol
        assert call["use_predicted_funding"] is False
        assert call["default_funding_interval_hours"] == Decimal("4")

    @pytest.mark.asyncio
    async def test_funding_failure_raises(self, carry_instrument):
        client = MockExchangeClient(
            funding_error=ExchangeRequestError("funding down"),
        )
        with pytest.raises(ExchangeRequestError):
            await fetch_funding_snapshot(
                client=client,
                instrument=carry_instrument,
                cycle_id="test-cycle",
                use_predicted_funding=True,
                default_funding_interval_hours=Decimal("8"),
            )