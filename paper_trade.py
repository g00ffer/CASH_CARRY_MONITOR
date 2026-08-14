#!/usr/bin/env python3
"""
Paper Trading simulator for cash-carry-monitor.

Replays historical signals (should_alert=True + delivered) and simulates
virtual long-spot/short-perp positions to estimate real PnL.

Run: python paper_trade.py
Output:
  - paper_trades.csv       (все сделки)
  - paper_trade_report.txt (сводка)
"""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

DB_PATH = Path("data/monitor.sqlite")
SYMBOLS_PATH = Path("config/symbols.yaml")
OUTPUT_CSV = Path("paper_trades.csv")
REPORT_PATH = Path("paper_trade_report.txt")

HOLDING_HOURS = 720  # должно совпадать с settings.yaml
HOLDING_MS = HOLDING_HOURS * 3600 * 1000


@dataclass
class PaperTrade:
    symbol: str
    entry_time_ms: int
    entry_time_utc: str
    exit_time_ms: int
    exit_time_utc: str
    entry_funding_annual_pct: float
    entry_net_annual_pct: float
    notional_usd: float
    funding_income_usd: float
    one_time_costs_usd: float
    net_pnl_usd: float
    net_pnl_pct: float
    funding_payments_count: int
    status: str  # 'closed' | 'open'


def ms_to_utc(ms: int) -> str:
    return datetime.fromtimestamp(
        ms / 1000, tz=timezone.utc,
    ).strftime("%Y-%m-%d %H:%M:%S UTC")


def load_symbols_notional() -> dict[str, float]:
    """Загружает notional_usd из symbols.yaml."""
    with open(SYMBOLS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return {
        s["name"]: float(s.get("notional_usd", 10000))
        for s in data.get("symbols", [])
    }


def load_signals(conn: sqlite3.Connection) -> list[dict]:
    """
    Все моменты, когда бот реально отправил сигнал в Telegram.
    Это триггеры входа в виртуальную позицию.
    """
    cursor = conn.execute(
        """
        SELECT
            d.symbol_name,
            d.decision_timestamp_ms,
            d.cycle_id,
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
        ORDER BY d.symbol_name, d.decision_timestamp_ms
        """,
    )
    cols = [c[0] for c in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def load_funding_settlements(
    conn: sqlite3.Connection,
    symbol: str,
    from_ms: int,
    to_ms: int,
) -> list[tuple[int, float, int]]:
    """
    Funding settlements для символа в [from_ms, to_ms].
    
    Дедупликация по next_funding_timestamp_ms (это момент settlement).
    Для каждого уникального settlement берём predicted_funding_rate из
    последнего snapshot перед ним (наиболее точная оценка ставки).
    
    Возвращает список (received_at_ms, rate, settlement_time_ms).
    """
    # Ищем все snapshots в интервале, где predicted_funding_rate доступен
    # и settlement_time попадает в [from, to]
    cursor = conn.execute(
        """
        SELECT
            received_at_ms,
            CAST(predicted_funding_rate AS REAL) AS predicted_rate,
            next_funding_timestamp_ms
        FROM funding_snapshots
        WHERE symbol_name = ?
          AND received_at_ms >= ?
          AND received_at_ms <= ?
          AND next_funding_timestamp_ms IS NOT NULL
          AND next_funding_timestamp_ms >= ?
          AND next_funding_timestamp_ms <= ?
          AND predicted_funding_rate IS NOT NULL
        ORDER BY next_funding_timestamp_ms, received_at_ms DESC
        """,
        (symbol, from_ms, to_ms, from_ms, to_ms),
    )
    
    # Дедупликация: берём ОДИН snapshot на каждый уникальный next_funding
    # (первый по received_at_ms DESC — самый свежий перед settlement)
    seen_next: set[int] = set()
    settlements: list[tuple[int, float, int]] = []
    for row in cursor:
        next_ms = row[2]
        if next_ms in seen_next:
            continue
        seen_next.add(next_ms)
        settlements.append((row[0], float(row[1]), next_ms))
    
    return settlements


def load_metrics_at(
    conn: sqlite3.Connection, symbol: str, target_ms: int,
) -> dict:
    """Ближайшая metrics-запись к target_ms."""
    cursor = conn.execute(
        """
        SELECT
            CAST(json_extract(payload, '$.one_time_costs') AS REAL),
            CAST(funding_annual AS REAL),
            CAST(net_annual AS REAL)
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
        "funding_annual": (
            float(row[1]) if row and row[1] is not None else 0.0
        ),
        "net_annual": (
            float(row[2]) if row and row[2] is not None else 0.0
        ),
    }


def simulate_position(
    signal: dict,
    conn: sqlite3.Connection,
    notional: float,
    now_ms: int,
) -> PaperTrade:
    """Симулирует одну виртуальную позицию."""
    entry_ms = int(signal["decision_timestamp_ms"])
    exit_ms = entry_ms + HOLDING_MS

    metrics = load_metrics_at(conn, signal["symbol_name"], entry_ms)
    one_time_costs_pct = metrics["one_time_costs"]
    # one_time_costs уже включает entry+exit round-trip
    costs_usd = notional * one_time_costs_pct

    settlements = load_funding_settlements(
        conn, signal["symbol_name"], entry_ms, exit_ms,
    )
    funding_income_usd = sum(
        notional * rate
        for _, rate, _ in settlements
    )

    status = "closed" if exit_ms <= now_ms else "open"
    net_pnl_usd = funding_income_usd - costs_usd
    net_pnl_pct = net_pnl_usd / notional * 100 if notional > 0 else 0.0

    print(
        f"  {signal['symbol_name']}: "
        f"{len(settlements)} settlements за "
        f"{HOLDING_HOURS}h, "
        f"funding_income={funding_income_usd:.2f}$, "
        f"costs={costs_usd:.2f}$",
    )

    return PaperTrade(
        symbol=signal["symbol_name"],
        entry_time_ms=entry_ms,
        entry_time_utc=ms_to_utc(entry_ms),
        exit_time_ms=exit_ms,
        exit_time_utc=ms_to_utc(exit_ms),
        entry_funding_annual_pct=(
            float(signal["funding_annual"] or 0) * 100
        ),
        entry_net_annual_pct=float(signal["net_annual"] or 0) * 100,
        notional_usd=notional,
        funding_income_usd=funding_income_usd,
        one_time_costs_usd=costs_usd,
        net_pnl_usd=net_pnl_usd,
        net_pnl_pct=net_pnl_pct,
        funding_payments_count=len(settlements),
        status=status,
    )


def print_report(trades: list[PaperTrade]) -> None:
    lines: list[str] = []
    lines.append("=" * 100)
    lines.append(
        "PAPER TRADING: симуляция виртуальных позиций "
        "на исторических сигналах",
    )
    lines.append("=" * 100)
    lines.append(f"Всего сигналов (should_alert + delivered): {len(trades)}")
    lines.append(
        f"Holding horizon: {HOLDING_HOURS}h "
        f"({HOLDING_HOURS // 24}d)",
    )
    lines.append("")

    header = (
        f"{'#':<3} {'Symbol':<12} {'Entry UTC':<22} "
        f"{'Fund@ent':>9} {'Net@ent':>9} "
        f"{'Notional':>10} {'Fund Inc':>10} "
        f"{'Costs':>8} {'Net PnL':>10} "
        f"{'PnL%':>7} {'Sts':<6}"
    )
    lines.append(header)
    lines.append("-" * len(header))

    for i, t in enumerate(trades, 1):
        lines.append(
            f"{i:<3} {t.symbol:<12} {t.entry_time_utc[:19]:<22} "
            f"{t.entry_funding_annual_pct:>8.2f}% {t.entry_net_annual_pct:>8.2f}% "
            f"{t.notional_usd:>9.0f}$ {t.funding_income_usd:>+9.2f}$ "
            f"{t.one_time_costs_usd:>7.2f}$ {t.net_pnl_usd:>+9.2f}$ "
            f"{t.net_pnl_pct:>+6.2f}% {t.status:<6}",
        )

    lines.append("-" * len(header))

    # Сводка по символам
    lines.append("")
    lines.append("📊 Сводка по символам:")
    by_symbol: dict[str, list[PaperTrade]] = {}
    for t in trades:
        by_symbol.setdefault(t.symbol, []).append(t)

    for symbol in sorted(by_symbol):
        st = by_symbol[symbol]
        closed = [t for t in st if t.status == "closed"]
        total_notional = sum(t.notional_usd for t in st)
        total_pnl = sum(t.net_pnl_usd for t in st)
        avg_pnl_pct = (
            sum(t.net_pnl_pct for t in closed) / len(closed)
            if closed else 0.0
        )
        winning = [t for t in closed if t.net_pnl_usd > 0]
        win_rate = len(winning) / len(closed) * 100 if closed else 0.0

        lines.append(
            f"  {symbol:<12}: {len(st):>3} сделок "
            f"| Notional: {total_notional:>10.0f}$ "
            f"| PnL: {total_pnl:>+9.2f}$ "
            f"| Avg%: {avg_pnl_pct:>+6.2f}% "
            f"| Win rate: {win_rate:>5.1f}% "
            f"| Closed: {len(closed)}/{len(st)}",
        )

    # Итог
    closed_trades = [t for t in trades if t.status == "closed"]
    open_trades = [t for t in trades if t.status == "open"]
    total_notional = sum(t.notional_usd for t in trades)
    total_income = sum(t.funding_income_usd for t in trades)
    total_costs = sum(t.one_time_costs_usd for t in trades)
    total_pnl = sum(t.net_pnl_usd for t in closed_trades)
    avg_pnl_pct = (
        sum(t.net_pnl_pct for t in closed_trades) / len(closed_trades)
        if closed_trades else 0.0
    )
    winning = [t for t in closed_trades if t.net_pnl_usd > 0]
    win_rate = (
        len(winning) / len(closed_trades) * 100 if closed_trades else 0.0
    )

    lines.append("")
    lines.append("=" * 100)
    lines.append("💰 ИТОГ:")
    lines.append(
        f"  Всего сделок: {len(trades)} "
        f"(закрыто: {len(closed_trades)}, "
        f"открыто: {len(open_trades)})",
    )
    lines.append(f"  Суммарный notional (все сделки): {total_notional:,.0f}$")
    lines.append(
        f"  Суммарный funding income (все): {total_income:+,.2f}$",
    )
    lines.append(f"  Суммарные комиссии (все): {total_costs:,.2f}$")
    lines.append(f"  Чистый PnL (closed): {total_pnl:+,.2f}$")
    lines.append(
        f"  Средний PnL% на сделку (closed): {avg_pnl_pct:+.2f}%",
    )
    lines.append(
        f"  Win rate (closed): {win_rate:.1f}% "
        f"({len(winning)}/{len(closed_trades)})",
    )
    lines.append("")
    if open_trades:
        unrealized = sum(t.net_pnl_usd for t in open_trades)
        lines.append(
            f"  🟡 Unrealized PnL (open positions): {unrealized:+,.2f}$",
        )
    lines.append("")
    lines.append("📁 Детали: " + str(OUTPUT_CSV))
    lines.append("📄 Отчёт: " + str(REPORT_PATH))
    lines.append("=" * 100)

    report_text = "\n".join(lines)
    print(report_text)
    REPORT_PATH.write_text(report_text, encoding="utf-8")


def main() -> None:
    print("Загрузка конфигурации символов...")
    notional_map = load_symbols_notional()
    print(f"Символы: {list(notional_map.keys())}")

    conn = sqlite3.connect(DB_PATH)

    print("Загрузка сигналов (should_alert=True + delivered)...")
    signals = load_signals(conn)
    print(f"Найдено {len(signals)} уникальных сигналов для симуляции")

    if not signals:
        print("⚠️  Нет сигналов для симуляции!")
        conn.close()
        return

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    print("Симуляция виртуальных позиций...")
    trades: list[PaperTrade] = []
    for s in signals:
        notional = notional_map.get(s["symbol_name"], 10000)
        trade = simulate_position(s, conn, notional, now_ms)
        trades.append(trade)

    conn.close()

    # CSV dump
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        if trades:
            writer = csv.DictWriter(
                f, fieldnames=list(asdict(trades[0]).keys()),
            )
            writer.writeheader()
            for t in trades:
                writer.writerow(asdict(t))
    print(f"Записано в {OUTPUT_CSV}")

    print_report(trades)


if __name__ == "__main__":
    main()