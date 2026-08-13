#!/usr/bin/env python3
"""
Backtesting engine for cash-carry-monitor.

Reads accumulated metrics from SQLite and simulates the signal engine
under alternative configs (fees / horizon / thresholds):
"what would have happened if...?"

Run: python backtest.py
Output: console table + backtest_report.txt
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

DB_PATH = Path("data/monitor.sqlite")
REPORT_PATH = Path("backtest_report.txt")

HOURS_PER_YEAR = 8760.0
PERIODS_PER_YEAR_8H = 1095.0  # 8760 / 8

# One-time round-trip costs (decimal fractions)
# taker: 2*0.10% + 2*0.05% + slippage 2+2 bps + spread buffer 2 bps
TAKER_COSTS = 0.0036
# maker: 2*0.075% + 2*0.02% + slippage 2+2 bps + spread buffer 2 bps
MAKER_COSTS = 0.0025

CYCLE_SEC = 10.0  # market_interval_ms = 10000


@dataclass(frozen=True)
class Scenario:
    label: str
    horizon_h: float
    amort_h: float
    one_time_costs: float
    threshold: float  # min net annual, decimal


@dataclass
class SimResult:
    alerts: int
    active_cycles: int
    total_cycles: int
    funding_at_alert: list = field(default_factory=list)
    net_at_alert: list = field(default_factory=list)

    @property
    def hours_active(self) -> float:
        return self.active_cycles * CYCLE_SEC / 3600.0

    @property
    def pct_active(self) -> float:
        return 100.0 * self.active_cycles / max(1, self.total_cycles)

    @property
    def avg_funding_at_alert(self) -> float:
        if not self.funding_at_alert:
            return 0.0
        return sum(self.funding_at_alert) / len(self.funding_at_alert)

    @property
    def avg_net_at_alert(self) -> float:
        if not self.net_at_alert:
            return 0.0
        return sum(self.net_at_alert) / len(self.net_at_alert)


def load_series() -> dict[str, list[tuple[int, float]]]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """
        SELECT symbol_name, calculated_at_ms, CAST(funding_annual AS REAL)
        FROM metrics
        ORDER BY symbol_name, calculated_at_ms
        """,
    ).fetchall()
    conn.close()

    series: dict[str, list[tuple[int, float]]] = {}
    for symbol, ts, funding in rows:
        series.setdefault(symbol, []).append((int(ts), float(funding)))
    return series


def simulate(
    rows: list[tuple[int, float]],
    *,
    horizon_h: float,
    amort_h: float,
    one_time_costs: float,
    threshold: float,
    hysteresis: float = 0.01,
    confirmations: int = 3,
    cooldown_sec: int = 3600,
    min_net_horizon: float = 0.001,
    min_rate: float = 0.00005,
) -> SimResult:
    """
    Replay the signal state machine over a funding time series.
    Mirrors engine.py semantics: hysteresis, consecutive confirmations,
    cooldown, funding sign/rate gates.
    """
    alerts = 0
    active_cycles = 0
    funding_at_alert: list[float] = []
    net_at_alert: list[float] = []
    conf = 0
    active = False
    last_alert_ms: int | None = None

    costs_annualized = one_time_costs * HOURS_PER_YEAR / amort_h

    for ts_ms, funding_annual in rows:
        net_annual = funding_annual - costs_annualized
        net_horizon = net_annual * horizon_h / HOURS_PER_YEAR
        rate = funding_annual / PERIODS_PER_YEAR_8H

        eff_threshold = threshold - (hysteresis if active else 0.0)
        checks_ok = (
            net_annual >= eff_threshold
            and net_horizon >= min_net_horizon
            and rate >= min_rate
            and funding_annual > 0
        )

        conf = conf + 1 if checks_ok else 0
        was_active = active
        active = conf >= confirmations

        if active:
            active_cycles += 1

        # Alert only on NORMAL -> ACTIVE transition, respecting cooldown
        if active and not was_active:
            if (
                last_alert_ms is None
                or (ts_ms - last_alert_ms) >= cooldown_sec * 1000
            ):
                alerts += 1
                last_alert_ms = ts_ms
                funding_at_alert.append(funding_annual)
                net_at_alert.append(net_annual)

    return SimResult(
        alerts=alerts,
        active_cycles=active_cycles,
        total_cycles=len(rows),
        funding_at_alert=funding_at_alert,
        net_at_alert=net_at_alert,
    )


def build_scenarios() -> list[Scenario]:
    scenarios: list[Scenario] = []
    for fees_label, costs in (("taker", TAKER_COSTS), ("maker", MAKER_COSTS)):
        for horizon in (168, 720, 2160):
            for thr_pct in (6.0, 7.0, 8.0):
                scenarios.append(
                    Scenario(
                        label=f"{fees_label}/H{horizon}/T{thr_pct:.0f}",
                        horizon_h=float(horizon),
                        amort_h=float(horizon),
                        one_time_costs=costs,
                        threshold=thr_pct / 100.0,
                    ),
                )
    return scenarios


def main() -> None:
    print("Loading metrics from SQLite...")
    series = load_series()
    print(f"Symbols: {', '.join(sorted(series))}")
    print(f"Total rows: {sum(len(v) for v in series.values())}")

    scenarios = build_scenarios()
    lines: list[str] = []
    lines.append("=" * 100)
    lines.append("BACKTEST: what-if scenarios over accumulated data")
    lines.append(
        "fees = taker(0.36%)/maker(0.25%) | H = holding/amortization hours | "
        "T = min_net_annual threshold",
    )
    lines.append("=" * 100)

    header = (
        f"{'scenario':<20}{'symbol':<12}{'alerts':>7}{'h_active':>10}"
        f"{'%active':>9}{'fund@alrt':>11}{'net@alrt':>10}"
    )
    lines.append(header)
    print(header)

    for sc in scenarios:
        for symbol in sorted(series):
            res = simulate(
                series[symbol],
                horizon_h=sc.horizon_h,
                amort_h=sc.amort_h,
                one_time_costs=sc.one_time_costs,
                threshold=sc.threshold,
            )
            row = (
                f"{sc.label:<20}{symbol:<12}{res.alerts:>7}"
                f"{res.hours_active:>10.1f}{res.pct_active:>8.1f}%"
                f"{res.avg_funding_at_alert * 100:>10.2f}%"
                f"{res.avg_net_at_alert * 100:>9.2f}%"
            )
            lines.append(row)
            print(row)
        lines.append("-" * 100)

    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nReport saved: {REPORT_PATH}")


if __name__ == "__main__":
    main()