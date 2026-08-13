#!/usr/bin/env python3
"""
Analyze accumulated data from SQLite database.
Run: python analyze_data.py
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

DB_PATH = Path("data/monitor.sqlite")


def load_data():
    """Load all relevant tables from SQLite."""
    conn = sqlite3.connect(DB_PATH)

    # Metrics (основные расчёты)
    metrics = pd.read_sql_query("""
        SELECT 
            cycle_id,
            symbol_name,
            calculated_at_ms,
            CAST(basis_entry AS REAL) AS basis_entry,
            CAST(funding_annual AS REAL) AS funding_annual,
            CAST(net_horizon AS REAL) AS net_horizon,
            CAST(net_annual AS REAL) AS net_annual,
            payload
        FROM metrics
        ORDER BY calculated_at_ms
    """, conn)

    # Signal decisions
    decisions = pd.read_sql_query("""
        SELECT 
            cycle_id,
            symbol_name,
            decision_timestamp_ms,
            state,
            should_alert,
            consecutive_confirmations,
            payload
        FROM signal_decisions
        ORDER BY decision_timestamp_ms
    """, conn)

    # Quality reports
    quality = pd.read_sql_query("""
        SELECT 
            cycle_id,
            symbol_name,
            checked_at_ms,
            is_ok,
            payload
        FROM quality_reports
        ORDER BY checked_at_ms
    """, conn)

    # Funding snapshots
    funding = pd.read_sql_query("""
        SELECT 
            cycle_id,
            symbol_name,
            received_at_ms,
            CAST(effective_funding_rate AS REAL) AS effective_funding_rate,
            CAST(funding_interval_hours AS REAL) AS funding_interval_hours,
            next_funding_timestamp_ms
        FROM funding_snapshots
        ORDER BY received_at_ms
    """, conn)

    # Alerts
    alerts = pd.read_sql_query("""
        SELECT 
            alert_id,
            cycle_id,
            symbol_name,
            alert_type,
            delivery_status,
            created_at_ms,
            sent_at_ms,
            error_message
        FROM alerts
        ORDER BY created_at_ms
    """, conn)

    conn.close()

    # Конвертируем миллисекунды в datetime
    metrics['timestamp'] = pd.to_datetime(metrics['calculated_at_ms'], unit='ms', utc=True)
    decisions['timestamp'] = pd.to_datetime(decisions['decision_timestamp_ms'], unit='ms', utc=True)
    quality['timestamp'] = pd.to_datetime(quality['checked_at_ms'], unit='ms', utc=True)
    funding['timestamp'] = pd.to_datetime(funding['received_at_ms'], unit='ms', utc=True)
    if len(alerts) > 0:
        alerts['timestamp'] = pd.to_datetime(alerts['created_at_ms'], unit='ms', utc=True)

    # Извлекаем one_time_costs из payload (JSON)
    def extract_one_time_costs(payload_str):
        try:
            payload = json.loads(payload_str)
            return float(payload.get('one_time_costs', 0))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    metrics['one_time_costs'] = metrics['payload'].apply(extract_one_time_costs)

    # Извлекаем reasons из payload (JSON)
    def extract_reasons(payload_str):
        try:
            payload = json.loads(payload_str)
            reasons = payload.get('reasons', [])
            return ', '.join(reasons) if reasons else ''
        except (json.JSONDecodeError, TypeError):
            return ''

    decisions['reasons'] = decisions['payload'].apply(extract_reasons)

    return metrics, decisions, quality, funding, alerts


def print_summary(metrics, decisions, quality, funding, alerts):
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
    print(f"  Длительность: {duration_hours:.1f} часов ({duration_hours/24:.1f} дней)")

    # Количество циклов
    total_cycles = metrics['cycle_id'].nunique()
    symbols = sorted(metrics['symbol_name'].unique())

    print(f"\n🔄 Циклы:")
    print(f"  Всего записей metrics: {len(metrics)}")
    print(f"  Уникальных циклов: {total_cycles}")
    print(f"  Символы: {', '.join(symbols)}")

    # Качество данных
    quality_ok = quality['is_ok'].sum()
    quality_total = len(quality)
    quality_pct = 100 * quality_ok / quality_total if quality_total > 0 else 0

    print(f"\n✅ Качество данных:")
    print(f"  Валидных проверок: {quality_ok}/{quality_total} ({quality_pct:.2f}%)")
    print(f"  Невалидных: {quality_total - quality_ok}")

    # Алерты
    if len(alerts) > 0:
        print(f"\n📬 Алерты:")
        print(f"  Всего: {len(alerts)}")
        for alert_type in alerts['alert_type'].unique():
            type_alerts = alerts[alerts['alert_type'] == alert_type]
            delivered = type_alerts[type_alerts['delivery_status'] == 'DELIVERED']
            print(f"    {alert_type}: {len(type_alerts)} (доставлено: {len(delivered)})")
    else:
        print(f"\n📬 Алерты: нет")

    # Статистика по каждому символу
    print(f"\n💰 Статистика по символам:")
    for symbol in symbols:
        symbol_metrics = metrics[metrics['symbol_name'] == symbol]
        symbol_decisions = decisions[decisions['symbol_name'] == symbol]

        print(f"\n  {'='*60}")
        print(f"  {symbol}")
        print(f"  {'='*60}")
        print(f"    Записей:                {len(symbol_metrics)}")
        print(f"    Funding annual (средн): {symbol_metrics['funding_annual'].mean()*100:.2f}%")
        print(f"    Funding annual (макс):  {symbol_metrics['funding_annual'].max()*100:.2f}%")
        print(f"    Funding annual (мин):   {symbol_metrics['funding_annual'].min()*100:.2f}%")
        print(f"    Net annual (средн):     {symbol_metrics['net_annual'].mean()*100:.2f}%")
        print(f"    Net annual (макс):      {symbol_metrics['net_annual'].max()*100:.2f}%")
        print(f"    Net horizon (средн):    {symbol_metrics['net_horizon'].mean()*100:.4f}%")
        print(f"    Basis entry (средн):    {symbol_metrics['basis_entry'].mean()*100:.4f}%")

        # One-time costs
        otc = symbol_metrics['one_time_costs'].dropna()
        if len(otc) > 0:
            print(f"    One-time costs (средн): {otc.mean()*100:.2f}%")

        # Near-miss анализ
        near_miss = symbol_metrics[
            (symbol_metrics['net_annual'] > 0.05) & 
            (symbol_metrics['net_annual'] < 0.08)
        ]
        print(f"    Near-miss (5-8%):       {len(near_miss)} раз")

        # Сигналы
        alerts_sent = symbol_decisions[symbol_decisions['should_alert'] == 1]
        print(f"    Сигналов (should_alert): {len(alerts_sent)}")

        # Подтверждения
        max_conf = symbol_decisions['consecutive_confirmations'].max()
        print(f"    Макс подтверждений:     {max_conf}")

        # Распределение состояний
        state_counts = symbol_decisions['state'].value_counts()
        print(f"    Состояния:")
        for state, count in state_counts.items():
            print(f"      {state}: {count}")

        # Топ причин блокировки
        blocked = symbol_decisions[symbol_decisions['should_alert'] == 0]
        if len(blocked) > 0:
            all_reasons = []
            for reasons_str in blocked['reasons']:
                if reasons_str:
                    all_reasons.extend(reasons_str.split(', '))
            if all_reasons:
                reason_counts = pd.Series(all_reasons).value_counts()
                print(f"    Топ причин блокировки:")
                for reason, count in reason_counts.head(5).items():
                    print(f"      {reason}: {count}")


def plot_metrics(metrics):
    """Plot key metrics over time."""
    fig, axes = plt.subplots(3, 1, figsize=(16, 12))
    fig.suptitle('Cash-Carry Monitor: Metrics Over Time', fontsize=16, fontweight='bold')

    symbols = sorted(metrics['symbol_name'].unique())
    colors = ['#2E86AB', '#A23B72', '#F18F01', '#C73E1D']

    # 1. Funding Annual
    ax = axes[0]
    for i, symbol in enumerate(symbols):
        data = metrics[metrics['symbol_name'] == symbol]
        # Downsample для скорости отрисовки
        data_ds = data.set_index('timestamp').resample('5min').mean()
        ax.plot(data_ds.index, data_ds['funding_annual'] * 100, 
                label=symbol, color=colors[i % len(colors)], alpha=0.8, linewidth=1)
    ax.set_ylabel('Funding Annual (%)')
    ax.set_title('Funding Rate (Annual)')
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.axhline(y=8, color='r', linestyle='--', alpha=0.5, label='Min threshold (8%)')

    # 2. Net Annual
    ax = axes[1]
    for i, symbol in enumerate(symbols):
        data = metrics[metrics['symbol_name'] == symbol]
        data_ds = data.set_index('timestamp').resample('5min').mean()
        ax.plot(data_ds.index, data_ds['net_annual'] * 100, 
                label=symbol, color=colors[i % len(colors)], alpha=0.8, linewidth=1)
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
        data_ds = data.set_index('timestamp').resample('5min').mean()
        ax.plot(data_ds.index, data_ds['net_horizon'] * 100, 
                label=symbol, color=colors[i % len(colors)], alpha=0.8, linewidth=1)
    ax.set_ylabel('Net Horizon (%)')
    ax.set_xlabel('Time (UTC)')
    ax.set_title('Net Yield (Per Holding Period)')
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.axhline(y=0.1, color='r', linestyle='--', alpha=0.5, label='Min threshold (0.1%)')
    ax.axhline(y=0, color='k', linestyle='-', alpha=0.3)

    for ax in axes:
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha='right')

    plt.tight_layout()
    plt.savefig('analysis_metrics.png', dpi=150, bbox_inches='tight')
    print("\n📈 График сохранён: analysis_metrics.png")


def plot_distribution(metrics):
    """Plot distribution of key metrics."""
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle('Distribution of Key Metrics', fontsize=16, fontweight='bold')

    symbols = sorted(metrics['symbol_name'].unique())
    colors = ['#2E86AB', '#A23B72', '#F18F01', '#C73E1D']

    # Funding Annual histogram
    ax = axes[0, 0]
    for i, symbol in enumerate(symbols):
        data = metrics[metrics['symbol_name'] == symbol]['funding_annual'] * 100
        ax.hist(data, bins=50, alpha=0.6, label=symbol, color=colors[i % len(colors)])
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
        ax.hist(data, bins=50, alpha=0.6, label=symbol, color=colors[i % len(colors)])
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
        ax.hist(data, bins=50, alpha=0.6, label=symbol, color=colors[i % len(colors)])
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
        ax.hist(data, bins=50, alpha=0.6, label=symbol, color=colors[i % len(colors)])
    ax.axvline(x=0, color='k', linestyle='-', alpha=0.5)
    ax.set_xlabel('Basis Entry (%)')
    ax.set_ylabel('Count')
    ax.set_title('Basis Entry Distribution')
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig('analysis_distribution.png', dpi=150, bbox_inches='tight')
    print("📊 Гистограммы сохранены: analysis_distribution.png")


def analyze_hourly_patterns(metrics):
    """Analyze patterns by hour of day."""
    metrics = metrics.copy()
    metrics['hour'] = metrics['timestamp'].dt.hour

    print("\n" + "=" * 80)
    print("⏰ АНАЛИЗ ПО ЧАСАМ (UTC)")
    print("=" * 80)

    # Средний funding по часам
    hourly_funding = metrics.groupby('hour')['funding_annual'].mean() * 100
    max_funding = hourly_funding.max()
    
    print("\nСредний funding annual по часам (%):")
    for hour, value in hourly_funding.items():
        bar_len = int(value / max_funding * 40) if max_funding > 0 else 0
        bar = '█' * bar_len
        print(f"  {hour:02d}:00  {value:6.2f}%  {bar}")

    # Лучшее время для net_annual
    hourly_net = metrics.groupby('hour')['net_annual'].mean() * 100
    best_hour = hourly_net.idxmax()
    print(f"\nЛучший час по net_annual: {best_hour:02d}:00 ({hourly_net[best_hour]:.2f}%)")

    # Near-miss по часам
    good_opportunities = metrics[metrics['net_annual'] > 0.05]
    if len(good_opportunities) > 0:
        hourly_good = good_opportunities.groupby('hour').size()
        print(f"\nЧасы с net_annual > 5% ({len(good_opportunities)} записей всего):")
        for hour, count in hourly_good.items():
            print(f"  {hour:02d}:00  {count} раз")
    else:
        print("\nЗаписей с net_annual > 5% не найдено.")


def main():
    print("Загрузка данных из SQLite...")
    metrics, decisions, quality, funding, alerts = load_data()

    print(f"Загружено: {len(metrics)} metrics, {len(decisions)} decisions, "
          f"{len(quality)} quality, {len(funding)} funding, {len(alerts)} alerts")

    # Выводим сводку
    print_summary(metrics, decisions, quality, funding, alerts)

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