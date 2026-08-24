from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from monitor.domain import (
    FundingSnapshot,
    PerpTicker,
    SpotTicker,
)


class ExchangeClientError(RuntimeError):
    """
    Base error for exchange client failures.
    """


class ExchangeRequestError(ExchangeClientError):
    """
    Network/API request error.
    """


class ExchangeDataError(ExchangeClientError):
    """
    Exchange returned invalid or incomplete data.
    """


class ExchangeClient(ABC):
    """
    Abstract exchange client.

    Stage 1 implements only Binance, but this interface allows adding
    Bybit/OKX later without rewriting signal/data layers.
    """

    @abstractmethod
    async def close(self) -> None:
        """
        Close underlying HTTP sessions.
        """

    @abstractmethod
    async def fetch_exchange_time_ms(self) -> int:
        """
        Fetch exchange server time in UTC milliseconds.
        """

    @abstractmethod
    async def fetch_spot_ticker(self, symbol: str) -> SpotTicker:
        """
        Fetch normalized spot ticker.

        Example:
            symbol = "BTC/USDT"
        """

    @abstractmethod
    async def fetch_perp_ticker(self, symbol: str) -> PerpTicker:
        """
        Fetch normalized perpetual ticker.

        Example:
            symbol = "BTC/USDT:USDT"
        """

    @abstractmethod
    async def fetch_funding_snapshot(
        self,
        *,
        cycle_id: str,
        symbol_name: str,
        perp_symbol: str,
        use_predicted_funding: bool,
        default_funding_interval_hours: Decimal,
    ) -> FundingSnapshot:
        """
        Fetch normalized funding snapshot for perpetual symbol.

        Example:
            perp_symbol = "BTC/USDT:USDT"
        """
    async def fetch_all_perp_tickers(self) -> list[PerpTicker]:
        """
        Fetch all perpetual tickers in one request (for universe selection).
        Default: not implemented; concrete clients override.
        """
        raise NotImplementedError(
            "fetch_all_perp_tickers is not implemented by this client",
        )