from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from monitor.utils import ZERO


@dataclass(frozen=True, slots=True, kw_only=True)
class UniverseCandidate:
    """One candidate instrument with the metrics needed for selection."""
    symbol_name: str
    base: str
    quote: str
    perp_symbol: str
    predicted_funding_rate: Decimal   # decimal per funding interval
    quote_volume_24h: Decimal
    open_interest: Decimal | None = None
    spread: Decimal = ZERO            # decimal fraction


@dataclass(frozen=True, slots=True, kw_only=True)
class UniverseSelectorParams:
    max_active_symbols: int
    min_predicted_funding: Decimal      # decimal per interval
    min_quote_volume_24h: Decimal
    min_open_interest: Decimal | None   # None = не проверяем
    max_spread: Decimal                 # decimal fraction
    always_include: tuple[str, ...]
    weight_funding: float
    weight_liquidity: float

    def __post_init__(self) -> None:
        if self.max_active_symbols < 1:
            raise ValueError("max_active_symbols must be >= 1")
        if self.weight_funding + self.weight_liquidity <= 0:
            raise ValueError("score weights sum must be > 0")


@dataclass(frozen=True, slots=True, kw_only=True)
class UniverseSelectionResult:
    selected: tuple[str, ...]
    scores: dict[str, float]
    excluded: dict[str, str]


def _normalize(value: Decimal, max_value: Decimal) -> float:
    if max_value <= ZERO:
        return 0.0
    return float(min(value, max_value) / max_value)


def select_universe(
    candidates: list[UniverseCandidate],
    params: UniverseSelectorParams,
) -> UniverseSelectionResult:
    """
    Filter candidates, score the rest (funding + liquidity),
    take top-N, then force-include anchors.
    """
    excluded: dict[str, str] = {}
    eligible: list[UniverseCandidate] = []

    for c in candidates:
        if c.spread > params.max_spread:
            excluded[c.symbol_name] = "spread_too_wide"
            continue
        if c.quote_volume_24h < params.min_quote_volume_24h:
            excluded[c.symbol_name] = "low_liquidity"
            continue
        if (
            params.min_open_interest is not None
            and (c.open_interest is None
                 or c.open_interest < params.min_open_interest)
        ):
            excluded[c.symbol_name] = "low_open_interest"
            continue
        if c.predicted_funding_rate < params.min_predicted_funding:
            excluded[c.symbol_name] = "funding_below_minimum"
            continue
        eligible.append(c)

    max_funding = max(
        (c.predicted_funding_rate for c in eligible), default=ZERO,
    )
    max_volume = max(
        (c.quote_volume_24h for c in eligible), default=ZERO,
    )

    scores: dict[str, float] = {}
    for c in eligible:
        f_norm = _normalize(c.predicted_funding_rate, max_funding)
        v_norm = _normalize(c.quote_volume_24h, max_volume)
        scores[c.symbol_name] = (
            params.weight_funding * f_norm
            + params.weight_liquidity * v_norm
        )

    ranked = sorted(
        eligible, key=lambda c: scores[c.symbol_name], reverse=True,
    )
    selected: list[str] = []
    for c in ranked:
        if len(selected) >= params.max_active_symbols:
            excluded.setdefault(c.symbol_name, "not_in_top_n")
            break
        selected.append(c.symbol_name)

    # Якоря всегда присутствуют (если кандидата вообще нет — игнорируем)
    by_name = {c.symbol_name: c for c in candidates}
    for name in params.always_include:
        if name in by_name and name not in selected:
            selected.append(name)
            excluded.pop(name, None)

    return UniverseSelectionResult(
        selected=tuple(selected),
        scores=scores,
        excluded=excluded,
    )