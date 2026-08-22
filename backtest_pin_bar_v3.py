#!/usr/bin/env python3
"""
backtest_pin_bar_v3.py — Пин-бар на скользящих уровнях Герчика (Н1).

Ключевые исправления относительно v2:
1. Скользящие уровни: определяются на последних 96 часах для каждого бара
2. Узкий допуск зоны (10% ширины)
3. Усиленный фильтр пин-бара (тень >= 3× тела)
4. Комиссия 0.28% на круг (0.14% × 2)

Стратегия:
1. Для каждой монеты: определяем уровни на скользящем окне (96h, шаг 24h)
2. Ищем пин-бары на актуальных уровнях
3. Вход на закрытии пин-бара, стоп за тенью, тейк 2× стопа

Данные: data/klines_top50.sqlite (часовые свечи)
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

# Параметры Герчика
LEFT_BARS = 5              # баров слева для фрактала
RIGHT_BARS = 5             # баров справа для фрактала
ATR_PERIOD = 14            # период ATR
CLUSTER_ATR_MULT = 1.5     # множитель ATR для ширины зоны
MIN_TOUCHES = 2            # минимум касаний уровня
SCORE_THRESHOLD = 50.0     # порог скоринга уровня (0-100)
MIN_FALSE_BREAKOUTS = 1    # минимум ложных пробоев у уровня

# Скользящее окно для уровней
LEVEL_WINDOW_H = 96        # 4 дня для формирования уровня
LEVEL_STEP_H = 24          # пересчёт уровней каждые 24 часа

# Параметры пин-бара
PIN_SHADOW_RATIO = 3.0     # нижняя тень >= 3× тела (было 2.0)
ZONE_TOLERANCE = 0.10      # допуск зоны: 10% ширины (было 50% + 0.2%)
RISK_REWARD = 2.0          # тейк = 2× стопа
TAKER_FEE = 0.0014         # 0.14% per side (0.28% на круг)
MAX_HOLDING_H = 72         # максимум 72 часа удержания
MIN_STOP_PCT = 0.3         # минимальный стоп 0.3%


# ─────────────────────────────────────────────────────────────────────
# GERCHIK LEVEL DETECTOR (адаптированный)
# ─────────────────────────────────────────────────────────────────────
def calculate_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                  period: int = ATR_PERIOD) -> np.ndarray:
    """Расчёт ATR."""
    n = len(closes)
    if n < period + 1:
        return np.zeros(n)
    
    tr = np.zeros(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr[i] = max(hl, hc, lc)
    
    atr = np.zeros(n)
    atr[period - 1] = np.mean(tr[:period])
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    
    return atr


def find_pivots(highs: np.ndarray, lows: np.ndarray,
                left_bars: int = LEFT_BARS,
                right_bars: int = RIGHT_BARS) -> list[dict]:
    """Поиск значимых Swing High и Swing Low (фракталы)."""
    n = len(highs)
    pivots = []
    
    for i in range(left_bars, n - right_bars):
        # Swing High
        is_high = True
        for j in range(1, left_bars + 1):
            if highs[i - j] >= highs[i]:
                is_high = False
                break
        if is_high:
            for j in range(1, right_bars + 1):
                if highs[i + j] >= highs[i]:
                    is_high = False
                    break
        if is_high:
            pivots.append({'index': i, 'price': highs[i], 'type': 'high'})
        
        # Swing Low
        is_low = True
        for j in range(1, left_bars + 1):
            if lows[i - j] <= lows[i]:
                is_low = False
                break
        if is_low:
            for j in range(1, right_bars + 1):
                if lows[i + j] <= lows[i]:
                    is_low = False
                    break
        if is_low:
            pivots.append({'index': i, 'price': lows[i], 'type': 'low'})
    
    return pivots


def cluster_pivots(pivots: list[dict], atr: float,
                   eps_mult: float = CLUSTER_ATR_MULT) -> list[list[dict]]:
    """Простая кластеризация пивотов по цене."""
    if not pivots:
        return []
    
    eps = atr * eps_mult
    sorted_pivots = sorted(pivots, key=lambda p: p['price'])
    
    clusters = []
    current_cluster = [sorted_pivots[0]]
    
    for i in range(1, len(sorted_pivots)):
        if sorted_pivots[i]['price'] - current_cluster[-1]['price'] <= eps:
            current_cluster.append(sorted_pivots[i])
        else:
            if len(current_cluster) >= 2:
                clusters.append(current_cluster)
            current_cluster = [sorted_pivots[i]]
    
    if len(current_cluster) >= 2:
        clusters.append(current_cluster)
    
    return clusters


def analyze_zone(cluster: list[dict], highs: np.ndarray, lows: np.ndarray,
                 closes: np.ndarray, min_touches: int = MIN_TOUCHES) -> dict:
    """Анализ конкретной зоны (кластера) и сбор метрик."""
    prices = [p['price'] for p in cluster]
    zone_high = max(prices)
    zone_low = min(prices)
    zone_center = (zone_high + zone_low) / 2
    zone_width = zone_high - zone_low
    
    metrics = {
        'upper': zone_high,
        'lower': zone_low,
        'center': zone_center,
        'touches': 0,
        'false_breakouts': 0,
        'is_mirror': False,
        'reaction_strength': 0.0,
        'round_number_boost': 0,
        'impulse_before': 0.0,
    }
    
    # Типы пивотов в зоне (для зеркальности)
    types_in_zone = set(p['type'] for p in cluster)
    if len(types_in_zone) > 1:
        metrics['is_mirror'] = True
    
    # Подсчёт касаний и ложных пробоев
    n = len(closes)
    touches = 0
    prev_touch_idx = -999
    
    for i in range(n):
        # Бар заходит в зону
        if highs[i] >= zone_low and lows[i] <= zone_high:
            if i - prev_touch_idx > 3:  # кулдаун 3 бара
                touches += 1
                prev_touch_idx = i
                
                # Проверка ложного пробоя
                if (highs[i] > zone_high and closes[i] < zone_high) or \
                   (lows[i] < zone_low and closes[i] > zone_low):
                    metrics['false_breakouts'] += 1
                
                # Оценка силы реакции (отскока)
                if i + 5 < n:
                    if closes[i] <= zone_center:  # отскок вверх
                        max_up = np.max(highs[i + 1:i + 6])
                        metrics['reaction_strength'] += (max_up - closes[i]) / closes[i] * 100
                    else:  # отскок вниз
                        min_down = np.min(lows[i + 1:i + 6])
                        metrics['reaction_strength'] += (closes[i] - min_down) / closes[i] * 100
    
    metrics['touches'] = touches
    
    # Круглые числа
    if zone_center > 0:
        price_magnitude = len(str(int(zone_center)))
        if price_magnitude >= 2:
            round_step = 10 ** (price_magnitude - 2)
            nearest_round = round(zone_center / round_step) * round_step
            distance_pct = abs(zone_center - nearest_round) / zone_center * 100
            if distance_pct < 0.5:
                metrics['round_number_boost'] = 15
            elif distance_pct < 1.5:
                metrics['round_number_boost'] = 5
    
    # Импульс до уровня
    first_touch_idx = min(p['index'] for p in cluster)
    if first_touch_idx > 20:
        past_price = closes[first_touch_idx - 20]
        move_pct = abs(zone_center - past_price) / past_price * 100
        metrics['impulse_before'] = min(move_pct, 20.0)
    
    return metrics


def calculate_score(metrics: dict) -> float:
    """Система скоринга силы уровня."""
    score = 0.0
    
    # Касания (макс 30 баллов)
    score += min(metrics['touches'] * 6, 30)
    
    # Ложные пробои (макс 25 баллов)
    score += min(metrics['false_breakouts'] * 12.5, 25)
    
    # Зеркальность (15 баллов)
    if metrics['is_mirror']:
        score += 15
    
    # Сила реакции (макс 15 баллов)
    score += min(metrics['reaction_strength'] * 2, 15)
    
    # Круглое число (макс 15 баллов)
    score += metrics['round_number_boost']
    
    # Импульс до уровня (макс 10 баллов)
    if metrics['impulse_before'] > 10:
        score += 10
    elif metrics['impulse_before'] > 5:
        score += 5
    
    return round(score, 2)


def detect_levels_on_window(highs: np.ndarray, lows: np.ndarray,
                            closes: np.ndarray,
                            score_threshold: float = SCORE_THRESHOLD) -> list[dict]:
    """Обнаружение уровней на окне данных."""
    n = len(closes)
    if n < ATR_PERIOD + LEFT_BARS + RIGHT_BARS + 10:
        return []
    
    # 1. Расчёт ATR
    atr = calculate_atr(highs, lows, closes)
    current_atr = atr[-1]
    if current_atr <= 0:
        return []
    
    # 2. Поиск пивотов
    pivots = find_pivots(highs, lows)
    if not pivots:
        return []
    
    # 3. Кластеризация
    clusters = cluster_pivots(pivots, current_atr)
    if not clusters:
        return []
    
    # 4. Анализ зон и скоринг
    levels = []
    for cluster in clusters:
        if len(cluster) < MIN_TOUCHES:
            continue
        
        metrics = analyze_zone(cluster, highs, lows, closes)
        if metrics['touches'] < MIN_TOUCHES:
            continue
        
        # Фильтр по ложным пробоям
        if metrics['false_breakouts'] < MIN_FALSE_BREAKOUTS:
            continue
        
        metrics['score'] = calculate_score(metrics)
        metrics['cluster_size'] = len(cluster)
        
        if metrics['score'] >= score_threshold:
            levels.append(metrics)
    
    # Сортируем по скорингу
    levels.sort(key=lambda x: x['score'], reverse=True)
    return levels


# ─────────────────────────────────────────────────────────────────────
# ДЕТЕКЦИЯ ПИН-БАРА
# ─────────────────────────────────────────────────────────────────────
def is_bullish_pin_bar(open_p: float, high_p: float, low_p: float, close_p: float,
                       shadow_ratio: float = PIN_SHADOW_RATIO) -> bool:
    """Бычий пин-бар."""
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
    
    # Нижняя тень >= 3× тела
    if body <= 0:
        if lower_shadow < candle_range * 0.7:
            return False
    else:
        if lower_shadow < body * shadow_ratio:
            return False
    
    # Тело в верхней половине диапазона
    body_center = (open_p + close_p) / 2.0
    if body_center < low_p + candle_range * 0.5:
        return False
    
    # Верхняя тень не должна быть большой
    if upper_shadow > candle_range * 0.25:
        return False
    
    return True


# ─────────────────────────────────────────────────────────────────────
# ДАННЫЕ
# ─────────────────────────────────────────────────────────────────────
def get_symbols(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM klines ORDER BY symbol"
    ).fetchall()
    return [r[0] for r in rows]


def load_ohlcv(conn: sqlite3.Connection, symbol: str):
    """Загрузка OHLC: (times, opens, highs, lows, closes)."""
    rows = conn.execute(
        "SELECT open_time_ms, open, high, low, close FROM klines "
        "WHERE symbol = ? AND interval = '1h' "
        "ORDER BY open_time_ms",
        (symbol,),
    ).fetchall()
    if not rows:
        return tuple(np.array([]) for _ in range(5))
    times = np.array([r[0] for r in rows])
    opens = np.array([float(r[1]) for r in rows])
    highs = np.array([float(r[2]) for r in rows])
    lows = np.array([float(r[3]) for r in rows])
    closes = np.array([float(r[4]) for r in rows])
    return times, opens, highs, lows, closes


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
    level_score: float = 0.0
    exit_idx: int = 0
    exit_time: int = 0
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl_pct: float = 0.0


def simulate_symbol(symbol: str, times: np.ndarray, opens: np.ndarray,
                    highs: np.ndarray, lows: np.ndarray,
                    closes: np.ndarray) -> list[Trade]:
    """Симуляция стратегии пин-бара на скользящих уровнях."""
    n = len(closes)
    if n < LEVEL_WINDOW_H + MAX_HOLDING_H + 100:
        return []
    
    # Кэшируем уровни для каждого окна (чтобы не пересчитывать для каждого бара)
    level_cache: dict[int, list[dict]] = {}
    
    trades: list[Trade] = []
    position_open = False
    current_stop = 0.0
    current_take = 0.0
    entry_idx = 0
    
    for i in range(LEVEL_WINDOW_H, n):
        # Если позиция открыта — проверяем стоп/тейк
        if position_open:
            holding_hours = i - entry_idx
            
            if lows[i] <= current_stop:
                trades[-1].exit_idx = i
                trades[-1].exit_time = times[i]
                trades[-1].exit_price = current_stop
                trades[-1].exit_reason = "stop"
                gross = (current_stop / trades[-1].entry_price - 1.0) * 100
                trades[-1].pnl_pct = gross - TAKER_FEE * 2 * 100
                position_open = False
                continue
            
            if highs[i] >= current_take:
                trades[-1].exit_idx = i
                trades[-1].exit_time = times[i]
                trades[-1].exit_price = current_take
                trades[-1].exit_reason = "take"
                gross = (current_take / trades[-1].entry_price - 1.0) * 100
                trades[-1].pnl_pct = gross - TAKER_FEE * 2 * 100
                position_open = False
                continue
            
            if holding_hours >= MAX_HOLDING_H:
                trades[-1].exit_idx = i
                trades[-1].exit_time = times[i]
                trades[-1].exit_price = closes[i]
                trades[-1].exit_reason = "timeout"
                gross = (closes[i] / trades[-1].entry_price - 1.0) * 100
                trades[-1].pnl_pct = gross - TAKER_FEE * 2 * 100
                position_open = False
                continue
            
            continue
        
        # Проверка: пин-бар?
        if not is_bullish_pin_bar(opens[i], highs[i], lows[i], closes[i]):
            continue
        
        # Определяем ключ кэша (округляем до шага 24 часа)
        window_key = (i // LEVEL_STEP_H) * LEVEL_STEP_H
        
        # Получаем уровни из кэша или пересчитываем
        if window_key not in level_cache:
            window_start = max(0, window_key - LEVEL_WINDOW_H)
            window_highs = highs[window_start:window_key]
            window_lows = lows[window_start:window_key]
            window_closes = closes[window_start:window_key]
            
            if len(window_closes) < LEVEL_WINDOW_H // 2:
                level_cache[window_key] = []
            else:
                level_cache[window_key] = detect_levels_on_window(
                    window_highs, window_lows, window_closes
                )
        
        levels = level_cache[window_key]
        if not levels:
            continue
        
        # Проверяем, есть ли сильный уровень рядом с нижней тенью
        shadow_low = lows[i]
        matched_level = None
        
        for level in levels:
            zone_low = level['lower']
            zone_high = level['upper']
            zone_width = zone_high - zone_low
            
            # Узкий допуск: 10% ширины зоны
            tolerance = zone_width * ZONE_TOLERANCE
            
            if shadow_low >= zone_low - tolerance and shadow_low <= zone_high + tolerance:
                matched_level = level
                break
        
        if matched_level is None:
            continue
        
        # Сигнал найден: открываем позицию
        entry_price = closes[i]
        stop_price = lows[i] * 0.999  # за нижней тенью
        
        stop_pct = (entry_price - stop_price) / entry_price * 100
        if stop_pct < MIN_STOP_PCT:
            continue
        
        take_price = entry_price + (entry_price - stop_price) * RISK_REWARD
        
        trades.append(Trade(
            symbol=symbol,
            entry_idx=i,
            entry_time=times[i],
            entry_price=entry_price,
            stop_price=stop_price,
            take_price=take_price,
            level_score=matched_level['score'],
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
    print("PIN BAR BACKTEST v3 — скользящие уровни Герчика (Н1)")
    print(f"  Уровень: окно {LEVEL_WINDOW_H}h, скоринг ≥ {SCORE_THRESHOLD}, ложных пробоев ≥ {MIN_FALSE_BREAKOUTS}")
    print(f"  Пин-бар: нижняя тень ≥ {PIN_SHADOW_RATIO}× тела")
    print(f"  Допуск зоны: {ZONE_TOLERANCE * 100:.0f}% ширины")
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
        if len(closes) < LEVEL_WINDOW_H + MAX_HOLDING_H + 100:
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
        print("  Попробуйте снизить SCORE_THRESHOLD или ослабить другие фильтры")
        return

    closed = all_trades
    wins = [t for t in closed if t.pnl_pct > 0]
    losses = [t for t in closed if t.pnl_pct <= 0]

    total_pnl = sum(t.pnl_pct for t in closed)
    avg_pnl = total_pnl / len(closed) if closed else 0
    win_rate = len(wins) / len(closed) * 100 if closed else 0

    avg_win = np.mean([t.pnl_pct for t in wins]) if wins else 0
    avg_loss = np.mean([t.pnl_pct for t in losses]) if losses else 0

    reasons: dict[str, int] = {}
    for t in closed:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1

    avg_hold = np.mean([max(0, t.exit_idx - t.entry_idx) for t in closed]) if closed else 0

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
    if wins and losses:
        profit_factor = abs(sum(t.pnl_pct for t in wins) / sum(t.pnl_pct for t in losses))
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

    # Анализ по скорингу уровней
    print("\n📊 Анализ по скорингу уровней:")
    score_bins = [(50, 60), (60, 70), (70, 80), (80, 90), (90, 100)]
    for low, high in score_bins:
        bin_trades = [t for t in closed if low <= t.level_score < high]
        if bin_trades:
            bin_wins = [t for t in bin_trades if t.pnl_pct > 0]
            bin_wr = len(bin_wins) / len(bin_trades) * 100
            bin_pnl = sum(t.pnl_pct for t in bin_trades)
            print(f"  Скоринг {low}-{high}: {len(bin_trades)} сделок, WR {bin_wr:.1f}%, PnL {bin_pnl:+.2f}%")

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