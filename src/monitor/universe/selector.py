from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from monitor.domain import PerpTicker
from monitor.utils import ZERO


@dataclass(frozen=True, slots=True, kw_only=True)
class UniverseParams:
    """Пороги отбора пула (все rate/spread — decimal fractions)."""

    max_active_symbols: int
    min_funding_rate: Decimal
    min_quote_volume_24h: Decimal
    min_open_interest_usd: Decimal | None
    max_spread: Decimal
    always_include: tuple[str, ...]      # имена, напр. BTC_CARRY
    weight_funding: Decimal
    weight_liquidity: Decimal
    candidate_universe_size: int

    def __post_init__(self) -> None:
        if self.max_active_symbols < 1:
            raise ValueError("max_active_symbols must be >= 1")
        if self.candidate_universe_size < self.max_active_symbols:
            raise ValueError(
                "candidate_universe_size must be >= max_active_symbols"
            )
        if self.weight_funding + self.weight_liquidity <= ZERO:
            raise ValueError("score weights sum must be > 0")


@dataclass(frozen=True, slots=True, kw_only=True)
class UniverseCandidate:
    name: str
    base: str
    quote: str
    spot_symbol: str
    perp_symbol: str
    funding_rate: Decimal
    funding_interval_hours: Decimal
    quote_volume_24h: Decimal
    open_interest: Decimal | None
    spread: Decimal
    score: Decimal
    is_anchor: bool
    selected: bool


def split_perp_symbol(symbol: str) -> tuple[str, str] | None:
    """BTC/USDT:USDT -> (BTC, USDT)."""
    if ":" not in symbol:
        return None
    pair, _settle = symbol.split(":", 1)
    if "/" not in pair:
        return None
    base, quote = pair.split("/", 1)
    return base, quote


def candidate_spread(ticker: PerpTicker) -> Decimal | None:
    """Spread как decimal fraction: spread_abs / mid."""
    if ticker.mid_price <= ZERO:
        return None
    return ticker.spread_abs / ticker.mid_price


def filter_liquid_candidates(
    tickers: list[PerpTicker],
    params: UniverseParams,
) -> list[tuple[PerpTicker, Decimal]]:
    """(ticker, spread) прошедшие фильтры ликвидности, sort by volume desc."""
    out: list[tuple[PerpTicker, Decimal]] = []
    for ticker in tickers:
        if split_perp_symbol(ticker.symbol) is None:
            continue
        if ticker.quote_volume_24h is None:
            continue
        if ticker.quote_volume_24h < params.min_quote_volume_24h:
            continue
        if (
            params.min_open_interest_usd is not None
            and (
                ticker.open_interest is None
                or ticker.open_interest < params.min_open_interest_usd
            )
        ):
            continue
        spread = candidate_spread(ticker)
        if spread is None or spread > params.max_spread:
            continue
        out.append((ticker, spread))
    out.sort(key=lambda item: item[0].quote_volume_24h, reverse=True)
    return out


def score_candidates(
    candidates: list[UniverseCandidate],
    params: UniverseParams,
) -> list[UniverseCandidate]:
    """score = (w_f * norm(funding) + w_l * norm(volume)) / (w_f + w_l)."""
    if not candidates:
        return []
    max_funding = max((c.funding_rate for c in candidates), default=ZERO)
    max_volume = max((c.quote_volume_24h for c in candidates), default=ZERO)
    weight_sum = params.weight_funding + params.weight_liquidity
    scored: list[UniverseCandidate] = []
    for c in candidates:
        funding_norm = (
            c.funding_rate / max_funding if max_funding > ZERO else ZERO
        )
        volume_norm = (
            c.quote_volume_24h / max_volume if max_volume > ZERO else ZERO
        )
        score = (
            params.weight_funding * funding_norm
            + params.weight_liquidity * volume_norm
        ) / weight_sum
        scored.append(replace(c, score=score))
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored


def select_universe(
    candidates: list[UniverseCandidate],
    params: UniverseParams,
) -> list[UniverseCandidate]:
    """Топ-N по score + якоря всегда в пуле. Возвращает ВСЕх с флагом selected."""
    scored = score_candidates(candidates, params)
    selected_names: set[str] = set()
    for c in scored:
        if c.is_anchor:
            selected_names.add(c.name)
    remaining = max(0, params.max_active_symbols - len(selected_names))
    for c in scored:
        if remaining <= 0:
            break
        if c.name in selected_names:
            continue
        selected_names.add(c.name)
        remaining -= 1
    return [
        replace(c, selected=c.name in selected_names) for c in scored
    ]