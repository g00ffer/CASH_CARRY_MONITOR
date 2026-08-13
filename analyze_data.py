#!/usr/bin/env python3
"""
Analyze accumulated data from SQLite database.
Run: python analyze_data.py
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# Путь к базе
DB_PATH = Path("data/monitor.sqlite")

def load_data():
    """Load all relevant tables from SQLite."""
    conn = sqlite3.connect(DB_PATH)
    
    # Metrics (основные расчёты)
    metrics = pd.read_sql_query("""
        SELECT 
            cycle_id,
            symbol_name,
            basis_entry,
            funding_annual,
            one_time_costs,
            net_horizon,
            net_annual,
            calculated_at_ms
        FROM metrics
        ORDER BY calculated_at_ms
    """, conn)
    
    # Signal decisions (решения сигнального движка)
    decisions = pd.read_sql_query("""
        SELECT 
            cycle_id,
            symbol_name,
            state,
            should_alert,
            consecutive_confirmations,
            reasons,
            timestamp_ms
        FROM signal_decisions
        ORDER BY timestamp_ms
    """, conn)
    
    # Quality reports (проверки качества)
    quality = pd.read_sql_query("""
        SELECT 
            cycle_id,
            symbol_name,
            is_ok,
            error_count,
            warning_count,
            checked_at_ms
        FROM quality_reports
        ORDER BY checked_at_ms
    """, conn)
    
    # Funding snapshots (фандинг)
    funding = pd.read_sql_query("""
        SELECT 
            cycle_id,
            symbol_name,
            effective_funding_rate,
            funding_interval_hours,
            received_at_ms
        FROM funding_snapshots
        ORDER BY received_at_ms
    """, conn)
    
    conn.close()
    
    # Конвертируем миллисекунды в datetime
    for df in [metrics, decisions, quality, funding]:
        if 'calculated_at_ms' in df.columns:
            df['timestamp'] = pd.to_datetime(df['calculated_at_ms'], unit='ms', utc=True)
        elif 'timestamp_ms' in df.columns:
            df['timestamp'] = pd.to_datetime(df['timestamp_ms'], unit='ms', utc=True)
        elif 'checked_at_ms' in df.columns:
            df['timestamp'] = pd.to_datetime(df['checked_at_ms'], unit='ms', utc=True)
        elif 'received_at_ms' in df.columns:
            df['timestamp'] = pd.to_datetime(df['received_at_ms'], unit='ms', utc=True)
    
    return metrics, decisions, quality, funding


def print_summary(metrics, decisions, quality, funding):
    """Print summary statistics."""
    print("=" * 80)
    print("АНАЛИЗ ДАННЫХ CASH-CARRY MONITOR")
    print("=" * 80)
    
    # Период наблюдений
    start_time = metrics['timestamp'].min()
    end_time = metrics['timestamp'].max()
    duration_hours = (end_time - start_time).total_seconds() / 3600
    
    print(f"\n📊 Период наблюдений:")
    print(f"  Начало: {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Конец:  {end_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Длительность: {duration_hours:.1f} часов")
    
    # Количество циклов
    total_cycles = metrics['cycle_id'].nunique()
    symbols = metrics['symbol_name'].unique()
    
    print(f"\n🔄 Циклы:")
    print(f"  Всего циклов: {total_cycles}")
    print(f"  Символы: {', '.join(symbols)}")
    print(f"  Циклов на символ: {total_cycles // len(symbols)}")
    
    # Качество данных
    quality_ok = quality['is_ok'].sum()
    quality_total = len(quality)
    quality_pct = 100 * quality_ok / quality_total if quality_total > 0 else 0
    
    print(f"\n✅ Качество данных:")
    print(f"  Валидных проверок: {quality_ok}/{quality_total} ({quality_pct:.1f}%)")
    print(f"  Ошибок: {quality['error_count'].sum()}")
    print(f"  Предупреждений: {quality['warning_count'].sum()}")
    
    # Статистика по каждому символу
    print(f"\n💰 Статистика по символам:")
    for symbol in symbols:
        symbol_metrics = metrics[metrics['symbol_name'] == symbol]
        symbol_decisions = decisions[decisions['symbol_name'] == symbol]
        
        print(f"\n  {symbol}:")
        print(f"    Funding annual (среднее): {symbol_metrics['funding_annual'].mean():.2%}")
        print(f"    Funding annual (макс):    {symbol_metrics['funding_annual'].max():.2%}")
        print(f"    Net annual (среднее):     {symbol_metrics['net_annual'].mean():.2%}")
        print(f"    Net annual (макс):        {symbol_metrics['net_annual'].max():.2%}")
        print(f"    Net horizon (среднее):    {symbol_metrics['net_horizon'].mean():.4f}")
        
        # Near-miss анализ (близки к сигналу)
        near_miss = symbol_metrics[
            (symbol_metrics['net_annual'] > 0.05) & 
            (symbol_metrics['net_annual'] < 0.08)
        ]
        print(f"    Near-miss (5-8% annual):  {len(near_miss)} раз")
        
        # Сигналы
        alerts = symbol_decisions[symbol_decisions['should_alert'] == 1]
        print(f"    Сигналов отправлено:      {len(alerts)}")
        
        # Подтверждения
        max_confirmations = symbol_decisions['consecutive_confirmations'].max()
        print(f"    Макс подтверждений:       {max_confirmations}")


def plot_metrics(metrics):
    """Plot key metrics over time."""
    fig, axes = plt.subplots(3, 1, figsize=(14, 10))
    fig.suptitle('Cash-Carry Monitor: Metrics Over Time', fontsize=16, fontweight='bold')
    
    symbols = metrics['symbol_name'].unique()
    colors = ['#2E86AB', '#A23B72']
    
    # 1. Funding Annual
    ax = axes[0]
    for i, symbol in enumerate(symbols):
        data = metrics[metrics['symbol_name'] == symbol]
        ax.plot(data['timestamp'], data['funding_annual'] * 100, 
                label=symbol, color=colors[i], alpha=0.7, linewidth=1)
    ax.set_ylabel('Funding Annual (%)')
    ax.set_title('Funding Rate (Annual)')
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.axhline(y=8, color='r', linestyle='--', alpha=0.5, label='Min threshold (8%)')
    
    # 2. Net Annual
    ax = axes[1]
    for i, symbol in enumerate(symbols):
        data = metrics[metrics['symbol_name'] == symbol]
        ax.plot(data['timestamp'], data['net_annual'] * 100, 
                label=symbol, color=colors[i], alpha=0.7, linewidth=1)
    ax.set_ylabel('Net Annual (%)')
    ax.set_title('Net Yield (Annual)')
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.axhline(y=8, color='r', linestyle='--', alpha=0.5, label='Min threshold (8%)')
    ax.axhline(y=0, color='k', linestyle='-', alpha=0.3)
    
    # 3. Net Horizon
    ax = axes[2]
    for i, symbol in enumerate(symbols):
        data = metrics[metrics['symbol_name'] == symbol]
        ax.plot(data['timestamp'], data['net_horizon'] * 100, 
                label=symbol, color=colors[i], alpha=0.7, linewidth=1)
    ax.set_ylabel('Net Horizon (%)')
    ax.set_xlabel('Time')
    ax.set_title('Net Yield (Per Holding Period)')
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.axhline(y=0.1, color='r', linestyle='--', alpha=0.5, label='Min threshold (0.1%)')
    ax.axhline(y=0, color='k', linestyle='-', alpha=0.3)
    
    # Форматирование оси времени
    for ax in axes:
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha='right')
    
    plt.tight_layout()
    plt.savefig('analysis_metrics.png', dpi=150, bbox_inches='tight')
    print("\n📈 График сохранён: analysis_metrics.png")
    plt.show()


def plot_distribution(metrics):
    """Plot distribution of key metrics."""
    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    fig.suptitle('Distribution of Key Metrics', fontsize=16, fontweight='bold')
    
    symbols = metrics['symbol_name'].unique()
    colors = ['#2E86AB', '#A23B72']
    
    # Funding Annual histogram
    ax = axes[0, 0]
    for i, symbol in enumerate(symbols):
        data = metrics[metrics['symbol_name'] == symbol]['funding_annual'] * 100
        ax.hist(data, bins=30, alpha=0.6, label=symbol, color=colors[i])
    ax.axvline(x=8, color='r', linestyle='--', alpha=0.7, label='Threshold (8%)')
    ax.set_xlabel('Funding Annual (%)')
    ax.set_ylabel('Count')
    ax.set_title('Funding Rate Distribution')
    ax.legend()
    ax.grid(True, alpha=0.3)
    
    # Net Annual histogram
    ax = axes[0, 1]
    for i, symbol in enumerate(symbols):
        data = metrics[metrics['symbol_name'] == symbol]['net_annual'] * 100
        ax.hist(data, bins=30, alpha=0.6, label=symbol, color=colors[i])
    ax.axvline(x=8, color='r', linestyle='--', alpha=0.7, label='Threshold (8%)')
    ax.axvline(x=0, color='k', linestyle='-', alpha=0.5)
    ax.set_xlabel('Net Annual (%)')
    ax.set_ylabel('Count')
    ax.set_title('Net Yield Distribution')
    ax.legend()
    ax.grid(True, alpha=0.3)
    
    # Net Horizon histogram
    ax = axes[1, 0]
    for i, symbol in enumerate(symbols):
        data = metrics[metrics['symbol_name'] == symbol]['net_horizon'] * 100
        ax.hist(data, bins=30, alpha=0.6, label=symbol, color=colors[i])
    ax.axvline(x=0.1, color='r', linestyle='--', alpha=0.7, label='Threshold (0.1%)')
    ax.axvline(x=0, color='k', linestyle='-', alpha=0.5)
    ax.set_xlabel('Net Horizon (%)')
    ax.set_ylabel('Count')
    ax.set_title('Net Horizon Distribution')
    ax.legend()
    ax.grid(True, alpha=0.3)
    
    # Basis Entry histogram
    ax = axes[1, 1]
    for i, symbol in enumerate(symbols):
        data = metrics[metrics['symbol_name'] == symbol]['basis_entry'] * 100
        ax.hist(data, bins=30, alpha=0.6, label=symbol, color=colors[i])
    ax.axvline(x=0, color='k', linestyle='-', alpha=0.5)
    ax.set_xlabel('Basis Entry (%)')
    ax.set_ylabel('Count')
    ax.set_title('Basis Entry Distribution')
    ax.legend()
    ax.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig('analysis_distribution.png', dpi=150, bbox_inches='tight')
    print("📊 Гистограммы сохранены: analysis_distribution.png")
    plt.show()


def analyze_hourly_patterns(metrics):
    """Analyze patterns by hour of day."""
    metrics['hour'] = metrics['timestamp'].dt.hour
    
    print("\n" + "=" * 80)
    print("⏰ АНАЛИЗ ПО ЧАСАМ")
    print("=" * 80)
    
    # Средний funding по часам
    hourly_funding = metrics.groupby('hour')['funding_annual'].mean() * 100
    print("\nСредний funding annual по часам (%):")
    for hour, value in hourly_funding.items():
        bar = '█' * int(value / 2)
        print(f"  {hour:02d}:00  {value:6.2f}%  {bar}")
    
    # Лучшее время для сигналов (net_annual > 5%)
    good_opportunities = metrics[metrics['net_annual'] > 0.05]
    if len(good_opportunities) > 0:
        hourly_good = good_opportunities.groupby('hour').size()
        print(f"\nЧасы с net_annual > 5% ({len(good_opportunities)} всего):")
        for hour, count in hourly_good.items():
            print(f"  {hour:02d}:00  {count} раз")


def main():
    print("Загрузка данных из SQLite...")
    metrics, decisions, quality, funding = load_data()
    
    print(f"Загружено: {len(metrics)} записей metrics, {len(decisions)} decisions")
    
    # Выводим сводку
    print_summary(metrics, decisions, quality, funding)
    
    # Анализ по часам
    analyze_hourly_patterns(metrics)
    
    # Строим графики
    print("\nПостроение графиков...")
    plot_metrics(metrics)
    plot_distribution(metrics)
    
    print("\n" + "=" * 80)
    print("✅ Анализ завершён!")
    print("=" * 80)


if __name__ == "__main__":
    main()