#!/usr/bin/env python3
"""
visualize_trades_v6.py — визуализация сделок v6.

Логика симуляции импортируется из backtest_pin_bar_v6 (нет расхождений).
Генерирует 8 графиков: 2 лучших + 2 худших по каждому триггеру (pin/rails).
Зона уровня пересчитывается для каждой выбранной сделки.
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

from backtest_pin_bar_v7 import (
    DB_PATH, get_symbols, load_ohlcv, simulate_symbol,
    detect_levels_on_window, LEVEL_STEP_H, LEVEL_WINDOW_H, ZONE_TOLERANCE,
    RISK_REWARD,
)

OUTPUT_DIR = Path("visualizations_v7")
BARS_BEFORE_ENTRY = 120
BARS_AFTER_ENTRY = 96


def ms_to_dt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def find_level_for_trade(highs, lows, closes, trade):
    """Пересчитывает зону уровня для сделки (то же окно, что в бэктесте)."""
    i = trade.entry_idx
    window_key = (i // LEVEL_STEP_H) * LEVEL_STEP_H
    ws = max(0, window_key - LEVEL_WINDOW_H)
    levels = detect_levels_on_window(
        highs[ws:window_key], lows[ws:window_key], closes[ws:window_key]
    )
    if not levels:
        return None
    trigger_low = trade.stop_price / 0.999
    for level in levels:
        zw = level['upper'] - level['lower']
        tol = zw * ZONE_TOLERANCE
        if level['lower'] - tol <= trigger_low <= level['upper'] + tol:
            return level
    return levels[0]


def plot_trade(symbol, times, opens, highs, lows, closes, trade, level, output_path):
    entry_idx = trade.entry_idx
    exit_idx = trade.exit_idx

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

    up_color, down_color = '#26a69a', '#ef5350'
    width = 0.6
    for i in range(n_bars):
        color = up_color if c[i] >= o[i] else down_color
        body_low, body_high = min(o[i], c[i]), max(o[i], c[i])
        body_height = max(body_high - body_low, 1e-10)
        ax.add_patch(Rectangle((i - width / 2, body_low), width, body_height,
                               facecolor=color, edgecolor=color,
                               linewidth=0.5, alpha=0.9))
        ax.plot([i, i], [l[i], body_low], color=color, linewidth=0.8)
        ax.plot([i, i], [body_high, h[i]], color=color, linewidth=0.8)

    entry_local = entry_idx - start_idx
    exit_local = exit_idx - start_idx

    zone_low, zone_high = level['lower'], level['upper']

    ax.axhspan(zone_low, zone_high, color='#ffd700', alpha=0.15,
               label=f"Zone (score={level['score']:.0f})")
    ax.axhline(zone_high, color='#ffd700', linestyle='--', linewidth=1, alpha=0.6)
    ax.axhline(zone_low, color='#ffd700', linestyle='--', linewidth=1, alpha=0.6)

    ax.axhline(trade.stop_price, color='#ff4444', linestyle=':', linewidth=1.5,
               alpha=0.8, label=f"Stop: ${trade.stop_price:.4f}")
    ax.axhline(trade.take_price, color='#00ff88', linestyle=':', linewidth=1.5,
               alpha=0.8, label=f"Take: ${trade.take_price:.4f}")
    ax.axhspan(trade.stop_price, trade.entry_price, color='#ff4444', alpha=0.08)
    ax.axhspan(trade.entry_price, trade.take_price, color='#00ff88', alpha=0.08)

    price_span = h.max() - l.min()

    ax.annotate('', xy=(entry_local, l[entry_local]),
                xytext=(entry_local, l[entry_local] - price_span * 0.08),
                arrowprops=dict(arrowstyle='->', color='#00bfff', lw=2.5,
                                mutation_scale=25))
    ax.text(entry_local, l[entry_local] - price_span * 0.12,
            f'ENTRY\n${trade.entry_price:.4f}', ha='center', va='top',
            color='#00bfff', fontsize=9, fontweight='bold',
            bbox=dict(boxstyle='round,pad=0.3', facecolor='#0a1929',
                      edgecolor='#00bfff', alpha=0.9))

    pnl = trade.pnl_pct
    exit_color = '#00ff88' if pnl > 0 else '#ff4444'
    ax.annotate('', xy=(exit_local, h[exit_local]),
                xytext=(exit_local, h[exit_local] + price_span * 0.08),
                arrowprops=dict(arrowstyle='->', color=exit_color, lw=2.5,
                                mutation_scale=25))
    ax.text(exit_local, h[exit_local] + price_span * 0.12,
            f'{trade.exit_reason.upper()}\n${trade.exit_price:.4f}',
            ha='center', va='bottom', color=exit_color, fontsize=9,
            fontweight='bold',
            bbox=dict(boxstyle='round,pad=0.3', facecolor='#0a1929',
                      edgecolor=exit_color, alpha=0.9))

    entry_dt = ms_to_dt(times[entry_idx]).strftime('%Y-%m-%d %H:%M')
    exit_dt = ms_to_dt(times[exit_idx]).strftime('%Y-%m-%d %H:%M')
    ax.set_title(
        f"{symbol}  |  {trade.trigger.upper()} (score={level['score']:.0f})  |  "
        f"{entry_dt} → {exit_dt}  |  Hold: {exit_idx - entry_idx}h  |  "
        f"Exit: {trade.exit_reason}",
        color='white', fontsize=12, fontweight='bold', pad=10)

    pnl_color = '#00ff88' if pnl > 0 else '#ff4444'
    ax.text(0.98, 0.98, f"PnL: {pnl:+.3f}%", transform=ax.transAxes,
            ha='right', va='top', color=pnl_color, fontsize=20, fontweight='bold',
            bbox=dict(boxstyle='round,pad=0.5', facecolor='#0a1929',
                      edgecolor=pnl_color, alpha=0.95))

    ax.text(0.02, 0.98,
            f"Zone: ${zone_low:.4f} - ${zone_high:.4f}\n"
            f"Touches: {level['touches']}\n"
            f"False breakouts: {level['false_breakouts']}\n"
            f"R:R = 1:{RISK_REWARD}",
            transform=ax.transAxes, ha='left', va='top', color='#ffd700',
            fontsize=9, family='monospace',
            bbox=dict(boxstyle='round,pad=0.5', facecolor='#0a1929',
                      edgecolor='#ffd700', alpha=0.9))

    ax.set_xlim(-1, n_bars)
    margin = price_span * 0.15
    ax.set_ylim(l.min() - margin, h.max() + margin)

    ticks = list(range(0, n_bars, 12))
    ax.set_xticks(ticks)
    ax.set_xticklabels(
        [ms_to_dt(times[start_idx + i]).strftime('%m-%d\n%H:%M')
         if start_idx + i < len(times) else '' for i in ticks],
        color='gray', fontsize=8)

    ax.set_ylabel('Price (USDT)', color='gray', fontsize=10)
    ax.tick_params(axis='y', colors='gray')
    ax.grid(True, alpha=0.15, color='gray')
    ax.legend(loc='lower left', facecolor='#0a1929', edgecolor='#ffd700',
              labelcolor='white', fontsize=8)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color('gray')
    ax.spines['bottom'].set_color('gray')

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight',
                facecolor='#1a1a2e', edgecolor='none')
    plt.close()
    print(f"  ✅ {output_path.name}")


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    print("=" * 85)
    print("VISUALIZATION v7 — 8 графиков (best/worst pin + rails)")
    print("=" * 85)

    conn = sqlite3.connect(DB_PATH)
    symbols = get_symbols(conn)
    conn.close()
    print(f"\nВселенная: {len(symbols)} монет")

    all_trades = []
    for i, sym in enumerate(symbols):
        times, opens, highs, lows, closes = load_ohlcv(sqlite3.connect(DB_PATH), sym)
        all_trades.extend(simulate_symbol(sym, times, opens, highs, lows, closes))
        if (i + 1) % 10 == 0 or i == len(symbols) - 1:
            print(f"  [{i + 1:2d}/{len(symbols)}] сделок: {len(all_trades)}")

    closed = [t for t in all_trades if t.exit_idx > 0]
    print(f"\nЗакрытых сделок: {len(closed)}")
    if not closed:
        print("❌ Нет сделок")
        sys.exit(1)

    selected = []
    for trig in ("pin", "rails"):
        sub = [t for t in closed if t.trigger == trig]
        if not sub:
            continue
        best = sorted(sub, key=lambda t: t.pnl_pct, reverse=True)[:2]
        worst = sorted(sub, key=lambda t: t.pnl_pct)[:2]
        selected.extend([(f"{trig}_best", t) for t in best])
        selected.extend([(f"{trig}_worst", t) for t in worst])

    print(f"Выбрано сделок: {len(selected)}")

    for i, (category, trade) in enumerate(selected, 1):
        sym = trade.symbol
        times, opens, highs, lows, closes = load_ohlcv(sqlite3.connect(DB_PATH), sym)
        level = find_level_for_trade(highs, lows, closes, trade)
        if level is None:
            print(f"  ⚠️ {category} {sym}: уровень не найден, пропуск")
            continue
        filename = f"{i}_{category}_{sym}_{trade.entry_idx}_pnl{trade.pnl_pct:+.1f}.png"
        plot_trade(sym, times, opens, highs, lows, closes, trade, level,
                   OUTPUT_DIR / filename)

    print(f"\n✅ Графики в {OUTPUT_DIR}/")
    print(f"  scp gooffer@r1237472:~/cash-carry-monitor/visualizations_v7/*.png ~/Downloads/")


if __name__ == "__main__":
    main()