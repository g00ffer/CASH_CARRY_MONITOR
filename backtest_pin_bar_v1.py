#!/usr/bin/env python3
"""
backtest_pin_bar_v1.py — Бэктест пин-бара на уровне поддержки (Н1).

Гипотеза:
- Уровень поддержки, сформированный за 3-4 дня
- Красная свеча с нижней тенью ≥ 2× тела на уровне
- Цена уходит вверх на 2× стопа

Данные: data/klines_top50.sqlite (часовые свечи, уже загружены)

Правила:
1. Уровень: локальный минимум за 96h, касаний ≥ 2
2. Пин-бар: красная свеча, нижняя тень ≥ 2× тела, тело в верхней половине
3. Вход: на закрытии пин-бара
4. Стоп: за нижней тенью (-0.1% запаса)
5. Тейк: 2× стопа
6. Макс. удержание: 72h
"""
from __future__ import annotations

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

LEVEL_LOOKBACK_H = 96       # 4 дня для формирования уровня
LEVEL_TOUCHES = 2           # минимум касаний уровня
LEVEL_ZONE_PCT = 0.3        # зона уровня: ±0.3%
PIN_SHADOW_RATIO = 2.0      # нижняя тень ≥ 2× тела
RISK_REWARD = 2.0           # тейк = 2× стопа
TAKER_FEE = 0.0018          # 0.18% per side
MAX_HOLDING_H = 72          # максимум 72 часа удержания
MIN_STOP_PCT = 0.3          # минимальный стоп 0.3% (иначе комиссии съедят)


# ─────────────────────────────────────────────────────────────────────
# ДАННЫЕ
# ─────────────────────────────────────────────────────────────────────
def get_symbols(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM klines ORDER BY symbol"
    ).fetchall()
    return [r[0] for r in rows]


def load_ohlcv(
    conn: sqlite3.Connection, symbol: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Загрузка OHLC: (times, opens, highs, lows, closes)."""
    rows = conn.execute(
        "SELECT open_time_ms, open, high, low, close FROM klines "
        "WHERE symbol = ? AND interval = '1h' "
        "ORDER BY open_time_ms",
        (symbol,),
    ).fetchall()
    if not rows:
        return (np.array([]) for _ in range(5))
    times = np.array([r[0] for r in rows])
    opens = np.array([float(r[1]) for r in rows])
    highs = np.array([float(r[2]) for r in rows])
    lows = np.array([float(r[3]) for r in rows])
    closes = np.array([float(r[4]) for r in rows])
    return times, opens, highs, lows, closes


# ─────────────────────────────────────────────────────────────────────
# ДЕТЕКЦИЯ УРОВНЕЙ
# ─────────────────────────────────────────────────────────────────────
def find_support_levels(
    closes: np.ndarray,
    lows: np.ndarray,
    end_idx: int,
    lookback: int = LEVEL_LOOKBACK_H,
    zone_pct: float = LEVEL_ZONE_PCT,
    min_touches: int = LEVEL_TOUCHES,
) -> list[float]:
    """
    Находит уровни поддержки за последние `lookback` баров до `end_idx`.
    Уровень = локальный минимум с количеством касаний >= min_touches.
    """
    if end_idx < lookback:
        return []

    window_lows = lows[end_idx - lookback:end_idx]
    if len(window_lows) == 0:
        return []

    # Локальные минимумы (ниже соседей)
    local_mins = []
    for i in range(1, len(window_lows) - 1):
        if window_lows[i] < window_lows[i - 1] and window_lows[i] < window_lows[i + 1]:
            local_mins.append(window_lows[i])

    if not local_mins:
        # Если нет локальных минимумов, берём абсолютный минимум
        local_mins = [window_lows.min()]

    # Кластеризуем близкие уровни и считаем касания
    levels = []
    for level in local_mins:
        # Зона уровня
        zone = level * (zone_pct / 100.0)
        touches = np.sum(
            (window_lows >= level - zone) & (window_lows <= level + zone)
        )
        if touches >= min_touches:
            levels.append(level)

    # Убираем дубликаты (близкие уровни)
    if not levels:
        return []
    levels.sort()
    filtered = [levels[0]]
    for lvl in levels[1:]:
        if (lvl - filtered[-1]) / filtered[-1] * 100 > zone_pct:
            filtered.append(lvl)

    return filtered


# ─────────────────────────────────────────────────────────────────────
# ДЕТЕКЦИЯ ПИН-БАРА
# ─────────────────────────────────────────────────────────────────────
def is_bullish_pin_bar(
    open_p: float, high_p: float, low_p: float, close_p: float,
    shadow_ratio: float = PIN_SHADOW_RATIO,
) -> bool:
    """
    Бычий пин-бар:
    - Красная свеча (close < open) или маленькое тело
    - Нижняя тень >= 2× тела
    - Тело в верхней половине диапазона
    """
    if high_p <= low_p or low_p <= 0:
        return False

    body = abs(close_p - open_p)
    candle_range = high_p - low_p
    if candle_range <= 0:
        return False

    # Минимальный размер свечи (шум)
    if candle_range / low_p * 100 < 0.05:
        return False

    lower_shadow = min(open_p, close_p) - low_p
    upper_shadow = high_p - max(open_p, close_p)

    # Нижняя тень >= 2× тела
    if body <= 0:
        # Доджи: тело почти нулевое, тень должна быть большой
        if lower_shadow < candle_range * 0.6:
            return False
    else:
        if lower_shadow < body * shadow_ratio:
            return False

    # Тело в верхней половине диапазона
    body_center = (open_p + close_p) / 2.0
    if body_center < low_p + candle_range * 0.5:
        return False

    # Верхняя тень не должна быть большой (< 30% диапазона)
    if upper_shadow > candle_range * 0.3:
        return False

    return True


# ─────────────────────────────────────────────────────────────────────
# СИМУЛЯЦИЯ
# ─────────────────────────────────────────────────────────────────────
@dataclass
class Trade:
    symbol: str
    entry_idx: int
    entry_time: int
    entry_price: float
    stop_price: float
    take_price: float
    exit_idx: int = 0
    exit_time: int = 0
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl_pct: float = 0.0


def simulate_symbol(
    symbol: str,
    times: np.ndarray,
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
) -> list[Trade]:
    """Симуляция стратегии пин-бара на одной монете."""
    n = len(closes)
    if n < LEVEL_LOOKBACK_H + MAX_HOLDING_H + 10:
        return []

    trades: list[Trade] = []
    position_open = False
    current_stop = 0.0
    current_take = 0.0
    entry_idx = 0

    for i in range(LEVEL_LOOKBACK_H, n):
        # Если позиция открыта — проверяем стоп/тейк
        if position_open:
            holding_hours = i - entry_idx

            # Проверка стопа (консервативно: если лой бара ниже стопа)
            if lows[i] <= current_stop:
                trades[-1].exit_idx = i
                trades[-1].exit_time = times[i]
                trades[-1].exit_price = current_stop
                trades[-1].exit_reason = "stop"
                gross = (current_stop / trades[-1].entry_price - 1.0) * 100
                trades[-1].pnl_pct = gross - TAKER_FEE * 2 * 100
                position_open = False
                continue

            # Проверка тейка (хай бара выше тейка)
            if highs[i] >= current_take:
                trades[-1].exit_idx = i
                trades[-1].exit_time = times[i]
                trades[-1].exit_price = current_take
                trades[-1].exit_reason = "take"
                gross = (current_take / trades[-1].entry_price - 1.0) * 100
                trades[-1].pnl_pct = gross - TAKER_FEE * 2 * 100
                position_open = False
                continue

            # Максимальное удержание
            if holding_hours >= MAX_HOLDING_H:
                trades[-1].exit_idx = i
                trades[-1].exit_time = times[i]
                trades[-1].exit_price = closes[i]
                trades[-1].exit_reason = "timeout"
                gross = (closes[i] / trades[-1].entry_price - 1.0) * 100
                trades[-1].pnl_pct = gross - TAKER_FEE * 2 * 100
                position_open = False
                continue

            continue  # позиция открыта, новые сигналы не рассматриваем

        # Проверка: пин-бар на уровне?
        if not is_bullish_pin_bar(opens[i], highs[i], lows[i], closes[i]):
            continue

        # Ищем уровни поддержки
        levels = find_support_levels(closes, lows, i)
        if not levels:
            continue

        # Проверяем, входит ли нижняя тень в зону уровня
        level_zone = LEVEL_ZONE_PCT / 100.0
        shadow_low = lows[i]
        on_level = False
        matched_level = 0.0
        for level in levels:
            zone = level * level_zone
            if shadow_low <= level + zone and shadow_low >= level - zone:
                on_level = True
                matched_level = level
                break
            # Или закрытие близко к уровню
            if closes[i] <= level + zone * 2 and closes[i] >= level - zone:
                on_level = True
                matched_level = level
                break

        if not on_level:
            continue

        # Сигнал найден: открываем позицию
        entry_price = closes[i]
        stop_price = lows[i] * 0.999  # за нижней тенью, -0.1%

        # Проверка минимального стопа
        stop_pct = (entry_price - stop_price) / entry_price * 100
        if stop_pct < MIN_STOP_PCT:
            continue  # слишком маленький стоп, комиссии съедят

        take_price = entry_price + (entry_price - stop_price) * RISK_REWARD

        trades.append(Trade(
            symbol=symbol,
            entry_idx=i,
            entry_time=times[i],
            entry_price=entry_price,
            stop_price=stop_price,
            take_price=take_price,
        ))
        position_open = True
        current_stop = stop_price
        current_take = take_price
        entry_idx = i

    return trades


# ─────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────
def main():
    print("=" * 85)
    print("PIN BAR BACKTEST v1 — пин-бар на уровне поддержки (Н1)")
    print(f"  Уровень: {LEVEL_LOOKBACK_H}h, касаний ≥ {LEVEL_TOUCHES}, зона ±{LEVEL_ZONE_PCT}%")
    print(f"  Пин-бар: нижняя тень ≥ {PIN_SHADOW_RATIO}× тела")
    print(f"  R:R = 1:{RISK_REWARD}, стоп за тенью, макс. удержание {MAX_HOLDING_H}h")
    print(f"  Комиссия: {TAKER_FEE * 100:.2f}% × 2 = {TAKER_FEE * 2 * 100:.2f}%")
    print("=" * 85)

    if not DB_PATH.exists():
        print(f"\n❌ БД не найдена: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    symbols = get_symbols(conn)
    print(f"\nВселенная: {len(symbols)} монет")

    all_trades: list[Trade] = []
    start_time = time.time()

    for i, sym in enumerate(symbols):
        times, opens, highs, lows, closes = load_ohlcv(conn, sym)
        if len(closes) < LEVEL_LOOKBACK_H + MAX_HOLDING_H + 100:
            continue

        trades = simulate_symbol(sym, times, opens, highs, lows, closes)
        all_trades.extend(trades)

        if (i + 1) % 10 == 0 or i == len(symbols) - 1:
            elapsed = time.time() - start_time
            print(f"  [{i + 1:2d}/{len(symbols)}] обработано за {elapsed:.1f}s, сигналов: {len(all_trades)}")

    conn.close()

    # Анализ результатов
    print("\n" + "=" * 85)
    print("📊 РЕЗУЛЬТАТЫ")
    print("=" * 85)

    if not all_trades:
        print("  ❌ Ни одного сигнала не найдено.")
        print("  Попробуйте ослабить параметры (уменьшить PIN_SHADOW_RATIO, увеличить LEVEL_ZONE_PCT)")
        return

    closed = all_trades  # все сделки закрылись (стоп/тейк/таймаут)
    wins = [t for t in closed if t.pnl_pct > 0]
    losses = [t for t in closed if t.pnl_pct <= 0]

    total_pnl = sum(t.pnl_pct for t in closed)
    avg_pnl = total_pnl / len(closed) if closed else 0
    win_rate = len(wins) / len(closed) * 100 if closed else 0

    avg_win = np.mean([t.pnl_pct for t in wins]) if wins else 0
    avg_loss = np.mean([t.pnl_pct for t in losses]) if losses else 0

    # По причинам выхода
    reasons: dict[str, int] = {}
    for t in closed:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1

    # Среднее удержание
    avg_hold = np.mean([
        (t.exit_idx - t.entry_idx) for t in closed
    ]) if closed else 0

    print(f"  Всего сигналов:      {len(closed)}")
    print(f"  Монет с сигналами:   {len(set(t.symbol for t in closed))}")
    print(f"  Win rate:            {win_rate:.1f}% ({len(wins)}/{len(closed)})")
    print(f"  Средний PnL/сделку:  {avg_pnl:+.3f}%")
    print(f"  Средний выигрыш:     {avg_win:+.3f}%")
    print(f"  Средний проигрыш:    {avg_loss:+.3f}%")
    print(f"  Суммарный PnL:       {total_pnl:+.2f}%")
    print(f"  Среднее удержание:   {avg_hold:.1f}h")
    print()
    print("  📊 Причины выхода:")
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"    {reason:<10}: {count}")

    # Прибыльность
    print()
    if win_rate > 0 and avg_loss != 0:
        profit_factor = abs(sum(t.pnl_pct for t in wins) / sum(t.pnl_pct for t in losses)) if losses else float('inf')
        expectancy = avg_pnl
        breakeven_wr = abs(avg_loss) / (abs(avg_loss) + avg_win) * 100 if avg_win > 0 else 100
        print(f"  Profit Factor:       {profit_factor:.2f}")
        print(f"  Expectancy/сделку:   {expectancy:+.3f}%")
        print(f"  Breakeven win rate:  {breakeven_wr:.1f}%")
        print(f"  Реальный win rate:   {win_rate:.1f}%")
        print()
        if win_rate > breakeven_wr and profit_factor > 1.0:
            print("  ✅ Стратегия прибыльна (с учётом комиссий)")
        else:
            print("  ❌ Стратегия убыточна или на грани безубыточности")

    # Топ монеты
    print("\n📈 Топ-5 монет по суммарному PnL:")
    by_symbol: dict[str, list[float]] = {}
    for t in closed:
        by_symbol.setdefault(t.symbol, []).append(t.pnl_pct)
    sorted_syms = sorted(by_symbol.items(), key=lambda kv: sum(kv[1]), reverse=True)
    for sym, pnls in sorted_syms[:5]:
        print(f"  {sym:<15} {sum(pnls):+8.2f}%  ({len(pnls)} trades)")

    print("\n📉 Худшие 5 монет:")
    for sym, pnls in sorted_syms[-5:]:
        print(f"  {sym:<15} {sum(pnls):+8.2f}%  ({len(pnls)} trades)")

    print("\n" + "=" * 85)
    print("✅ Готово")
    print("=" * 85)


if __name__ == "__main__":
    main()