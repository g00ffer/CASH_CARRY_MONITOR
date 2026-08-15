#!/usr/bin/env python3
"""Live klines updater: upsert последних 3 свечей каждые 5 минут."""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import httpx

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


async def main() -> None:
    conn = sqlite3.connect(DB, timeout=10)
    init_db(conn)
    print("klines collector started")

    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            started = time.monotonic()
            for sym in SYMBOLS:
                try:
                    r = await client.get(
                        BASE,
                        params={"symbol": sym, "interval": INTERVAL, "limit": 3},
                    )
                    r.raise_for_status()
                    conn.executemany(
                        "INSERT OR REPLACE INTO klines "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        [
                            (sym, INTERVAL, int(k[0]), float(k[1]),
                             float(k[2]), float(k[3]), float(k[4]), float(k[5]))
                            for k in r.json()
                        ],
                    )
                    conn.commit()
                except Exception as exc:
                    print(f"warn {sym}: {exc}")
            await asyncio.sleep(max(1.0, POLL_SEC - (time.monotonic() - started)))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("stopped")