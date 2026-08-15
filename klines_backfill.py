#!/usr/bin/env python3
"""Backfill klines через Bybit linear (perpetual) — оконная пагинация."""
import sqlite3, subprocess, json, time
from pathlib import Path

DB = Path("data/klines.sqlite")
URL = "https://api.bybit.com/v5/market/kline"
SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","AVAXUSDT"]

def init_db(c):
    c.execute("""CREATE TABLE IF NOT EXISTS klines(
        symbol TEXT, interval TEXT, open_time_ms INTEGER,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY(symbol,interval,open_time_ms))""")
    c.commit()

def fetch_batch(sym, end_ms):
    """Запрашивает 1000 свечей, заканчивающихся на end_ms."""
    u = (f"{URL}?category=linear&symbol={sym}&interval=60"
         f"&limit=1000&end={end_ms}")
    for a in range(7):
        try:
            r = subprocess.run(["curl","-sS","--max-time","30",u],
                             capture_output=True, text=True, timeout=35)
            if r.returncode != 0:
                raise RuntimeError(f"exit {r.returncode}")
            d = json.loads(r.stdout)
            if d.get("retCode") != 0:
                raise RuntimeError(f"retCode={d.get('retCode')}")
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
    
    print(f"Loading 2 years of 1h candles for {len(SYMBOLS)} symbols...")
    print(f"Target: {time.strftime('%Y-%m-%d', time.gmtime(target_ms/1000))} → now")
    
    for sym in SYMBOLS:
        tot, pages = 0, 0
        end_ms = now_ms
        
        while end_ms > target_ms:
            cs = fetch_batch(sym, end_ms)
            if not cs:
                break
            c.executemany("INSERT OR IGNORE INTO klines VALUES(?,?,?,?,?,?,?,?)",
                         [(sym,"1h",*x) for x in cs])
            c.commit()
            tot += len(cs); pages += 1
            
            # сдвигаем окно: end становится временем самой старой свечи в батче
            oldest = min(x[0] for x in cs)
            end_ms = oldest - 1
            
            oldest_date = time.strftime('%Y-%m-%d', time.gmtime(oldest/1000))
            if pages % 5 == 0:
                print(f"  {sym}: {tot} candles, oldest={oldest_date}")
            
            time.sleep(0.3)
        
        print(f"{sym}: {tot} candles ({pages} pages)")
        time.sleep(2)  # пауза между символами (защита от DNS)
    
    c.close(); print("done")

if __name__ == "__main__":
    print("starting windowed backfill (linear category)...")
    main()