#!/usr/bin/env python3
"""
Backtest v4: cross-exchange PERP-PERP funding spread arbitrage.

Strategy (market-neutral, no spot leg):
  - spread = binance_rate - bybit_rate (per 8h settlement)
  - spread > thr: short Binance perp + long Bybit perp
  - spread < -thr: long Binance perp + short Bybit perp
  - earn |spread| x notional at every settlement while held
  - exit when |spread| < exit_thr or max_hold reached

Fees (round trip, both legs, maker):
  2 x (binance_perp_maker + bybit_perp_maker)
  = 2 x (0.02% + 0.036%) = 0.112%

Run: python backtest_v4_cross_exchange.py
"""
from __future__ import annotations

import bisect
import sqlite3
from dataclasses import dataclass
from pathlib import Path

BINANCE_DB = Path("data/monitor.sqlite")
BYBIT_DB = Path("data/bybit_funding.sqlite")
REPORT_PATH = Path("backtest_v4_report.txt")

NOTIONAL = 10_000.0
BINANCE_PERP_MAKER = 0.0002   # 0.02%
BYBIT_PERP_MAKER = 0.00036    # 0.036% (твой уровень Bybit)
ROUND_TRIP_FEE_PCT = 2 * (BINANCE_PERP_MAKER + BYBIT_PERP_MAKER)
ROUND_TRIP_FEE_USD = NOTIONAL * ROUND_TRIP_FEE_PCT

SYMBOLS = [
    "BTC_CARRY", "ETH_CARRY", "SOL_CARRY",
    "XRP_CARRY", "ADA_CARRY", "AVAX_CARRY",
]

ENTRY_THRESHOLDS = [0.0001, 0.00015, 0.0002, 0.0003]
MAX_HOLD_SETTLEMENTS = 90  # 30 дней


@dataclass
class Trade:
    symbol: str
    entry_ms: int
    exit_ms: int
    direction: str          # short_binance | long_binance
    settlements: int
    gross_income: float
    fees: float
    net_pnl: float


def load_settlements(
    conn: sqlite3.Connection, table: str, rate_col: str,
) -> dict[str, tuple[list[int], list[float]]]:
    """Уникальные settlements per symbol: {symbol: (times, rates)}."""
    cursor = conn.execute(
        f"""
        SELECT symbol_name, next_funding_timestamp_ms,
               CAST({rate_col} AS REAL)
        FROM {table}
        WHERE next_funding_timestamp_ms IS NOT NULL
          AND {rate_col} IS NOT NULL
        ORDER BY symbol_name, next_funding_timestamp_ms,
                 received_at_ms DESC
        """,
    )
    out: dict[str, tuple[list[int], list[float]]] = {}
    seen: dict[str, set[int]] = {}
    for sym, nxt, rate in cursor:
        s = seen.setdefault(sym, set())
        if nxt in s:
            continue
        s.add(nxt)
        times, rates = out.setdefault(sym, ([], []))
        times.append(nxt)
        rates.append(rate)
    return out


def nearest_index(times: list[int], target: int, tol_ms: int) -> int | None:
    if not times:
        return None
    i = bisect.bisect_left(times, target)
    best, best_d = None, tol_ms + 1
    for j in (i - 1, i):
        if 0 <= j < len(times):
            d = abs(times[j] - target)
            if d < best_d:
                best, best_d = j, d
    return best


def simulate_symbol(
    symbol: str,
    bin_times: list[int],
    bin_rates: list[float],
    byb_times: list[int],
    byb_rates: list[float],
    entry_thr: float,
) -> tuple[list[Trade], dict]:
    """Симуляция + профиль спреда."""
    exit_thr = entry_thr / 4
    trades: list[Trade] = []

    # Профиль спреда на пересечении данных
    spreads: list[tuple[int, float]] = []
    for t, r in zip(bin_times, bin_rates):
        j = nearest_index(byb_times, t, 10 * 60 * 1000)
        if j is None:
            continue
        spreads.append((t, r - byb_rates[j]))

    profile = {
        "overlap_settlements": len(spreads),
        "abs_mean": 0.0,
        "abs_max": 0.0,
        "pct_above_thr": 0.0,
    }
    if spreads:
        abs_vals = [abs(s) for _, s in spreads]
        profile["abs_mean"] = sum(abs_vals) / len(abs_vals)
        profile["abs_max"] = max(abs_vals)
        profile["pct_above_thr"] = (
            100 * sum(1 for v in abs_vals if v >= entry_thr)
            / len(abs_vals)
        )

    # Симуляция позиции
    i = 0
    while i < len(spreads):
        ts, sp = spreads[i]
        if abs(sp) < entry_thr:
            i += 1
            continue

        direction = "short_binance" if sp > 0 else "long_binance"
        gross = 0.0
        held = 0
        exit_ms = ts
        j = i
        while j < len(spreads) and held < MAX_HOLD_SETTLEMENTS:
            _, s = spreads[j]
            # пока держим позицию в том же направлении
            if (s >= 0) != (sp >= 0) and abs(s) < exit_thr:
                break
            gross += NOTIONAL * abs(s)
            held += 1
            exit_ms = spreads[j][0]
            if abs(s) < exit_thr and held > 1:
                break
            j += 1

        fees = ROUND_TRIP_FEE_USD
        trades.append(Trade(
            symbol=symbol,
            entry_ms=ts,
            exit_ms=exit_ms,
            direction=direction,
            settlements=held,
            gross_income=gross,
            fees=fees,
            net_pnl=gross - fees,
        ))
        i = max(j, i + 1)

    return trades, profile


def main() -> None:
    if not BYBIT_DB.exists():
        print("🔴 Нет data/bybit_funding.sqlite — запусти коллектор!")
        return

    bin_conn = sqlite3.connect(BINANCE_DB)
    byb_conn = sqlite3.connect(BYBIT_DB)
    bin_data = load_settlements(
        bin_conn, "funding_snapshots", "effective_funding_rate",
    )
    byb_data = load_settlements(
        byb_conn, "funding_bybit", "funding_rate",
    )
    bin_conn.close()
    byb_conn.close()

    lines: list[str] = []
    lines.append("=" * 100)
    lines.append("BACKTEST v4: cross-exchange perp-perp funding spread")
    lines.append("=" * 100)
    lines.append(
        f"Notional: ${NOTIONAL:,.0f} | Round-trip fees: "
        f"{ROUND_TRIP_FEE_PCT*100:.3f}% = ${ROUND_TRIP_FEE_USD:.2f}",
    )
    lines.append("")

    any_data = False
    for entry_thr in ENTRY_THRESHOLDS:
        lines.append(
            f"--- entry threshold: {entry_thr*100:.3f}% per 8h "
            f"({entry_thr*1095*100:.1f}% annual diff) ---",
        )
        all_trades: list[Trade] = []
        for symbol in SYMBOLS:
            if symbol not in bin_data or symbol not in byb_data:
                continue
            bt, br = bin_data[symbol]
            yt, yr = byb_data[symbol]
            trades, profile = simulate_symbol(
                symbol, bt, br, yt, yr, entry_thr,
            )
            if profile["overlap_settlements"] > 0:
                any_data = True
            all_trades.extend(trades)
            lines.append(
                f"  {symbol:<12} overlap={profile['overlap_settlements']:>4} "
                f"|spread| avg={profile['abs_mean']*100:.4f}% "
                f"max={profile['abs_max']*100:.4f}% "
                f"above_thr={profile['pct_above_thr']:>5.1f}% "
                f"| trades={len(trades):>3}",
            )

        wins = [t for t in all_trades if t.net_pnl > 0]
        total_net = sum(t.net_pnl for t in all_trades)
        total_gross = sum(t.gross_income for t in all_trades)
        lines.append(
            f"  ИТОГО: trades={len(all_trades)}, "
            f"win={len(wins)}, "
            f"gross=${total_gross:+,.2f}, "
            f"fees=${len(all_trades)*ROUND_TRIP_FEE_USD:,.2f}, "
            f"NET=${total_net:+,.2f}",
        )
        lines.append("")

    if not any_data:
        lines.append(
            "🔴 Пересечения данных Binance×Bybit пока нет или оно "
            "слишком короткое. Коллектор должен поработать 3-7 дней.",
        )

    report = "\n".join(lines)
    print(report)
    REPORT_PATH.write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()