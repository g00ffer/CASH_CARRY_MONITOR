#!/usr/bin/env python3
"""
Backtest v5: Basis-Only (mean-reversion) strategy.

Торгуем сходимость базиса к нулю, игнорируя funding.

  A) backwardation (basis <= -entry_thr): short spot (margin) + long perp
     gross% = b_exit - b_entry; платим borrow за шорт-плечо
  B) contango     (basis >= +entry_thr): long spot + short perp
     gross% = b_entry - b_exit

Exit: конвергенция (|b| <= exit_thr) | стоп (|b| >= stop_thr) | время.

Costs: fees round-trip + borrow (только сторона A).

Run: python backtest_v5_basis_only.py
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("data/monitor.sqlite")
REPORT_PATH = Path("backtest_v5_report.txt")

NOTIONAL = 10_000.0
FEE_RT = 0.0010            # 0.10% round-trip (maker обе ноги)
BORROW_ANNUAL = 0.08       # 8% годовых заём спота (сторона A)
RESAMPLE_MIN = 5           # 5-минутные бакеты — гасим шум 10s-поллинга

SYMBOLS = [
    "BTC_CARRY", "ETH_CARRY", "SOL_CARRY",
    "XRP_CARRY", "ADA_CARRY", "AVAX_CARRY",
]

ENTRY_THRS = [0.0006, 0.0008, 0.0010, 0.0015]   # |basis| входа
EXIT_THR = 0.0002                               # |basis| выхода (сошлось)
STOP_THR = 0.0025                               # стоп: базис расширился
MAX_HOLD_H = [24, 48, 72]

BUCKET_MS = RESAMPLE_MIN * 60 * 1000


@dataclass
class Trade:
    symbol: str
    side: str
    entry_thr: float
    max_hold_h: int
    entry_utc: str
    exit_utc: str
    hours: float
    b0_bps: float
    b1_bps: float
    gross_usd: float
    fees_usd: float
    borrow_usd: float
    net_usd: float
    exit_reason: str


def ms_to_utc(ms: int) -> str:
    return datetime.fromtimestamp(
        ms / 1000, tz=timezone.utc,
    ).strftime("%m-%d %H:%M")


def load_basis(
    conn: sqlite3.Connection, symbol: str,
) -> tuple[list[int], list[float]]:
    """5-минутные бакеты basis_entry (последнее значение в бакете)."""
    cursor = conn.execute(
        """
        SELECT calculated_at_ms, CAST(basis_entry AS REAL)
        FROM metrics
        WHERE symbol_name = ? AND basis_entry IS NOT NULL
        ORDER BY calculated_at_ms
        """,
        (symbol,),
    )
    buckets: dict[int, float] = {}
    for ts, b in cursor:
        buckets[ts // BUCKET_MS] = float(b)
    keys = sorted(buckets)
    return [k * BUCKET_MS for k in keys], [buckets[k] for k in keys]


def basis_profile(symbol: str, vals: list[float]) -> str:
    if not vals:
        return f"  {symbol:<12}: нет данных"
    n = len(vals)
    mean = sum(vals) / n
    below_8 = sum(1 for v in vals if v <= -0.0008) / n * 100
    above_5 = sum(1 for v in vals if v >= 0.0005) / n * 100
    return (
        f"  {symbol:<12}: buckets={n:>5} "
        f"mean={mean*1e4:>+7.2f}bps min={min(vals)*1e4:>+8.2f} "
        f"max={max(vals)*1e4:>+7.2f} | "
        f"<=-8bps: {below_8:>5.1f}% | >=+5bps: {above_5:>4.1f}%"
    )


def simulate(
    symbol: str,
    times: list[int],
    vals: list[float],
    side: str,
    entry_thr: float,
    max_hold_h: int,
) -> list[Trade]:
    trades: list[Trade] = []
    i = 0
    n = len(times)
    while i < n:
        b = vals[i]
        enter = (
            (side == "backwardation" and b <= -entry_thr)
            or (side == "contango" and b >= entry_thr)
        )
        if not enter:
            i += 1
            continue

        b0, t0 = b, times[i]
        j = i + 1
        exit_reason = "time"
        b1 = b0
        while j < n:
            bj = vals[j]
            b1 = bj
            converged = abs(bj) <= EXIT_THR
            stopped = (
                (side == "backwardation" and bj <= -STOP_THR)
                or (side == "contango" and bj >= STOP_THR)
            )
            expired = (times[j] - t0) >= max_hold_h * 3_600_000
            if converged:
                exit_reason = "converged"
                break
            if stopped:
                exit_reason = "stop"
                break
            if expired:
                exit_reason = "time"
                break
            j += 1

        hours = (times[j] - t0) / 3_600_000 if j < n else 0.0
        gross_pct = (b1 - b0) if side == "backwardation" else (b0 - b1)
        gross_usd = NOTIONAL * gross_pct
        fees_usd = NOTIONAL * FEE_RT
        borrow_usd = (
            NOTIONAL * BORROW_ANNUAL * hours / 8760
            if side == "backwardation" else 0.0
        )
        net_usd = gross_usd - fees_usd - borrow_usd

        trades.append(Trade(
            symbol=symbol,
            side=side,
            entry_thr=entry_thr,
            max_hold_h=max_hold_h,
            entry_utc=ms_to_utc(t0),
            exit_utc=ms_to_utc(times[j]) if j < n else "-",
            hours=hours,
            b0_bps=b0 * 1e4,
            b1_bps=b1 * 1e4,
            gross_usd=gross_usd,
            fees_usd=fees_usd,
            borrow_usd=borrow_usd,
            net_usd=net_usd,
            exit_reason=exit_reason,
        ))
        i = j + 1  # не входим сразу после выхода

    return trades


def aggregate(trades: list[Trade]) -> dict:
    if not trades:
        return {"n": 0, "win": 0.0, "avg_gross": 0.0,
                "avg_net": 0.0, "total_net": 0.0}
    wins = [t for t in trades if t.net_usd > 0]
    return {
        "n": len(trades),
        "win": len(wins) / len(trades) * 100,
        "avg_gross": sum(t.gross_usd for t in trades) / len(trades),
        "avg_net": sum(t.net_usd for t in trades) / len(trades),
        "total_net": sum(t.net_usd for t in trades),
    }


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    data = {}
    lines: list[str] = []
    lines.append("=" * 100)
    lines.append("BACKTEST v5: Basis-Only mean-reversion")
    lines.append("=" * 100)
    lines.append(
        f"Notional ${NOTIONAL:,.0f} | fees RT {FEE_RT*100:.2f}% | "
        f"borrow {BORROW_ANNUAL*100:.0f}% annual (backwardation only)",
    )
    lines.append(
        f"Exit: |b|<={EXIT_THR*1e4:.0f}bps | stop |b|>={STOP_THR*1e4:.0f}bps "
        f"| resample {RESAMPLE_MIN}min",
    )
    lines.append("")
    lines.append("📐 Профиль базиса (где вообще есть возможности):")

    for symbol in SYMBOLS:
        times, vals = load_basis(conn, symbol)
        data[symbol] = (times, vals)
        lines.append(basis_profile(symbol, vals))
    lines.append("")

    for side in ("backwardation", "contango"):
        lines.append(f"===== SIDE: {side} =====")
        header = (
            f"{'entry':>7} {'hold':>5} {'trades':>7} {'win%':>6} "
            f"{'avg gross$':>11} {'avg net$':>9} {'total$':>9}"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for thr in ENTRY_THRS:
            for hold in MAX_HOLD_H:
                all_trades: list[Trade] = []
                for symbol in SYMBOLS:
                    t, v = data[symbol]
                    all_trades.extend(
                        simulate(symbol, t, v, side, thr, hold),
                    )
                a = aggregate(all_trades)
                lines.append(
                    f"{thr*1e4:>6.1f}b {hold:>4}h {a['n']:>7} "
                    f"{a['win']:>5.1f}% {a['avg_gross']:>+11.2f} "
                    f"{a['avg_net']:>+9.2f} {a['total_net']:>+9.2f}",
                )
        lines.append("")

        # Детализация лучшей конфигурации
        best = None
        for thr in ENTRY_THRS:
            for hold in MAX_HOLD_H:
                tr = []
                for symbol in SYMBOLS:
                    t, v = data[symbol]
                    tr.extend(simulate(symbol, t, v, side, thr, hold))
                a = aggregate(tr)
                if a["n"] >= 5 and (best is None or a["total_net"] > best[2]):
                    best = (thr, hold, a, tr)
        if best:
            thr, hold, a, tr = best
            lines.append(
                f"🏆 Best {side}: entry={thr*1e4:.1f}bps hold={hold}h "
                f"-> {a['n']} trades, win {a['win']:.0f}%, "
                f"total {a['total_net']:+,.2f}$",
            )
            reasons: dict[str, int] = {}
            for t in tr:
                reasons[t.exit_reason] = (
                    reasons.get(t.exit_reason, 0) + 1
                )
            lines.append(f"   exits: {reasons}")
            by_sym: dict[str, list[Trade]] = {}
            for t in tr:
                by_sym.setdefault(t.symbol, []).append(t)
            for symbol in sorted(by_sym):
                a2 = aggregate(by_sym[symbol])
                lines.append(
                    f"   {symbol:<12}: {a2['n']:>3} trades, "
                    f"win {a2['win']:>5.1f}%, "
                    f"avg gross {a2['avg_gross']:>+7.2f}$, "
                    f"total {a2['total_net']:>+8.2f}$",
                )
        lines.append("")

    conn.close()
    report = "\n".join(lines)
    print(report)
    REPORT_PATH.write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()