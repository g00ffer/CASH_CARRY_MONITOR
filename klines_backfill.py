#!/usr/bin/env python3
"""Backfill через data.binance.vision (официальный архив Binance)."""
import sqlite3
import subprocess
import zipfile
import io
import time
from pathlib import Path
from datetime import datetime, timedelta

DB = Path("data/klines.sqlite")
BASE_URL = "https://data.binance.vision/data/spot/monthly/klines"
INTERVAL = "1h"
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

def download_month(symbol, year, month):
    url = f"{BASE_URL}/{symbol}/{INTERVAL}/{symbol}-{INTERVAL}-{year}-{month:02d}.zip"
    try:
        r = subprocess.run(
            ["curl", "-sS", "--max-time", "120", "-L", url],
            capture_output=True, timeout=130
        )
        if r.returncode != 0 or not r.stdout:
            return None
        with zipfile.ZipFile(io.BytesIO(r.stdout)) as z:
            csv_name = z.namelist()[0]
            return z.read(csv_name).decode('utf-8')
    except Exception as e:
        print(f"  {symbol} {year}-{month:02d}: {e}")
        return None

def main():
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    init_db(conn)
    
    end = datetime.now()
    start = end - timedelta(days=730)  # 2 года
    
    for sym in SYMBOLS:
        total = 0
        current = start.replace(day=1)
        while current <= end:
            csv = download_month(sym, current.year, current.month)
            if csv:
                rows = []
                for line in csv.strip().split('\n'):
                    parts = line.split(',')
                    if len(parts) >= 6:
                        rows.append((
                            sym, INTERVAL, int(parts[0]),
                            float(parts[1]), float(parts[2]),
                            float(parts[3]), float(parts[4]), float(parts[5])
                        ))
                if rows:
                    conn.executemany(
                        "INSERT OR IGNORE INTO klines VALUES (?,?,?,?,?,?,?,?)",
                        rows
                    )
                    conn.commit()
                    total += len(rows)
            current = (current + timedelta(days=32)).replace(day=1)
            time.sleep(0.5)
        print(f"{sym}: {total} candles")
    conn.close()
    print("✅ backfill done")

if __name__ == "__main__":
    main()