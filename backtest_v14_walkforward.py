#!/usr/bin/env python3
"""
backtest_v14_walkforward.py — Walk-forward backtest с динамическим отбором
монет на top-50 фиксированной вселенной.

Логика:
1. Читаем историю из data/klines_top50.sqlite (top-50 ликвидных)
2. Разбиваем период на окна по rebalance_days (30 дней)
3. В начале каждого окна:
   - Берём данные за последние lookback_days (150 дней)
   - Считаем признаки: Hurst, VR720, Vol, Corr(BTC)
   - Применяем trend-фильтры
   - Применяем корреляционный фильтр между парами (corr < 0.75)
   - Торгуем TS Momentum на отобранных (с 720h warmup)
4. Equal weight по отобранным символам
5. Собираем equity curve и метрики
"""
import sqlite3
import math
from pathlib import Path
from typing import List, Optional, Dict
from datetime import datetime, timedelta
from dataclasses import dataclass
import numpy as np

DB = Path("data/klines_top50.sqlite")

# Параметры стратегии
INITIAL_CAPITAL = 10_000.0
POSITION_SIZE_PCT = 0.10
VOL_TARGET_ANNUAL = 0.15
TRANSACTION_COST_BPS = 5.0
RISK_FREE_RATE = 0.04
HOURS_PER_YEAR = 365.25 * 24

# Walk-forward
LOOKBACK_DAYS = 150        # >= 120 для VR720 (нужно min 720*4=2880 часов)
REBALANCE_DAYS = 30

# Trend filters
MIN_HURST = 0.55
MIN_VR720 = 1.0
MIN_VOL = 0.40
MAX_VOL = 1.50
MAX_CORR_BTC = 0.85
MAX_PAIR_CORR = 0.75


@dataclass
class Trade:
    entry_ts: int
    entry_price: float
    side: int
    exit_ts: int = 0
    exit_price: float = 0.0
    pnl_pct: float = 0.0


# ---------- TREND METRICS ----------
def hurst_exponent(ts: np.ndarray) -> float:
    """R/S анализ. H>0.5 = trending, H<0.5 = mean-reverting."""
    n = len(ts)
    if n < 200:
        return 0.5
    points = []
    for k in [16, 32, 64, 128, 256, 512]:
        if k > n // 4:
            continue
        rs_vals = []
        for b in range(n // k):
            block = ts[b * k:(b + 1) * k]
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


def variance_ratio(ts: np.ndarray, q: int) -> float:
    """VR(q): >1 = trending на масштабе q."""
    n = len(ts)
    if n < q * 4:
        return 1.0
    rq = np.array([ts[i:i + q].sum() for i in range(0, n - q, q)])
    v1 = np.var(ts, ddof=1)
    vq = np.var(rq, ddof=1)
    if v1 <= 1e-15:
        return 1.0
    return float(vq / (q * v1))


def realized_vol(ts: np.ndarray) -> float:
    """Годовая реализованная волатильность."""
    if len(ts) < 2:
        return 0.0
    return float(np.std(ts) * math.sqrt(HOURS_PER_YEAR))


# ---------- DATA ACCESS ----------
def load_klines_range(symbol: str, start_ms: int, end_ms: int) -> np.ndarray:
    """Возвращает массив close-цен."""
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT close FROM klines "
        "WHERE symbol=? AND interval='1h' "
        "AND open_time_ms BETWEEN ? AND ? "
        "ORDER BY open_time_ms",
        (symbol, start_ms, end_ms),
    ).fetchall()
    conn.close()
    return np.array([float(r[0]) for r in rows])


def get_all_symbols() -> List[str]:
    conn = sqlite3.connect(DB)
    symbols = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM klines").fetchall()]
    conn.close()
    return sorted(symbols)


# ---------- UNIVERSE SELECTION (walk-forward) ----------
def select_symbols_dynamic(all_symbols: List[str], end_date: datetime) -> List[str]:
    """
    Отбор символов на основе данных за последние LOOKBACK_DAYS от end_date.
    Возвращает список после trend + pair-correlation фильтров.
    """
    lookback_start = end_date - timedelta(days=LOOKBACK_DAYS)
    start_ms = int(lookback_start.timestamp() * 1000)
    end_ms = int(end_date.timestamp() * 1000)

    # BTC returns для корреляции
    btc_closes = load_klines_range("BTCUSDT", start_ms, end_ms)
    btc_rets = np.diff(np.log(btc_closes)) if len(btc_closes) > 1 else None

    # Считаем признаки для всех символов
    features: Dict[str, dict] = {}

    for sym in all_symbols:
        closes = load_klines_range(sym, start_ms, end_ms)
        if len(closes) < 500:
            continue
        rets = np.diff(np.log(closes))
        h = hurst_exponent(rets)
        vr = variance_ratio(rets, 720)
        vol = realized_vol(rets)

        corr_btc = 0.0
        if btc_rets is not None:
            m = min(len(rets), len(btc_rets))
            if m > 10:
                corr_btc = float(np.corrcoef(rets[-m:], btc_rets[-m:])[0, 1])

        ok_h = h > MIN_HURST
        ok_vr = vr > MIN_VR720
        ok_vol = MIN_VOL < vol < MAX_VOL
        ok_corr = corr_btc < MAX_CORR_BTC

        if ok_h and ok_vr and ok_vol and ok_corr:
            features[sym] = {"vr": vr, "closes": closes, "rets": rets}

    if len(features) < 2:
        return list(features.keys())

    # Корреляционный фильтр между парами
    min_len = min(len(f["rets"]) for f in features.values())
    returns_dict = {s: f["rets"][-min_len:] for s, f in features.items()}
    df = np.array(list(returns_dict.values()))
    symbols = list(returns_dict.keys())

    corr_matrix = np.corrcoef(df)
    score = {s: features[s]["vr"] for s in symbols}
    rejected = set()
    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            a, b = symbols[i], symbols[j]
            if a in rejected or b in rejected:
                continue
            if abs(corr_matrix[i, j]) > MAX_PAIR_CORR:
                if score[a] < score[b]:
                    rejected.add(a)
                else:
                    rejected.add(b)

    return sorted(s for s in symbols if s not in rejected)


# ---------- STRATEGY ----------
def rolling_std(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    for i in range(period - 1, len(values)):
        w = values[i - period + 1:i + 1]
        mean = sum(w) / period
        var = sum((x - mean) ** 2 for x in w) / period
        out[i] = math.sqrt(var) if var > 0 else 0.0
    return out


def run_ts_momentum_period(closes: np.ndarray, initial_equity: float,
                           lookback_hours: int = 720) -> tuple:
    """
    Запускает TS Momentum. Возвращает (trades, equity_curve).
    equity_curve строится только по trading period (после warmup).
    """
    rets = [0.0] + [
        closes[i] / closes[i - 1] - 1.0 if closes[i - 1] > 0 else 0.0
        for i in range(1, len(closes))
    ]
    vol = rolling_std(rets, lookback_hours)
    vt = VOL_TARGET_ANNUAL / math.sqrt(HOURS_PER_YEAR)
    cm = 1.0 - TRANSACTION_COST_BPS / 10_000

    eq = initial_equity
    equity_curve = [eq]
    trades: List[Trade] = []
    pos: Optional[Trade] = None

    for i in range(lookback_hours + 1, len(closes)):
        price = closes[i]
        prev = closes[i - lookback_hours]
        if prev <= 0:
            equity_curve.append(eq)
            continue
        mom = price / prev - 1.0
        rv = vol[i]
        if rv is None or rv <= 1e-10:
            equity_curve.append(eq)
            continue
        vs = min(2.0, max(0.1, vt / rv))
        side = 1 if mom > 0 else -1
        tp = POSITION_SIZE_PCT * vs

        if pos is not None and pos.side != side:
            ep = price * cm
            pos.exit_price = ep
            pos.pnl_pct = (ep / pos.entry_price - 1.0) * pos.side
            eq += pos.pnl_pct * eq * POSITION_SIZE_PCT
            trades.append(pos)
            pos = None

        if pos is None:
            pos = Trade(
                entry_ts=0,
                entry_price=price * (1 + TRANSACTION_COST_BPS / 10_000),
                side=side,
            )

        mtm = (price / pos.entry_price - 1.0) * eq * tp * pos.side if pos else 0.0
        equity_curve.append(eq + mtm)

    if pos is not None:
        ep = closes[-1] * cm
        pos.exit_price = ep
        pos.pnl_pct = (ep / pos.entry_price - 1.0) * pos.side
        eq += pos.pnl_pct * eq * POSITION_SIZE_PCT
        trades.append(pos)
        equity_curve[-1] = eq

    return trades, equity_curve


# ---------- METRICS ----------
def compute_metrics(equity_curve: List[float], n_trades: int, trading_days: int) -> dict:
    """
    Расчёт метрик портфеля.
    trading_days: реальное количество дней торговли.
    """
    if len(equity_curve) < 2:
        return {"trades": 0, "win": 0.0, "cagr": 0.0, "sharpe": 0.0,
                "sortino": 0.0, "max_dd": 0.0, "total_ret": 0.0}

    n = n_trades
    total_ret = equity_curve[-1] / equity_curve[0] - 1.0
    years = trading_days / 365.25 if trading_days > 0 else 0.0
    cagr = (equity_curve[-1] / equity_curve[0]) ** (1 / years) - 1.0 \
        if years > 0 and equity_curve[0] > 0 else 0.0

    # Дневные returns (каждая точка ≈ 1 ребаланс = 30 дней)
    rets = [equity_curve[i] / equity_curve[i - 1] - 1.0
            for i in range(1, len(equity_curve)) if equity_curve[i - 1] > 0]
    if not rets:
        return {"trades": n, "win": 0.0, "cagr": cagr * 100, "sharpe": 0.0,
                "sortino": 0.0, "max_dd": 0.0, "total_ret": total_ret * 100}

    # Sharpe на дневных returns (примерно 1 ребаланс в 30 дней)
    # Annualize: sqrt(365/30) = 3.49
    rf_daily = (1 + RISK_FREE_RATE) ** (1 / 365.25) - 1.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    std = math.sqrt(var) if var > 0 else 0.0
    dvar = sum((min(0, r - mean)) ** 2 for r in rets) / len(rets)
    dstd = math.sqrt(dvar) if dvar > 0 else 0.0

    # Annualization factor: каждая точка = REBALANCE_DAYS дней
    ann_factor = math.sqrt(365.25 / REBALANCE_DAYS)
    sharpe = (mean - rf_daily * REBALANCE_DAYS) / std * ann_factor if std > 1e-12 else 0.0
    sortino = (mean - rf_daily * REBALANCE_DAYS) / dstd * ann_factor if dstd > 1e-12 else 0.0

    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    return {"trades": n, "win": 0.0, "cagr": cagr * 100,
            "sharpe": sharpe, "sortino": sortino,
            "max_dd": max_dd * 100, "total_ret": total_ret * 100}


# ---------- MAIN ----------
def main():
    print("=" * 85)
    print("Walk-forward backtest: TS Momentum на top-50 universe")
    print(f"Lookback: {LOOKBACK_DAYS}d, Rebalance: {REBALANCE_DAYS}d")
    print(f"Filters: Hurst>{MIN_HURST}, VR720>{MIN_VR720}, Vol {MIN_VOL}-{MAX_VOL}, "
          f"CorrBTC<{MAX_CORR_BTC}, PairCorr<{MAX_PAIR_CORR}")
    print("=" * 85)

    all_symbols = get_all_symbols()
    print(f"Universe: {len(all_symbols)} symbols in {DB}")

    conn = sqlite3.connect(DB)
    min_ts = conn.execute("SELECT MIN(open_time_ms) FROM klines").fetchone()[0]
    max_ts = conn.execute("SELECT MAX(open_time_ms) FROM klines").fetchone()[0]
    conn.close()

    start_date = datetime.fromtimestamp(min_ts / 1000)
    end_date = datetime.fromtimestamp(max_ts / 1000)
    trade_start = start_date + timedelta(days=LOOKBACK_DAYS)

    print(f"Data: {start_date.date()} → {end_date.date()}")
    print(f"Trading: {trade_start.date()} → {end_date.date()}")
    print()

    current_date = trade_start
    equity = INITIAL_CAPITAL
    all_trades: List[Trade] = []
    rebalance_log = []
    rebalance_count = 0

    while current_date < end_date - timedelta(days=REBALANCE_DAYS):
        rebalance_count += 1
        rebalance_end = min(
            current_date + timedelta(days=REBALANCE_DAYS), end_date
        )

        selected = select_symbols_dynamic(all_symbols, current_date)
        rebalance_log.append({
            "date": current_date.date(),
            "selected": selected,
            "n": len(selected),
        })

        if not selected:
            print(f"[{rebalance_count:3d}] {current_date.date()} → "
                  f"{rebalance_end.date()}: NO SYMBOLS, hold cash")
            current_date = rebalance_end
            continue

        # Торгуем на отобранных символах (с warmup-периодом для momentum)
        warmup_start = current_date - timedelta(hours=720)
        start_ms = int(warmup_start.timestamp() * 1000)
        end_ms = int(rebalance_end.timestamp() * 1000)

        period_rets = []
        period_trades_count = 0
        for sym in selected:
            closes = load_klines_range(sym, start_ms, end_ms)
            if len(closes) < 800:
                continue
            trades, equity_curve = run_ts_momentum_period(
                closes, initial_equity=INITIAL_CAPITAL
            )
            all_trades.extend(trades)
            period_trades_count += len(trades)
            # Берём return за весь trading period (после warmup)
            if len(equity_curve) > 0 and equity_curve[0] > 0:
                trading_ret = equity_curve[-1] / equity_curve[0] - 1.0
                period_rets.append(trading_ret)

        if period_rets:
            avg_ret = sum(period_rets) / len(period_rets)
            equity *= (1.0 + avg_ret)

        print(f"[{rebalance_count:3d}] {current_date.date()} → "
              f"{rebalance_end.date()}: {len(selected):2d} symbols, "
              f"{period_trades_count:3d} trades, equity=${equity:.2f}")

        current_date = rebalance_end

    # Итоговая equity curve (1 точка на ребаланс)
    equity_curve = [INITIAL_CAPITAL]
    eq = INITIAL_CAPITAL
    for r in rebalance_log:
        date = datetime.combine(r["date"], datetime.min.time())
        end = date + timedelta(days=REBALANCE_DAYS)
        warmup_start = date - timedelta(hours=720)
        start_ms = int(warmup_start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        if r["selected"]:
            rets = []
            for sym in r["selected"]:
                closes = load_klines_range(sym, start_ms, end_ms)
                if len(closes) < 800:
                    continue
                _, ec = run_ts_momentum_period(closes, INITIAL_CAPITAL)
                if len(ec) > 0 and ec[0] > 0:
                    rets.append(ec[-1] / ec[0] - 1.0)
            if rets:
                eq *= (1.0 + sum(rets) / len(rets))
        equity_curve.append(eq)

    trading_days = (end_date - trade_start).days
    metrics = compute_metrics(equity_curve, len(all_trades), trading_days)

    print()
    print("=" * 85)
    print("ИТОГ")
    print("=" * 85)
    print(f"  Trades:           {metrics['trades']}")
    print(f"  CAGR:             {metrics['cagr']:.2f}%")
    print(f"  Sharpe:           {metrics['sharpe']:.3f}")
    print(f"  Sortino:          {metrics['sortino']:.3f}")
    print(f"  Max Drawdown:     {metrics['max_dd']:.2f}%")
    print(f"  Total Return:     {metrics['total_ret']:.2f}%")
    print(f"  Final Equity:     ${equity:.2f}")
    print(f"  Rebalances:       {rebalance_count}")
    print(f"  Trading days:     {trading_days}")
    avg_selected = (
        sum(r["n"] for r in rebalance_log) / len(rebalance_log)
        if rebalance_log else 0
    )
    print(f"  Avg selected:     {avg_selected:.1f} symbols per rebalance")

    changes = sum(
        1 for i in range(1, len(rebalance_log))
        if rebalance_log[i]["selected"] != rebalance_log[i - 1]["selected"]
    )
    print(f"  Universe changes: {changes}/{rebalance_count} rebalances")
    print("=" * 85)


if __name__ == "__main__":
    main()