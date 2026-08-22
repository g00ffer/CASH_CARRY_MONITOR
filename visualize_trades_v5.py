#!/usr/bin/env python3
"""
visualize_trades_v5.py — Визуализация сделок v5 с разбивкой по режимам.

Генерирует 8 графиков:
- 2 лучших прибыльных Режим A (верхняя граница)
- 2 худших убыточных Режим A
- 2 лучших прибыльных Режим B (нижняя граница)
- 2 худших убыточных Режим B

Плюс отдельно:
- 2 лучших pin
- 2 худших pin
- 2 лучших rails
- 2 худших rails

Итого 16 графиков для полного разбора.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
from datetime import datetime, timezone

DB_PATH = Path("data/klines_top50.sqlite")
OUTPUT_DIR = Path("visualizations_v5")

# Параметры v5 (должны совпадать с backtest_pin_bar_v5.py)
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
APPROACH_BARS = 3
MIN_RED_BARS_PIN = 2
MIN_RED_BARS_RAILS = 1
PIN_SHADOW_RATIO = 2.0
RAILS_BODY_MIN = 0.4
CLOSE_ZONE_MAX_PCT = 15.0
MAX_APPROACH_DROP_PCT = 15.0
RISK_REWARD = 3.0
TAKER_FEE = 0.0014
MAX_HOLDING_H = 144
MIN_STOP_PCT = 0.5

BARS_BEFORE_ENTRY = 120
BARS_AFTER_ENTRY = 96


# ============================================================================
# Gerchik Level Detector (копия из v5)
# ============================================================================
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


# ============================================================================
# Триггеры и фильтры (копия из v5)
# ============================================================================
def is_pin_bar(o, h, l, c, shadow_ratio=PIN_SHADOW_RATIO):
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


def is_rails_valid(o1, h1, l1, c1, o2, h2, l2, c2):
    if c1 >= o1:
        return False
    if c2 <= o2:
        return False
    r1, r2 = h1 - l1, h2 - l2
    if r1 <= 0 or r2 <= 0:
        return False
    if abs(c1 - o1) < r1 * RAILS_BODY_MIN:
        return False
    if abs(c2 - o2) < r2 * RAILS_BODY_MIN:
        return False
    return is_pin_bar(o1, max(h1, h2), min(l1, l2), c2)


def red_approach(opens, closes, anchor, bars, min_red):
    if anchor < bars:
        return False
    red_count = sum(
        1 for k in range(1, bars + 1) if closes[anchor - k] < opens[anchor - k]
    )
    if red_count < min_red:
        return False
    return closes[anchor - bars] > closes[anchor]


def approach_drop(closes, anchor, bars=APPROACH_BARS):
    if anchor < bars:
        return 0.0
    base = closes[anchor - bars]
    if base <= 0:
        return 0.0
    return (base - closes[anchor]) / base * 100


# ============================================================================
# Данные и симуляция
# ============================================================================
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


def close_trade(t, i, times, price, reason):
    t['exit_idx'] = i
    t['exit_time'] = times[i]
    t['exit_price'] = price
    t['exit_reason'] = reason
    gross = (price / t['entry_price'] - 1.0) * 100
    t['pnl_pct'] = gross - TAKER_FEE * 2 * 100


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
            if lows[i] <= t['stop_price']:
                close_trade(t, i, times, t['stop_price'], "stop")
                position_open = False
                continue
            if highs[i] >= t['take_price']:
                close_trade(t, i, times, t['take_price'], "take")
                position_open = False
                continue
            if i - t['entry_idx'] >= MAX_HOLDING_H:
                close_trade(t, i, times, closes[i], "timeout")
                position_open = False
                continue
            continue

        trigger = None
        trigger_low = 0.0
        anchor = i

        if is_pin_bar(opens[i], highs[i], lows[i], closes[i]) and \
           red_approach(opens, closes, i, APPROACH_BARS, MIN_RED_BARS_PIN):
            trigger = 'pin'
            trigger_low = lows[i]
            anchor = i

        if trigger is None and i >= 1 and \
           is_rails_valid(opens[i - 1], highs[i - 1], lows[i - 1], closes[i - 1],
                          opens[i], highs[i], lows[i], closes[i]) and \
           red_approach(opens, closes, i - 1, APPROACH_BARS, MIN_RED_BARS_RAILS):
            trigger = 'rails'
            trigger_low = min(lows[i - 1], lows[i])
            anchor = i - 1

        if trigger is None:
            continue

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

        matched = None
        for level in levels:
            zw = level['upper'] - level['lower']
            tol = zw * ZONE_TOLERANCE
            if level['lower'] - tol <= trigger_low <= level['upper'] + tol:
                matched = level
                break
        if matched is None:
            continue

        zone_low = matched['lower']
        zone_high = matched['upper']
        zone_mid = (zone_low + zone_high) / 2.0

        if closes[i] > zone_high * (1.0 + CLOSE_ZONE_MAX_PCT / 100.0):
            continue

        if trigger_low >= zone_mid:
            mode = 'A'
            if approach_drop(closes, anchor) > MAX_APPROACH_DROP_PCT:
                continue
        else:
            mode = 'B'

        entry_price = closes[i]
        stop_price = trigger_low * 0.999
        if (entry_price - stop_price) / entry_price * 100 < MIN_STOP_PCT:
            continue
        take_price = entry_price + (entry_price - stop_price) * RISK_REWARD

        trades.append({
            'symbol': symbol,
            'trigger': trigger,
            'mode': mode,
            'entry_idx': i,
            'entry_time': times[i],
            'entry_price': entry_price,
            'stop_price': stop_price,
            'take_price': take_price,
            'level_score': matched['score'],
            'level': matched,
        })
        position_open = True

    return trades


# ============================================================================
# Отрисовка
# ============================================================================
def ms_to_dt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def plot_trade(symbol, times, opens, highs, lows, closes, trade, output_path):
    entry_idx = trade['entry_idx']
    exit_idx = trade.get('exit_idx', len(times) - 1)

    start_idx = max(0, entry_idx - BARS_BEFORE_ENTRY)
    end_idx = min(len(times) - 1, exit_idx + BARS_AFTER_ENTRY)

    t = times[start_idx:end_idx + 1]
    o = opens[start_idx:end_idx + 1]
    h = highs[start_idx:end_idx + 1]
    l = lows[start_idx:end_idx + 1]
    c = closes[start_idx:end_idx + 1]

    n_bars = len(t)

    fig, ax = plt.subplots(figsize=(16, 9))
    fig.patch.set_facecolor('#1a1a2e')
    ax.set_facecolor('#16213e')

    up_color = '#26a69a'
    down_color = '#ef5350'

    width = 0.6
    for i in range(n_bars):
        color = up_color if c[i] >= o[i] else down_color
        body_low = min(o[i], c[i])
        body_high = max(o[i], c[i])
        body_height = body_high - body_low
        if body_height < 1e-10:
            body_height = 1e-10

        rect = Rectangle(
            (i - width / 2, body_low),
            width,
            body_height,
            facecolor=color,
            edgecolor=color,
            linewidth=0.5,
            alpha=0.9,
        )
        ax.add_patch(rect)

        ax.plot([i, i], [l[i], body_low], color=color, linewidth=0.8)
        ax.plot([i, i], [body_high, h[i]], color=color, linewidth=0.8)

    entry_local_idx = entry_idx - start_idx
    exit_local_idx = exit_idx - start_idx if exit_idx is not None else None

    level = trade['level']
    zone_low = level['lower']
    zone_high = level['upper']
    zone_mid = (zone_low + zone_high) / 2.0

    ax.axhspan(zone_low, zone_high, color='#ffd700', alpha=0.15,
               label=f"Zone (score={level['score']:.0f})")
    ax.axhline(zone_high, color='#ffd700', linestyle='--', linewidth=1, alpha=0.6)
    ax.axhline(zone_mid, color='#ffa500', linestyle=':', linewidth=0.8, alpha=0.4,
               label="Mid boundary")
    ax.axhline(zone_low, color='#ffd700', linestyle='--', linewidth=1, alpha=0.6)

    tolerance = (zone_high - zone_low) * ZONE_TOLERANCE
    ax.axhspan(zone_low - tolerance, zone_high + tolerance,
               color='#ffd700', alpha=0.05)

    ax.axhline(trade['stop_price'], color='#ff4444', linestyle=':',
               linewidth=1.5, alpha=0.8,
               label=f"Stop: ${trade['stop_price']:.4f}")
    ax.axhline(trade['take_price'], color='#00ff88', linestyle=':',
               linewidth=1.5, alpha=0.8,
               label=f"Take: ${trade['take_price']:.4f}")

    ax.axhspan(trade['stop_price'], trade['entry_price'],
               color='#ff4444', alpha=0.08)
    ax.axhspan(trade['entry_price'], trade['take_price'],
               color='#00ff88', alpha=0.08)

    ax.annotate('', xy=(entry_local_idx, l[entry_local_idx]),
                xytext=(entry_local_idx, l[entry_local_idx] - (h.max() - l.min()) * 0.08),
                arrowprops=dict(arrowstyle='->', color='#00bfff', lw=2.5,
                                mutation_scale=25))
    ax.text(entry_local_idx,
            l[entry_local_idx] - (h.max() - l.min()) * 0.12,
            f'ENTRY\n${trade["entry_price"]:.4f}',
            ha='center', va='top',
            color='#00bfff', fontsize=9, fontweight='bold',
            bbox=dict(boxstyle='round,pad=0.3', facecolor='#0a1929',
                      edgecolor='#00bfff', alpha=0.9))

    if exit_local_idx is not None:
        pnl = trade.get('pnl_pct', 0)
        exit_color = '#00ff88' if pnl > 0 else '#ff4444'
        exit_label = trade.get('exit_reason', 'unknown').upper()

        ax.annotate('', xy=(exit_local_idx, h[exit_local_idx]),
                    xytext=(exit_local_idx, h[exit_local_idx] + (h.max() - l.min()) * 0.08),
                    arrowprops=dict(arrowstyle='->', color=exit_color, lw=2.5,
                                    mutation_scale=25))
        ax.text(exit_local_idx,
                h[exit_local_idx] + (h.max() - l.min()) * 0.12,
                f'{exit_label}\n${trade["exit_price"]:.4f}',
                ha='center', va='bottom',
                color=exit_color, fontsize=9, fontweight='bold',
                bbox=dict(boxstyle='round,pad=0.3', facecolor='#0a1929',
                          edgecolor=exit_color, alpha=0.9))

    holding_hours = (exit_idx - entry_idx) if exit_idx is not None else 0
    entry_dt = ms_to_dt(times[entry_idx]).strftime('%Y-%m-%d %H:%M')
    exit_dt = ms_to_dt(times[exit_idx]).strftime('%Y-%m-%d %H:%M') if exit_idx else 'OPEN'

    trigger_label = trade['trigger'].upper()
    mode = trade['mode']
    pnl = trade.get('pnl_pct', 0)
    pnl_color = '#00ff88' if pnl > 0 else '#ff4444'
    pnl_symbol = '+' if pnl > 0 else ''

    title = (
        f"{symbol}  |  {trigger_label} Mode {mode} (score={level['score']:.0f})  |  "
        f"{entry_dt} → {exit_dt}  |  "
        f"Hold: {holding_hours}h  |  "
        f"Exit: {trade.get('exit_reason', 'open')}"
    )
    ax.set_title(title, color='white', fontsize=12, fontweight='bold', pad=10)

    pnl_text = f"PnL: {pnl_symbol}{pnl:.3f}%"
    ax.text(0.98, 0.98, pnl_text, transform=ax.transAxes,
            ha='right', va='top',
            color=pnl_color, fontsize=20, fontweight='bold',
            bbox=dict(boxstyle='round,pad=0.5', facecolor='#0a1929',
                      edgecolor=pnl_color, alpha=0.95))

    mode_desc = "Верхняя граница (мягкий откат)" if mode == 'A' else "Нижняя граница (цена пришла)"
    level_info = (
        f"Mode: {mode} - {mode_desc}\n"
        f"Zone: ${zone_low:.4f} - ${zone_high:.4f}\n"
        f"Mid: ${zone_mid:.4f}\n"
        f"Touches: {level['touches']}\n"
        f"R:R = 1:{RISK_REWARD}\n"
        f"Stop: ${trade['stop_price']:.4f}\n"
        f"Take: ${trade['take_price']:.4f}"
    )
    ax.text(0.02, 0.98, level_info, transform=ax.transAxes,
            ha='left', va='top',
            color='#ffd700', fontsize=9,
            family='monospace',
            bbox=dict(boxstyle='round,pad=0.5', facecolor='#0a1929',
                      edgecolor='#ffd700', alpha=0.9))

    ax.set_xlim(-1, n_bars)
    price_margin = (h.max() - l.min()) * 0.15
    ax.set_ylim(l.min() - price_margin, h.max() + price_margin)

    tick_positions = list(range(0, n_bars, 12))
    tick_labels = [
        ms_to_dt(times[start_idx + i]).strftime('%m-%d\n%H:%M')
        if start_idx + i < len(times) else ''
        for i in tick_positions
    ]
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels, color='gray', fontsize=8)

    ax.set_ylabel('Price (USDT)', color='gray', fontsize=10)
    ax.tick_params(axis='y', colors='gray')
    ax.grid(True, alpha=0.15, color='gray')
    ax.legend(loc='lower left', facecolor='#0a1929',
              edgecolor='#ffd700', labelcolor='white', fontsize=8)

    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color('gray')
    ax.spines['bottom'].set_color('gray')

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight',
                facecolor='#1a1a2e', edgecolor='none')
    plt.close()
    print(f"  ✅ {output_path.name}")


# ============================================================================
# MAIN
# ============================================================================
def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    print("=" * 85)
    print("VISUALIZATION v5 — разбор сделок по режимам A/B")
    print("=" * 85)

    if not DB_PATH.exists():
        print(f"\n❌ БД не найдена: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    symbols = get_symbols(conn)
    print(f"\nВселенная: {len(symbols)} монет")

    all_trades = []
    print("\nЗапуск бэктеста для сбора сделок...")
    for i, sym in enumerate(symbols):
        times, opens, highs, lows, closes = load_ohlcv(conn, sym)
        all_trades.extend(simulate_symbol(sym, times, opens, highs, lows, closes))
        if (i + 1) % 10 == 0 or i == len(symbols) - 1:
            print(f"  [{i + 1:2d}/{len(symbols)}] сделок: {len(all_trades)}")

    conn.close()

    if not all_trades:
        print("❌ Нет сделок для визуализации")
        sys.exit(1)

    closed = [t for t in all_trades if 'pnl_pct' in t]
    print(f"\nВсего закрытых сделок: {len(closed)}")

    mode_a = [t for t in closed if t['mode'] == 'A']
    mode_b = [t for t in closed if t['mode'] == 'B']
    pin_trades = [t for t in closed if t['trigger'] == 'pin']
    rails_trades = [t for t in closed if t['trigger'] == 'rails']

    print(f"  Режим A (верхняя граница): {len(mode_a)}")
    print(f"  Режим B (нижняя граница): {len(mode_b)}")
    print(f"  Pin: {len(pin_trades)}")
    print(f"  Rails: {len(rails_trades)}")

    selected = []

    # Режим A
    if mode_a:
        a_best = sorted(mode_a, key=lambda t: t['pnl_pct'], reverse=True)[:2]
        a_worst = sorted(mode_a, key=lambda t: t['pnl_pct'])[:2]
        selected.extend([('modeA_best', t) for t in a_best])
        selected.extend([('modeA_worst', t) for t in a_worst])

    # Режим B
    if mode_b:
        b_best = sorted(mode_b, key=lambda t: t['pnl_pct'], reverse=True)[:2]
        b_worst = sorted(mode_b, key=lambda t: t['pnl_pct'])[:2]
        selected.extend([('modeB_best', t) for t in b_best])
        selected.extend([('modeB_worst', t) for t in b_worst])

    # Pin
    if pin_trades:
        pin_best = sorted(pin_trades, key=lambda t: t['pnl_pct'], reverse=True)[:2]
        pin_worst = sorted(pin_trades, key=lambda t: t['pnl_pct'])[:2]
        selected.extend([('pin_best', t) for t in pin_best])
        selected.extend([('pin_worst', t) for t in pin_worst])

    # Rails
    if rails_trades:
        rails_best = sorted(rails_trades, key=lambda t: t['pnl_pct'], reverse=True)[:2]
        rails_worst = sorted(rails_trades, key=lambda t: t['pnl_pct'])[:2]
        selected.extend([('rails_best', t) for t in rails_best])
        selected.extend([('rails_worst', t) for t in rails_worst])

    print(f"\nВыбрано сделок для визуализации: {len(selected)}")

    for i, (category, trade) in enumerate(selected, 1):
        sym = trade['symbol']
        times, opens, highs, lows, closes = load_ohlcv(sqlite3.connect(DB_PATH), sym)
        filename = f"{i}_{category}_{sym}_{trade['entry_idx']}_pnl{trade['pnl_pct']:+.1f}.png"
        output_path = OUTPUT_DIR / filename
        plot_trade(sym, times, opens, highs, lows, closes, trade, output_path)

    print(f"\n✅ Все графики сохранены в {OUTPUT_DIR}/")
    print("\nСкачать на локалку:")
    print(f"  scp gooffer@r1237472:~/cash-carry-monitor/visualizations_v5/*.png ~/Downloads/")


if __name__ == "__main__":
    main()