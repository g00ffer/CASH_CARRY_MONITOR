#!/usr/bin/env python3
"""Backfill 1h klines для 25 монет через Bybit linear."""
import sqlite3, subprocess, json, time
from pathlib import Path

DB = Path("data/klines_extended.sqlite")
URL = "https://api.bybit.com/v5/market/kline"
SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "DOGEUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","UNIUSDT",
    "ATOMUSDT","NEARUSDT","APTUSDT","ARBUSDT","OPUSDT",
    "MATICUSDT","FILUSDT","LTCUSDT","DOTUSDT","TRXUSDT",
    "SHIBUSDT","PEPEUSDT","FLOKIUSDT","INJUSDT","TIAUSDT"
]

def init_db(c):
    c.execute("""CREATE TABLE IF NOT EXISTS klines(
        symbol TEXT, interval TEXT, open_time_ms INTEGER,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY(symbol,interval,open_time_ms))""")
    c.commit()

def fetch_batch(sym, end_ms):
    u = f"{URL}?category=linear&symbol={sym}&interval=60&limit=1000&end={end_ms}"
    for a in range(7):
        try:
            r = subprocess.run(["curl","-sS","--max-time","30",u],
                             capture_output=True, text=True, timeout=35)
            if r.returncode != 0: raise RuntimeError(f"exit {r.returncode}")
            d = json.loads(r.stdout)
            if d.get("retCode") != 0: raise RuntimeError(f"retCode={d.get('retCode')}")
            return [(int(k[0]),float(k[1]),float(k[2]),float(k[3]),float(k[4]),float(k[5]))
                    for k in d["result"]["list"]]
        except Exception as x:
            print(f"    {sym} retry {a+1}: {x}")
            time.sleep(3 * (a+1))
    return []

def main():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB); init_db(c)
    now_ms = int(time.time() * 1000)
    target_ms = int((time.time() - 2*365.25*86400)*1000)
    print(f"Loading 1h candles for {len(SYMBOLS)} symbols...")
    for sym in SYMBOLS:
        tot, pages, end_ms = 0, 0, now_ms
        while end_ms > target_ms:
            cs = fetch_batch(sym, end_ms)
            if not cs: break
            c.executemany("INSERT OR IGNORE INTO klines VALUES(?,?,?,?,?,?,?,?)",
                         [(sym,"1h",*x) for x in cs])
            c.commit()
            tot += len(cs); pages += 1
            end_ms = min(x[0] for x in cs) - 1
            time.sleep(0.3)
        print(f"{sym}: {tot} candles ({pages} pages)")
        time.sleep(1)
    c.close(); print("done")

if __name__ == "__main__":
    print("starting extended klines backfill...")
    main()
