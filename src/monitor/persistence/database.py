from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Sequence

from monitor.utils import utc_now_ms


class DatabaseError(RuntimeError):
    """
    Raised when SQLite operation fails.
    """


# ---------------------------------------------------------------------
# JSON encoder
# ---------------------------------------------------------------------


class JsonEncoder(json.JSONEncoder):
    """
    JSON encoder for SQLite payload columns.
    """

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return str(obj)

        if isinstance(obj, Enum):
            return obj.value

        if isinstance(obj, dt.datetime):
            return obj.isoformat()

        if isinstance(obj, dt.date):
            return obj.isoformat()

        if isinstance(obj, Exception):
            return repr(obj)

        if is_dataclass(obj):
            return asdict(obj)

        if isinstance(obj, bytes):
            return obj.decode("utf-8", errors="replace")

        try:
            return super().default(obj)
        except TypeError:
            return str(obj)


def to_json(value: Any) -> str:
    """
    Serialize value to JSON string using JsonEncoder.
    """

    return json.dumps(
        value,
        cls=JsonEncoder,
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------
# Database params
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class DatabaseParams:
    """
    SQLite persistence settings.
    """

    sqlite_path: str = "data/monitor.sqlite"
    save_raw_responses: bool = True
    retention_days: int = 90

    def __post_init__(self) -> None:
        if not self.sqlite_path.strip():
            raise ValueError("sqlite_path cannot be empty")

        if self.retention_days < 1:
            raise ValueError("retention_days must be >= 1")


# ---------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    symbol_name TEXT NOT NULL,
    received_at_ms INTEGER NOT NULL,
    spot_bid TEXT,
    spot_ask TEXT,
    perp_bid TEXT,
    perp_ask TEXT,
    payload TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_market_snapshots_cycle
    ON market_snapshots (cycle_id);

CREATE INDEX IF NOT EXISTS idx_market_snapshots_symbol_time
    ON market_snapshots (symbol_name, received_at_ms);


CREATE TABLE IF NOT EXISTS funding_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    symbol_name TEXT NOT NULL,
    received_at_ms INTEGER NOT NULL,
    effective_funding_rate TEXT NOT NULL,
    funding_interval_hours TEXT NOT NULL,
    next_funding_timestamp_ms INTEGER,
    payload TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_funding_snapshots_cycle
    ON funding_snapshots (cycle_id);

CREATE INDEX IF NOT EXISTS idx_funding_snapshots_symbol_time
    ON funding_snapshots (symbol_name, received_at_ms);


CREATE TABLE IF NOT EXISTS quality_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    symbol_name TEXT NOT NULL,
    checked_at_ms INTEGER NOT NULL,
    is_ok INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quality_reports_cycle
    ON quality_reports (cycle_id);

CREATE INDEX IF NOT EXISTS idx_quality_reports_symbol_time
    ON quality_reports (symbol_name, checked_at_ms);


CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    symbol_name TEXT NOT NULL,
    calculated_at_ms INTEGER NOT NULL,
    basis_entry TEXT,
    funding_annual TEXT,
    net_horizon TEXT,
    net_annual TEXT,
    payload TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_metrics_cycle
    ON metrics (cycle_id);

CREATE INDEX IF NOT EXISTS idx_metrics_symbol_time
    ON metrics (symbol_name, calculated_at_ms);


CREATE TABLE IF NOT EXISTS signal_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    symbol_name TEXT NOT NULL,
    decision_timestamp_ms INTEGER NOT NULL,
    state TEXT NOT NULL,
    should_alert INTEGER NOT NULL,
    consecutive_confirmations INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signal_decisions_cycle
    ON signal_decisions (cycle_id);

CREATE INDEX IF NOT EXISTS idx_signal_decisions_symbol_time
    ON signal_decisions (symbol_name, decision_timestamp_ms);


CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT UNIQUE NOT NULL,
    cycle_id TEXT NOT NULL,
    symbol_name TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    delivery_status TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    sent_at_ms INTEGER,
    message_payload TEXT,
    error_message TEXT,
    inserted_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_cycle
    ON alerts (cycle_id);

CREATE INDEX IF NOT EXISTS idx_alerts_symbol_time
    ON alerts (symbol_name, created_at_ms);
"""


class Database:
    """
    Simple SQLite wrapper for Stage 1.

    This implementation is synchronous.
    For Stage 1 monitoring workload this is acceptable.

    If later write volume grows, replace with aiosqlite or move writes
    into a background worker.
    """

    def __init__(self, params: DatabaseParams) -> None:
        self._params = params

        sqlite_path = Path(params.sqlite_path).expanduser()
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)

        self._connection = sqlite3.connect(
            str(sqlite_path),
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row

        self._lock = threading.Lock()

        self._init_schema()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._connection.commit()

    # ------------------------------------------------------------------
    # Execution helpers
    # ------------------------------------------------------------------

    def execute(
        self,
        query: str,
        params: Sequence[Any] = (),
    ) -> int:
        """
        Execute INSERT/UPDATE/DELETE query.

        Returns lastrowid.
        """

        try:
            with self._lock:
                cursor = self._connection.execute(query, tuple(params))
                self._connection.commit()
                return cursor.lastrowid or 0
        except sqlite3.Error as exc:
            raise DatabaseError(f"SQLite execute failed: {exc}") from exc

    def fetch_one(
        self,
        query: str,
        params: Sequence[Any] = (),
    ) -> dict[str, Any] | None:
        """
        Fetch one row as dict.
        """

        try:
            with self._lock:
                cursor = self._connection.execute(query, tuple(params))
                row = cursor.fetchone()

                if row is None:
                    return None

                return dict(row)
        except sqlite3.Error as exc:
            raise DatabaseError(f"SQLite fetch_one failed: {exc}") from exc

    def fetch_all(
        self,
        query: str,
        params: Sequence[Any] = (),
    ) -> list[dict[str, Any]]:
        """
        Fetch all rows as list of dicts.
        """

        try:
            with self._lock:
                cursor = self._connection.execute(query, tuple(params))
                rows = cursor.fetchall()

                return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise DatabaseError(f"SQLite fetch_all failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    def cleanup_old_records(
        self,
        retention_days: int | None = None,
    ) -> None:
        """
        Delete old records.

        Uses inserted/created timestamp columns.
        """

        days = retention_days or self._params.retention_days

        if days < 1:
            raise ValueError("retention_days must be >= 1")

        cutoff_ms = utc_now_ms() - days * 24 * 60 * 60 * 1000

        tables_and_columns: Iterable[tuple[str, str]] = (
            ("market_snapshots", "created_at_ms"),
            ("funding_snapshots", "created_at_ms"),
            ("quality_reports", "created_at_ms"),
            ("metrics", "created_at_ms"),
            ("signal_decisions", "created_at_ms"),
            ("alerts", "inserted_at_ms"),
        )

        for table_name, column_name in tables_and_columns:
            self.execute(
                f"DELETE FROM {table_name} WHERE {column_name} < ?",
                (cutoff_ms,),
            )
