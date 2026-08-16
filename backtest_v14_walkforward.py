#!/usr/bin/env python3
"""
Walk-forward backtest с динамическим отбором монет.

Логика:
  1. Разбиваем период на окна по rebalance_days (например, 30 дней)
  2. В начале каждого окна:
     - Берём данные за последние lookback_days (90 дней)
     - Отбираем монеты через universe_filter
     - Торгуем только отобранные монеты следующие rebalance_days
  3. Собираем equity curve за весь период
"""
import sqlite3
import math
from pathlib import Path
from typing import List, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass

from universe_filter import select_symbols_dynamic, DB

# Параметры стратегии
INITIAL_CAPITAL = 10_000.0
POSITION_SIZE_PCT = 0.10
VOL_TARGET_ANNUAL = 0.15
TRANSACTION_COST_BPS = 5.0
RISK_FREE_RATE = 0.04
HOURS_PER_YEAR = 365.25 * 24

# Walk-forward параметры
LOOKBACK_DAYS = 90      # период для расчёта признаков
REBALANCE_DAYS = 30     # частота переотбора


@dataclass
class Trade:
    entry_ts: int
    entry_price: float
    side: int
    exit_ts: int = 0
    exit_price: float = 0.0
    pnl_pct: float = 0.0


def load_klines_range(symbol: str, start_ms: int, end_ms: int) -> List[dict]:
    """Загружает klines из БД в указанном диапазоне времени."""
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT open_time_ms, close FROM klines "
        "WHERE symbol=? AND interval='1h' "
        "AND open_time_ms BETWEEN ? AND ? "
        "ORDER BY open_time_ms",
        (symbol, start_ms, end_ms)
    ).fetchall()
    conn.close()
    return [{"ts": r[0], "close": float(r[1])} for r in rows]


def rolling_std(values: List[float], period: int) -> List[Optional[float]]:
    """Rolling standard deviation."""
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    for i in range(period - 1, len(values)):
        w = values[i - period + 1:i + 1]
        mean = sum(w) / period
        var = sum((x - mean) ** 2 for x in w) / period
        out[i] = math.sqrt(var) if var > 0 else 0.0
    return out


def run_ts_momentum_period(klines: List[dict], initial_equity: float,
                           lookback_hours: int = 720,
                           vol_window: int = 720) -> tuple:
    """
    Запускает TS Momentum на периоде.
    Возвращает (trades, final_equity, equity_curve).
    """
    closes = [k["close"] for k in klines]
    rets = [0.0] + [
        closes[i] / closes[i - 1] - 1.0 if closes[i - 1] > 0 else 0.0
        for i in range(1, len(closes))
    ]
    vol = rolling_std(rets, vol_window)
    vt = VOL_TARGET_ANNUAL / math.sqrt(HOURS_PER_YEAR)
    cm = 1.0 - TRANSACTION_COST_BPS / 10_000

    eq = initial_equity
    ec = [eq]
    trades: List[Trade] = []
    pos: Optional[Trade] = None

    for i in range(max(lookback_hours, vol_window) + 1, len(klines)):
        price = closes[i]
        prev = closes[i - lookback_hours]
        if prev <= 0:
            ec.append(eq)
            continue

        mom = price / prev - 1.0
        rv = vol[i]
        if rv is None or rv <= 1e-10:
            ec.append(eq)
            continue

        vs = min(2.0, max(0.1, vt / rv))
        side = 1 if mom > 0 else -1
        tp = POSITION_SIZE_PCT * vs

        # Смена направления → закрыть
        if pos is not None and pos.side != side:
            ep = price * cm
            pos.exit_ts = klines[i]["ts"]
            pos.exit_price = ep
            pos.pnl_pct = (ep / pos.entry_price - 1.0) * pos.side
            eq += pos.pnl_pct * eq * POSITION_SIZE_PCT
            trades.append(pos)
            pos = None

        # Открыть позицию
        if pos is None:
            pos = Trade(
                entry_ts=klines[i]["ts"],
                entry_price=price * (1 + TRANSACTION_COST_BPS / 10_000),
                side=side
            )

        # MTM
        mtm = (price / pos.entry_price - 1.0) * eq * tp * pos.side if pos else 0.0
        ec.append(eq + mtm)

    # Закрыть финальную позицию
    if pos is not None:
        ep = closes[-1] * cm
        pos.exit_ts = klines[-1]["ts"]
        pos.exit_price = ep
        pos.pnl_pct = (ep / pos.entry_price - 1.0) * pos.side
        eq += pos.pnl_pct * eq * POSITION_SIZE_PCT
        trades.append(pos)
        ec[-1] = eq

    return trades, eq, ec


def compute_portfolio_metrics(equity_curve: List[float],
                              all_trades: List[Trade]) -> dict:
    """Рассчитывает итоговые метрики портфеля."""
    n_trades = len(all_trades)
    win_rate = sum(1 for t in all_trades if t.pnl_pct > 0) / n_trades if n_trades else 0.0
    total_ret = equity_curve[-1] / equity_curve[0] - 1.0 if equity_curve else 0.0
    years = (len(equity_curve) - 1) / HOURS_PER_YEAR if len(equity_curve) > 1 else 0.0
    cagr = (equity_curve[-1] / equity_curve[0]) ** (1 / years) - 1.0 if years > 0 else 0.0

    rets = [equity_curve[i] / equity_curve[i - 1] - 1.0
            for i in range(1, len(equity_curve)) if equity_curve[i - 1] > 0]

    if not rets:
        return {"trades": 0, "win": 0.0, "cagr": 0.0, "sharpe": 0.0,
                "sortino": 0.0, "max_dd": 0.0, "total_ret": 0.0}

    rf = (1 + RISK_FREE_RATE) ** (1 / HOURS_PER_YEAR) - 1.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    std = math.sqrt(var) if var > 0 else 0.0
    dvar = sum((min(0, r - mean)) ** 2 for r in rets) / len(rets)
    dstd = math.sqrt(dvar) if dvar > 0 else 0.0
    sharpe = (mean - rf) / std * math.sqrt(HOURS_PER_YEAR) if std > 1e-12 else 0.0
    sortino = (mean - rf) / dstd * math.sqrt(HOURS_PER_YEAR) if dstd > 1e-12 else 0.0

    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    return {"trades": n_trades, "win": win_rate * 100, "cagr": cagr * 100,
            "sharpe": sharpe, "sortino": sortino,
            "max_dd": max_dd * 100, "total_ret": total_ret * 100}


def main():
    print("=" * 85)
    print("Walk-forward backtest с динамическим отбором")
    print(f"Lookback: {LOOKBACK_DAYS} days, Rebalance: {REBALANCE_DAYS} days")
    print(f"Strategy: TS Momentum (720h)")
    print("=" * 85)

    # Определяем период бэктеста (весь доступный диапазон)
    conn = sqlite3.connect(DB)
    min_ts = conn.execute("SELECT MIN(open_time_ms) FROM klines").fetchone()[0]
    max_ts = conn.execute("SELECT MAX(open_time_ms) FROM klines").fetchone()[0]
    conn.close()

    start_date = datetime.fromtimestamp(min_ts / 1000)
    end_date = datetime.fromtimestamp(max_ts / 1000)

    # Начинаем торговлю через LOOKBACK_DAYS после начала данных
    trade_start = start_date + timedelta(days=LOOKBACK_DAYS)

    print(f"Data period: {start_date.date()} → {end_date.date()}")
    print(f"Trading period: {trade_start.date()} → {end_date.date()}")
    print()

    # Walk-forward loop
    current_date = trade_start
    all_trades: List[Trade] = []
    equity = INITIAL_CAPITAL
    equity_curve = [equity]
    rebalance_log = []
    rebalance_count = 0

    while current_date < end_date - timedelta(days=REBALANCE_DAYS):
        rebalance_count += 1
        rebalance_end = min(current_date + timedelta(days=REBALANCE_DAYS), end_date)

        # Отбор монет на основе данных за последние LOOKBACK_DAYS
        selected = select_symbols_dynamic(lookback_days=LOOKBACK_DAYS, end_date=current_date, verbose=False)

        rebalance_log.append({
            "date": current_date.date(),
            "selected": selected,
            "n": len(selected),
        })

        if not selected:
            # Нет отобранных монет — пропускаем период (держим cash)
            print(f"[{rebalance_count:3d}] {current_date.date()} → {rebalance_end.date()}: "
                  f"NO SYMBOLS SELECTED, hold cash")
            current_date = rebalance_end
            continue

        # Торгуем на отобранных монетах
        start_ms = int(current_date.timestamp() * 1000)
        end_ms = int(rebalance_end.timestamp() * 1000)

        period_rets = []
        period_trades_count = 0
        for sym in selected:
            klines = load_klines_range(sym, start_ms, end_ms)
            if len(klines) < 800:
                continue

            trades, final_eq, _ = run_ts_momentum_period(
                klines, initial_equity=INITIAL_CAPITAL, lookback_hours=720
            )
            all_trades.extend(trades)
            period_trades_count += len(trades)

            if final_eq > 0:
                period_rets.append(final_eq / INITIAL_CAPITAL - 1.0)

        # Усредняем returns по символам (equal weight)
        if period_rets:
            avg_ret = sum(period_rets) / len(period_rets)
            equity *= (1.0 + avg_ret)

        # Логируем в equity curve с hourly resolution (примерно)
        hours_in_period = (rebalance_end - current_date).total_seconds() / 3600
        steps = max(1, int(hours_in_period))
        prev_equity = equity_curve[-1]
        for step in range(1, steps + 1):
            frac = step / steps
            eq = prev_equity * (1.0 + avg_ret * frac) if period_rets else prev_equity
            equity_curve.append(eq)

        print(f"[{rebalance_count:3d}] {current_date.date()} → {rebalance_end.date()}: "
              f"{len(selected):2d} symbols, {period_trades_count:3d} trades, "
              f"equity=${equity:.2f}")

        current_date = rebalance_end

    # Итоговые метрики
    metrics = compute_portfolio_metrics(equity_curve, all_trades)

    print()
    print("=" * 85)
    print("ИТОГ")
    print("=" * 85)
    print(f"  Trades:        {metrics['trades']}")
    print(f"  Win Rate:      {metrics['win']:.1f}%")
    print(f"  CAGR:          {metrics['cagr']:.2f}%")
    print(f"  Sharpe:        {metrics['sharpe']:.3f}")
    print(f"  Sortino:       {metrics['sortino']:.3f}")
    print(f"  Max Drawdown:  {metrics['max_dd']:.2f}%")
    print(f"  Total Return:  {metrics['total_ret']:.2f}%")
    print(f"  Final Equity:  ${equity:.2f}")
    print(f"  Rebalances:    {rebalance_count}")

    # Статистика по отбору
    avg_selected = sum(r["n"] for r in rebalance_log) / len(rebalance_log) if rebalance_log else 0
    print(f"  Avg selected:  {avg_selected:.1f} symbols per rebalance")

    # Как часто отбор менялся
    changes = 0
    for i in range(1, len(rebalance_log)):
        if rebalance_log[i]["selected"] != rebalance_log[i - 1]["selected"]:
            changes += 1
    print(f"  Universe changes: {changes}/{rebalance_count} rebalances")

    print("=" * 85)


if __name__ == "__main__":
    main()