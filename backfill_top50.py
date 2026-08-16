#!/usr/bin/env python3
"""
backfill_top50.py — Загрузка 2 лет часовых свечей для top-50 ликвидных
USDT-перпетуалов Bybit.

Архитектура:
1. Запрос всех USDT-перпетуалов через Bybit API (instruments-info)
2. Запрос turnover24h для всех (tickers)
3. Сортировка по ликвидности, берём top-50
4. Последовательная загрузка 18000 часовых свечей на каждую монету
5. Запись в data/klines_top50.sqlite

Время: ~5-10 минут на 50 монет.
"""
import sqlite3
import subprocess
import json
import time
from pathlib import Path

DB = Path("data/klines_top50.sqlite")
API = "https://api.bybit.com/v5/market"
TOP_N = 50
HISTORY_DAYS = 2 * 365


def http_get(url: str, timeout: int = 30) -> dict:
    """HTTP GET через curl с ретраями."""
    for attempt in range(5):
        try:
            result = subprocess.run(
                ["curl", "-sS", "--max-time", str(timeout), url],
                capture_output=True, text=True, timeout=timeout + 5,
            )
            if result.returncode != 0:
                raise RuntimeError(f"exit {result.returncode}")
            data = json.loads(result.stdout)
            if data.get("retCode") != 0:
                raise RuntimeError(f"retCode={data.get('retCode')}: {data.get('retMsg')}")
            return data
        except Exception as e:
            print(f"    http_get retry {attempt+1}/5: {e}")
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"http_get failed after 5 retries: {url}")


def fetch_all_linear_symbols() -> list:
    """Все активные USDT-перпетуалы."""
    STABLECOINS = {'USDC', 'DAI', 'BUSD', 'TUSD', 'USDP', 'USDD',
                   'USTC', 'PAX', 'GUSD', 'USDJ', 'USDS'}
    data = http_get(f"{API}/instruments-info?category=linear&limit=1000")
    symbols = []
    for item in data["result"]["list"]:
        symbol = item["symbol"]
        status = item["status"]
        base = item.get("baseCoin", "")
        if symbol.endswith("USDT") and status == "Trading":
            if base in STABLECOINS:
                continue
            if any(x in symbol for x in ['3L', '3S', '2L', '2S', '5L', '5S']):
                continue
            symbols.append(symbol)
    return sorted(symbols)


def fetch_24h_volumes(symbols: list) -> dict:
    """Объёмы торгов за 24h для всех символов одним запросом."""
    data = http_get(f"{API}/tickers?category=linear")
    volumes = {}
    for item in data["result"]["list"]:
        sym = item["symbol"]
        if sym in symbols:
            volumes[sym] = float(item.get("turnover24h", 0) or 0)
    return volumes


def fetch_batch(symbol: str, end_ms: int):
    """1000 часовых свечей, заканчивающихся на end_ms."""
    url = f"{API}/kline?category=linear&symbol={symbol}&interval=60&limit=1000&end={end_ms}"
    for a in range(7):
        try:
            r = subprocess.run(
                ["curl", "-sS", "--max-time", "30", url],
                capture_output=True, text=True, timeout=35,
            )
            if r.returncode != 0:
                raise RuntimeError(f"exit {r.returncode}")
            d = json.loads(r.stdout)
            if d.get("retCode") != 0:
                raise RuntimeError(f"retCode={d.get('retCode')}")
            return [
                (int(k[0]), float(k[1]), float(k[2]),
                 float(k[3]), float(k[4]), float(k[5]))
                for k in d["result"]["list"]
            ]
        except Exception as x:
            print(f"    {symbol} retry {a+1}: {x}")
            time.sleep(3 * (a + 1))
    return []


def init_db(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS klines(
        symbol TEXT, interval TEXT, open_time_ms INTEGER,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY(symbol,interval,open_time_ms))""")
    conn.commit()


def main():
    print("=" * 85)
    print(f"Backfill: top-{TOP_N} liquid USDT perpetuals, 2 years of 1h candles")
    print("=" * 85)

    print("\n[1/3] Fetching all linear symbols...")
    all_symbols = fetch_all_linear_symbols()
    print(f"      Found {len(all_symbols)} USDT perpetuals")

    print("\n[2/3] Fetching 24h volumes, selecting top by liquidity...")
    volumes = fetch_24h_volumes(all_symbols)
    ranked = sorted(
        [(s, volumes.get(s, 0)) for s in all_symbols],
        key=lambda x: -x[1]
    )
    top_symbols = [s for s, v in ranked[:TOP_N] if v > 0]
    print(f"      Top {len(top_symbols)} by turnover24h:")
    for i, s in enumerate(top_symbols[:10]):
        print(f"        {i+1:2d}. {s:<15} ${volumes[s]/1e6:8.1f}M")
    print(f"        ... ({len(top_symbols)-10} more)")

    print(f"\n[3/3] Loading {HISTORY_DAYS} days of 1h candles...")
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    init_db(conn)

    now_ms = int(time.time() * 1000)
    target_ms = int((time.time() - HISTORY_DAYS * 86400) * 1000)

    for i, sym in enumerate(top_symbols):
        tot, pages, end_ms = 0, 0, now_ms
        while end_ms > target_ms:
            cs = fetch_batch(sym, end_ms)
            if not cs:
                break
            conn.executemany(
                "INSERT OR IGNORE INTO klines VALUES(?,?,?,?,?,?,?,?)",
                [(sym, "1h", *x) for x in cs],
            )
            conn.commit()
            tot += len(cs)
            pages += 1
            end_ms = min(x[0] for x in cs) - 1
            time.sleep(0.25)

        oldest = conn.execute(
            "SELECT MIN(open_time_ms) FROM klines WHERE symbol=?", (sym,)
        ).fetchone()[0]
        oldest_date = time.strftime('%Y-%m-%d', time.gmtime(oldest / 1000)) if oldest else "?"
        print(f"  [{i+1:2d}/{len(top_symbols)}] {sym:<15} {tot:6d} candles, "
              f"oldest={oldest_date}")
        time.sleep(0.5)

    conn.close()
    print("\nDone!")
    print(f"DB: {DB} ({DB.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()