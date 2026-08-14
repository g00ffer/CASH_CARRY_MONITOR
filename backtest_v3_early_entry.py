#!/usr/bin/env python3
"""
Backtest v3: Early-Entry Funding Arbitrage

Проверяем гипотезу: "если войти за 1-2 эпохи ДО пика funding,
базис ещё не сжат толпой, и мы зарабатываем на convergence + funding".

Sweep параметров:
  - lead_time_hours: за сколько часов до settlement входить
  - peak_threshold: минимальный funding rate для "пика"
  - holding_settlements: сколько сеттлментов держать после входа

Метрика: funding_income + basis_pnl - costs на notional $10,000

Run: python backtest_v3_early_entry.py
Output: backtest_v3_report.txt + backtest_v3_chart.png
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

DB_PATH = Path("data/monitor.sqlite")
REPORT_PATH = Path("backtest_v3_report.txt")
CHART_PATH = Path("backtest_v3_chart.png")

NOTIONAL = 10_000.0
COSTS_PCT = 0.0005  # 0.05% round-trip (агрессивный maker)
COSTS_USD = NOTIONAL * COSTS_PCT

# Активные символы (исключаем BNB/DOGE — funding почти нулевой)
SYMBOLS = [
    "BTC_CARRY", "ETH_CARRY", "SOL_CARRY",
    "XRP_CARRY", "ADA_CARRY", "AVAX_CARRY",
]

# Sweep-параметры
LEAD_TIMES_H = [0, 4, 8, 12, 16, 20, 24, 32, 40, 48]
PEAK_THRESHOLDS = [0.008, 0.010, 0.012]  # 8.76%, 10.95%, 13.14% annual
HOLDING_SETTLEMENTS = [1, 2, 3, 5]


@dataclass
class Settlement:
    """Уникальный funding settlement."""
    symbol: str
    settle_time_ms: int
    rate: float
    basis_entry: float  # basis в момент перед settlement


@dataclass
class TradeResult:
    """Результат одной виртуальной сделки."""
    lead_hours: float
    entry_time_ms: int
    exit_time_ms: int
    peak_rate: float
    entry_basis: float
    exit_basis: float
    funding_income: float
    basis_pnl: float
    net_pnl: float
    settlements_count: int


def load_settlements(
    conn: sqlite3.Connection,
    symbol: str,
) -> list[Settlement]:
    """
    Все уникальные funding settlements символа.
    Дедупликация по next_funding_timestamp_ms.
    Берём basis_entry из metrics в момент settlement.
    """
    cursor = conn.execute(
        """
        SELECT
            f.next_funding_timestamp_ms,
            CAST(f.effective_funding_rate AS REAL),
            CAST(
                (SELECT CAST(m.basis_entry AS REAL)
                 FROM metrics m
                 WHERE m.symbol_name = f.symbol_name
                 ORDER BY ABS(m.calculated_at_ms - f.next_funding_timestamp_ms)
                 LIMIT 1)
            AS REAL)
        FROM funding_snapshots f
        WHERE f.symbol_name = ?
          AND f.next_funding_timestamp_ms IS NOT NULL
          AND f.effective_funding_rate IS NOT NULL
        ORDER BY f.next_funding_timestamp_ms, f.received_at_ms DESC
        """,
        (symbol,),
    )
    seen: set[int] = set()
    settlements: list[Settlement] = []
    for nxt, rate, basis in cursor:
        if nxt in seen:
            continue
        seen.add(nxt)
        settlements.append(
            Settlement(
                symbol=symbol,
                settle_time_ms=nxt,
                rate=float(rate),
                basis_entry=float(basis) if basis is not None else 0.0,
            ),
        )
    return settlements


def simulate_early_entry(
    settlements: list[Settlement],
    *,
    lead_hours: float,
    peak_threshold: float,
    holding_settlements: int,
) -> list[TradeResult]:
    """
    Симулирует сделки: вход за lead_hours до пика, удержание
    в течение holding_settlements сеттлментов.
    """
    results: list[TradeResult] = []
    lead_ms = int(lead_hours * 3_600_000)

    for i, s in enumerate(settlements):
        # Settlement считается "пиком", если rate > threshold
        if s.rate < peak_threshold:
            continue

        entry_ms = s.settle_time_ms - lead_ms

        # Берём holding_settlements сеттлментов начиная с пика
        end_idx = min(i + holding_settlements, len(settlements) - 1)
        if end_idx < i:
            continue

        # Funding income: сумма rate * notional за удержанные сеттлменты
        funding_income = sum(
            NOTIONAL * settlements[j].rate
            for j in range(i, end_idx + 1)
        )

        # Basis PnL: (entry_basis - exit_basis) * notional
        # Положительный базис на входе → convergence даёт прибыль
        entry_basis = settlements[max(0, i - int(lead_hours / 8))].basis_entry
        exit_basis = settlements[end_idx].basis_entry
        basis_pnl = NOTIONAL * (entry_basis - exit_basis)

        net_pnl = funding_income + basis_pnl - COSTS_USD

        results.append(
            TradeResult(
                lead_hours=lead_hours,
                entry_time_ms=entry_ms,
                exit_time_ms=settlements[end_idx].settle_time_ms,
                peak_rate=s.rate,
                entry_basis=entry_basis,
                exit_basis=exit_basis,
                funding_income=funding_income,
                basis_pnl=basis_pnl,
                net_pnl=net_pnl,
                settlements_count=end_idx - i + 1,
            ),
        )

    return results


def aggregate(results: list[TradeResult]) -> dict:
    if not results:
        return {
            "count": 0, "win_rate": 0.0,
            "avg_pnl": 0.0, "total_pnl": 0.0,
            "avg_funding": 0.0, "avg_basis_pnl": 0.0,
        }
    wins = [r for r in results if r.net_pnl > 0]
    return {
        "count": len(results),
        "win_rate": len(wins) / len(results) * 100,
        "avg_pnl": sum(r.net_pnl for r in results) / len(results),
        "total_pnl": sum(r.net_pnl for r in results),
        "avg_funding": sum(r.funding_income for r in results) / len(results),
        "avg_basis_pnl": sum(r.basis_pnl for r in results) / len(results),
    }


def run_sweep(conn: sqlite3.Connection) -> dict:
    """Собирает settlements для всех символов и запускает sweep."""
    all_settlements: list[Settlement] = []
    for symbol in SYMBOLS:
        print(f"  Загрузка {symbol}...")
        all_settlements.extend(load_settlements(conn, symbol))
    print(f"  Всего settlements: {len(all_settlements)}")

    # Sweep только по lead_time (фиксируем peak_threshold=0.010, holding=3)
    # Это даст основную кривую
    sweep_results: dict[float, list[TradeResult]] = {}
    for lead_h in LEAD_TIMES_H:
        trades = simulate_early_entry(
            all_settlements,
            lead_hours=float(lead_h),
            peak_threshold=0.010,  # 10.95% annual
            holding_settlements=3,
        )
        sweep_results[lead_h] = trades
        print(
            f"  lead={lead_h:>2}h: {len(trades):>3} trades, "
            f"avg PnL={aggregate(trades)['avg_pnl']:>+.2f}$",
        )

    return sweep_results


def write_report(sweep_results: dict) -> None:
    lines: list[str] = []
    lines.append("=" * 90)
    lines.append("BACKTEST v3: Early-Entry Funding Arbitrage")
    lines.append("=" * 90)
    lines.append(f"Символы: {', '.join(SYMBOLS)}")
    lines.append(f"Notional: ${NOTIONAL:,.0f} per trade")
    lines.append(f"Costs: {COSTS_PCT*100:.2f}% round-trip = ${COSTS_USD:.2f}")
    lines.append(f"Peak threshold: 1.0% per 8h (= 10.95% annual)")
    lines.append(f"Holding: 3 settlements (24h)")
    lines.append("")

    header = (
        f"{'Lead h':>7} {'Trades':>7} {'Win%':>7} "
        f"{'Avg PnL$':>10} {'Total$':>10} "
        f"{'Avg fund$':>10} {'Avg basis$':>11}"
    )
    lines.append(header)
    lines.append("-" * len(header))

    best_lead = None
    best_avg = -1e9
    sweep_data: list[tuple[float, float, float]] = []

    for lead_h in LEAD_TIMES_H:
        trades = sweep_results[lead_h]
        agg = aggregate(trades)
        lines.append(
            f"{lead_h:>7} {agg['count']:>7} {agg['win_rate']:>6.1f}% "
            f"{agg['avg_pnl']:>+10.2f} {agg['total_pnl']:>+10.2f} "
            f"{agg['avg_funding']:>+10.2f} {agg['avg_basis_pnl']:>+11.2f}",
        )
        sweep_data.append((lead_h, agg['avg_pnl'], agg['win_rate']))
        if agg['count'] > 0 and agg['avg_pnl'] > best_avg:
            best_avg = agg['avg_pnl']
            best_lead = lead_h

    lines.append("")
    lines.append("=" * 90)
    if best_lead is not None and best_avg > 0:
        lines.append(
            f"✅ Оптимальный lead_time: {best_lead}h "
            f"(avg PnL {best_avg:+.2f}$ per trade)",
        )
    else:
        lines.append(
            "🔴 Ни один lead_time не даёт положительного avg PnL. "
            "Стратегия убыточна даже при раннем входе.",
        )
    lines.append("")

    # Детальный анализ best lead
    if best_lead is not None and best_avg > 0:
        lines.append(f"📊 Детали для оптимального lead_time = {best_lead}h:")
        trades = sweep_results[best_lead]
        by_symbol: dict[str, list[TradeResult]] = {}
        for t in trades:
            # Определяем symbol по ближайшему settlement
            by_symbol.setdefault(_find_symbol(trades, t), []).append(t)

        for symbol in sorted(by_symbol):
            st = by_symbol[symbol]
            wins = len([t for t in st if t.net_pnl > 0])
            avg_pnl = sum(t.net_pnl for t in st) / len(st)
            lines.append(
                f"  {symbol:<12}: {len(st):>3} trades, "
                f"win {wins/len(st)*100:>5.1f}%, "
                f"avg PnL {avg_pnl:+.2f}$",
            )
    lines.append("")
    lines.append("📁 Details: " + str(REPORT_PATH))
    lines.append("📈 Chart:   " + str(CHART_PATH))
    lines.append("=" * 90)

    report = "\n".join(lines)
    print(report)
    REPORT_PATH.write_text(report, encoding="utf-8")
    return sweep_data


def _find_symbol(trades, t) -> str:
    # Fallback: не можем восстановить symbol из TradeResult
    return "UNKNOWN"


def plot_sweep(sweep_data: list[tuple[float, float, float]]) -> None:
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 9))
    fig.suptitle(
        "Backtest v3: Early Entry vs Lead Time",
        fontsize=14, fontweight="bold",
    )

    leads = [d[0] for d in sweep_data]
    avg_pnls = [d[1] for d in sweep_data]
    win_rates = [d[2] for d in sweep_data]

    ax1.plot(leads, avg_pnls, "o-", color="#2E86AB", linewidth=2, markersize=8)
    ax1.axhline(y=0, color="k", linestyle="-", alpha=0.3)
    ax1.axhline(y=COSTS_USD, color="r", linestyle="--", alpha=0.5,
                label=f"Break-even: ${COSTS_USD:.0f} costs")
    ax1.set_xlabel("Lead time (hours before peak)")
    ax1.set_ylabel("Average PnL per trade ($)")
    ax1.set_title("Average Net PnL vs Lead Time")
    ax1.grid(True, alpha=0.3)
    ax1.legend()

    ax2.plot(leads, win_rates, "s-", color="#A23B72", linewidth=2, markersize=8)
    ax2.axhline(y=50, color="r", linestyle="--", alpha=0.5, label="50% break-even")
    ax2.set_xlabel("Lead time (hours before peak)")
    ax2.set_ylabel("Win rate (%)")
    ax2.set_title("Win Rate vs Lead Time")
    ax2.grid(True, alpha=0.3)
    ax2.legend()

    plt.tight_layout()
    plt.savefig(CHART_PATH, dpi=120)
    plt.close(fig)
    print(f"📈 Chart saved: {CHART_PATH}")


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    print("Запуск sweep...")
    sweep_results = run_sweep(conn)
    conn.close()

    sweep_data = write_report(sweep_results)
    plot_sweep(sweep_data)


if __name__ == "__main__":
    main()