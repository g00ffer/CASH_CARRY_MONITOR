#!/usr/bin/env python3
"""
Klines backfill через curl (устойчиво к middlebox/DPI).
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

DB = Path("data/klines.sqlite")
BASE = "https://api.binance.com/api/v3/klines"
INTERVAL = "1h"
MS_PER_CANDLE = 3_600_000
YEARS = 2
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT",
]


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS klines (
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            open_time_ms INTEGER NOT NULL,
            open REAL, high REAL, low REAL,
            close REAL, volume REAL,
            PRIMARY KEY (symbol, interval, open_time_ms)
        )
        """,
    )
    conn.commit()


def fetch_curl(symbol: str, start_ms: int, end_ms: int):
    """Fetch через curl — устойчиво к middlebox/DPI."""
    url = (
        f"{BASE}?symbol={symbol}&interval={INTERVAL}"
        f"&startTime={start_ms}&endTime={end_ms}&limit=1000"
    )
    for attempt in range(5):
        try:
            r = subprocess.run(
                ["curl", "-sS", "--max-time", "60", url],
                capture_output=True, text=True, timeout=70,
            )
            if r.returncode != 0:
                raise RuntimeError(f"curl exit {r.returncode}: {r.stderr}")
            return json.loads(r.stdout)
        except Exception as exc:
            print(f"  {symbol} retry {attempt + 1}: {exc}")
            time.sleep(3 * (attempt + 1))
    return []


def main() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    init_db(conn)

    now_ms = int(time.time() * 1000)
    start_all = now_ms - int(YEARS * 365.25 * 86400 * 1000)
    start_all -= start_all % MS_PER_CANDLE

    for sym in SYMBOLS:
        row = conn.execute(
            "SELECT MAX(open_time_ms) FROM klines "
            "WHERE symbol=? AND interval=?",
            (sym, INTERVAL),
        ).fetchone()
        cursor_ms = max(
            start_all, (row[0] + MS_PER_CANDLE) if row[0] else start_all,
        )
        total = 0
        while cursor_ms < now_ms:
            batch = fetch_curl(sym, cursor_ms, now_ms)
            if not batch:
                break
            conn.executemany(
                "INSERT OR IGNORE INTO klines VALUES (?,?,?,?,?,?,?,?)",
                [
                    (sym, INTERVAL, int(k[0]), float(k[1]),
                     float(k[2]), float(k[3]), float(k[4]), float(k[5]))
                    for k in batch
                ],
            )
            conn.commit()
            total += len(batch)
            cursor_ms = int(batch[-1][0]) + MS_PER_CANDLE
            time.sleep(0.3)
        print(f"{sym}: {total} candles")
        time.sleep(1)
    conn.close()
    print("✅ backfill done")


if __name__ == "__main__":
    main()
