#!/usr/bin/env python3
"""Feature-анализ монет для отбора в momentum-стратегию.

Критерии ex-ante (до бэктеста):
  1. Hurst exponent > 0.55  (трендовость)
  2. Autocorrelation(24h) > 0.05  (память)
  3. 0.40 < Realized vol < 1.50  (адекватная волатильность)
  4. Correlation с BTC < 0.85  (своя динамика)
"""
import sqlite3, math
import numpy as np
from pathlib import Path

DB = Path("data/klines_extended.sqlite")
HOURS_PER_YEAR = 365.25 * 24

def hurst_exponent(ts: np.ndarray) -> float:
    """Упрощённый R/S анализ. H>0.5 = trending, H<0.5 = mean-reverting."""
    n = len(ts)
    if n < 200:
        return 0.5
    max_k = n // 4
    points = []
    for k in [16, 32, 64, 128, 256, 512]:
        if k > max_k:
            continue
        n_blocks = n // k
        rs_vals = []
        for b in range(n_blocks):
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

def autocorr(ts: np.ndarray, lag: int) -> float:
    """Автокорреляция доходностей с лагом lag."""
    if len(ts) <= lag:
        return 0.0
    x = ts[:-lag]
    y = ts[lag:]
    if np.std(x) < 1e-12 or np.std(y) < 1e-12:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])

def realized_vol(ts: np.ndarray) -> float:
    """Годовая реализованная волатильность из часовых доходностей."""
    if len(ts) < 2:
        return 0.0
    return float(np.std(ts) * math.sqrt(HOURS_PER_YEAR))

def variance_ratio(ts, q):
    """VR(q): >1 = трендовость на масштабе q, <1 = mean-reversion."""
    n = len(ts)
    if n < q * 4:
        return 1.0
    r1 = ts
    rq = np.array([ts[i:i+q].sum() for i in range(0, n - q, q)])
    v1 = np.var(r1, ddof=1)
    vq = np.var(rq, ddof=1)
    if v1 <= 1e-15:
        return 1.0
    return float(vq / (q * v1))


def _print_table(results):
    print()
    print(f"{'Symbol':<12} {'Candles':>8} {'Hurst':>7} {'AC24':>7} "
          f"{'AC168':>7} {'VR720':>7} {'Vol%':>7} {'CorrBTC':>8} {'PASS':>5}")
    print("-" * 83)
    passed = []
    for r in sorted(results, key=lambda x: -x["hurst"]):
        ok_h = r["hurst"] > 0.55
        ok_ac = r["vr"] > 1.0
        ok_vol = 0.40 < r["rv"] < 1.50
        ok_corr = r["corr_btc"] < 0.85
        passed_all = ok_h and ok_ac and ok_vol and ok_corr
        if passed_all:
            passed.append(r["symbol"])
        print(f"{r['symbol']:<12} {r['n']:>8} {r['hurst']:>7.3f} "
              f"{r['ac24']:>7.3f} {r['ac168']:>7.3f} {r['vr']:>7.3f} {r['rv']*100:>7.1f} "
              f"{r['corr_btc']:>8.3f} {'✓' if passed_all else '✗':>5}")
    print()
    print(f"PASSED ({len(passed)}): {', '.join(passed)}")
    print()
    print("Критерии: Hurst>0.55, VR(720h)>1.0,")
    print("          40%<Vol<150%, Corr(BTC)<0.85")

def load_closes(symbol: str) -> np.ndarray:
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT close FROM klines WHERE symbol=? AND interval='1h' "
        "ORDER BY open_time_ms", (symbol,),
    ).fetchall()
    conn.close()
    return np.array([float(r[0]) for r in rows])

def main():
    conn = sqlite3.connect(DB)
    symbols = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM klines ORDER BY symbol").fetchall()]
    conn.close()
    print(f"Loaded {len(symbols)} symbols from {DB}")
    
    results = []
    closes_map = {}
    for sym in symbols:
        closes = load_closes(sym)
        if len(closes) < 1000:
            print(f"  {sym}: skip (only {len(closes)} candles)")
            continue
        closes_map[sym] = closes
        rets = np.diff(np.log(closes))
        h = hurst_exponent(rets)
        ac24 = autocorr(rets, 24)
        ac168 = autocorr(rets, 168)
        ac720 = autocorr(rets, 720)
        vr = variance_ratio(rets, 720)
        rv = realized_vol(rets)
        results.append({
            "symbol": sym, "n": len(closes),
            "hurst": h, "ac24": ac24, "ac168": ac168, "ac720": ac720, "vr": vr, "rv": rv,
        })
    
    btc = closes_map.get("BTCUSDT")
    if btc is not None:
        btc_rets = np.diff(np.log(btc))
        for r in results:
            sym_rets = np.diff(np.log(closes_map[r["symbol"]]))
            m = min(len(sym_rets), len(btc_rets))
            r["corr_btc"] = float(np.corrcoef(sym_rets[-m:], btc_rets[-m:])[0,1])
    else:
        for r in results:
            r["corr_btc"] = 0.0
    _print_table(results)

if __name__ == "__main__":
    main()
