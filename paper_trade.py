#!/usr/bin/env python3
"""
Paper Trading simulator for cash-carry-monitor.

Replays historical signals (should_alert=True + delivered) and simulates
virtual long-spot/short-perp positions to estimate real PnL.

Run: python paper_trade.py
Output:
  - paper_trades.csv        (все сделки)
  - paper_trade_report.txt  (сводка)
"""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

DB_PATH = Path("data/monitor.sqlite")
SYMBOLS_PATH = Path("config/symbols.yaml")
OUTPUT_CSV = Path("paper_trades.csv")
REPORT_PATH = Path("paper_trade_report.txt")

HOLDING_HOURS = 720  # должно совпадать с settings.yaml
HOLDING_MS = HOLDING_HOURS * 3600 * 1000


@dataclass
class PaperTrade:
    symbol: str
    entry_time_ms: int
    entry_time_utc: str
    exit_time_ms: int
    exit_time_utc: str
    entry_funding_annual_pct: float
    entry_net_annual_pct: float
    notional_usd: float
    funding_income_usd: float
    one_time_costs_usd: float
    net_pnl_usd: float
    net_pnl_pct: float
    funding_payments_count: int
    status: str  # 'closed' | 'open'


def ms_to_utc(ms: int) -> str:
    return datetime.fromtimestamp(
        ms / 1000, tz=timezone.utc,
    ).strftime("%Y-%m-%d %H:%M:%S UTC")


def load_symbols_notional() -> dict[str, float]:
    """Загружает notional_usd из symbols.yaml."""
    with open(SYMBOLS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return {
        s["name"]: float(s.get("notional_usd", 10000))
        for s in data.get("symbols", [])
    }


def load_signals(conn: sqlite3.Connection) -> list[dict]:
    """
    Все моменты, когда бот реально отправил сигнал в Telegram.
    Это триггеры входа в виртуальную позицию.
    """
    cursor = conn.execute(
        """
        SELECT
            d.symbol_name,
            d.decision_timestamp_ms,
            d.cycle_id,
            CAST(m.funding_annual AS REAL) AS funding_annual,
            CAST(m.net_annual AS REAL) AS net_annual
        FROM signal_decisions d
        JOIN alerts a
          ON a.cycle_id = d.cycle_id
         AND a.symbol_name = d.symbol_name
        JOIN metrics m
          ON m.cycle_id = d.cycle_id
         AND m.symbol_name = d.symbol_name
        WHERE d.should_alert = 1
          AND a.alert_type = 'signal'
          AND a.delivery_status = 'sent'
        ORDER BY d.symbol_name, d.decision_timestamp_ms
        """,
    )
    cols = [c[0] for c in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def load_funding_settlements(
    conn: sqlite3.Connection,
    symbol: str,
    from_ms: int,
    to_ms: int,
) -> list[tuple[int, float, int]]:
    """
    Funding settlements для символа в [from_ms, to_ms].

    Дедупликация по next_funding_timestamp_ms (момент settlement).
    Для каждого уникального settlement берём effective_funding_rate
    из ПОСЛЕДНЕГО snapshot перед ним — это наиболее точная оценка
    ставки, применённой в этот момент (settled/predicted rate).

    Возвращает: (received_at_ms, rate, settlement_time_ms).
    """
    cursor = conn.execute(
        """
        SELECT
            received_at_ms,
            CAST(effective_funding_rate AS REAL) AS funding_rate,
            next_funding_timestamp_ms
        FROM funding_snapshots
        WHERE symbol_name = ?
          AND next_funding_timestamp_ms IS NOT NULL
          AND next_funding_timestamp_ms >= ?
          AND next_funding_timestamp_ms <= ?
          AND effective_funding_rate IS NOT NULL
        ORDER BY next_funding_timestamp_ms, received_at_ms DESC
        """,
        (symbol, from_ms, to_ms),
    )

    seen_next: set[int] = set()
    settlements: list[tuple[int, float, int]] = []
    for row in cursor:
        next_ms = row[2]
        if next_ms in seen_next:
            continue
        seen_next.add(next_ms)
        settlements.append((row[0], float(row[1]), next_ms))

    return settlements


def load_metrics_at(
    conn: sqlite3.Connection, symbol: str, target_ms: int,
) -> dict:
    """Ближайшая metrics-запись к target_ms."""
    cursor = conn.execute(
        """
        SELECT
            CAST(json_extract(payload, '$.one_time_costs') AS REAL),
            CAST(funding_annual AS REAL),
            CAST(net_annual AS REAL)
        FROM metrics
        WHERE symbol_name = ?
        ORDER BY ABS(calculated_at_ms - ?)
        LIMIT 1
        """,
        (symbol, target_ms),
    )
    row = cursor.fetchone()
    return {
        "one_time_costs": (
            float(row[0]) if row and row[0] is not None else 0.0025
        ),
        "funding_annual": (
            float(row[1]) if row and row[1] is not None else 0.0
        ),
        "net_annual": (
            float(row[2]) if row and row[2] is not None else 0.0
        ),
    }


def simulate_position(
    signal: dict,
    conn: sqlite3.Connection,
    notional: float,
    now_ms: int,
) -> PaperTrade:
    """Симулирует одну виртуальную позицию."""
    entry_ms = int(signal["decision_timestamp_ms"])
    exit_ms = entry_ms + HOLDING_MS

    metrics = load_metrics_at(conn,