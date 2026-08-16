#!/usr/bin/env python3
"""
Динамический отбор монет с Bybit для momentum-стратегии.

Шаги:
1. Запрос всех USDT-перпетуалов с Bybit (~447 монет)
2. Фильтрация по ликвидности (объём > MIN_VOLUME_USD)
3. Расчёт признаков: Hurst, VR720, Vol, Corr(BTC)
4. Отбор по критериям трендовости
"""
import sqlite3
import math
import json
import subprocess
import numpy as np
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime, timedelta

DB = Path("data/klines_all.sqlite")
BYBIT_API = "https://api.bybit.com/v5/market"

# Критерии ликвидности
MIN_VOLUME_24H_USD = 5_000_000  # минимум $5M объёма за 24h

# Критерии трендовости
MIN_HURST = 0.55
MIN_VR720 = 1.0
MIN_VOL = 0.40
MAX_VOL = 1.50
MAX_CORR_BTC = 0.85


def _http_get(url: str) -> dict:
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
    url = f"{BYBIT_API}/instruments-info?category=linear&limit=1000"
    data = _http_get(url)
    
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
    url = f"{BYBIT_API}/tickers?category=linear"
    data = _http_get(url)
    
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit API error: {data.get('retMsg')}")
    
    volumes = {}
    for item in data["result"]["list"]:
        symbol = item["symbol"]
        if symbol in symbols:
            turnover = float(item.get("turnover24h", 0))
            volumes[symbol] = turnover
    
    return volumes


def filter_by_liquidity(symbols: List[str], min_volume_usd: float) -> List[str]:
    """Фильтрует символы по объёму торгов за 24h."""
    volumes = fetch_24h_volumes(symbols)
    passed = [s for s in symbols if volumes.get(s, 0) >= min_volume_usd]
    return sorted(passed)


def _hurst(ts: np.ndarray) -> float:
    """Упрощённый R/S анализ. H>0.5 = trending, H<0.5 = mean-reverting."""
    n = len(ts)
    if n < 200:
        return 0.5
    points = []
    for k in [16, 32, 64, 128, 256, 512]:
        if k > n // 4:
            continue
        rs_vals = []
        for b in range(n // k):
            block = ts[b*k:(b+1)*k]
            mean = np.mean(block)
            dev = np.cumsum(block - mean)
            R = np.max(dev) - np.min(dev)
            S = np.std(block)
            if S > 1e-12:
                rs_vals.append(R / S)
        if rs_vals:
            points.append((math.log(k), math.log(np.mean(rs_vals))))
    if len(points) < 2:
        return 0.5
    x = np.array([p[0] for p in points])
    y = np.array([p[1] for p in points])
    return float(np.polyfit(x, y, 1)[0])


def _variance_ratio(ts: np.ndarray, q: int) -> float:
    """VR(q): >1 = трендовость на масштабе q, <1 = mean-reversion."""
    n = len(ts)
    if n < q * 4:
        return 1.0
    rq = np.array([ts[i:i+q].sum() for i in range(0, n - q, q)])
    v1 = np.var(ts, ddof=1)
    vq = np.var(rq, ddof=1)
    if v1 <= 1e-15:
        return 1.0
    return float(vq / (q * v1))


def _realized_vol(ts: np.ndarray) -> float:
    """Годовая реализованная волатильность из часовых доходностей."""
    if len(ts) < 2:
        return 0.0
    return float(np.std(ts) * math.sqrt(365.25 * 24))


def calculate_features(symbol: str, klines: List[dict],
                       btc_klines: Optional[List[dict]] = None) -> Optional[Dict]:
    """Рассчитывает признаки для одной монеты."""
    closes = np.array([k["close"] for k in klines])
    if len(closes) < 1000:
        return None

    rets = np.diff(np.log(closes))

    corr_btc = 0.0
    if btc_klines is not None and len(btc_klines) >= len(klines):
        btc_closes = np.array([k["close"] for k in btc_klines])
        btc_rets = np.diff(np.log(btc_closes))
        m = min(len(rets), len(btc_rets))
        corr_btc = float(np.corrcoef(rets[-m:], btc_rets[-m:])[0, 1])

    return {
        "symbol": symbol,
        "n_candles": len(closes),
        "hurst": _hurst(rets),
        "vr720": _variance_ratio(rets, 720),
        "vol": _realized_vol(rets),
        "corr_btc": corr_btc,
    }


def filter_universe(features_list: List[Dict]) -> List[str]:
    """Применяет критерии фильтра и возвращает список прошедших символов."""
    passed = []
    for f in features_list:
        if f is None:
            continue
        ok_hurst = f["hurst"] > MIN_HURST
        ok_vr = f["vr720"] > MIN_VR720
        ok_vol = MIN_VOL < f["vol"] < MAX_VOL
        ok_corr = f["corr_btc"] < MAX_CORR_BTC
        if ok_hurst and ok_vr and ok_vol and ok_corr:
            passed.append(f["symbol"])
    return sorted(passed)


def select_symbols_dynamic(lookback_days: int = 90,
                           end_date: Optional[datetime] = None,
                           db_path: Path = DB,
                           min_volume_usd: float = MIN_VOLUME_24H_USD,
                           verbose: bool = True) -> List[str]:
    """
    Главная функция: динамический отбор монет с Bybit.
    
    Args:
        lookback_days: период для расчёта признаков (по умолчанию 90 дней)
        end_date: конечная дата (по умолчанию now)
        db_path: путь к БД с klines
        min_volume_usd: минимальный объём торгов за 24h
        verbose: печатать прогресс
    
    Returns:
        список отобранных символов
    """
    if end_date is None:
        end_date = datetime.now()
    
    if verbose:
        print(f"[1/4] Fetching all linear symbols from Bybit...")
    
    all_symbols = fetch_all_linear_symbols()
    
    if verbose:
        print(f"      Found {len(all_symbols)} USDT perpetuals")
        print(f"[2/4] Filtering by liquidity (> ${min_volume_usd:,} / 24h)...")
    
    liquid_symbols = filter_by_liquidity(all_symbols, min_volume_usd)
    
    if verbose:
        print(f"      {len(liquid_symbols)} symbols passed liquidity filter")
        print(f"[3/4] Calculating features for symbols with data in {db_path.name}...")
    
    start_date = end_date - timedelta(days=lookback_days)
    start_ms = int(start_date.timestamp() * 1000)
    end_ms = int(end_date.timestamp() * 1000)
    
    conn = sqlite3.connect(db_path)
    
    # BTC для корреляции
    btc_rows = conn.execute(
        "SELECT open_time_ms, close FROM klines "
        "WHERE symbol='BTCUSDT' AND interval='1h' "
        "AND open_time_ms BETWEEN ? AND ? "
        "ORDER BY open_time_ms",
        (start_ms, end_ms)
    ).fetchall()
    btc_klines = [{"ts": r[0], "close": float(r[1])} for r in btc_rows]
    
    features_list = []
    skipped_no_data = 0
    
    for i, sym in enumerate(liquid_symbols):
        rows = conn.execute(
            "SELECT open_time_ms, close FROM klines "
            "WHERE symbol=? AND interval='1h' "
            "AND open_time_ms BETWEEN ? AND ? "
            "ORDER BY open_time_ms",
            (sym, start_ms, end_ms)
        ).fetchall()
        
        if len(rows) < 1000:
            skipped_no_data += 1
            continue
        
        klines = [{"ts": r[0], "close": float(r[1])} for r in rows]
        features = calculate_features(sym, klines, btc_klines)
        if features:
            features_list.append(features)
        
        if verbose and (i + 1) % 50 == 0:
            print(f"      Processed {i+1}/{len(liquid_symbols)} symbols...")
    
    conn.close()
    
    if verbose:
        print(f"      {len(features_list)} symbols with sufficient data")
        if skipped_no_data > 0:
            print(f"      ({skipped_no_data} symbols skipped - no data in DB)")
        print(f"[4/4] Applying trend filters...")
    
    selected = filter_universe(features_list)
    
    if verbose:
        print(f"      {len(selected)} symbols passed all filters")
    
    return selected


if __name__ == "__main__":
    print("=" * 85)
    print("Динамический отбор монет с Bybit")
    print("=" * 85)
    print()
    
    selected = select_symbols_dynamic(lookback_days=90, verbose=True)
    
    print()
    print("=" * 85)
    print(f"Final selection ({len(selected)} symbols):")
    print(", ".join(selected))
    print("=" * 85)