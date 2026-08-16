#!/usr/bin/env python3
"""Ensemble: TS Momentum (720h) + Breakout (20d/10d) на отобранных монетах.

Логика:
  - Список символов задаётся в SYMBOLS (вручную после feature_analysis)
  - Каждая стратегия запускается отдельно на каждой монете
  - Equity curves усредняются с весами (по умолчанию 50/50)
  - Отчёт по ensemble + по каждой стратегии отдельно
"""
import sqlite3, math
from pathlib import Path
from typing import List, Tuple, Optional
from dataclasses import dataclass, field

DB = Path("data/klines_extended.sqlite")
# ВСТАВИТЬ СЮДА СПИСОК ИЗ feature_analysis.py
SYMBOLS = ["ADAUSDT", "ARBUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT", "SOLUSDT"]  # ← обновить после feature_analysis

INITIAL_CAPITAL = 10_000.0
POSITION_SIZE_PCT = 0.10
VOL_TARGET_ANNUAL = 0.15
TRANSACTION_COST_BPS = 5.0
RISK_FREE_RATE = 0.04
HOURS_PER_YEAR = 365.25 * 24

# Веса ensemble
W_TS_MOMENTUM = 0.5
W_BREAKOUT = 0.5

@dataclass
class Trade:
    entry_ts: int
    entry_price: float
    side: int
    exit_ts: int = 0
    exit_price: float = 0.0
    pnl_pct: float = 0.0

@dataclass
class StrategyResult:
    name: str
    symbol: str
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)

def load_klines(symbol: str) -> List[dict]:
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT open_time_ms, open, high, low, close, volume FROM klines "
        "WHERE symbol=? AND interval='1h' ORDER BY open_time_ms",
        (symbol,),
    ).fetchall()
    conn.close()
    return [{"ts": r[0], "open": float(r[1]), "high": float(r[2]),
             "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])}
            for r in rows]

def rolling_std(values: List[float], period: int) -> List[Optional[float]]:
    out = [None] * len(values)
    if len(values) < period:
        return out
    for i in range(period - 1, len(values)):
        w = values[i - period + 1:i + 1]
        mean = sum(w) / period
        var = sum((x - mean) ** 2 for x in w) / period
        out[i] = math.sqrt(var) if var > 0 else 0.0
    return out


def run_ts_momentum(symbol, klines, lookback_hours=720, vol_window=720):
    closes = [k["close"] for k in klines]
    rets = [0.0] + [closes[i]/closes[i-1]-1.0 if closes[i-1]>0 else 0.0
                    for i in range(1, len(closes))]
    vol = rolling_std(rets, vol_window)
    vt = VOL_TARGET_ANNUAL / math.sqrt(HOURS_PER_YEAR)
    cm = 1.0 - TRANSACTION_COST_BPS / 10_000
    eq = INITIAL_CAPITAL
    ec = [eq]; trades = []; pos = None
    for i in range(max(lookback_hours, vol_window) + 1, len(klines)):
        price = closes[i]
        prev = closes[i - lookback_hours]
        if prev <= 0:
            ec.append(eq); continue
        mom = price / prev - 1.0
        rv = vol[i]
        if rv is None or rv <= 1e-10:
            ec.append(eq); continue
        vs = min(2.0, max(0.1, vt / rv))
        side = 1 if mom > 0 else -1
        tp = POSITION_SIZE_PCT * vs
        if pos is not None and pos.side != side:
            ep = price * cm
            pos.exit_ts, pos.exit_price = klines[i]["ts"], ep
            pos.pnl_pct = (ep / pos.entry_price - 1.0) * pos.side
            eq += pos.pnl_pct * eq * POSITION_SIZE_PCT
            trades.append(pos); pos = None
        if pos is None:
            pos = Trade(entry_ts=klines[i]["ts"],
                        entry_price=price*(1+TRANSACTION_COST_BPS/10_000), side=side)
        mtm = (price/pos.entry_price-1.0)*eq*tp*pos.side if pos else 0.0
        ec.append(eq + mtm)
    if pos is not None:
        ep = closes[-1] * cm
        pos.exit_ts, pos.exit_price = klines[-1]["ts"], ep
        pos.pnl_pct = (ep / pos.entry_price - 1.0) * pos.side
        eq += pos.pnl_pct * eq * POSITION_SIZE_PCT
        trades.append(pos); ec[-1] = eq
    return StrategyResult("TS Momentum (720h)", symbol, trades=trades, equity_curve=ec)


def run_breakout(symbol: str, klines: List[dict],
                 entry_period: int = 20 * 24, exit_period: int = 10 * 24) -> StrategyResult:
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    closes = [k["close"] for k in klines]
    cost_mult = 1.0 - TRANSACTION_COST_BPS / 10_000

    equity = INITIAL_CAPITAL
    equity_curve = [equity]
    trades: List[Trade] = []
    position: Optional[Trade] = None

    for i in range(max(entry_period, exit_period) + 1, len(klines)):
        price = closes[i]
        entry_high = max(highs[i - entry_period:i])
        exit_low = min(lows[i - exit_period:i])

        if position is None and price > entry_high:
            entry_price = price * (1 + TRANSACTION_COST_BPS / 10_000)
            position = Trade(entry_ts=klines[i]["ts"],
                             entry_price=entry_price, side=1)
        elif position is not None and price < exit_low:
            exit_price = price * cost_mult
            position.exit_ts = klines[i]["ts"]
            position.exit_price = exit_price
            position.pnl_pct = exit_price / position.entry_price - 1.0
            pnl = position.pnl_pct * equity * POSITION_SIZE_PCT
            equity += pnl
            trades.append(position)
            position = None

        if position is not None:
            mtm = (price / position.entry_price - 1.0) * equity * POSITION_SIZE_PCT
            equity_curve.append(equity + mtm)
        else:
            equity_curve.append(equity)

    if position is not None:
        price = closes[-1]
        exit_price = price * cost_mult
        position.exit_ts = klines[-1]["ts"]
        position.exit_price = exit_price
        position.pnl_pct = exit_price / position.entry_price - 1.0
        pnl = position.pnl_pct * equity * POSITION_SIZE_PCT
        equity += pnl
        trades.append(position)
        equity_curve[-1] = equity

    return StrategyResult(name=f"Breakout ({entry_period//24}d/{exit_period//24}d)",
                          symbol=symbol, trades=trades, equity_curve=equity_curve)

def compute_metrics(result: StrategyResult) -> dict:
    ec = result.equity_curve
    trades = result.trades
    n_trades = len(trades)
    win_rate = sum(1 for t in trades if t.pnl_pct > 0) / n_trades if n_trades else 0.0
    total_ret = ec[-1] / ec[0] - 1.0 if ec else 0.0
    years = (len(ec) - 1) / HOURS_PER_YEAR if len(ec) > 1 else 0.0
    cagr = (ec[-1] / ec[0]) ** (1 / years) - 1.0 if years > 0 and ec[0] > 0 else 0.0

    rets = [ec[i] / ec[i-1] - 1.0 for i in range(1, len(ec)) if ec[i-1] > 0]
    if not rets:
        return {"trades": 0, "win": 0.0, "cagr": 0.0, "sharpe": 0.0,
                "sortino": 0.0, "max_dd": 0.0, "total_ret": 0.0}

    rf_h = (1 + RISK_FREE_RATE) ** (1 / HOURS_PER_YEAR) - 1.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    std = math.sqrt(var) if var > 0 else 0.0
    dvar = sum((min(0, r - mean)) ** 2 for r in rets) / len(rets)
    dstd = math.sqrt(dvar) if dvar > 0 else 0.0
    sharpe = (mean - rf_h) / std * math.sqrt(HOURS_PER_YEAR) if std > 1e-12 else 0.0
    sortino = (mean - rf_h) / dstd * math.sqrt(HOURS_PER_YEAR) if dstd > 1e-12 else 0.0

    peak = ec[0]
    max_dd = 0.0
    for v in ec:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    return {"trades": n_trades, "win": win_rate * 100,
            "cagr": cagr * 100, "sharpe": sharpe, "sortino": sortino,
            "max_dd": max_dd * 100, "total_ret": total_ret * 100}

def build_ensemble(ts_results, bo_results):
    min_len = min(min(len(r.equity_curve) for r in ts_results),
                  min(len(r.equity_curve) for r in bo_results))
    n_ts, n_bo = len(ts_results), len(bo_results)
    ensemble = [INITIAL_CAPITAL] * min_len
    ts_eq = [INITIAL_CAPITAL] * min_len
    bo_eq = [INITIAL_CAPITAL] * min_len
    for i in range(min_len):
        ts_ret = sum(r.equity_curve[i] for r in ts_results) / n_ts / INITIAL_CAPITAL - 1.0
        bo_ret = sum(r.equity_curve[i] for r in bo_results) / n_bo / INITIAL_CAPITAL - 1.0
        ts_eq[i] = INITIAL_CAPITAL * (1.0 + ts_ret)
        bo_eq[i] = INITIAL_CAPITAL * (1.0 + bo_ret)
        ensemble[i] = INITIAL_CAPITAL * (1.0 + ts_ret * W_TS_MOMENTUM + bo_ret * W_BREAKOUT)
    ts_agg = StrategyResult("TS Momentum", "ALL", equity_curve=ts_eq)
    bo_agg = StrategyResult("Breakout", "ALL", equity_curve=bo_eq)
    ens = StrategyResult("ENSEMBLE", "ALL", equity_curve=ensemble)
    return compute_metrics(ts_agg), compute_metrics(bo_agg), compute_metrics(ens)

def main():
    print(f"Ensemble on {len(SYMBOLS)} symbols: {', '.join(SYMBOLS)}")
    ts_results, bo_results = [], []
    for sym in SYMBOLS:
        klines = load_klines(sym)
        if len(klines) < 1000:
            print(f"  {sym}: skip ({len(klines)} candles)"); continue
        ts_results.append(run_ts_momentum(sym, klines))
        bo_results.append(run_breakout(sym, klines))
        print(f"  {sym}: TS={len(ts_results[-1].trades)} trades, "
              f"BO={len(bo_results[-1].trades)} trades")
    ts_m, bo_m, ens_m = build_ensemble(ts_results, bo_results)
    print()
    print(f"{'Strategy':<20} {'Trades':>7} {'Win%':>7} {'CAGR%':>8} "
          f"{'Sharpe':>8} {'Sortino':>9} {'MaxDD%':>8} {'TotalR%':>9}")
    print("-" * 85)
    for name, m in [("TS Momentum", ts_m), ("Breakout", bo_m), ("ENSEMBLE", ens_m)]:
        print(f"{name:<20} {m['trades']:>7} {m['win']:>7.1f} {m['cagr']:>8.2f} "
              f"{m['sharpe']:>8.3f} {m['sortino']:>9.3f} {m['max_dd']:>8.2f} "
              f"{m['total_ret']:>9.2f}")

if __name__ == "__main__":
    main()
