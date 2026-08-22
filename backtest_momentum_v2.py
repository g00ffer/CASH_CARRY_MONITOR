#!/usr/bin/env python3
"""
backtest_momentum_v2.py — Momentum backtest с фильтром истории и stop-loss.

Исправления относительно v1:
1. Фильтр минимальной истории (1 год) — отсекает новые монеты
2. Поиск общего периода для всех монет
3. Stop-loss -25% на позицию
4. Правильный расчёт комиссий

Стратегия:
- 720h momentum: mom > 0 → лонг, mom < 0 → шорт
- Смена знака → закрытие + открытие (комиссия)
- Stop-loss: если позиция теряет > 25% → выход

Комиссии:
- Taker: 0.18% per side
- Round-trip: 0.36%

Run:
  python backtest_momentum_v2.py           # полный прогон
  python backtest_momentum_v2.py --quick   # быстрый тест (5 монет)
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
MIN_HISTORY_H = 8760            # минимум 1 год данных
MAKER_FEE = 0.001               # 0.1%
TAKER_FEE = 0.0018              # 0.18%
FEE_PER_SIDE = TAKER_FEE        # используем рыночные ордера
MAX_LOSS_PCT = 0.25             # stop-loss: -25%
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


def load_symbol_data(
    conn: sqlite3.Connection, symbol: str,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Загружает временные метки и close-цены для символа.
    Возвращает (timestamps, closes).
    """
    rows = conn.execute(
        "SELECT open_time_ms, close FROM klines "
        "WHERE symbol = ? AND interval = '1h' "
        "ORDER BY open_time_ms",
        (symbol,),
    ).fetchall()
    if not rows:
        return np.array([]), np.array([])
    timestamps = np.array([r[0] for r in rows])
    closes = np.array([float(r[1]) for r in rows])
    return timestamps, closes


# ─────────────────────────────────────────────────────────────────────
# ФИЛЬТРАЦИЯ И ВЫРАВНИВАНИЕ
# ─────────────────────────────────────────────────────────────────────
def filter_and_align(
    conn: sqlite3.Connection,
    symbols: list[str],
    min_history: int = MIN_HISTORY_H,
    lookback: int = MOMENTUM_LOOKBACK_H,
) -> tuple[dict[str, np.ndarray], np.ndarray, int, int]:
    """
    1. Фильтрует монеты с недостаточной историей
    2. Находит общий период для всех оставшихся монет
    3. Выравнивает данные по общему периоду

    Возвращает:
    - data: {symbol: closes} — выровненные данные
    - common_timestamps: общие временные метки
    - start_idx: индекс начала общего периода
    - end_idx: индекс конца общего периода
    """
    raw_data: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    filtered_out = []

    for sym in symbols:
        ts, closes = load_symbol_data(conn, sym)
        if len(closes) < lookback + min_history:
            filtered_out.append((sym, len(closes)))
            continue
        raw_data[sym] = (ts, closes)

    if not raw_data:
        return {}, np.array([]), 0, 0

    # Находим общий период: максимум стартов, минимум концов
    max_start = max(ts[0] for ts, _ in raw_data.values())
    min_end = min(ts[-1] for ts, _ in raw_data.values())

    if max_start >= min_end:
        return {}, np.array([]), 0, 0

    # Выравниваем данные по общему периоду
    aligned_data: dict[str, np.ndarray] = {}
    common_timestamps = None

    for sym, (ts, closes) in raw_data.items():
        # Находим индексы в пределах общего периода
        mask = (ts >= max_start) & (ts <= min_end)
        aligned_ts = ts[mask]
        aligned_closes = closes[mask]

        if len(aligned_closes) < lookback + 100:
            filtered_out.append((sym, len(aligned_closes)))
            continue

        aligned_data[sym] = aligned_closes
        if common_timestamps is None:
            common_timestamps = aligned_ts

    # Проверяем, что все массивы одинаковой длины
    if aligned_data:
        min_len = min(len(c) for c in aligned_data.values())
        for sym in list(aligned_data.keys()):
            aligned_data[sym] = aligned_data[sym][:min_len]
        if common_timestamps is not None:
            common_timestamps = common_timestamps[:min_len]

    return aligned_data, common_timestamps or np.array([]), 0, 0


# ─────────────────────────────────────────────────────────────────────
# СТРАТЕГИЯ: ОДНА МОНЕТА
# ─────────────────────────────────────────────────────────────────────
@dataclass
class SymbolResult:
    symbol: str
    n_trades: int = 0
    n_stop_losses: int = 0
    total_return_pct: float = 0.0
    equity_curve: list = field(default_factory=list)


def run_momentum_single(
    closes: np.ndarray,
    lookback: int = MOMENTUM_LOOKBACK_H,
    fee: float = FEE_PER_SIDE,
    max_loss: float = MAX_LOSS_PCT,
) -> SymbolResult:
    """
    Запускает momentum-стратегию на одной монете.

    Логика:
    - Для каждого бара после warmup: mom = price[i]/price[i-lookback] - 1
    - target = +1 (лонг) если mom > 0, -1 (шорт) если mom < 0
    - При смене target: платим комиссию (выход + вход)
    - Stop-loss: если позиция теряет > max_loss → выход

    Возвращает:
    - equity_curve: нормированная кривая (старт = 1.0)
    - n_trades: количество смен позиции
    - n_stop_losses: количество срабатываний stop-loss
    """
    n = len(closes)
    if n < lookback + 10:
        return SymbolResult(symbol="?", n_trades=0)

    equity = 1.0
    position = 0  # 0 = нет позиции, +1 = лонг, -1 = шорт
    entry_price = 0.0
    n_trades = 0
    n_stop_losses = 0
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

        # Проверка stop-loss
        if position != 0 and entry_price > 0:
            pnl_pct = (price / entry_price - 1.0) * position
            if pnl_pct < -max_loss:
                # Stop-loss сработал: выходим
                equity *= (1.0 - fee)  # комиссия за выход
                position = 0
                entry_price = 0.0
                n_stop_losses += 1
                equity_curve.append(equity)
                continue

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
            entry_price = price

        equity_curve.append(equity)

    total_return_pct = (equity - 1.0) * 100.0
    return SymbolResult(
        symbol="",
        n_trades=n_trades,
        n_stop_losses=n_stop_losses,
        total_return_pct=total_return_pct,
        equity_curve=equity_curve,
    )


# ─────────────────────────────────────────────────────────────────────
# ПОРТФЕЛЬ: АГРЕГАЦИЯ
# ─────────────────────────────────────────────────────────────────────
def aggregate_portfolio(
    results: list[SymbolResult],
) -> tuple[list[float], int, int]:
    """
    Агрегирует результаты всех монет в портфель с равным весом.

    Возвращает:
    - portfolio_equity: нормированная кривая (старт = 1.0)
    - total_trades: суммарное количество сделок
    - total_stop_losses: суммарное количество stop-loss
    """
    if not results:
        return [1.0], 0, 0

    min_len = min(len(r.equity_curve) for r in results)
    if min_len < 2:
        return [1.0], 0, 0

    total_trades = sum(r.n_trades for r in results)
    total_stop_losses = sum(r.n_stop_losses for r in results)
    n_symbols = len(results)

    # Равный вес: каждая монета = 1/N портфеля
    portfolio = np.ones(min_len)
    for r in results:
        curve = np.array(r.equity_curve[:min_len])
        returns = curve / curve[0] - 1.0
        portfolio += returns / n_symbols

    return portfolio.tolist(), total_trades, total_stop_losses


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
    parser = argparse.ArgumentParser(description="Momentum Backtest v2")
    parser.add_argument("--quick", action="store_true",
                        help="Быстрый тест: только 5 монет")
    parser.add_argument("--symbols", type=int, default=0,
                        help="Ограничить количество монет (0 = все)")
    args = parser.parse_args()

    print("=" * 85)
    print("MOMENTUM BACKTEST v2 — TS Momentum 720h + фильтр истории + stop-loss")
    print(f"  Lookback: {MOMENTUM_LOOKBACK_H}h ({MOMENTUM_LOOKBACK_H // 24}d)")
    print(f"  Min history: {MIN_HISTORY_H}h ({MIN_HISTORY_H // 24}d)")
    print(f"  Stop-loss: {MAX_LOSS_PCT * 100:.0f}%")
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

    # Фильтрация и выравнивание
    print(f"\nФильтрация по минимальной истории ({MIN_HISTORY_H}h)...")
    aligned_data, common_ts, _, _ = filter_and_align(conn, symbols)
    conn.close()

    if not aligned_data:
        print("\n❌ Нет монет с достаточной историей.")
        sys.exit(1)

    n_hours = len(common_ts)
    n_years = n_hours / HOURS_PER_YEAR
    print(f"  Монет после фильтра: {len(aligned_data)}")
    print(f"  Общий период: {n_hours}h ({n_years:.2f} лет)")

    if common_ts is not None and len(common_ts) > 0:
        from datetime import datetime, timezone
        start_dt = datetime.fromtimestamp(common_ts[0] / 1000, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(common_ts[-1] / 1000, tz=timezone.utc)
        print(f"  Диапазон дат: {start_dt.date()} → {end_dt.date()}")

    # Прогон стратегии для каждой монеты
    print(f"\nЗапуск стратегии на {len(aligned_data)} монетах...")
    start_time = time.time()

    results: list[SymbolResult] = []
    for i, (sym, closes) in enumerate(aligned_data.items()):
        result = run_momentum_single(closes)
        result.symbol = sym
        results.append(result)

        if (i + 1) % 10 == 0 or i == len(aligned_data) - 1:
            elapsed = time.time() - start_time
            print(f"  [{i + 1:2d}/{len(aligned_data)}] обработано за {elapsed:.1f}s")

    # Агрегация портфеля
    print(f"\nАгрегация портфеля ({len(results)} монет)...")
    portfolio_equity, total_trades, total_stop_losses = aggregate_portfolio(results)

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
    print(f"  Stop-loss сработал: {total_stop_losses} раз")
    print()
    print(f"  Total Return:       {metrics.get('total_return_pct', 0):+.2f}%")
    print(f"  CAGR:               {metrics.get('cagr_pct', 0):+.2f}%")
    print(f"  Sharpe:             {metrics.get('sharpe', 0):.3f}")
    print(f"  Sortino:            {metrics.get('sortino', 0):.3f}")
    print(f"  Max Drawdown:       {metrics.get('max_dd_pct', 0):.2f}%")
    print()

    # Сравнение с комиссиями и без
    print("  --- Разложение эффекта комиссий ---")
    results_nofee = []
    for sym, closes in aligned_data.items():
        result = run_momentum_single(closes, fee=0.0)
        result.symbol = sym
        results_nofee.append(result)

    portfolio_nofee, _, _ = aggregate_portfolio(results_nofee)
    metrics_nofee = calculate_metrics(portfolio_nofee, total_trades, n_hours)

    print(f"  Total Return (без комиссий): {metrics_nofee.get('total_return_pct', 0):+.2f}%")
    print(f"  CAGR (без комиссий):         {metrics_nofee.get('cagr_pct', 0):+.2f}%")
    print(f"  Sharpe (без комиссий):       {metrics_nofee.get('sharpe', 0):.3f}")
    fee_impact = metrics_nofee.get('total_return_pct', 0) - metrics.get('total_return_pct', 0)
    print(f"  Потеря от комиссий:          {fee_impact:+.2f}%")
    print("=" * 85)

    # Топ и худшие монеты
    print("\n📈 Топ-5 монет по доходности:")
    sorted_results = sorted(results, key=lambda r: r.total_return_pct, reverse=True)
    for r in sorted_results[:5]:
        print(f"  {r.symbol:<15} {r.total_return_pct:+8.2f}%  ({r.n_trades} trades, {r.n_stop_losses} SL)")

    print("\n📉 Худшие 5 монет:")
    for r in sorted_results[-5:]:
        print(f"  {r.symbol:<15} {r.total_return_pct:+8.2f}%  ({r.n_trades} trades, {r.n_stop_losses} SL)")

    print("\n" + "=" * 85)
    print("✅ Готово")
    print("=" * 85)


if __name__ == "__main__":
    main()