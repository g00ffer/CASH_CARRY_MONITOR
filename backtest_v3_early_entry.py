#!/usr/bin/env python3
"""
Backtest v3: Early-Entry Funding Arbitrage

Гипотеза: вход за 1-2 эпохи ДО пика funding даёт лучший базис
(толпа ещё не сжала премию) + больше высоких сеттлментов.

Sweep: lead_time 0..48h. Peak = settlement с rate >= 1.0% per 8h.
PnL = funding_income + basis_pnl - costs (notional $10k, costs 0.05%).

Run: python backtest_v3_early_entry.py
Output: backtest_v3_report.txt, backtest_v3_chart.png
"""
from __future__ import annotations

import bisect
import sqlite3
from dataclasses import dataclass
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

SYMBOLS = [
    "BTC_CARRY", "ETH_CARRY", "SOL_CARRY",
    "XRP_CARRY", "ADA_CARRY", "AVAX_CARRY",
]

LEAD_TIMES_H = [0, 4, 8, 12, 16, 20, 24, 32, 40, 48]
PEAK_THRESHOLD = 0.001       # 1.0% per 8h = 10.95% annual
HOLDING_SETTLEMENTS = 3      # пик + ещё 2 сеттлмента


@dataclass
class TradeResult:
    symbol: str
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


# ======================================================================
# Data loading
# ======================================================================

def load_settlements(
    conn: sqlite3.Connection, symbol: str,
) -> tuple[list[int], list[float]]:
    """Уникальные funding settlements: (times, rates)."""
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


def load_basis_series(
    conn: sqlite3.Connection, symbol: str,
) -> tuple[list[int], list[float]]:
    """Временная серия базиса из metrics: (times, basis)."""
    cursor = conn.execute(
        """
        SELECT calculated_at_ms, CAST(basis_entry AS REAL)
        FROM metrics
        WHERE symbol_name = ?
          AND basis_entry IS NOT NULL
        ORDER BY calculated_at_ms
        """,
        (symbol,),
    )
    ts: list[int] = []
    vals: list[float] = []
    for t, b in cursor:
        ts.append(t)
        vals.append(float(b))
    return ts, vals


def value_at(
    ts: list[int], vals: list[float], target_ms: int,
) -> float:
    """Ближайшее по времени значение серии."""
    if not ts:
        return 0.0
    i = bisect.bisect_left(ts, target_ms)
    if i == 0:
        return vals[0]
    if i == len(ts):
        return vals[-1]
    before, after = ts[i - 1], ts[i]
    return vals[i - 1] if (target_ms - before) <= (after - target_ms) else vals[i]


# ======================================================================
# Simulation
# ======================================================================

def simulate_early_entry(
    symbol: str,
    times: list[int],
    rates: list[float],
    basis_ts: list[int],
    basis_vals: list[float],
    *,
    lead_hours: float,
) -> list[TradeResult]:
    """Сделки: вход за lead_hours до пика, удержание HOLDING_SETTLEMENTS."""
    results: list[TradeResult] = []
    lead_ms = int(lead_hours * 3_600_000)

    for i, peak_ms in enumerate(times):
        if rates[i] < PEAK_THRESHOLD:
            continue

        last_idx = min(i + HOLDING_SETTLEMENTS - 1, len(times) - 1)
        exit_ms = times[last_idx]
        entry_ms = peak_ms - lead_ms

        # Funding income: ВСЕ сеттлменты в (entry, exit]
        j_start = bisect.bisect_right(times, entry_ms)
        j_end = bisect.bisect_right(times, exit_ms) - 1
        if j_end < j_start:
            continue
        funding_income = NOTIONAL * sum(rates[j_start:j_end + 1])

        entry_basis = value_at(basis_ts, basis_vals, entry_ms)
        exit_basis = value_at(basis_ts, basis_vals, exit_ms)
        # long spot / short perp: прибыль при (b0 - b1) > 0
        basis_pnl = NOTIONAL * (entry_basis - exit_basis)

        net_pnl = funding_income + basis_pnl - COSTS_USD

        results.append(
            TradeResult(
                symbol=symbol,
                lead_hours=lead_hours,
                entry_time_ms=entry_ms,
                exit_time_ms=exit_ms,
                peak_rate=rates[i],
                entry_basis=entry_basis,
                exit_basis=exit_basis,
                funding_income=funding_income,
                basis_pnl=basis_pnl,
                net_pnl=net_pnl,
                settlements_count=j_end - j_start + 1,
            ),
        )

    return results


def aggregate(trades: list[TradeResult]) -> dict:
    if not trades:
        return {
            "count": 0, "win_rate": 0.0, "avg_pnl": 0.0,
            "total_pnl": 0.0, "avg_funding": 0.0, "avg_basis": 0.0,
        }
    wins = [t for t in trades if t.net_pnl > 0]
    n = len(trades)
    return {
        "count": n,
        "win_rate": len(wins) / n * 100,
        "avg_pnl": sum(t.net_pnl for t in trades) / n,
        "total_pnl": sum(t.net_pnl for t in trades),
        "avg_funding": sum(t.funding_income for t in trades) / n,
        "avg_basis": sum(t.basis_pnl for t in trades) / n,
    }


# ======================================================================
# Sweep + report + chart
# ======================================================================

def run_sweep(conn: sqlite3.Connection) -> dict[float, list[TradeResult]]:
    data: dict[str, tuple[list[int], list[float], list[int], list[float]]] = {}
    for symbol in SYMBOLS:
        print(f"  Загрузка {symbol}...")
        times, rates = load_settlements(conn, symbol)
        basis_ts, basis_vals = load_basis_series(conn, symbol)
        data[symbol] = (times, rates, basis_ts, basis_vals)
        print(f"    settlements: {len(times)}, basis points: {len(basis_ts)}")

    sweep: dict[float, list[TradeResult]] = {}
    for lead_h in LEAD_TIMES_H:
        trades: list[TradeResult] = []
        for symbol, (times, rates, bts, bvals) in data.items():
            trades.extend(
                simulate_early_entry(
                    symbol, times, rates, bts, bvals,
                    lead_hours=float(lead_h),
                ),
            )
        sweep[lead_h] = trades
        agg = aggregate(trades)
        print(
            f"  lead={lead_h:>2}h: {agg['count']:>3} trades, "
            f"win {agg['win_rate']:>5.1f}%, "
            f"avg PnL {agg['avg_pnl']:>+7.2f}$",
        )
    return sweep


def write_report(sweep: dict[float, list[TradeResult]]) -> list:
    lines: list[str] = []
    lines.append("=" * 95)
    lines.append("BACKTEST v3: Early-Entry Funding Arbitrage")
    lines.append("=" * 95)
    lines.append(f"Символы: {', '.join(SYMBOLS)}")
    lines.append(f"Notional: ${NOTIONAL:,.0f} | Costs: {COSTS_PCT*100:.2f}% = ${COSTS_USD:.2f}")
    lines.append(
        f"Peak: rate >= {PEAK_THRESHOLD*100:.2f}% per 8h | "
        f"Holding: {HOLDING_SETTLEMENTS} settlements",
    )
    lines.append("")

    header = (
        f"{'Lead h':>7} {'Trades':>7} {'Win%':>7} "
        f"{'Avg PnL$':>10} {'Total$':>10} "
        f"{'Avg fund$':>10} {'Avg basis$':>11}"
    )
    lines.append(header)
    lines.append("-" * len(header))

    sweep_data = []
    best_lead, best_avg = None, -1e9
    for lead_h in LEAD_TIMES_H:
        agg = aggregate(sweep[lead_h])
        lines.append(
            f"{lead_h:>7} {agg['count']:>7} {agg['win_rate']:>6.1f}% "
            f"{agg['avg_pnl']:>+10.2f} {agg['total_pnl']:>+10.2f} "
            f"{agg['avg_funding']:>+10.2f} {agg['avg_basis']:>+11.2f}",
        )
        sweep_data.append((lead_h, agg["avg_pnl"], agg["win_rate"]))
        if agg["count"] > 0 and agg["avg_pnl"] > best_avg:
            best_avg, best_lead = agg["avg_pnl"], lead_h

    lines.append("")
    lines.append("=" * 95)
    if best_lead is not None and best_avg > 0:
        lines.append(
            f"✅ Оптимальный lead_time: {best_lead}h "
            f"(avg PnL {best_avg:+.2f}$ per trade)",
        )
        lines.append("")
        lines.append(f"📊 Разбивка по символам (lead={best_lead}h):")
        by_symbol: dict[str, list[TradeResult]] = {}
        for t in sweep[best_lead]:
            by_symbol.setdefault(t.symbol, []).append(t)
        for symbol in sorted(by_symbol):
            st = by_symbol[symbol]
            agg = aggregate(st)
            lines.append(
                f"  {symbol:<12}: {agg['count']:>3} trades, "
                f"win {agg['win_rate']:>5.1f}%, "
                f"avg PnL {agg['avg_pnl']:>+7.2f}$, "
                f"avg basis {agg['avg_basis']:>+7.2f}$",
            )
    else:
        lines.append(
            "🔴 Ни один lead_time не даёт положительного avg PnL.",
        )
    lines.append("=" * 95)

    report = "\n".join(lines)
    print(report)
    REPORT_PATH.write_text(report, encoding="utf-8")
    return sweep_data


def plot_sweep(sweep_data: list) -> None:
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 9))
    fig.suptitle("Backtest v3: Early Entry vs Lead Time", fontsize=14, fontweight="bold")

    leads = [d[0] for d in sweep_data]
    pnls = [d[1] for d in sweep_data]
    wins = [d[2] for d in sweep_data]

    ax1.plot(leads, pnls, "o-", color="#2E86AB", linewidth=2, markersize=8)
    ax1.axhline(y=0, color="k", linestyle="-", alpha=0.3)
    ax1.set_xlabel("Lead time (hours before peak)")
    ax1.set_ylabel("Average PnL per trade ($)")
    ax1.set_title("Average Net PnL vs Lead Time")
    ax1.grid(True, alpha=0.3)

    ax2.plot(leads, wins, "s-", color="#A23B72", linewidth=2, markersize=8)
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
    sweep = run_sweep(conn)
    conn.close()

    sweep_data = write_report(sweep)
    plot_sweep(sweep_data)


if __name__ == "__main__":
    main()