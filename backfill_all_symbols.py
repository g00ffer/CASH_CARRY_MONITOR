#!/usr/bin/env python3
"""
Загрузка исторических klines для всех ликвидных монет Bybit.

Шаги:
1. Запрос всех USDT-перпетуалов с Bybit (~447)
2. Фильтрация по объёму торгов за 24h (> MIN_VOLUME_USD)
3. Загрузка 2 лет часовых свечей для каждого символа
"""
import sqlite3
import subprocess
import json
import time
from pathlib import Path
from typing import List, Dict

DB = Path("data/klines_all.sqlite")
URL = "https://api.bybit.com/v5/market"
MIN_VOLUME_USD = 5_000_000  # минимум $5M объёма за 24h


def http_get(url: str) -> dict:
    """Простой HTTP GET через curl."""
    result = subprocess.run(
        ["curl", "-sS", "--max-time", "30", url],
        capture_output=True, text=True, timeout=35
    )
    if result.returncode != 0:
        raise RuntimeError(f"HTTP request failed: {result.returncode}")
    return json.loads(result.stdout)


def fetch_all_linear_symbols() -> List[str]:
    """Запрашивает все USDT-перпетуалы с Bybit."""
    url = f"{URL}/instruments-info?category=linear&limit=1000"
    data = http_get(url)
    
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit API error: {data.get('retMsg')}")
    
    symbols = []
    for item in data["result"]["list"]:
        symbol = item["symbol"]
        status = item["status"]
        if symbol.endswith("USDT") and status == "Trading":
            symbols.append(symbol)
    
    return sorted(symbols)


def fetch_24h_volumes(symbols: List[str]) -> Dict[str, float]:
    """Запрашивает объём торгов за 24h одним запросом."""
    url = f"{URL}/tickers?category=linear"
    data = http_get(url)
    
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit API error: {data.get('retMsg')}")
    
    volumes = {}
    for item in data["result"]["list"]:
        symbol = item["symbol"]
        if symbol in symbols:
            turnover = float(item.get("turnover24h", 0))
            volumes[symbol] = turnover
    
    return volumes


def init_db(c):
    c.execute("""CREATE TABLE IF NOT EXISTS klines(
        symbol TEXT, interval TEXT, open_time_ms INTEGER,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY(symbol,interval,open_time_ms))""")
    c.commit()


def fetch_batch(sym: str, end_ms: int):
    """Запрашивает 1000 часовых свечей, заканчивающихся на end_ms."""
    u = f"{URL}/kline?category=linear&symbol={sym}&interval=60&limit=1000&end={end_ms}"
    for a in range(7):
        try:
            r = subprocess.run(["curl", "-sS", "--max-time", "30", u],
                               capture_output=True, text=True, timeout=35)
            if r.returncode != 0:
                raise RuntimeError(f"exit {r.returncode}")
            d = json.loads(r.stdout)
            if d.get("retCode") != 0:
                raise RuntimeError(f"retCode={d.get('retCode')}")
            return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                     float(k[4]), float(k[5])) for k in d["result"]["list"]]
        except Exception as x:
            print(f"    {sym} retry {a+1}: {x}")
            time.sleep(3 * (a + 1))
    return []


def main():
    print("=" * 85)
    print("Загрузка klines для всех ликвидных монет Bybit")
    print("=" * 85)
    
    # Шаг 1: запрос всех символов
    print("\n[1/3] Fetching all linear symbols from Bybit...")
    all_symbols = fetch_all_linear_symbols()
    print(f"      Found {len(all_symbols)} USDT perpetuals")
    
    # Шаг 2: фильтрация по ликвидности
    print(f"\n[2/3] Filtering by liquidity (> ${MIN_VOLUME_USD:,} / 24h)...")
    volumes = fetch_24h_volumes(all_symbols)
    liquid_symbols = [s for s in all_symbols if volumes.get(s, 0) >= MIN_VOLUME_USD]
    print(f"      {len(liquid_symbols)} symbols passed liquidity filter")
    
    # Шаг 3: загрузка данных
    print(f"\n[3/3] Loading 2 years of 1h candles for {len(liquid_symbols)} symbols...")
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    init_db(c)
    
    now_ms = int(time.time() * 1000)
    target_ms = int((time.time() - 2 * 365.25 * 86400) * 1000)
    
    for i, sym in enumerate(liquid_symbols):
        tot, pages, end_ms = 0, 0, now_ms
        
        while end_ms > target_ms:
            cs = fetch_batch(sym, end_ms)
            if not cs:
                break
            c.executemany("INSERT OR IGNORE INTO klines VALUES(?,?,?,?,?,?,?,?)",
                          [(sym, "1h", *x) for x in cs])
            c.commit()
            tot += len(cs)
            pages += 1
            end_ms = min(x[0] for x in cs) - 1
            time.sleep(0.3)
        
        if (i + 1) % 10 == 0:
            print(f"      [{i+1}/{len(liquid_symbols)}] {sym}: {tot} candles")
        
        time.sleep(1)
    
    c.close()
    print("\nDone!")


if __name__ == "__main__":
    main()