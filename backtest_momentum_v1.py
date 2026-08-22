#!/usr/bin/env python3
"""
backtest_momentum_v1.py — Простой бэктест TS Momentum на топ-50 вселенной.

Этап 1: проверка ядра стратегии БЕЗ динамического отбора.
Все монеты торгуются одновременно, равный вес.

Стратегия:
- Для каждой монеты: 720h momentum
- mom > 0 → лонг, mom < 0 → шорт
- Смена знака → закрытие + открытие (платим комиссию)

Комиссии:
- Taker: 0.18% per side
- Round-trip: 0.36%

Run:
  python backtest_momentum_v1.py           # полный прогон
  python backtest_momentum_v1.py --quick   # быстрый тест (5 монет)
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# ─────────────────────────────────────────────────────────────────────
# ПАРАМЕТРЫ
# ─────────────────────────────────────────────────────────────────────
DB_PATH = Path("data/klines_top50.sqlite")

MOMENTUM_LOOKBACK_H = 720       # 30 дней
MAKER_FEE = 0.001               # 0.1%
TAKER_FEE = 0.0018              # 0.18%
FEE_PER_SIDE = TAKER_FEE        # используем рыночные ордера
INITIAL_CAPITAL = 10_000.0
RISK_FREE_RATE = 0.04           # 4% годовых
HOURS_PER_YEAR = 365.25 * 24


# ─────────────────────────────────────────────────────────────────────
# ЗАГРУЗКА ДАННЫХ
# ─────────────────────────────────────────────────────────────────────
def get_symbols(conn: sqlite3.Connection) -> list[str]:
    """Все символы в БД."""
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM klines ORDER BY symbol"
    ).fetchall()
    return [r[0] for r in rows]


def load_closes(conn: sqlite3.Connection, symbol: str) -> np.ndarray:
    """Загружает close-цены для символа."""
    rows = conn.execute(
        "SELECT close FROM klines "
        "WHERE symbol = ? AND interval = '1h' "
        "ORDER BY open_time_ms",
        (symbol,),
    ).fetchall()
    return np.array([float(r[0]) for r in rows])


# ─────────────────────────────────────────────────────────────────────
# СТРАТЕГИЯ: ОДНА МОНЕТА
# ─────────────────────────────────────────────────────────────────────
@dataclass
class SymbolResult:
    symbol: str
    n_trades: int = 0
    total_return_pct: float = 0.0
    equity_curve: list = field(default_factory=list)
    hours: list = field(default_factory=list)


def run_momentum_single(
    closes: np.ndarray,
    lookback: int = MOMENTUM_LOOKBACK_H,
    fee: float = FEE_PER_SIDE,
) -> SymbolResult:
    """
    Запускает momentum-стратегию на одной монете.

    Логика:
    - Для каждого бара после warmup: mom = price[i]/price[i-lookback] - 1
    - target = +1 (лонг) если mom > 0, -1 (шорт) если mom < 0
    - При смене target: платим комиссию (выход + вход)
    - Доход за бар: position * (price[i]/price[i-1] - 1)

    Возвращает:
    - equity_curve: нормированная кривая (старт = 1.0)
    - n_trades: количество смен позиции
    """
    n = len(closes)
    if n < lookback + 10:
        return SymbolResult(symbol="?", n_trades=0)

    equity = 1.0
    position = 0  # 0 = нет позиции, +1 = лонг, -1 = шорт
    n_trades = 0
    equity_curve = [1.0]

    for i in range(lookback, n):
        price = closes[i]
        prev_price = closes[i - 1]

        if prev_price <= 0 or price <= 0:
            equity_curve.append(equity)
            continue

        # Доход за текущий бар от текущей позиции
        bar_return = price / prev_price - 1.0
        equity *= (1.0 + position * bar_return)

        # Рассчитываем momentum
        lookback_price = closes[i - lookback]
        if lookback_price <= 0:
            equity_curve.append(equity)
            continue

        momentum = price / lookback_price - 1.0
        target = 1 if momentum > 0 else -1

        # Смена позиции
        if target != position:
            # Платим комиссию за выход из старой и вход в новую
            if position != 0:
                equity *= (1.0 - fee)  # выход
            equity *= (1.0 - fee)      # вход
            n_trades += 1
            position = target

        equity_curve.append(equity)

    total_return_pct = (equity - 1.0) * 100.0
    return SymbolResult(
        symbol="",
        n_trades=n_trades,
        total_return_pct=total_return_pct,
        equity_curve=equity_curve,
    )


# ─────────────────────────────────────────────────────────────────────
# ПОРТФЕЛЬ: АГРЕГАЦИЯ
# ─────────────────────────────────────────────────────────────────────
def aggregate_portfolio(
    results: list[SymbolResult],
) -> tuple[list[float], int]:
    """
    Агрегирует результаты всех монет в портфель с равным весом.

    Возвращает:
    - portfolio_equity: нормированная кривая (старт = 1.0)
    - total_trades: суммарное количество сделок
    """
    if not results:
        return [1.0], 0

    # Находим минимальную длину (все монеты должны быть одинаковой длины)
    min_len = min(len(r.equity_curve) for r in results)
    if min_len < 2:
        return [1.0], 0

    total_trades = sum(r.n_trades for r in results)
    n_symbols = len(results)

    # Равный вес: каждая монета = 1/N портфеля
    portfolio = np.ones(min_len)
    for r in results:
        curve = np.array(r.equity_curve[:min_len])
        # Доходность каждой монеты
        returns = curve / curve[0] - 1.0
        portfolio += returns / n_symbols

    return portfolio.tolist(), total_trades


# ─────────────────────────────────────────────────────────────────────
# МЕТРИКИ
# ─────────────────────────────────────────────────────────────────────
def calculate_metrics(
    equity_curve: list[float],
    n_trades: int,
    n_hours: int,
) -> dict:
    """Рассчитывает метрики портфеля."""
    if len(equity_curve) < 2:
        return {}

    total_return = equity_curve[-1] / equity_curve[0] - 1.0
    years = n_hours / HOURS_PER_YEAR
    cagr = (equity_curve[-1] / equity_curve[0]) ** (1 / years) - 1.0 if years > 0 else 0.0

    # Часовые доходности
    returns = [
        equity_curve[i] / equity_curve[i - 1] - 1.0
        for i in range(1, len(equity_curve))
        if equity_curve[i - 1] > 0
    ]

    if not returns:
        return {"total_return_pct": total_return * 100, "cagr_pct": cagr * 100}

    # Sharpe (annualized)
    rf_hourly = (1 + RISK_FREE_RATE) ** (1 / HOURS_PER_YEAR) - 1.0
    mean_ret = np.mean(returns)
    std_ret = np.std(returns)
    sharpe = (mean_ret - rf_hourly) / std_ret * math.sqrt(HOURS_PER_YEAR) if std_ret > 1e-12 else 0.0

    # Sortino
    downside = [min(0, r - rf_hourly) for r in returns]
    downside_std = np.std(downside) if downside else 0.0
    sortino = (mean_ret - rf_hourly) / downside_std * math.sqrt(HOURS_PER_YEAR) if downside_std > 1e-12 else 0.0

    # Max Drawdown
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    return {
        "total_return_pct": total_return * 100,
        "cagr_pct": cagr * 100,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_dd_pct": max_dd * 100,
        "n_trades": n_trades,
        "years": years,
        "n_hours": n_hours,
    }


# ─────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Momentum Backtest v1")
    parser.add_argument("--quick", action="store_true",
                        help="Быстрый тест: только 5 монет")
    parser.add_argument("--symbols", type=int, default=0,
                        help="Ограничить количество монет (0 = все)")
    args = parser.parse_args()

    print("=" * 85)
    print("MOMENTUM BACKTEST v1 — TS Momentum 720h, все монеты без отбора")
    print(f"  Lookback: {MOMENTUM_LOOKBACK_H}h ({MOMENTUM_LOOKBACK_H // 24}d)")
    print(f"  Fee: {FEE_PER_SIDE * 100:.2f}% per side (taker)")
    print(f"  Round-trip: {FEE_PER_SIDE * 2 * 100:.2f}%")
    print("=" * 85)

    if not DB_PATH.exists():
        print(f"\n❌ БД не найдена: {DB_PATH}")
        print("   Запустите backfill_top50.py для загрузки данных.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    symbols = get_symbols(conn)
    print(f"\nВселенная: {len(symbols)} монет в {DB_PATH}")

    # Ограничение для быстрого теста
    if args.quick:
        symbols = symbols[:5]
        print(f"⚡ Quick mode: только {len(symbols)} монет")
    elif args.symbols > 0:
        symbols = symbols[:args.symbols]
        print(f"⚡ Ограничено: {len(symbols)} монет")

    # Прогон стратегии для каждой монеты
    print(f"\nЗапуск стратегии на {len(symbols)} монетах...")
    start_time = time.time()

    results: list[SymbolResult] = []
    for i, sym in enumerate(symbols):
        closes = load_closes(conn, sym)
        if len(closes) < MOMENTUM_LOOKBACK_H + 100:
            print(f"  [{i+1:2d}/{len(symbols)}] {sym:<15} ⏭  слишком мало данных ({len(closes)}h)")
            continue

        result = run_momentum_single(closes)
        result.symbol = sym
        results.append(result)

        if (i + 1) % 10 == 0 or i == len(symbols) - 1:
            elapsed = time.time() - start_time
            print(f"  [{i+1:2d}/{len(symbols)}] обработано за {elapsed:.1f}s")

    conn.close()

    if not results:
        print("\n❌ Нет данных для бэктеста.")
        sys.exit(1)

    # Агрегация портфеля
    print(f"\nАгрегация портфеля ({len(results)} монет)...")
    portfolio_equity, total_trades = aggregate_portfolio(results)

    # Определяем количество часов
    n_hours = len(portfolio_equity) - 1

    # Метрики
    metrics = calculate_metrics(portfolio_equity, total_trades, n_hours)

    # Вывод результатов
    print("\n" + "=" * 85)
    print("📊 РЕЗУЛЬТАТЫ")
    print("=" * 85)
    print(f"  Монет в портфеле:   {len(results)}")
    print(f"  Период:             {metrics.get('years', 0):.2f} лет ({n_hours} часов)")
    print(f"  Сделок всего:       {total_trades}")
    print(f"  Сделок на монету:   {total_trades / len(results):.1f}")
    print()
    print(f"  Total Return:       {metrics.get('total_return_pct', 0):+.2f}%")
    print(f"  CAGR:               {metrics.get('cagr_pct', 0):+.2f}%")
    print(f"  Sharpe:             {metrics.get('sharpe', 0):.3f}")
    print(f"  Sortino:            {metrics.get('sortino', 0):.3f}")
    print(f"  Max Drawdown:       {metrics.get('max_dd_pct', 0):.2f}%")
    print()

    # Сравнение с комиссиями и без
    # Пересчитаем без комиссий для понимания их вклада
    print("  --- Разложение эффекта комиссий ---")
    results_nofee = []
    conn = sqlite3.connect(DB_PATH)
    for sym in [r.symbol for r in results]:
        closes = load_closes(conn, sym)
        result = run_momentum_single(closes, fee=0.0)
        result.symbol = sym
        results_nofee.append(result)
    conn.close()

    portfolio_nofee, _ = aggregate_portfolio(results_nofee)
    metrics_nofee = calculate_metrics(portfolio_nofee, total_trades, n_hours)

    print(f"  Total Return (без комиссий): {metrics_nofee.get('total_return_pct', 0):+.2f}%")
    print(f"  CAGR (без комиссий):         {metrics_nofee.get('cagr_pct', 0):+.2f}%")
    print(f"  Sharpe (без комиссий):       {metrics_nofee.get('sharpe', 0):.3f}")
    fee_impact = metrics_nofee.get('total_return_pct', 0) - metrics.get('total_return_pct', 0)
    print(f"  Вклад комиссий:              {fee_impact:+.2f}%")
    print("=" * 85)

    # Топ и худшие монеты
    print("\n📈 Топ-5 монет по доходности:")
    sorted_results = sorted(results, key=lambda r: r.total_return_pct, reverse=True)
    for r in sorted_results[:5]:
        print(f"  {r.symbol:<15} {r.total_return_pct:+8.2f}%  ({r.n_trades} trades)")

    print("\n📉 Худшие 5 монет:")
    for r in sorted_results[-5:]:
        print(f"  {r.symbol:<15} {r.total_return_pct:+8.2f}%  ({r.n_trades} trades)")

    print("\n" + "=" * 85)
    print("✅ Готово")
    print("=" * 85)


if __name__ == "__main__":
    main()