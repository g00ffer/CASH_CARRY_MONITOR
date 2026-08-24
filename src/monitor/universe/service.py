from __future__ import annotations

import logging
from decimal import Decimal

from monitor.domain import CarryInstrument, PerpTicker
from monitor.domain.enums import StrategyDirection
from monitor.persistence import log_event
from monitor.utils import ZERO, utc_now_ms

from .selector import (
    UniverseCandidate,
    UniverseParams,
    candidate_spread,
    filter_liquid_candidates,
    select_universe,
    split_perp_symbol,
)


class UniverseService:
    """
    Обновление активного пула инструментов.
    Ошибки по отдельным инструментам не роняют обновление;
    ошибка fetch_all пробрасывается наружу (app держит старый пул).
    """

    def __init__(
        self,
        *,
        client,
        repository,
        params: UniverseParams,
        exchange_id: str,
        default_notional_usd: Decimal,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._repository = repository
        self._params = params
        self._exchange_id = exchange_id
        self._notional = default_notional_usd
        self._logger = logger or logging.getLogger("monitor.universe")

    async def refresh(
        self,
        *,
        cycle_id: str,
        now_ms: int | None = None,
    ) -> tuple[CarryInstrument, ...]:
        now = int(now_ms) if now_ms is not None else utc_now_ms()
        tickers = await self._client.fetch_all_perp_tickers()

        liquid = filter_liquid_candidates(tickers, self._params)
        liquid = liquid[: self._params.candidate_universe_size]

        anchor_bases: dict[str, str] = {}
        for name in self._params.always_include:
            anchor_bases[name.split("_")[0]] = name

        def base_of(t: PerpTicker) -> str | None:
            split = split_perp_symbol(t.symbol)
            return split[0] if split else None

        # кандидаты = ликвидные + якоря (даже если не прошли фильтры)
        cand_tickers: list[tuple[PerpTicker, Decimal | None, bool]] = []
        seen_bases: set[str] = set()
        for ticker, spread in liquid:
            base = base_of(ticker)
            if base is None:
                continue
            cand_tickers.append((ticker, spread, base in anchor_bases))
            seen_bases.add(base)
        for ticker in tickers:
            base = base_of(ticker)
            if base is None or base in seen_bases:
                continue
            if base in anchor_bases:
                cand_tickers.append(
                    (ticker, candidate_spread(ticker), True),
                )
                seen_bases.add(base)

        candidates: list[UniverseCandidate] = []
        for ticker, spread, is_anchor in cand_tickers:
            split = split_perp_symbol(ticker.symbol)
            if split is None:
                continue
            base, quote = split
            name = anchor_bases.get(base, f"{base}_CARRY")
            try:
                funding = await self._client.fetch_funding_snapshot(
                    cycle_id=cycle_id,
                    symbol_name=name,
                    perp_symbol=ticker.symbol,
                    use_predicted_funding=True,
                    default_funding_interval_hours=Decimal("8"),
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    event="universe_funding_fetch_failed",
                    level=logging.WARNING,
                    symbol_name=name,
                    payload={"error": str(exc)},
                )
                continue
            rate = funding.effective_funding_rate
            if not is_anchor and rate < self._params.min_funding_rate:
                continue
            candidates.append(
                UniverseCandidate(
                    name=name,
                    base=base,
                    quote=quote,
                    spot_symbol=f"{base}/{quote}",
                    perp_symbol=ticker.symbol,
                    funding_rate=rate,
                    funding_interval_hours=funding.funding_interval_hours,
                    quote_volume_24h=ticker.quote_volume_24h or ZERO,
                    open_interest=ticker.open_interest,
                    spread=spread or ZERO,
                    score=ZERO,
                    selected=False,
                    is_anchor=is_anchor,
                ),
            )

        scored = select_universe(candidates, self._params)

        try:
            self._repository.replace_active(scored, now)
            self._repository.append_history(scored, now)
        except Exception as exc:
            log_event(
                self._logger,
                event="universe_persist_failed",
                level=logging.WARNING,
                payload={"error": str(exc)},
            )

        instruments = tuple(
            self._to_instrument(c) for c in scored if c.selected
        )
        log_event(
            self._logger,
            event="universe_refreshed",
            payload={
                "selected": len(instruments),
                "candidates": len(scored),
                "names": [i.name for i in instruments],
            },
        )
        return instruments

    def _to_instrument(self, c: UniverseCandidate) -> CarryInstrument:
        return CarryInstrument(
            name=c.name,
            exchange=self._exchange_id,
            base=c.base,
            quote=c.quote,
            spot_symbol=c.spot_symbol,
            perp_symbol=c.perp_symbol,
            direction=StrategyDirection.LONG_SPOT_SHORT_PERP,
            notional_usd=self._notional,
            enabled=True,
        )