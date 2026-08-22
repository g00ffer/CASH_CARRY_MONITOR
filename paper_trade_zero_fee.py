#!/usr/bin/env python3
"""
Paper Trading simulator v2 — ZERO-FEE VERSION.

Тест для разделения двух эффектов:
  (A) Комиссии убивают стратегию
  (B) Сама природа funding-штормов не генерирует alpha

Логика полностью идентична paper_trade.py, но costs_usd = 0.
Сравнение результатов двух запусков даёт ответ:

  zero_fee_pnl - real_pnl = вклад комиссий
  zero_fee_pnl              = вклад "чистой" природы штормов

Run: python paper_trade_zero_fee.py
Output: paper_trades_zero_fee.csv, paper_trade_report_zero_fee.txt
"""
from __future__ import annotations

import bisect
import csv
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

DB_PATH = Path("data/monitor.sqlite")
SYMBOLS_PATH = Path("config/symbols.yaml")
OUTPUT_CSV = Path("paper_trades_zero_fee.csv")
REPORT_PATH = Path("paper_trade_report_zero_fee.txt")

# ---------------------------------------------------------------------
# Стратегия выхода (настраивается)
# ---------------------------------------------------------------------
MAX_HOLDING_HOURS = 720          # жёсткий потолок удержания
MAX_HOLD_MS = MAX_HOLDING_HOURS * 3600 * 1000
COST_AMORT_HOURS = 720           # амортизация costs (как в settings.yaml)
PERIODS_PER_YEAR = 1095.0        # 8760 / 8
EXIT_ON_NEGATIVE_FUNDING = True  # выход при rate < 0
LOW_FUNDING_FACTOR = 1.0         # выход при funding < factor * costs_annualized
LOW_FUNDING_CONSECUTIVE = 6      # ...в течение 6 сеттлментов подряд (48h)


@dataclass
class PaperTrade:
    symbol: str
    entry_time_utc: str
    exit_time_utc: str
    exit_reason: str             # negative_funding | funding_decayed | max_holding | open
    holding_hours: float
    notional_usd: float
    entry_funding_annual_pct: float
    funding_income_usd: float
    basis_pnl_usd: float
    costs_usd: float
    net_pnl_usd: float
    net_pnl_pct: float
    settlements_count: int
    status: str                  # closed | open


def ms_to_utc(ms: int) -> str:
    return datetime.fromtimestamp(
        ms / 1000, tz=timezone.utc,
    ).strftime("%Y-%m-%d %H:%M:%S UTC")


def load_symbols_notional() -> dict[str, float]:
    with open(SYMBOLS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return {
        s["name"]: float(s.get("notional_usd", 10000))
        for s in data.get("symbols", [])
    }


def load_signals(conn: sqlite3.Connection) -> list[dict]:
    """Реально отправленные сигналы (триггеры входа)."""
    cursor = conn.execute(
        """
        SELECT
            d.symbol_name,
            d.decision_timestamp_ms,
            CAST(m.funding_annual AS REAL) AS funding_annual,
            CAST(m.net_annual AS REAL) AS net_annual
        FROM signal_decisions d
        JOIN alerts a
            ON a.cycle_id = d.cycle_id
            AND a.symbol_name = d.symbol_name
        JOIN metrics m
            ON m.cycle_id = d.cycle_id
            AND m.symbol_name = d.symbol_name
        WHERE d.should_alert = 1
          AND a.alert_type = 'signal'
          AND a.delivery_status = 'sent'
        ORDER BY d.decision_timestamp_ms
        """,
    )
    cols = [c[0] for c in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def load_symbol_settlements(
    conn: sqlite3.Connection, symbol: str,
) -> tuple[list[int], list[float]]:
    """
    Все уникальные funding settlements символа за всю историю.
    (settlement_time_ms, rate). Дедупликация по next_funding_timestamp_ms,
    берётся последний snapshot перед сеттлментом.
    """
    cursor = conn.execute(
        """
        SELECT
            next_funding_timestamp_ms,
            CAST(effective_funding_rate AS REAL)
        FROM funding_snapshots
        WHERE symbol_name = ?
          AND next_funding_timestamp_ms IS NOT NULL
          AND effective_funding_rate IS NOT NULL
        ORDER BY next_funding_timestamp_ms, received_at_ms DESC
        """,
        (symbol,),
    )
    seen: set[int] = set()
    times: list[int] = []
    rates: list[float] = []
    for nxt, rate in cursor:
        if nxt in seen:
            continue
        seen.add(nxt)
        times.append(nxt)
        rates.append(rate)
    return times, rates


def load_metrics_at(
    conn: sqlite3.Connection, symbol: str, target_ms: int,
) -> dict:
    """Ближайшая metrics-запись: costs + basis."""
    cursor = conn.execute(
        """
        SELECT
            CAST(json_extract(payload, '$.one_time_costs') AS REAL),
            CAST(basis_entry AS REAL)
        FROM metrics
        WHERE symbol_name = ?
        ORDER BY ABS(calculated_at_ms - ?)
        LIMIT 1
        """,
        (symbol, target_ms),
    )
    row = cursor.fetchone()
    return {
        "one_time_costs": (
            float(row[0]) if row and row[0] is not None else 0.0025
        ),
        "basis_entry": (
            float(row[1]) if row and row[1] is not None else 0.0
        ),
    }


def simulate_symbol(
    conn: sqlite3.Connection,
    symbol: str,
    signals: list[dict],
    notional: float,
    now_ms: int,
) -> tuple[list[PaperTrade], int]:
    """Симулирует позиции символа с дедупликацией и exit-логикой."""
    times, rates = load_symbol_settlements(conn, symbol)
    trades: list[PaperTrade] = []
    skipped = 0
    last_exit_ms = -1

    for sig in signals:
        entry_ms = int(sig["decision_timestamp_ms"])

        # Дедупликация: позиция уже открыта — сигнал пропускаем
        if entry_ms < last_exit_ms:
            skipped += 1
            continue

        m_entry = load_metrics_at(conn, symbol, entry_ms)
        # --- ZERO-FEE: обнуляем costs ---
        # one_time = m_entry["one_time_costs"]  # было в оригинале
        basis0 = m_entry["basis_entry"]

        costs_usd = 0.0                         # <-- ZERO-FEE
        costs_annualized = 0.0                  # <-- ZERO-FEE (чтобы funding_decayed не срабатывал)

        funding_income = 0.0
        settlements_count = 0
        low_streak = 0
        exit_ms = None
        exit_reason = "open"

        idx = bisect.bisect_right(times, entry_ms)
        while idx < len(times) and times[idx] <= now_ms:
            settle_ms = times[idx]
            rate = rates[idx]

            # Максимальный горизонт
            if settle_ms > entry_ms + MAX_HOLD_MS:
                exit_ms = entry_ms + MAX_HOLD_MS
                exit_reason = "max_holding"
                break

            funding_income += notional * rate
            settlements_count += 1

            # Exit-триггеры
            if EXIT_ON_NEGATIVE_FUNDING and rate < 0:
                exit_ms = settle_ms
                exit_reason = "negative_funding"
                break

            annualized = rate * PERIODS_PER_YEAR
            if annualized < LOW_FUNDING_FACTOR * costs_annualized:
                low_streak += 1
            else:
                low_streak = 0
            if low_streak >= LOW_FUNDING_CONSECUTIVE:
                exit_ms = settle_ms
                exit_reason = "funding_decayed"
                break

            idx += 1

        # Позиция не закрылась триггером — держим до now (open)
        actual_end_ms = exit_ms if exit_ms else min(now_ms, entry_ms + MAX_HOLD_MS)
        if exit_ms is None:
            exit_reason = "open"

        # Basis PnL: (b0 - b1) * notional
        m_exit = load_metrics_at(conn, symbol, actual_end_ms)
        basis1 = m_exit["basis_entry"]
        basis_pnl_usd = notional * (basis0 - basis1)

        net_pnl_usd = funding_income + basis_pnl_usd - costs_usd
        net_pnl_pct = net_pnl_usd / notional * 100 if notional else 0.0
        status = "closed" if exit_ms is not None else "open"

        print(
            f"  {symbol}: entry={ms_to_utc(entry_ms)[:16]} "
            f"{exit_reason:>16} | "
            f"{settlements_count:>3} settl | "
            f"fund={funding_income:>+8.2f}$ "
            f"basis={basis_pnl_usd:>+8.2f}$ "
            f"costs={costs_usd:>6.2f}$ "
            f"net={net_pnl_usd:>+8.2f}$",
        )

        trades.append(
            PaperTrade(
                symbol=symbol,
                entry_time_utc=ms_to_utc(entry_ms),
                exit_time_utc=ms_to_utc(actual_end_ms),
                exit_reason=exit_reason,
                holding_hours=(actual_end_ms - entry_ms) / 3_600_000,
                notional_usd=notional,
                entry_funding_annual_pct=(
                    float(sig["funding_annual"] or 0) * 100
                ),
                funding_income_usd=funding_income,
                basis_pnl_usd=basis_pnl_usd,
                costs_usd=costs_usd,
                net_pnl_usd=net_pnl_usd,
                net_pnl_pct=net_pnl_pct,
                settlements_count=settlements_count,
                status=status,
            ),
        )
        last_exit_ms = actual_end_ms

    return trades, skipped


def print_report(
    trades: list[PaperTrade], skipped_total: int,
) -> None:
    lines: list[str] = []
    lines.append("=" * 110)
    lines.append("PAPER TRADING v2 (ZERO-FEE): exit-логика + дедупликация + basis PnL, costs=0")
    lines.append("=" * 110)
    lines.append(f"Сделок: {len(trades)} | Пропущено overlapping-сигналов: {skipped_total}")
    lines.append(
        f"Exit-правила: negative_funding | "
        f"funding_decayed ({LOW_FUNDING_CONSECUTIVE}×{LOW_FUNDING_FACTOR}×costs) | "
        f"max_holding {MAX_HOLDING_HOURS}h",
    )
    lines.append("")
    header = (
        f"{'#':<3} {'Symbol':<12} {'Entry':<17} {'Exit':<17} "
        f"{'Reason':<15} {'Hold h':>7} {'Fund$':>9} "
        f"{'Basis$':>9} {'Costs$':>8} {'Net$':>9} {'Net%':>7}"
    )
    lines.append(header)
    lines.append("-" * len(header))

    for i, t in enumerate(trades, 1):
        lines.append(
            f"{i:<3} {t.symbol:<12} {t.entry_time_utc[5:16]:<17} "
            f"{t.exit_time_utc[5:16]:<17} {t.exit_reason:<15} "
            f"{t.holding_hours:>7.0f} {t.funding_income_usd:>+9.2f} "
            f"{t.basis_pnl_usd:>+9.2f} {t.costs_usd:>8.2f} "
            f"{t.net_pnl_usd:>+9.2f} {t.net_pnl_pct:>+6.2f}",
        )

    lines.append("-" * len(header))

    closed = [t for t in trades if t.status == "closed"]
    winning = [t for t in closed if t.net_pnl_usd > 0]

    total_fund = sum(t.funding_income_usd for t in trades)
    total_basis = sum(t.basis_pnl_usd for t in trades)
    total_costs = sum(t.costs_usd for t in trades)
    total_net = sum(t.net_pnl_usd for t in trades)
    total_notional = sum(t.notional_usd for t in trades)

    lines.append("")
    lines.append("💰 ИТОГ (ZERO-FEE):")
    lines.append(f"  Сделок: {len(trades)} (closed: {len(closed)})")
    lines.append(f"  Суммарный notional: {total_notional:,.0f}$")
    lines.append(f"  Funding income: {total_fund:+,.2f}$")
    lines.append(f"  Basis PnL:      {total_basis:+,.2f}$")
    lines.append(f"  Комиссии:       {total_costs:,.2f}$ (обнулены для теста)")
    lines.append(f"  Чистый PnL:     {total_net:+,.2f}$")

    if closed:
        avg_pct = sum(t.net_pnl_pct for t in closed) / len(closed)
        avg_hold = sum(t.holding_hours for t in closed) / len(closed)
        lines.append(
            f"  Win rate (closed): "
            f"{len(winning) / len(closed) * 100:.1f}% "
            f"({len(winning)}/{len(closed)})",
        )
        lines.append(f"  Средний PnL% на сделку: {avg_pct:+.2f}%")
        lines.append(f"  Среднее время удержания: {avg_hold:.0f}h")

    lines.append("")
    lines.append("📊 Распределение причин выхода:")
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {reason:<16}: {count}")

    lines.append("")
    lines.append("📁 Детали: " + str(OUTPUT_CSV))
    lines.append("=" * 110)

    report_text = "\n".join(lines)
    print(report_text)
    REPORT_PATH.write_text(report_text, encoding="utf-8")


def main() -> None:
    notional_map = load_symbols_notional()
    conn = sqlite3.connect(DB_PATH)
    signals = load_signals(conn)

    print(f"Сигналов: {len(signals)}")
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    all_trades: list[PaperTrade] = []
    skipped_total = 0

    by_symbol: dict[str, list[dict]] = {}
    for s in signals:
        by_symbol.setdefault(s["symbol_name"], []).append(s)

    print("Симуляция позиций (v2 ZERO-FEE)...")
    for symbol in sorted(by_symbol):
        notional = notional_map.get(symbol, 10000)
        trades, skipped = simulate_symbol(
            conn, symbol, by_symbol[symbol], notional, now_ms,
        )
        all_trades.extend(trades)
        skipped_total += skipped

    conn.close()

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        if all_trades:
            writer = csv.DictWriter(
                f, fieldnames=list(asdict(all_trades[0]).keys()),
            )
            writer.writeheader()
            for t in all_trades:
                writer.writerow(asdict(t))

    print_report(all_trades, skipped_total)


if __name__ == "__main__":
    main()