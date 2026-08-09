from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Mapping

import ccxt.async_support as ccxt_async

from monitor.domain import (
    FundingSnapshot,
    PerpTicker,
    SpotTicker,
)
from monitor.utils import (
    ZERO,
    RetryPolicy,
    retry_async,
    to_decimal,
    utc_now_ms,
)

from .base import (
    ExchangeClient,
    ExchangeDataError,
    ExchangeRequestError,
)
from .normalizer import (
    normalize_funding_snapshot,
    normalize_perp_ticker,
    normalize_spot_ticker,
    parse_funding_interval_hours,
)


class BinanceClient(ExchangeClient):
    """
    Binance exchange client for Stage 1.

    Uses ccxt async REST API.

    Spot:
        BTC/USDT
        ETH/USDT

    USDⓈ-M linear perpetual:
        BTC/USDT:USDT
        ETH/USDT:USDT
    """

    def __init__(
        self,
        *,
        timeout_ms: int = 5000,
        retries: int = 3,
        retry_backoff_ms: int = 500,
        sandbox: bool = False,
        api_key: str | None = None,
        api_secret: str | None = None,
        max_weight_per_minute: int | None = None,
    ) -> None:
        self._timeout_ms = timeout_ms
        self._sandbox = sandbox
        self._api_key = api_key
        self._api_secret = api_secret
        self._max_weight_per_minute = max_weight_per_minute

        self._spot = self._create_exchange_client("spot")
        self._futures = self._create_exchange_client("swap")

        self._retry_policy = RetryPolicy(
            max_attempts=max(1, retries),
            initial_delay_ms=max(0, retry_backoff_ms),
            max_delay_ms=max(1000, retry_backoff_ms * 10),
            backoff_factor=2.0,
            jitter_ms=250,
        )

        self._markets_lock = asyncio.Lock()
        self._markets_loaded = False

        self._funding_interval_cache: dict[str, Decimal] = {}
        self._funding_info_lock = asyncio.Lock()
        self._funding_info_loaded = False

    # ------------------------------------------------------------------
    # Internal setup
    # ------------------------------------------------------------------

    def _create_exchange_client(self, default_type: str) -> ccxt_async.binance:
        config: dict[str, Any] = {
            "enableRateLimit": True,
            "timeout": self._timeout_ms,
            "options": {
                "defaultType": default_type,
                "recvWindow": 5000,
            },
        }

        if self._api_key:
            config["apiKey"] = self._api_key

        if self._api_secret:
            config["secret"] = self._api_secret

        client = ccxt_async.binance(config)

        if self._sandbox:
            client.set_sandbox_mode(True)

        return client

    async def _ensure_markets_loaded(self) -> None:
        async with self._markets_lock:
            if self._markets_loaded:
                return

            try:
                await retry_async(
                    self._spot.load_markets,
                    policy=self._retry_policy,
                )
                await retry_async(
                    self._futures.load_markets,
                    policy=self._retry_policy,
                )
            except Exception as exc:
                raise ExchangeRequestError(
                    "failed to load Binance markets",
                ) from exc

            self._markets_loaded = True

    def _require_positive_decimal(
        self,
        value: Decimal | int | float | str,
        field_name: str,
    ) -> Decimal:
        decimal_value = to_decimal(value)

        if decimal_value <= ZERO:
            raise ExchangeDataError(f"{field_name} must be positive")

        return decimal_value

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        await self._spot.close()
        await self._futures.close()

    # ------------------------------------------------------------------
    # Exchange time
    # ------------------------------------------------------------------

    async def fetch_exchange_time_ms(self) -> int:
        await self._ensure_markets_loaded()

        try:
            timestamp_ms = await retry_async(
                self._spot.fetch_time,
                policy=self._retry_policy,
            )
        except Exception as exc:
            raise ExchangeRequestError(
                "failed to fetch Binance exchange time",
            ) from exc

        if not isinstance(timestamp_ms, int) or timestamp_ms <= 0:
            raise ExchangeDataError("invalid Binance exchange time")

        return timestamp_ms

    # ------------------------------------------------------------------
    # Spot ticker
    # ------------------------------------------------------------------
    async def fetch_spot_ticker(self, symbol: str) -> SpotTicker:
        await self._ensure_markets_loaded()
        received_at_ms = utc_now_ms()
        try:
            raw = await retry_async(
                self._spot.fetch_ticker,
                symbol,
                policy=self._retry_policy,
            )
            # Binance 24hr ticker lacks bid/ask.
            # We fetch the order book to get the best bid/ask.
            ob = await retry_async(
                self._spot.fetch_order_book,
                symbol,
                10,
                policy=self._retry_policy,
            )
            if ob.get("bids") and ob["bids"][0]:
                raw["bid"] = ob["bids"][0][0]
                raw["bidVolume"] = ob["bids"][0][1]
            if ob.get("asks") and ob["asks"][0]:
                raw["ask"] = ob["asks"][0][0]
                raw["askVolume"] = ob["asks"][0][1]

            return normalize_spot_ticker(
                symbol=symbol,
                raw=raw,
                received_at_ms=received_at_ms,
            )
        except ExchangeDataError:
            raise
        except Exception as exc:
            raise ExchangeDataError(
                f"failed to normalize spot ticker for {symbol}",
            ) from exc

    # ------------------------------------------------------------------
    # Perpetual ticker
    # ------------------------------------------------------------------
    async def fetch_perp_ticker(self, symbol: str) -> PerpTicker:
        await self._ensure_markets_loaded()
        received_at_ms = utc_now_ms()
        try:
            raw = await retry_async(
                self._futures.fetch_ticker,
                symbol,
                policy=self._retry_policy,
            )
            # Binance 24hr ticker lacks bid/ask.
            ob = await retry_async(
                self._futures.fetch_order_book,
                symbol,
                10,
                policy=self._retry_policy,
            )
            if ob.get("bids") and ob["bids"][0]:
                raw["bid"] = ob["bids"][0][0]
                raw["bidVolume"] = ob["bids"][0][1]
            if ob.get("asks") and ob["asks"][0]:
                raw["ask"] = ob["asks"][0][0]
                raw["askVolume"] = ob["asks"][0][1]

            return normalize_perp_ticker(
                symbol=symbol,
                raw=raw,
                received_at_ms=received_at_ms,
            )
        except ExchangeDataError:
            raise
        except Exception as exc:
            raise ExchangeDataError(
                f"failed to normalize perp ticker for {symbol}",
            ) from exc

    # ------------------------------------------------------------------
    # Funding snapshot
    # ------------------------------------------------------------------

    async def fetch_funding_snapshot(
        self,
        *,
        cycle_id: str,
        symbol_name: str,
        perp_symbol: str,
        use_predicted_funding: bool,
        default_funding_interval_hours: Decimal,
    ) -> FundingSnapshot:
        await self._ensure_markets_loaded()

        try:
            raw_funding = await retry_async(
                self._futures.fetch_funding_rate,
                perp_symbol,
                policy=self._retry_policy,
            )
        except Exception as exc:
            raise ExchangeRequestError(
                f"failed to fetch funding rate for {perp_symbol}",
            ) from exc

        received_at_ms = utc_now_ms()

        funding_interval_hours = await self._resolve_funding_interval_hours(
            symbol=perp_symbol,
            raw_funding=raw_funding,
            default_funding_interval_hours=default_funding_interval_hours,
        )

        try:
            return normalize_funding_snapshot(
                cycle_id=cycle_id,
                symbol_name=symbol_name,
                perp_symbol=perp_symbol,
                raw_funding=raw_funding,
                received_at_ms=received_at_ms,
                default_funding_interval_hours=funding_interval_hours,
                use_predicted_funding=use_predicted_funding,
            )
        except ExchangeDataError:
            raise
        except Exception as exc:
            raise ExchangeDataError(
                f"failed to normalize funding snapshot for {perp_symbol}",
            ) from exc

    # ------------------------------------------------------------------
    # Funding interval resolution
    # ------------------------------------------------------------------

    async def _resolve_funding_interval_hours(
        self,
        *,
        symbol: str,
        raw_funding: Mapping[str, Any],
        default_funding_interval_hours: Decimal,
    ) -> Decimal:
        default_decimal = self._require_positive_decimal(
            default_funding_interval_hours,
            "default_funding_interval_hours",
        )

        # 1. Try funding response itself.
        parsed_from_response = parse_funding_interval_hours(raw_funding)

        if parsed_from_response is not None and parsed_from_response > ZERO:
            return parsed_from_response

        # 2. Try market info.
        market_id: str | None = None

        try:
            market = self._futures.market(symbol)
        except Exception:
            market = None

        if isinstance(market, Mapping):
            market_id = market.get("id")

            info = market.get("info")

            if isinstance(info, Mapping):
                parsed_from_market = parse_funding_interval_hours(
                    {"info": info},
                )

                if (
                    parsed_from_market is not None
                    and parsed_from_market > ZERO
                ):
                    return parsed_from_market

        # 3. Try Binance funding info endpoint cache.
        if market_id:
            await self._load_funding_info_once()

            cached_interval = self._funding_interval_cache.get(market_id)

            if cached_interval is not None and cached_interval > ZERO:
                return cached_interval

        return default_decimal

    async def _load_funding_info_once(self) -> None:
        async with self._funding_info_lock:
            if self._funding_info_loaded:
                return

            func = getattr(self._futures, "fapiPublicGetFundingInfo", None)

            if not callable(func):
                self._funding_info_loaded = True
                return

            try:
                raw = await retry_async(
                    func,
                    policy=self._retry_policy,
                )
            except Exception:
                # This endpoint is best-effort. If unavailable, fall back
                # to default funding interval.
                self._funding_info_loaded = True
                return

            items = raw

            if isinstance(raw, Mapping):
                items = raw.get("data") or raw.get("result") or []

            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, Mapping):
                        continue

                    symbol_id = item.get("symbol")
                    interval_hours = item.get("fundingIntervalHours")

                    if symbol_id is None or interval_hours is None:
                        continue

                    try:
                        decimal_interval = to_decimal(interval_hours)
                    except (TypeError, ValueError):
                        continue

                    if decimal_interval > ZERO:
                        self._funding_interval_cache[str(symbol_id)] = (
                            decimal_interval
                        )

            self._funding_info_loaded = True
