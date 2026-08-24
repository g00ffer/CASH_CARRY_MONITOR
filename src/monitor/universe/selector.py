from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from monitor.domain import CarryInstrument
from monitor.domain.enums import StrategyDirection
from monitor.exchanges import ExchangeClient, ExchangeClientError
from monitor.utils import ZERO, utc_now_ms


@dataclass(frozen=True, slots=True, kw_only=True)
class UniverseSelectorParams:
    """
    Universe selection parameters (built from settings.universe).
    All rate/spread/volume values are decimal fractions / absolute units.
    """

    exchange_id: str
    refresh_interval_hours: int
    max_active_symbols: int
    min_funding_rate_per_interval: Decimal
    min_quote_volume_24h: Decimal
    max_spread: Decimal
    always_include: tuple[str, ...]
    weight_funding: float
    weight_liquidity: float
    candidate_universe_size: int
    default_notional_usd: Decimal

    def __post_init__(self) -> None:
        if self.max_active_symbols < 1:
            raise ValueError("max_active_symbols must be >= 1")
        if self.candidate_universe_size < 1:
            raise ValueError("candidate_universe_size must be >= 1")
        if self.weight_funding + self.weight_liquidity <= 0:
            raise ValueError("score weights sum must be > 0")


@dataclass(frozen=True, slots=True, kw_only=True)
class UniverseCandidate:
    name: str
    symbol: str
    base: str
    quote: str
    quote_volume_24h: Decimal
    spread: Decimal
    funding_rate: Decimal
    funding_interval_hours: Decimal
    score: float
    is_anchor: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class UniverseSelection:
    refreshed_at_ms: int
    included: tuple[UniverseCandidate, ...]
    # (instrument_name, reason) for audit/logging
    excluded: tuple[tuple[str, str], ...]


def _base_from_name(name: str) -> str:
    if name.endswith("_CARRY"):
        return name[: -len("_CARRY")]
    return name


async def select_universe(
    *,
    client: ExchangeClient,
    params: UniverseSelectorParams,
    default_funding_interval_hours: Decimal = Decimal("8"),
    now_ms: int | None = None,
) -> UniverseSelection:
    """
    Select dynamic universe:
    1. all USDT linear perps, liquid by 24h volume and spread;
    2. top candidate_universe_size by volume;
    3. funding enrichment + minimum funding filter;
    4. score = w_funding * funding_norm + w_liquidity * liquidity_norm;
    5. final pool = anchors (always_include) + top scored up to
       max_active_symbols.
    """
    now = int(now_ms) if now_ms is not None else utc_now_ms()
    excluded: list[tuple[str, str]] = []

    tickers = await client.fetch_all_perp_tickers()
    by_symbol = {t.symbol: t for t in tickers}

    # ------------------------------------------------------------------
    # 1-2. Liquid candidates, top by volume
    # ------------------------------------------------------------------
    filtered: list[tuple] = []
    for t in tickers:
        if not t.symbol.endswith(":USDT"):
            continue
        base_quote = t.symbol.split(":", 1)[0]
        if "/" not in base_quote:
            continue
        base, quote = base_quote.split("/", 1)
        if quote != "USDT":
            continue
        if (
            t.quote_volume_24h is None
            or t.quote_volume_24h < params.min_quote_volume_24h
        ):
            continue
        spread = (
            t.spread_abs / t.mid_price if t.mid_price > ZERO else Decimal("1")
        )
        if spread > params.max_spread:
            continue
        filtered.append((t, base, quote, spread))

    filtered.sort(key=lambda x: x[0].quote_volume_24h, reverse=True)
    filtered = filtered[: params.candidate_universe_size]

    # ------------------------------------------------------------------
    # 3. Funding enrichment + minimum funding filter
    # ------------------------------------------------------------------
    enriched: list[dict] = []
    for t, base, quote, spread in filtered:
        name = f"{base}_CARRY"
        try:
            fs = await client.fetch_funding_snapshot(
                cycle_id=f"universe-{now}",
                symbol_name=name,
                perp_symbol=t.symbol,
                use_predicted_funding=True,
                default_funding_interval_hours=default_funding_interval_hours,
            )
        except ExchangeClientError as exc:
            excluded.append((name, f"funding_fetch_failed:{exc}"))
            continue
        rate = fs.effective_funding_rate
        if rate < params.min_funding_rate_per_interval:
            excluded.append((name, "funding_below_minimum"))
            continue
        enriched.append(
            {
                "name": name,
                "symbol": t.symbol,
                "base": base,
                "quote": quote,
                "quote_volume_24h": t.quote_volume_24h,
                "spread": spread,
                "funding_rate": rate,
                "funding_interval_hours": fs.funding_interval_hours,
            },
        )

    # ------------------------------------------------------------------
    # 4. Scoring
    # ------------------------------------------------------------------
    if enriched:
        max_funding = max(c["funding_rate"] for c in enriched)
        vols = [float(c["quote_volume_24h"]) for c in enriched]
        max_vol, min_vol = max(vols), min(vols)
        log_span = math.log(max_vol / min_vol) if max_vol > min_vol else 1.0
        w_sum = params.weight_funding + params.weight_liquidity
        for c in enriched:
            f_norm = (
                float(c["funding_rate"] / max_funding)
                if max_funding > ZERO
                else 0.0
            )
            l_norm = (
                math.log(float(c["quote_volume_24h"]) / min_vol) / log_span
                if log_span > 0 and min_vol > 0
                else 1.0
            )
            c["score"] = (
                params.weight_funding * f_norm
                + params.weight_liquidity * l_norm
            ) / w_sum
    enriched.sort(key=lambda c: c["score"], reverse=True)

    # ------------------------------------------------------------------
    # 5. Anchors + top scored
    # ------------------------------------------------------------------
    selected: list[UniverseCandidate] = []
    seen: set[str] = set()

    for name in params.always_include:
        base = _base_from_name(name)
        symbol = f"{base}/USDT:USDT"
        t = by_symbol.get(symbol)
        if t is None:
            excluded.append((name, "symbol_not_listed"))
            continue
        try:
            fs = await client.fetch_funding_snapshot(
                cycle_id=f"universe-{now}",
                symbol_name=name,
                perp_symbol=symbol,
                use_predicted_funding=True,
                default_funding_interval_hours=default_funding_interval_hours,
            )
            rate, interval = fs.effective_funding_rate, fs.funding_interval_hours
        except ExchangeClientError:
            rate, interval = ZERO, default_funding_interval_hours
        selected.append(
            UniverseCandidate(
                name=name,
                symbol=symbol,
                base=base,
                quote="USDT",
                quote_volume_24h=t.quote_volume_24h or ZERO,
                spread=(
                    t.spread_abs / t.mid_price if t.mid_price > ZERO else ZERO
                ),
                funding_rate=rate,
                funding_interval_hours=interval,
                score=1.0,
                is_anchor=True,
            ),
        )
        seen.add(name)

    for c in enriched:
        if len(selected) >= params.max_active_symbols:
            excluded.append((c["name"], "not_in_top_n"))
            continue
        if c["name"] in seen:
            continue
        selected.append(
            UniverseCandidate(
                name=c["name"],
                symbol=c["symbol"],
                base=c["base"],
                quote=c["quote"],
                quote_volume_24h=c["quote_volume_24h"],
                spread=c["spread"],
                funding_rate=c["funding_rate"],
                funding_interval_hours=c["funding_interval_hours"],
                score=c["score"],
                is_anchor=False,
            ),
        )
        seen.add(c["name"])

    return UniverseSelection(
        refreshed_at_ms=now,
        included=tuple(selected),
        excluded=tuple(excluded),
    )


def build_instruments(
    selection: UniverseSelection,
    params: UniverseSelectorParams,
) -> tuple[CarryInstrument, ...]:
    """
    Convert selected candidates into CarryInstrument objects.
    """
    return tuple(
        CarryInstrument(
            name=c.name,
            exchange=params.exchange_id,
            base=c.base,
            quote=c.quote,
            spot_symbol=f"{c.base}/{c.quote}",
            perp_symbol=c.symbol,
            direction=StrategyDirection.LONG_SPOT_SHORT_PERP,
            notional_usd=params.default_notional_usd,
            enabled=True,
        )
        for c in selection.included
    )