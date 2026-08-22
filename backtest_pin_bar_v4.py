#!/usr/bin/env python3
"""
backtest_pin_bar_v4.py — Пин-бар / рельсы на уровнях Герчика (Н1).

Согласованные параметры (RESEARCH_NOTES.md, v4 baseline):
- Уровень: Gerchik detector, скользящее окно 96h, пересчёт 24h, score >= 50
- Подход: цена падает к зоне красными свечами
- Триггер A: пин-бар (ЛЮБОЙ цвет), тень >= 2x тела, входит в зону
- Триггер B: рельсы (красная -> зелёная), закрытие зелёной >= открытия
  красной, low красной в зоне
- Вход: на следующей свече после триггера
- Стоп: за тенью / за low рельс, тейк 2x стопа, макс. удержание 72h
- Комиссия: 0.14% x 2 = 0.28%

Правило итераций: менять ОДИН параметр за запуск, лог в RESEARCH_NOTES.md
"""
from __future__ import annotations

import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# ─────────────────────────────────────────────────────────────────────
# ПАРАМЕТРЫ (baseline v4 — не менять без лога в RESEARCH_NOTES.md)
# ─────────────────────────────────────────────────────────────────────
DB_PATH = Path("data/klines_top50.sqlite")

# Уровень (Герчик)
LEFT_BARS = 5
RIGHT_BARS = 5
ATR_PERIOD = 14
CLUSTER_ATR_MULT = 1.5
MIN_TOUCHES = 2
SCORE_THRESHOLD = 50.0
MIN_FALSE_BREAKOUTS = 1
LEVEL_WINDOW_H = 96
LEVEL_STEP_H = 24
ZONE_TOLERANCE = 0.10

# Подход к зоне (красные свечи)
APPROACH_BARS = 3
MIN_RED_BARS_PIN = 2      # для пин-бара: >=2 красные из 3
MIN_RED_BARS_RAILS = 1    # для рельс: >=1 красная из 3 (мягче)

# Триггеры
PIN_SHADOW_RATIO = 2.0    # тень >= 2x тела, цвет любой
RAILS_BODY_MIN = 0.4      # тела рельс >= 40% диапазона (не доджи)

# Вход/выход
RISK_REWARD = 2.0
TAKER_FEE = 0.0014        # 0.14% per side = 0.28% на круг
MAX_HOLDING_H = 72
MIN_STOP_PCT = 0.3


# ─────────────────────────────────────────────────────────────────────
# GERCHIK LEVEL DETECTOR (без изменений из v3)
# ─────────────────────────────────────────────────────────────────────
def calculate_atr(highs, lows, closes, period=ATR_PERIOD):
    n = len(closes)
    if n < period + 1:
        return np.zeros(n)
    tr = np.zeros(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]))
    atr = np.zeros(n)
    atr[period - 1] = np.mean(tr[:period])
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def find_pivots(highs, lows, left_bars=LEFT_BARS, right_bars=RIGHT_BARS):
    n = len(highs)
    pivots = []
    for i in range(left_bars, n - right_bars):
        is_high = all(highs[i - j] < highs[i] for j in range(1, left_bars + 1))
        if is_high:
            is_high = all(highs[i + j] < highs[i] for j in range(1, right_bars + 1))
        if is_high:
            pivots.append({'index': i, 'price': highs[i], 'type': 'high'})

        is_low = all(lows[i - j] > lows[i] for j in range(1, left_bars + 1))
        if is_low:
            is_low = all(lows[i + j] > lows[i] for j in range(1, right_bars + 1))
        if is_low:
            pivots.append({'index': i, 'price': lows[i], 'type': 'low'})
    return pivots


def cluster_pivots(pivots, atr, eps_mult=CLUSTER_ATR_MULT):
    if not pivots:
        return []
    eps = atr * eps_mult
    sorted_pivots = sorted(pivots, key=lambda p: p['price'])
    clusters = []
    current = [sorted_pivots[0]]
    for i in range(1, len(sorted_pivots)):
        if sorted_pivots[i]['price'] - current[-1]['price'] <= eps:
            current.append(sorted_pivots[i])
        else:
            if len(current) >= 2:
                clusters.append(current)
            current = [sorted_pivots[i]]
    if len(current) >= 2:
        clusters.append(current)
    return clusters


def analyze_zone(cluster, highs, lows, closes):
    prices = [p['price'] for p in cluster]
    zone_high = max(prices)
    zone_low = min(prices)
    zone_center = (zone_high + zone_low) / 2

    metrics = {
        'upper': zone_high, 'lower': zone_low, 'center': zone_center,
        'touches': 0, 'false_breakouts': 0, 'is_mirror': False,
        'reaction_strength': 0.0, 'round_number_boost': 0, 'impulse_before': 0.0,
    }

    if len(set(p['type'] for p in cluster)) > 1:
        metrics['is_mirror'] = True

    n = len(closes)
    touches = 0
    prev_touch_idx = -999
    for i in range(n):
        if highs[i] >= zone_low and lows[i] <= zone_high:
            if i - prev_touch_idx > 3:
                touches += 1
                prev_touch_idx = i
                if (highs[i] > zone_high and closes[i] < zone_high) or \
                   (lows[i] < zone_low and closes[i] > zone_low):
                    metrics['false_breakouts'] += 1
                if i + 5 < n:
                    if closes[i] <= zone_center:
                        max_up = np.max(highs[i + 1:i + 6])
                        metrics['reaction_strength'] += (max_up - closes[i]) / closes[i] * 100
                    else:
                        min_down = np.min(lows[i + 1:i + 6])
                        metrics['reaction_strength'] += (closes[i] - min_down) / closes[i] * 100
    metrics['touches'] = touches

    if zone_center > 0:
        mag = len(str(int(zone_center)))
        if mag >= 2:
            step = 10 ** (mag - 2)
            nearest = round(zone_center / step) * step
            dist = abs(zone_center - nearest) / zone_center * 100
            if dist < 0.5:
                metrics['round_number_boost'] = 15
            elif dist < 1.5:
                metrics['round_number_boost'] = 5

    first_idx = min(p['index'] for p in cluster)
    if first_idx > 20:
        past = closes[first_idx - 20]
        metrics['impulse_before'] = min(abs(zone_center - past) / past * 100, 20.0)

    return metrics


def calculate_score(m):
    score = 0.0
    score += min(m['touches'] * 6, 30)
    score += min(m['false_breakouts'] * 12.5, 25)
    if m['is_mirror']:
        score += 15
    score += min(m['reaction_strength'] * 2, 15)
    score += m['round_number_boost']
    if m['impulse_before'] > 10:
        score += 10
    elif m['impulse_before'] > 5:
        score += 5
    return round(score, 2)


def detect_levels_on_window(highs, lows, closes):
    n = len(closes)
    if n < ATR_PERIOD + LEFT_BARS + RIGHT_BARS + 10:
        return []
    atr = calculate_atr(highs, lows, closes)
    if atr[-1] <= 0:
        return []
    pivots = find_pivots(highs, lows)
    if not pivots:
        return []
    clusters = cluster_pivots(pivots, atr[-1])
    if not clusters:
        return []

    levels = []
    for cluster in clusters:
        if len(cluster) < MIN_TOUCHES:
            continue
        m = analyze_zone(cluster, highs, lows, closes)
        if m['touches'] < MIN_TOUCHES:
            continue
        if m['false_breakouts'] < MIN_FALSE_BREAKOUTS:
            continue
        m['score'] = calculate_score(m)
        if m['score'] >= SCORE_THRESHOLD:
            levels.append(m)

    levels.sort(key=lambda x: x['score'], reverse=True)
    return levels


# ─────────────────────────────────────────────────────────────────────
# ТРИГГЕРЫ
# ─────────────────────────────────────────────────────────────────────
def is_pin_bar(o, h, l, c, shadow_ratio=PIN_SHADOW_RATIO):
    """Пин-бар ЛЮБОГО цвета: тень >= 2x тела, тело в верхней половине."""
    if h <= l or l <= 0:
        return False
    body = abs(c - o)
    rng = h - l
    if rng <= 0 or rng / l * 100 < 0.05:
        return False

    lower_shadow = min(o, c) - l
    upper_shadow = h - max(o, c)

    if body <= 0:
        if lower_shadow < rng * 0.6:
            return False
    else:
        if lower_shadow < body * shadow_ratio:
            return False

    body_center = (o + c) / 2.0
    if body_center < l + rng * 0.5:
        return False
    if upper_shadow > rng * 0.3:
        return False
    return True


def is_rails(o1, h1, l1, c1, o2, h2, l2, c2):
    """Рельсы: красная -> зелёная, зелёная закрывается >= открытия красной."""
    if c1 >= o1:          # первая — красная
        return False
    if c2 <= o2:          # вторая — зелёная
        return False
    if c2 < o1:           # engulfing тела красной
        return False
    r1, r2 = h1 - l1, h2 - l2
    if r1 <= 0 or r2 <= 0:
        return False
    if abs(c1 - o1) < r1 * RAILS_BODY_MIN:   # тела заметные
        return False
    if abs(c2 - o2) < r2 * RAILS_BODY_MIN:
        return False
    return True


def red_approach(opens, closes, anchor, bars, min_red):
    """
    Подход красными свечами: среди `bars` свечей ДО `anchor`
    минимум `min_red` красных, и цена реально снизилась.
    """
    if anchor < bars:
        return False
    red_count = sum(
        1 for k in range(1, bars + 1) if closes[anchor - k] < opens[anchor - k]
    )
    if red_count < min_red:
        return False
    return closes[anchor - bars] > closes[anchor]


# ─────────────────────────────────────────────────────────────────────
# ДАННЫЕ
# ─────────────────────────────────────────────────────────────────────
def get_symbols(conn):
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM klines ORDER BY symbol"
    ).fetchall()
    return [r[0] for r in rows]


def load_ohlcv(conn, symbol):
    rows = conn.execute(
        "SELECT open_time_ms, open, high, low, close FROM klines "
        "WHERE symbol = ? AND interval = '1h' "
        "ORDER BY open_time_ms",
        (symbol,),
    ).fetchall()
    if not rows:
        return tuple(np.array([]) for _ in range(5))
    return (
        np.array([r[0] for r in rows]),
        np.array([float(r[1]) for r in rows]),
        np.array([float(r[2]) for r in rows]),
        np.array([float(r[3]) for r in rows]),
        np.array([float(r[4]) for r in rows]),
    )


# ─────────────────────────────────────────────────────────────────────
# СИМУЛЯЦИЯ
# ─────────────────────────────────────────────────────────────────────
@dataclass
class Trade:
    symbol: str
    trigger: str
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


def close_trade(t: Trade, i: int, times, closes, price: float, reason: str):
    t.exit_idx = i
    t.exit_time = times[i]
    t.exit_price = price
    t.exit_reason = reason
    gross = (price / t.entry_price - 1.0) * 100
    t.pnl_pct = gross - TAKER_FEE * 2 * 100


def simulate_symbol(symbol, times, opens, highs, lows, closes):
    n = len(closes)
    if n < LEVEL_WINDOW_H + MAX_HOLDING_H + 100:
        return []

    level_cache = {}
    trades = []
    position_open = False

    for i in range(LEVEL_WINDOW_H, n):
        if position_open:
            t = trades[-1]
            if lows[i] <= t.stop_price:
                close_trade(t, i, times, closes, t.stop_price, "stop")
                position_open = False
                continue
            if highs[i] >= t.take_price:
                close_trade(t, i, times, closes, t.take_price, "take")
                position_open = False
                continue
            if i - t.entry_idx >= MAX_HOLDING_H:
                close_trade(t, i, times, closes, closes[i], "timeout")
                position_open = False
                continue
            continue

        # ── Определяем триггер ──────────────────────────────────────
        trigger = None
        trigger_low = 0.0

        # A: пин-бар (любой цвет) + красный подход
        if is_pin_bar(opens[i], highs[i], lows[i], closes[i]) and \
           red_approach(opens, closes, i, APPROACH_BARS, MIN_RED_BARS_PIN):
            trigger = 'pin'
            trigger_low = lows[i]

        # B: рельсы (красная -> зелёная) + подход до красной рельсы
        if trigger is None and i >= 1 and \
           is_rails(opens[i - 1], highs[i - 1], lows[i - 1], closes[i - 1],
                    opens[i], highs[i], lows[i], closes[i]) and \
           red_approach(opens, closes, i - 1, APPROACH_BARS, MIN_RED_BARS_RAILS):
            trigger = 'rails'
            trigger_low = min(lows[i - 1], lows[i])

        if trigger is None:
            continue

        # ── Уровни из кэша (скользящее окно) ────────────────────────
        window_key = (i // LEVEL_STEP_H) * LEVEL_STEP_H
        if window_key not in level_cache:
            ws = max(0, window_key - LEVEL_WINDOW_H)
            wc = closes[ws:window_key]
            if len(wc) < LEVEL_WINDOW_H // 2:
                level_cache[window_key] = []
            else:
                level_cache[window_key] = detect_levels_on_window(
                    highs[ws:window_key], lows[ws:window_key], wc
                )
        levels = level_cache[window_key]
        if not levels:
            continue

        # ── Тень/low триггера входит в зону сильного уровня ─────────
        matched = None
        for level in levels:
            zw = level['upper'] - level['lower']
            tol = zw * ZONE_TOLERANCE
            if level['lower'] - tol <= trigger_low <= level['upper'] + tol:
                matched = level
                break
        if matched is None:
            continue

        # ── Вход ────────────────────────────────────────────────────
        entry_price = closes[i]
        stop_price = trigger_low * 0.999
        if (entry_price - stop_price) / entry_price * 100 < MIN_STOP_PCT:
            continue
        take_price = entry_price + (entry_price - stop_price) * RISK_REWARD

        trades.append(Trade(
            symbol=symbol,
            trigger=trigger,
            entry_idx=i,
            entry_time=times[i],
            entry_price=entry_price,
            stop_price=stop_price,
            take_price=take_price,
            level_score=matched['score'],
        ))
        position_open = True

    return trades


# ─────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────
def main():
    print("=" * 85)
    print("PIN BAR BACKTEST v4 — пин-бар/рельсы на уровнях Герчика (Н1)")
    print(f"  Уровень: окно {LEVEL_WINDOW_H}h, score ≥ {SCORE_THRESHOLD}, ложных пробоев ≥ {MIN_FALSE_BREAKOUTS}")
    print(f"  Пин-бар: тень ≥ {PIN_SHADOW_RATIO}× тела (цвет любой), подход {MIN_RED_BARS_PIN} из {APPROACH_BARS} красных")
    print(f"  Рельсы: красная→зелёная, подход {MIN_RED_BARS_RAILS} из {APPROACH_BARS} красных")
    print(f"  R:R = 1:{RISK_REWARD}, стоп за тенью, макс. удержание {MAX_HOLDING_H}h")
    print(f"  Комиссия: {TAKER_FEE * 2 * 100:.2f}% на круг")
    print("=" * 85)

    if not DB_PATH.exists():
        print(f"\n❌ БД не найдена: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    symbols = get_symbols(conn)
    print(f"\nВселенная: {len(symbols)} монет")

    all_trades = []
    start_time = time.time()
    for i, sym in enumerate(symbols):
        times, opens, highs, lows, closes = load_ohlcv(conn, sym)
        all_trades.extend(simulate_symbol(sym, times, opens, highs, lows, closes))
        if (i + 1) % 10 == 0 or i == len(symbols) - 1:
            print(f"  [{i + 1:2d}/{len(symbols)}] {time.time() - start_time:.1f}s, сигналов: {len(all_trades)}")
    conn.close()

    print("\n" + "=" * 85)
    print("📊 РЕЗУЛЬТАТЫ (baseline v4)")
    print("=" * 85)

    if not all_trades:
        print("  ❌ Ни одного сигнала. Ослабь ОДИН параметр и логируй в RESEARCH_NOTES.md")
        return

    closed = all_trades
    wins = [t for t in closed if t.pnl_pct > 0]
    losses = [t for t in closed if t.pnl_pct <= 0]
    total_pnl = sum(t.pnl_pct for t in closed)
    win_rate = len(wins) / len(closed) * 100
    avg_win = np.mean([t.pnl_pct for t in wins]) if wins else 0
    avg_loss = np.mean([t.pnl_pct for t in losses]) if losses else 0

    print(f"  Всего сигналов:      {len(closed)}")
    print(f"  Монет с сигналами:   {len(set(t.symbol for t in closed))}")
    print(f"  Win rate:            {win_rate:.1f}% ({len(wins)}/{len(closed)})")
    print(f"  Средний PnL/сделку:  {total_pnl / len(closed):+.3f}%")
    print(f"  Средний выигрыш:     {avg_win:+.3f}%")
    print(f"  Средний проигрыш:    {avg_loss:+.3f}%")
    print(f"  Суммарный PnL:       {total_pnl:+.2f}%")

    reasons = {}
    for t in closed:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    print("\n  📊 Причины выхода:")
    for r, c in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"    {r:<10}: {c}")

    if wins and losses:
        pf = abs(sum(t.pnl_pct for t in wins) / sum(t.pnl_pct for t in losses))
        be_wr = abs(avg_loss) / (abs(avg_loss) + avg_win) * 100 if avg_win > 0 else 100
        print(f"\n  Profit Factor:       {pf:.2f}")
        print(f"  Breakeven win rate:  {be_wr:.1f}%")
        print(f"  Реальный win rate:   {win_rate:.1f}%")
        print()
        print("  ✅ Прибыльна" if win_rate > be_wr and pf > 1.0 else "  ❌ Убыточна")

    print("\n📊 По типу триггера:")
    for trig in ("pin", "rails"):
        sub = [t for t in closed if t.trigger == trig]
        if sub:
            w = [t for t in sub if t.pnl_pct > 0]
            print(f"  {trig:<6}: {len(sub):5d} сделок, WR {len(w) / len(sub) * 100:4.1f}%, "
                  f"PnL {sum(t.pnl_pct for t in sub):+.2f}%")

    print("\n📊 По скорингу уровней:")
    for lo, hi in [(50, 60), (60, 70), (70, 80), (80, 90), (90, 100)]:
        sub = [t for t in closed if lo <= t.level_score < hi]
        if sub:
            w = [t for t in sub if t.pnl_pct > 0]
            print(f"  {lo}-{hi}: {len(sub):5d} сделок, WR {len(w) / len(sub) * 100:4.1f}%, "
                  f"PnL {sum(t.pnl_pct for t in sub):+.2f}%")

    by_symbol = {}
    for t in closed:
        by_symbol.setdefault(t.symbol, []).append(t.pnl_pct)
    sorted_syms = sorted(by_symbol.items(), key=lambda kv: sum(kv[1]), reverse=True)
    print("\n📈 Топ-5 монет:")
    for sym, pnls in sorted_syms[:5]:
        print(f"  {sym:<15} {sum(pnls):+8.2f}%  ({len(pnls)} trades)")
    print("📉 Худшие 5 монет:")
    for sym, pnls in sorted_syms[-5:]:
        print(f"  {sym:<15} {sum(pnls):+8.2f}%  ({len(pnls)} trades)")

    print("\n" + "=" * 85)
    print("✅ Готово. Результат = запуск #1 (baseline) в логе RESEARCH_NOTES.md")
    print("=" * 85)


if __name__ == "__main__":
    main()