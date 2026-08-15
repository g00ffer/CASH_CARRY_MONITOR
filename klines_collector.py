#!/usr/bin/env python3
"""Live klines updater через curl."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import time
from pathlib import Path

DB = Path("data/klines.sqlite")
BASE = "https://api.binance.com/api/v3/klines"
INTERVAL = "1h"
POLL_SEC = 300
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


def fetch_curl(sym: str):
    url = f"{BASE}?symbol={sym}&interval={INTERVAL}&limit=3"
    try:
        r = subprocess.run(
            ["curl", "-sS", "--max-time", "30", url],
            capture_output=True, text=True, timeout=35,
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except Exception as exc:
        print(f"warn {sym}: {exc}")
        return None


async def main() -> None:
    conn = sqlite3.connect(DB, timeout=10)
    init_db(conn)
    print("klines collector started (curl mode)")

    while True:
        started = time.monotonic()
        for sym in SYMBOLS:
            batch = fetch_curl(sym)
            if not batch:
                continue
            conn.executemany(
                "INSERT OR REPLACE INTO klines "
                "VALUES (?,?,?,?,?,?,?,?)",
                [
                    (sym, INTERVAL, int(k[0]), float(k[1]),
                     float(k[2]), float(k[3]), float(k[4]), float(k[5]))
                    for k in batch
                ],
            )
            conn.commit()
        await asyncio.sleep(max(1.0, POLL_SEC - (time.monotonic() - started)))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("stopped")