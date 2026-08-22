#!/usr/bin/env python3
"""
visualize_trade.py — Визуализация одной сделки из бэктеста пин-бара.

Строит профессиональный график:
- Свечи H1 (open/high/low/close)
- Зоны уровней Герчика (горизонтальные полосы)
- Точка входа (стрелка)
- Линии стопа и тейка
- Точка выхода
- Аннотации (скоринг уровня, PnL)

Run:
  python visualize_trade.py                  # лучшая сделка BTWUSDT
  python visualize_trade.py --symbol ETHUSDT # конкретная монета
  python visualize_trade.py --worst          # худшая сделка вместо лучшей
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import matplotlib
matplotlib.use('Agg')  # без GUI
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle
import numpy as np
from datetime import datetime, timezone

DB_PATH = Path("data/klines_top50.sqlite")

# Параметры стратегии (должны совпадать с v3)
LEFT_BARS = 5
RIGHT_BARS = 5
ATR_PERIOD = 14
CLUSTER_ATR_MULT = 1.5
MIN_TOUCHES = 2
SCORE_THRESHOLD = 50.0
MIN_FALSE_BREAKOUTS = 1
LEVEL_WINDOW_H = 96
LEVEL_STEP_H = 24
PIN_SHADOW_RATIO = 3.0
ZONE_TOLERANCE = 0.10
RISK_REWARD = 2.0
TAKER_FEE = 0.0014
MAX_HOLDING_H = 72
MIN_STOP_PCT = 0.3

# Окно отображения: N баров до входа и M баров после
BARS_BEFORE_ENTRY = 120   # 5 дней до сделки
BARS_AFTER_ENTRY = 96     # 4 дня после входа


# ============================================================================
# Gerchik Level Detector (копия из v3)
# ============================================================================
def calculate_atr(highs, lows, closes, period=ATR_PERIOD):
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


def find_pivots(highs, lows, left_bars=LEFT_BARS, right_bars=RIGHT_BARS):
    n = len(highs)
    pivots = []
    for i in range(left_bars, n - right_bars):
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


def cluster_pivots(pivots, atr, eps_mult=CLUSTER_ATR_MULT):
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
    types_in_zone = set(p['type'] for p in cluster)
    if len(types_in_zone) > 1:
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
        price_magnitude = len(str(int(zone_center)))
        if price_magnitude >= 2:
            round_step = 10 ** (price_magnitude - 2)
            nearest_round = round(zone_center / round_step) * round_step
            distance_pct = abs(zone_center - nearest_round) / zone_center * 100
            if distance_pct < 0.5:
                metrics['round_number_boost'] = 15
            elif distance_pct < 1.5:
                metrics['round_number_boost'] = 5
    first_touch_idx = min(p['index'] for p in cluster)
    if first_touch_idx > 20:
        past_price = closes[first_touch_idx - 20]
        move_pct = abs(zone_center - past_price) / past_price * 100
        metrics['impulse_before'] = min(move_pct, 20.0)
    return metrics


def calculate_score(metrics):
    score = 0.0
    score += min(metrics['touches'] * 6, 30)
    score += min(metrics['false_breakouts'] * 12.5, 25)
    if metrics['is_mirror']:
        score += 15
    score += min(metrics['reaction_strength'] * 2, 15)
    score += metrics['round_number_boost']
    if metrics['impulse_before'] > 10:
        score += 10
    elif metrics['impulse_before'] > 5:
        score += 5
    return round(score, 2)


def detect_levels_on_window(highs, lows, closes):
    n = len(closes)
    if n < ATR_PERIOD + LEFT_BARS + RIGHT_BARS + 10:
        return []
    atr = calculate_atr(highs, lows, closes)
    current_atr = atr[-1]
    if current_atr <= 0:
        return []
    pivots = find_pivots(highs, lows)
    if not pivots:
        return []
    clusters = cluster_pivots(pivots, current_atr)
    if not clusters:
        return []
    levels = []
    for cluster in clusters:
        if len(cluster) < MIN_TOUCHES:
            continue
        metrics = analyze_zone(cluster, highs, lows, closes)
        if metrics['touches'] < MIN_TOUCHES:
            continue
        if metrics['false_breakouts'] < MIN_FALSE_BREAKOUTS:
            continue
        metrics['score'] = calculate_score(metrics)
        if metrics['score'] >= SCORE_THRESHOLD:
            levels.append(metrics)
    levels.sort(key=lambda x: x['score'], reverse=True)
    return levels


def is_bullish_pin_bar(open_p, high_p, low_p, close_p):
    if high_p <= low_p or low_p <= 0:
        return False
    body = abs(close_p - open_p)
    candle_range = high_p - low_p
    if candle_range <= 0:
        return False
    if candle_range / low_p * 100 < 0.05:
        return False
    lower_shadow = min(open_p, close_p) - low_p
    upper_shadow = high_p - max(open_p, close_p)
    if body <= 0:
        if lower_shadow < candle_range * 0.7:
            return False
    else:
        if lower_shadow < body * PIN_SHADOW_RATIO:
            return False
    body_center = (open_p + close_p) / 2.0
    if body_center < low_p + candle_range * 0.5:
        return False
    if upper_shadow > candle_range * 0.25:
        return False
    return True


# ============================================================================
# Загрузка данных
# ============================================================================
def load_ohlcv(symbol):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT open_time_ms, open, high, low, close FROM klines "
        "WHERE symbol = ? AND interval = '1h' "
        "ORDER BY open_time_ms",
        (symbol,),
    ).fetchall()
    conn.close()
    if not rows:
        return None
    times = np.array([r[0] for r in rows])
    opens = np.array([float(r[1]) for r in rows])
    highs = np.array([float(r[2]) for r in rows])
    lows = np.array([float(r[3]) for r in rows])
    closes = np.array([float(r[4]) for r in rows])
    return times, opens, highs, lows, closes


# ============================================================================
# Поиск сделок
# ============================================================================
def find_trades(times, opens, highs, lows, closes):
    """Возвращает список сделок с уровнями."""
    n = len(closes)
    if n < LEVEL_WINDOW_H + MAX_HOLDING_H + 100:
        return []

    level_cache = {}
    trades = []
    position_open = False

    for i in range(LEVEL_WINDOW_H, n):
        if position_open:
            holding_hours = i - trades[-1]['entry_idx']
            trade = trades[-1]

            if lows[i] <= trade['stop_price']:
                trade['exit_idx'] = i
                trade['exit_price'] = trade['stop_price']
                trade['exit_reason'] = 'stop'
                gross = (trade['exit_price'] / trade['entry_price'] - 1.0) * 100
                trade['pnl_pct'] = gross - TAKER_FEE * 2 * 100
                position_open = False
                continue
            if highs[i] >= trade['take_price']:
                trade['exit_idx'] = i
                trade['exit_price'] = trade['take_price']
                trade['exit_reason'] = 'take'
                gross = (trade['exit_price'] / trade['entry_price'] - 1.0) * 100
                trade['pnl_pct'] = gross - TAKER_FEE * 2 * 100
                position_open = False
                continue
            if holding_hours >= MAX_HOLDING_H:
                trade['exit_idx'] = i
                trade['exit_price'] = closes[i]
                trade['exit_reason'] = 'timeout'
                gross = (trade['exit_price'] / trade['entry_price'] - 1.0) * 100
                trade['pnl_pct'] = gross - TAKER_FEE * 2 * 100
                position_open = False
                continue
            continue

        if not is_bullish_pin_bar(opens[i], highs[i], lows[i], closes[i]):
            continue

        window_key = (i // LEVEL_STEP_H) * LEVEL_STEP_H
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

        shadow_low = lows[i]
        matched_level = None
        for level in levels:
            zone_low = level['lower']
            zone_high = level['upper']
            zone_width = zone_high - zone_low
            tolerance = zone_width * ZONE_TOLERANCE
            if shadow_low >= zone_low - tolerance and shadow_low <= zone_high + tolerance:
                matched_level = level
                break

        if matched_level is None:
            continue

        entry_price = closes[i]
        stop_price = lows[i] * 0.999
        stop_pct = (entry_price - stop_price) / entry_price * 100
        if stop_pct < MIN_STOP_PCT:
            continue
        take_price = entry_price + (entry_price - stop_price) * RISK_REWARD

        trades.append({
            'entry_idx': i,
            'entry_price': entry_price,
            'stop_price': stop_price,
            'take_price': take_price,
            'level': matched_level,
        })
        position_open = True

    return trades


# ============================================================================
# Отрисовка
# ============================================================================
def ms_to_dt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def plot_trade(symbol, times, opens, highs, lows, closes, trade, output_path):
    """Строит профессиональный график сделки."""
    entry_idx = trade['entry_idx']
    exit_idx = trade['exit_idx']

    # Окно отображения
    start_idx = max(0, entry_idx - BARS_BEFORE_ENTRY)
    end_idx = min(len(times) - 1, exit_idx + BARS_AFTER_ENTRY)

    t = times[start_idx:end_idx + 1]
    o = opens[start_idx:end_idx + 1]
    h = highs[start_idx:end_idx + 1]
    l = lows[start_idx:end_idx + 1]
    c = closes[start_idx:end_idx + 1]

    # Преобразование в datetime
    dates = [ms_to_dt(ts) for ts in t]
    n_bars = len(dates)

    # Подготовка figure
    fig, ax = plt.subplots(figsize=(16, 9))
    fig.patch.set_facecolor('#1a1a2e')
    ax.set_facecolor('#16213e')

    # Цвета свечей
    up_color = '#26a69a'    # зелёный (бычья свеча)
    down_color = '#ef5350'  # красный (медвежья свеча)

    # Отрисовка свечей
    width = 0.6
    for i in range(n_bars):
        color = up_color if c[i] >= o[i] else down_color
        # Тело свечи
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

        # Тени (high и low)
        ax.plot([i, i], [l[i], body_low], color=color, linewidth=0.8)
        ax.plot([i, i], [body_high, h[i]], color=color, linewidth=0.8)

    # Индексы важных точек
    entry_local_idx = entry_idx - start_idx
    exit_local_idx = exit_idx - start_idx

    # =========================================================================
    # Отрисовка уровней Герчика (зоны)
    # =========================================================================
    level = trade['level']
    zone_low = level['lower']
    zone_high = level['upper']
    zone_width = zone_high - zone_low

    # Основная зона уровня (полупрозрачная)
    ax.axhspan(
        zone_low, zone_high,
        color='#ffd700', alpha=0.15,
        label=f"Level (score={level['score']:.0f})"
    )
    # Границы зоны
    ax.axhline(zone_high, color='#ffd700', linestyle='--', linewidth=1, alpha=0.6)
    ax.axhline(zone_low, color='#ffd700', linestyle='--', linewidth=1, alpha=0.6)

    # Зона допуска (tolerance)
    tolerance = zone_width * ZONE_TOLERANCE
    ax.axhspan(
        zone_low - tolerance, zone_high + tolerance,
        color='#ffd700', alpha=0.05,
    )

    # =========================================================================
    # Отрисовка стопа и тейка
    # =========================================================================
    # Стоп (красная пунктирная линия)
    ax.axhline(
        trade['stop_price'],
        color='#ff4444', linestyle=':', linewidth=1.5, alpha=0.8,
        label=f"Stop: ${trade['stop_price']:.4f}"
    )

    # Тейк (зелёная пунктирная линия)
    ax.axhline(
        trade['take_price'],
        color='#00ff88', linestyle=':', linewidth=1.5, alpha=0.8,
        label=f"Take: ${trade['take_price']:.4f}"
    )

    # Зоны стопа и тейка (полупрозрачные полосы между entry и stop/take)
    ax.axhspan(
        trade['stop_price'], trade['entry_price'],
        color='#ff4444', alpha=0.08,
    )
    ax.axhspan(
        trade['entry_price'], trade['take_price'],
        color='#00ff88', alpha=0.08,
    )

    # =========================================================================
    # Точка входа
    # =========================================================================
    ax.annotate(
        '',
        xy=(entry_local_idx, l[entry_local_idx]),
        xytext=(entry_local_idx, l[entry_local_idx] - (h.max() - l.min()) * 0.08),
        arrowprops=dict(
            arrowstyle='->', color='#00bfff', lw=2.5,
            mutation_scale=25,
        ),
    )
    ax.text(
        entry_local_idx,
        l[entry_local_idx] - (h.max() - l.min()) * 0.12,
        f'ENTRY\n${trade["entry_price"]:.4f}',
        ha='center', va='top',
        color='#00bfff', fontsize=9, fontweight='bold',
        bbox=dict(boxstyle='round,pad=0.3', facecolor='#0a1929',
                  edgecolor='#00bfff', alpha=0.9),
    )

    # =========================================================================
    # Точка выхода
    # =========================================================================
    exit_color = '#00ff88' if trade['pnl_pct'] > 0 else '#ff4444'
    exit_label = 'TAKE' if trade['exit_reason'] == 'take' else \
                 'STOP' if trade['exit_reason'] == 'stop' else 'TIMEOUT'

    ax.annotate(
        '',
        xy=(exit_local_idx, h[exit_local_idx]),
        xytext=(exit_local_idx, h[exit_local_idx] + (h.max() - l.min()) * 0.08),
        arrowprops=dict(
            arrowstyle='->', color=exit_color, lw=2.5,
            mutation_scale=25,
        ),
    )
    ax.text(
        exit_local_idx,
        h[exit_local_idx] + (h.max() - l.min()) * 0.12,
        f'{exit_label}\n${trade["exit_price"]:.4f}',
        ha='center', va='bottom',
        color=exit_color, fontsize=9, fontweight='bold',
        bbox=dict(boxstyle='round,pad=0.3', facecolor='#0a1929',
                  edgecolor=exit_color, alpha=0.9),
    )

    # =========================================================================
    # Информация в заголовке
    # =========================================================================
    holding_hours = exit_idx - entry_idx
    entry_dt = ms_to_dt(times[entry_idx]).strftime('%Y-%m-%d %H:%M')
    exit_dt = ms_to_dt(times[exit_idx]).strftime('%Y-%m-%d %H:%M')

    pnl_color = '#00ff88' if trade['pnl_pct'] > 0 else '#ff4444'
    pnl_symbol = '+' if trade['pnl_pct'] > 0 else ''

    title = (
        f"{symbol}  |  Pin Bar on Level (score={level['score']:.0f})  |  "
        f"{entry_dt} → {exit_dt}  |  "
        f"Hold: {holding_hours}h  |  "
        f"Exit: {trade['exit_reason']}\n"
        f"Entry ${trade['entry_price']:.4f}  →  Exit ${trade['exit_price']:.4f}  |  "
        f"Stop ${trade['stop_price']:.4f}  |  Take ${trade['take_price']:.4f}"
    )
    ax.set_title(title, color='white', fontsize=12, fontweight='bold', pad=10)

    # Большой PnL в правом верхнем углу
    pnl_text = f"PnL: {pnl_symbol}{trade['pnl_pct']:.3f}%"
    ax.text(
        0.98, 0.98, pnl_text,
        transform=ax.transAxes,
        ha='right', va='top',
        color=pnl_color, fontsize=20, fontweight='bold',
        bbox=dict(boxstyle='round,pad=0.5', facecolor='#0a1929',
                  edgecolor=pnl_color, alpha=0.95),
    )

    # Информация об уровне в левом верхнем углу
    level_info = (
        f"Level: ${zone_low:.4f} - ${zone_high:.4f}\n"
        f"Touches: {level['touches']}\n"
        f"False breakouts: {level['false_breakouts']}\n"
        f"R:R = 1:{RISK_REWARD}"
    )
    ax.text(
        0.02, 0.98, level_info,
        transform=ax.transAxes,
        ha='left', va='top',
        color='#ffd700', fontsize=9,
        family='monospace',
        bbox=dict(boxstyle='round,pad=0.5', facecolor='#0a1929',
                  edgecolor='#ffd700', alpha=0.9),
    )

    # =========================================================================
    # Форматирование осей
    # =========================================================================
    ax.set_xlim(-1, n_bars)
    price_margin = (h.max() - l.min()) * 0.15
    ax.set_ylim(l.min() - price_margin, h.max() + price_margin)

    # Метки X: каждые 12 часов
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

    # Убираем верхнюю и правую оси
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color('gray')
    ax.spines['bottom'].set_color('gray')

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight',
                facecolor='#1a1a2e', edgecolor='none')
    plt.close()
    print(f"✅ График сохранён: {output_path}")


# ============================================================================
# MAIN
# ============================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--symbol', default='BTWUSDT',
                        help='Символ для анализа (по умолчанию BTWUSDT)')
    parser.add_argument('--worst', action='store_true',
                        help='Показать худшую сделку вместо лучшей')
    parser.add_argument('--index', type=int, default=None,
                        help='Номер сделки (0-based)')
    args = parser.parse_args()

    print(f"📊 Загрузка данных {args.symbol}...")
    data = load_ohlcv(args.symbol)
    if data is None:
        print(f"❌ Нет данных для {args.symbol}")
        sys.exit(1)

    times, opens, highs, lows, closes = data
    print(f"   Баров: {len(times)}")

    print(f"🔍 Запуск стратегии на {args.symbol}...")
    trades = find_trades(times, opens, highs, lows, closes)
    print(f"   Найдено сделок: {len(trades)}")

    if not trades:
        print("❌ Нет сделок для визуализации")
        sys.exit(1)

    # Выбор сделки
    if args.index is not None:
        if args.index >= len(trades):
            print(f"❌ Индекс {args.index} вне диапазона (0..{len(trades) - 1})")
            sys.exit(1)
        trade = trades[args.index]
        selection = f"сделка #{args.index}"
    elif args.worst:
        trade = min(trades, key=lambda t: t.get('pnl_pct', 0) if 'pnl_pct' in t else 0)
        selection = "худшая"
        # Для худшей нужна сделка с exit (то есть с pnl_pct)
        closed_trades = [t for t in trades if 'pnl_pct' in t]
        if not closed_trades:
            # Все сделки ещё открыты — берём первую закрытую в цикле
            print("   Все сделки открыты, симулируем выход...")
        trade = min(closed_trades, key=lambda t: t['pnl_pct']) if closed_trades else trades[0]
    else:
        closed_trades = [t for t in trades if 'pnl_pct' in t]
        if closed_trades:
            trade = max(closed_trades, key=lambda t: t['pnl_pct'])
        else:
            trade = trades[0]
        selection = "лучшая"

    if 'pnl_pct' not in trade:
        print("⚠️  Сделка не закрыта, рассчитываем PnL по текущей цене")
        i = len(times) - 1
        holding_hours = i - trade['entry_idx']
        if lows[i] <= trade['stop_price']:
            trade['exit_idx'] = i
            trade['exit_price'] = trade['stop_price']
            trade['exit_reason'] = 'stop (current)'
        elif highs[i] >= trade['take_price']:
            trade['exit_idx'] = i
            trade['exit_price'] = trade['take_price']
            trade['exit_reason'] = 'take (current)'
        else:
            trade['exit_idx'] = i
            trade['exit_price'] = closes[i]
            trade['exit_reason'] = 'open'
        gross = (trade['exit_price'] / trade['entry_price'] - 1.0) * 100
        trade['pnl_pct'] = gross - TAKER_FEE * 2 * 100

    print(f"\n📈 Выбрана {selection} сделка:")
    print(f"   Entry: {ms_to_dt(times[trade['entry_idx']]).strftime('%Y-%m-%d %H:%M')}")
    print(f"   Exit:  {ms_to_dt(times[trade['exit_idx']]).strftime('%Y-%m-%d %H:%M')}")
    print(f"   Entry price: ${trade['entry_price']:.4f}")
    print(f"   Exit price:  ${trade['exit_price']:.4f}")
    print(f"   Stop: ${trade['stop_price']:.4f}")
    print(f"   Take: ${trade['take_price']:.4f}")
    print(f"   PnL: {trade['pnl_pct']:+.3f}%")
    print(f"   Exit reason: {trade['exit_reason']}")
    print(f"   Level score: {trade['level']['score']}")

    output_path = Path(f"trade_{args.symbol}_{trade['entry_idx']}.png")
    plot_trade(args.symbol, times, opens, highs, lows, closes, trade, output_path)

    print(f"\n📋 Список всех сделок (для --index):")
    for i, t in enumerate(trades):
        if 'pnl_pct' in t:
            marker = '✅' if t['pnl_pct'] > 0 else '❌'
            print(f"   [{i:3d}] {ms_to_dt(times[t['entry_idx']]).strftime('%m-%d %H:%M')} "
                  f"PnL={t['pnl_pct']:+6.2f}% score={t['level']['score']:4.0f} "
                  f"{marker} {t['exit_reason']}")


if __name__ == "__main__":
    main()