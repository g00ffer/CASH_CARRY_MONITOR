#!/usr/bin/env python3
"""Live klines через Bybit API (уже работает для funding)."""
import asyncio
import json
import sqlite3
import subprocess
import time
from pathlib import Path

DB = Path("data/klines.sqlite")
BYBIT_URL = "https://api.bybit.com/v5/market/kline"
INTERVAL = "60"  # 1 hour в минутах
POLL_SEC = 300
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
           "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT"]

def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS klines (
            symbol TEXT, interval TEXT, open_time_ms INTEGER,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (symbol, interval, open_time_ms)
        )
    """)
    conn.commit()

def fetch_bybit(sym):
    url = f"{BYBIT_URL}?category=spot&symbol={sym}&interval={INTERVAL}&limit=3"
    try:
        r = subprocess.run(
            ["curl", "-sS", "--max-time", "30", url],
            capture_output=True, text=True, timeout=35
        )
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        if data.get("retCode") != 0:
            return None
        # Bybit возвращает [start, open, high, low, close, volume, ...]
        return [
            (int(k[0]), float(k[1]), float(k[2]), 
             float(k[3]), float(k[4]), float(k[5]))
            for k in data["result"]["list"]
        ]
    except Exception as e:
        print(f"warn {sym}: {e}")
        return None

async def main():
    conn = sqlite3.connect(DB, timeout=10)
    init_db(conn)
    print("klines collector started (Bybit)")
    
    while True:
        started = time.monotonic()
        for sym in SYMBOLS:
            candles = fetch_bybit(sym)
            if not candles:
                continue
            conn.executemany(
                "INSERT OR REPLACE INTO klines VALUES (?,?,?,?,?,?,?,?)",
                [(sym, "1h", *c) for c in candles]
            )
            conn.commit()
        await asyncio.sleep(max(1.0, POLL_SEC - (time.monotonic() - started)))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("stopped")