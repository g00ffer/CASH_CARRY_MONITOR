#!/usr/bin/env python3
"""
analyze_basis.py — Анализ данных по базису из monitor.sqlite.

Цель: понять, есть ли в данных достаточно информации для бэктеста
базисного трейдинга.

Вывод:
1. Список таблиц в БД
2. Статистика по базису (средний, волатильность, экстремумы)
3. Частота отклонений базиса от среднего
4. Потенциальная прибыль от конвергенции
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np

DB_PATH = Path("data/monitor.sqlite")


def list_tables(conn: sqlite3.Connection) -> list[str]:
    """Список всех таблиц в БД."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    return [row[0] for row in cursor.fetchall()]


def analyze_metrics_table(conn: sqlite3.Connection) -> None:
    """Анализ таблицы metrics (basis_entry)."""
    print("\n" + "=" * 85)
    print("📊 АНАЛИЗ ТАБЛИЦЫ metrics (basis_entry)")
    print("=" * 85)

    # Количество записей
    count = conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
    print(f"  Всего записей: {count}")

    # Период данных
    period = conn.execute(
        "SELECT MIN(calculated_at_ms), MAX(calculated_at_ms) FROM metrics"
    ).fetchone()
    if period[0] and period[1]:
        from datetime import datetime, timezone
        start = datetime.fromtimestamp(period[0] / 1000, tz=timezone.utc)
        end = datetime.fromtimestamp(period[1] / 1000, tz=timezone.utc)
        days = (period[1] - period[0]) / (1000 * 3600 * 24)
        print(f"  Период: {start.date()} → {end.date()} ({days:.1f} дней)")

    # Количество символов
    symbols = conn.execute(
        "SELECT COUNT(DISTINCT symbol_name) FROM metrics"
    ).fetchone()[0]
    print(f"  Символов: {symbols}")

    # Статистика по базису
    stats = conn.execute(
        """
        SELECT
            symbol_name,
            COUNT(*) as n,
            AVG(basis_entry) as avg_basis,
            MIN(basis_entry) as min_basis,
            MAX(basis_entry) as max_basis
        FROM metrics
        WHERE basis_entry IS NOT NULL
        GROUP BY symbol_name
        ORDER BY n DESC
        """
    ).fetchall()

    if not stats:
        print("  ❌ Нет данных по базису")
        return

    print(f"\n  {'Символ':<15} {'Записей':>8} {'Ср. базис%':>10} {'Мин%':>8} {'Макс%':>8}")
    print("  " + "-" * 55)
    for row in stats[:20]:
        sym, n, avg, mn, mx = row
        avg_pct = float(avg) * 100 if avg else 0.0
        mn_pct = float(mn) * 100 if mn else 0.0
        mx_pct = float(mx) * 100 if mx else 0.0
        print(f"  {sym:<15} {n:>8} {avg_pct:>9.4f} {mn_pct:>7.4f} {mx_pct:>7.4f}")

    # Общая статистика
    all_basis = conn.execute(
        "SELECT basis_entry FROM metrics WHERE basis_entry IS NOT NULL"
    ).fetchall()
    if all_basis:
        basis_values = np.array([r[0] for r in all_basis])
        print(f"\n  📈 Общая статистика базиса:")
        print(f"    Средний: {basis_values.mean() * 100:.4f}%")
        print(f"    Медиана: {np.median(basis_values) * 100:.4f}%")
        print(f"    Ст. откл: {basis_values.std() * 100:.4f}%")
        print(f"    Мин: {basis_values.min() * 100:.4f}%")
        print(f"    Макс: {basis_values.max() * 100:.4f}%")

        # Процентильный анализ
        percentiles = [5, 10, 25, 50, 75, 90, 95]
        pvalues = np.percentile(basis_values, percentiles)
        print(f"\n  📊 Процентили:")
        for p, v in zip(percentiles, pvalues):
            print(f"    P{p:>2}: {v * 100:>8.4f}%")


def analyze_funding_table(conn: sqlite3.Connection) -> None:
    """Анализ таблицы funding_snapshots."""
    print("\n" + "=" * 85)
    print("📊 АНАЛИЗ ТАБЛИЦЫ funding_snapshots")
    print("=" * 85)

    count = conn.execute("SELECT COUNT(*) FROM funding_snapshots").fetchone()[0]
    print(f"  Всего записей: {count}")

    period = conn.execute(
        "SELECT MIN(received_at_ms), MAX(received_at_ms) FROM funding_snapshots"
    ).fetchone()
    if period[0] and period[1]:
        from datetime import datetime, timezone
        start = datetime.fromtimestamp(period[0] / 1000, tz=timezone.utc)
        end = datetime.fromtimestamp(period[1] / 1000, tz=timezone.utc)
        days = (period[1] - period[0]) / (1000 * 3600 * 24)
        print(f"  Период: {start.date()} → {end.date()} ({days:.1f} дней)")

    symbols = conn.execute(
        "SELECT COUNT(DISTINCT symbol_name) FROM funding_snapshots"
    ).fetchone()[0]
    print(f"  Символов: {symbols}")


def analyze_signals_table(conn: sqlite3.Connection) -> None:
    """Анализ таблицы signal_decisions."""
    print("\n" + "=" * 85)
    print("📊 АНАЛИЗ ТАБЛИЦЫ signal_decisions")
    print("=" * 85)

    count = conn.execute("SELECT COUNT(*) FROM signal_decisions").fetchone()[0]
    print(f"  Всего записей: {count}")

    alerts = conn.execute(
        "SELECT COUNT(*) FROM signal_decisions WHERE should_alert = 1"
    ).fetchone()[0]
    print(f"  Сигналов (should_alert=1): {alerts}")


def main():
    print("=" * 85)
    print("АНАЛИЗ ДАННЫХ ПО БАЗИСУ")
    print("=" * 85)

    if not DB_PATH.exists():
        print(f"\n❌ БД не найдена: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)

    # Список таблиц
    tables = list_tables(conn)
    print(f"\n📋 Таблицы в БД ({len(tables)}):")
    for t in tables:
        print(f"  - {t}")

    # Анализ основных таблиц
    analyze_metrics_table(conn)
    analyze_funding_table(conn)
    analyze_signals_table(conn)

    conn.close()

    print("\n" + "=" * 85)
    print("✅ Анализ завершён")
    print("=" * 85)


if __name__ == "__main__":
    main()