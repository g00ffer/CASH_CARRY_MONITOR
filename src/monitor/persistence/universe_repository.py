from __future__ import annotations

from typing import Sequence

from monitor.utils import utc_now_ms

from .database import Database


class UniverseRepository:
    """
    Repository for dynamic universe pool and its history.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def replace_active(
        self,
        rows: Sequence[dict],
        refreshed_at_ms: int | None = None,
    ) -> None:
        """
        Replace active_universe contents atomically.
        rows: dicts with keys instrument_name, symbol, score,
        is_anchor, quote_volume_24h, funding_rate.
        """
        now_ms = refreshed_at_ms or utc_now_ms()
        self._db.execute("DELETE FROM active_universe")
        for row in rows:
            self._db.execute(
                """
                INSERT INTO active_universe (
                    instrument_name, symbol, score, is_anchor,
                    quote_volume_24h, funding_rate, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["instrument_name"],
                    row["symbol"],
                    row.get("score"),
                    int(row.get("is_anchor", False)),
                    row.get("quote_volume_24h"),
                    row.get("funding_rate"),
                    now_ms,
                ),
            )

    def load_active(self) -> list[dict]:
        return self._db.fetch_all(
            """
            SELECT instrument_name, symbol, score, is_anchor,
                   quote_volume_24h, funding_rate, updated_at_ms
            FROM active_universe
            ORDER BY score DESC
            """,
        )

    def add_history(self, rows: Sequence[dict], refreshed_at_ms: int) -> None:
        for row in rows:
            self._db.execute(
                """
                INSERT INTO universe_history (
                    refreshed_at_ms, instrument_name, symbol,
                    included, is_anchor, score,
                    quote_volume_24h, funding_rate, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    refreshed_at_ms,
                    row["instrument_name"],
                    row["symbol"],
                    int(row.get("included", False)),
                    int(row.get("is_anchor", False)),
                    row.get("score"),
                    row.get("quote_volume_24h"),
                    row.get("funding_rate"),
                    row.get("reason"),
                ),
            )