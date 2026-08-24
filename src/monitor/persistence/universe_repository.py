from __future__ import annotations

from dataclasses import asdict
from typing import Sequence

from monitor.utils import utc_now_ms

from .database import Database, to_json


class UniverseRepository:
    """Хранение активного пула и истории отбора."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def replace_active(self, candidates: Sequence, now_ms: int) -> None:
        self._db.execute("DELETE FROM active_universe")
        for c in candidates:
            if not c.selected:
                continue
            self._db.execute(
                """
                INSERT INTO active_universe (
                    symbol_name, score, funding_rate, quote_volume_24h,
                    open_interest, spread, is_anchor, refreshed_at_ms,
                    payload, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    c.name,
                    str(c.score),
                    str(c.funding_rate),
                    str(c.quote_volume_24h),
                    (
                        str(c.open_interest)
                        if c.open_interest is not None
                        else None
                    ),
                    str(c.spread),
                    int(c.is_anchor),
                    now_ms,
                    to_json(asdict(c)),
                    utc_now_ms(),
                ),
            )

    def append_history(self, candidates: Sequence, now_ms: int) -> None:
        for c in candidates:
            self._db.execute(
                """
                INSERT INTO universe_history (
                    refreshed_at_ms, symbol_name, score, funding_rate,
                    quote_volume_24h, is_anchor, selected, payload,
                    created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_ms,
                    c.name,
                    str(c.score),
                    str(c.funding_rate),
                    str(c.quote_volume_24h),
                    int(c.is_anchor),
                    int(c.selected),
                    to_json(asdict(c)),
                    utc_now_ms(),
                ),
            )