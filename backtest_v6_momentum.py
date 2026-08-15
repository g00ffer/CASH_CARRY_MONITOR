#!/usr/bin/env python3
"""
backtest_v6_momentum.py — Momentum strategies on Bybit linear 1h klines.

Three strategies:
    1. SMA crossover (20/50 and 50/200)
    2. Time-series momentum (12-month return, vol-targeted)
    3. Breakout (Donchian channel 20/55)

Each strategy:
    - Fixed fractional position sizing with volatility target
    - Transaction costs: 5 bps taker (entry + exit)
    - Metrics: Sharpe, Sortino, max DD, win rate, CAGR
"""
from __future__ import annotations

import math
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DB = Path("data/klines.sqlite")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
           "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT"]

# Global parameters
INITIAL_CAPITAL = 10_000.0
POSITION_SIZE_PCT = 0.10          # 10% of equity per position
VOL_TARGET_ANNUAL = 0.15          # 15% annual vol target
TRANSACTION_COST_BPS = 5.0        # 5 bps per trade
RISK_FREE_RATE = 0.04             # 4% annual (for Sharpe)
HOURS_PER_YEAR = 365.25 * 24


# =====================================================================
# Data loading
# =====================================================================

def load_klines(symbol: str) -> List[dict]:
    """Load hourly klines for symbol, ordered by time."""
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT open_time_ms, open, high, low, close, volume "
        "FROM klines WHERE symbol=? AND interval='1h' "
        "ORDER BY open_time_ms ASC",
        (symbol,),
    ).fetchall()
    conn.close()
    return [
        {
            "ts": r[0],
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
        }
        for r in rows
    ]


# =====================================================================
# Indicators
# =====================================================================

def sma(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    window_sum = sum(values[:period])
    out[period - 1] = window_sum / period
    for i in range(period, len(values)):
        window_sum += values[i] - values[i - period]
        out[i] = window_sum / period
    return out


def rolling_std(returns: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(returns)
    if len(returns) < period:
        return out
    for i in range(period - 1, len(returns)):
        w = returns[i - period + 1 : i + 1]
        mean = sum(w) / period
        var = sum((x - mean) ** 2 for x in w) / period
        out[i] = math.sqrt(var)
    return out


def donchian_high(highs: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(highs)
    if len(highs) < period:
        return out
    for i in range(period - 1, len(highs)):
        out[i] = max(highs[i - period + 1 : i + 1])
    return out


def donchian_low(lows: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(lows)
    if len(lows) < period:
        return out
    for i in range(period - 1, len(lows)):
        out[i] = min(lows[i - period + 1 : i + 1])
    return out


# =====================================================================
# Strategy base
# =====================================================================

@dataclass
class Trade:
    entry_ts: int
    entry_price: float
    side: int              # +1 long, -1 short
    exit_ts: Optional[int] = None
    exit_price: Optional[float] = None
    pnl_pct: Optional[float] = None


@dataclass
class StrategyResult:
    name: str
    symbol: str
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if (t.pnl_pct or 0) > 0)
        return wins / len(self.trades)

    @property
    def total_return(self) -> float:
        if len(self.equity_curve) < 2:
            return 0.0
        return self.equity_curve[-1] / self.equity_curve[0] - 1.0

    @property
    def cagr(self) -> float:
        if len(self.equity_curve) < 2:
            return 0.0
        hours = len(self.equity_curve)
        years = hours / HOURS_PER_YEAR
        if years <= 0:
            return 0.0
        return (self.equity_curve[-1] / self.equity_curve[0]) ** (1 / years) - 1.0

    @property
    def sharpe(self) -> float:
        rets = self._hourly_returns()
        if len(rets) < 30:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        std = math.sqrt(var)
        if std <= 0:
            return 0.0
        rf_h = (1 + RISK_FREE_RATE) ** (1 / HOURS_PER_YEAR) - 1
        return (mean - rf_h) / std * math.sqrt(HOURS_PER_YEAR)

    @property
    def sortino(self) -> float:
        rets = self._hourly_returns()
        if len(rets) < 30:
            return 0.0
        mean = sum(rets) / len(rets)
        neg = [r for r in rets if r < 0]
        if not neg:
            return 0.0
        dstd = math.sqrt(sum((r) ** 2 for r in neg) / len(neg))
        if dstd <= 0:
            return 0.0
        rf_h = (1 + RISK_FREE_RATE) ** (1 / HOURS_PER_YEAR) - 1
        return (mean - rf_h) / dstd * math.sqrt(HOURS_PER_YEAR)

    @property
    def max_drawdown(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]
        max_dd = 0.0
        for eq in self.equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd
        return max_dd

    def _hourly_returns(self) -> List[float]:
        rets: List[float] = []
        for i in range(1, len(self.equity_curve)):
            prev = self.equity_curve[i - 1]
            if prev <= 0:
                continue
            rets.append(self.equity_curve[i] / prev - 1.0)
        return rets


# =====================================================================
# Strategy 1: SMA Crossover
# =====================================================================

def run_sma_crossover(
    symbol: str,
    klines: List[dict],
    fast: int = 50,
    slow: int = 200,
) -> StrategyResult:
    """Long when fast SMA > slow SMA; flat otherwise."""
    closes = [k["close"] for k in klines]
    fast_sma = sma(closes, fast)
    slow_sma = sma(closes, slow)

    equity = INITIAL_CAPITAL
    equity_curve = [equity]
    trades: List[Trade] = []
    position: Optional[Trade] = None

    cost_mult = 1.0 - TRANSACTION_COST_BPS / 10_000

    for i in range(1, len(klines)):
        prev_fast = fast_sma[i - 1]
        prev_slow = slow_sma[i - 1]
        cur_fast = fast_sma[i]
        cur_slow = slow_sma[i]
        price = klines[i]["close"]

        if prev_fast is None or prev_slow is None or cur_fast is None or cur_slow is None:
            equity_curve.append(equity)
            continue

        # Entry: cross above
        if position is None and prev_fast <= prev_slow and cur_fast > cur_slow:
            entry_price = price * (1 + TRANSACTION_COST_BPS / 10_000)
            position = Trade(
                entry_ts=klines[i]["ts"],
                entry_price=entry_price,
                side=1,
            )

        # Exit: cross below
        elif position is not None and prev_fast >= prev_slow and cur_fast < cur_slow:
            exit_price = price * cost_mult
            pnl = (exit_price / position.entry_price - 1.0) * equity * POSITION_SIZE_PCT
            position.exit_ts = klines[i]["ts"]
            position.exit_price = exit_price
            position.pnl_pct = exit_price / position.entry_price - 1.0
            equity += pnl
            trades.append(position)
            position = None

        # Mark-to-market
        if position is not None:
            mtm_pnl = (price / position.entry_price - 1.0) * equity * POSITION_SIZE_PCT
            equity_curve.append(equity + mtm_pnl)
        else:
            equity_curve.append(equity)

    return StrategyResult(
        name=f"SMA {fast}/{slow}",
        symbol=symbol,
        trades=trades,
        equity_curve=equity_curve,
    )


# =====================================================================
# Strategy 2: Time-Series Momentum
# =====================================================================

def run_time_series_momentum(
    symbol: str,
    klines: List[dict],
    lookback_hours: int = 24 * 30,   # ~1 month
    vol_window: int = 24 * 30,       # 30 days
) -> StrategyResult:
    """
    Time-series momentum:
        signal = sign(return over lookback)
        position size = (vol_target / realized_vol) * base_size
    """
    closes = [k["close"] for k in klines]
    equity = INITIAL_CAPITAL
    equity_curve = [equity]
    trades: List[Trade] = []
    position: Optional[Trade] = None

    hourly_rets = [0.0]
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            hourly_rets.append(closes[i] / closes[i - 1] - 1.0)
        else:
            hourly_rets.append(0.0)

    vol = rolling_std(hourly_rets, vol_window)
    vol_target_hourly = VOL_TARGET_ANNUAL / math.sqrt(HOURS_PER_YEAR)
    cost_mult = 1.0 - TRANSACTION_COST_BPS / 10_000

    for i in range(max(lookback_hours, vol_window), len(klines)):
        prev_close = closes[i - lookback_hours]
        cur_close = closes[i]
        if prev_close <= 0:
            equity_curve.append(equity)
            continue

        mom_ret = cur_close / prev_close - 1.0
        realized_vol = vol[i]
        if realized_vol is None or realized_vol <= 1e-10:
            equity_curve.append(equity)
            continue

        vol_scalar = min(2.0, max(0.1, vol_target_hourly / realized_vol))
        side = 1 if mom_ret > 0 else -1
        target_side = side
        target_size_pct = POSITION_SIZE_PCT * vol_scalar

        price = klines[i]["close"]

        # Flip logic: close old, open new
        if position is not None and position.side != target_side:
            exit_price = price * cost_mult
            pnl = (exit_price / position.entry_price - 1.0) * equity * POSITION_SIZE_PCT * position.side
            position.exit_ts = klines[i]["ts"]
            position.exit_price = exit_price
            position.pnl_pct = (exit_price / position.entry_price - 1.0) * position.side
            equity += pnl
            trades.append(position)
            position = None

        if position is None:
            entry_price = price * (1 + TRANSACTION_COST_BPS / 10_000)
            position = Trade(
                entry_ts=klines[i]["ts"],
                entry_price=entry_price,
                side=target_side,
            )

        # Mark-to-market
        if position is not None:
            mtm = (price / position.entry_price - 1.0) * equity * target_size_pct * position.side
            equity_curve.append(equity + mtm)
        else:
            equity_curve.append(equity)

    # Close final position
    if position is not None:
        price = klines[-1]["close"]
        exit_price = price * cost_mult
        pnl = (exit_price / position.entry_price - 1.0) * equity * POSITION_SIZE_PCT * position.side
        position.exit_ts = klines[-1]["ts"]
        position.exit_price = exit_price
        position.pnl_pct = (exit_price / position.entry_price - 1.0) * position.side
        equity += pnl
        trades.append(position)
        equity_curve[-1] = equity

    return StrategyResult(
        name=f"TS Momentum ({lookback_hours}h)",
        symbol=symbol,
        trades=trades,
        equity_curve=equity_curve,
    )


# =====================================================================
# Strategy 3: Donchian Breakout
# =====================================================================

def run_breakout(
    symbol: str,
    klines: List[dict],
    entry_period: int = 20 * 24,    # 20 days in hours
    exit_period: int = 10 * 24,     # 10 days in hours
) -> StrategyResult:
    """
    Classic turtle-style breakout:
        Entry long  when price > upper Donchian (entry_period)
        Exit long   when price < lower Donchian (exit_period)
    """
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    upper = donchian_high(highs, entry_period)
    lower = donchian_low(lows, exit_period)

    equity = INITIAL_CAPITAL
    equity_curve = [equity]
    trades: List[Trade] = []
    position: Optional[Trade] = None
    cost_mult = 1.0 - TRANSACTION_COST_BPS / 10_000

    for i in range(max(entry_period, exit_period), len(klines)):
        price = klines[i]["close"]
        prev_high = klines[i - 1]["high"]
        prev_low = klines[i - 1]["low"]
        cur_upper = upper[i - 1]
        cur_lower = lower[i - 1]

        if cur_upper is None or cur_lower is None:
            equity_curve.append(equity)
            continue

        # Entry: new high
        if position is None and prev_high > cur_upper:
            entry_price = price * (1 + TRANSACTION_COST_BPS / 10_000)
            position = Trade(
                entry_ts=klines[i]["ts"],
                entry_price=entry_price,
                side=1,
            )

        # Exit: new low
        elif position is not None and prev_low < cur_lower:
            exit_price = price * cost_mult
            pnl = (exit_price / position.entry_price - 1.0) * equity * POSITION_SIZE_PCT
            position.exit_ts = klines[i]["ts"]
            position.exit_price = exit_price
            position.pnl_pct = exit_price / position.entry_price - 1.0
            equity += pnl
            trades.append(position)
            position = None

        if position is not None:
            mtm = (price / position.entry_price - 1.0) * equity * POSITION_SIZE_PCT
            equity_curve.append(equity + mtm)
        else:
            equity_curve.append(equity)

    # Close final position
    if position is not None:
        price = klines[-1]["close"]
        exit_price = price * cost_mult
        pnl = (exit_price / position.entry_price - 1.0) * equity * POSITION_SIZE_PCT
        position.exit_ts = klines[-1]["ts"]
        position.exit_price = exit_price
        position.pnl_pct = exit_price / position.entry_price - 1.0
        equity += pnl
        trades.append(position)
        equity_curve[-1] = equity

    return StrategyResult(
        name=f"Breakout ({entry_period // 24}d/{exit_period // 24}d)",
        symbol=symbol,
        trades=trades,
        equity_curve=equity_curve,
    )


# =====================================================================
# Reporting
# =====================================================================

def print_results(results: List[StrategyResult]) -> None:
    """Print formatted table."""
    print()
    print(f"{'Strategy':<28} {'Symbol':<10} {'Trades':>7} "
          f"{'Win%':>6} {'CAGR%':>8} {'Sharpe':>8} {'Sortino':>8} "
          f"{'MaxDD%':>8} {'TotalR%':>9}")
    print("-" * 120)
    for r in results:
        print(
            f"{r.name:<28} {r.symbol:<10} {r.n_trades:>7} "
            f"{r.win_rate * 100:>6.1f} {r.cagr * 100:>8.2f} "
            f"{r.sharpe:>8.3f} {r.sortino:>8.3f} "
            f"{r.max_drawdown * 100:>8.2f} {r.total_return * 100:>9.2f}"
        )


def aggregate_by_strategy(results: List[StrategyResult]) -> Dict[str, List[StrategyResult]]:
    agg: Dict[str, List[StrategyResult]] = {}
    for r in results:
        agg.setdefault(r.name, []).append(r)
    return agg


def print_summary(agg: Dict[str, List[StrategyResult]]) -> None:
    print()
    print("=" * 120)
    print("SUMMARY BY STRATEGY (averages across 8 symbols)")
    print("=" * 120)
    print(f"{'Strategy':<30} {'Avg Trades':>11} {'Avg Win%':>10} "
          f"{'Avg CAGR%':>11} {'Avg Sharpe':>12} {'Avg MaxDD%':>12}")
    print("-" * 120)
    for name, group in agg.items():
        n = len(group)
        avg_trades = sum(r.n_trades for r in group) / n
        avg_wr = sum(r.win_rate for r in group) / n * 100
        avg_cagr = sum(r.cagr for r in group) / n * 100
        avg_sh = sum(r.sharpe for r in group) / n
        avg_dd = sum(r.max_drawdown for r in group) / n * 100
        print(
            f"{name:<30} {avg_trades:>11.1f} {avg_wr:>10.1f} "
            f"{avg_cagr:>11.2f} {avg_sh:>12.3f} {avg_dd:>12.2f}"
        )


# =====================================================================
# Main
# =====================================================================

def main() -> int:
    if not DB.exists():
        print(f"Database not found: {DB}", file=sys.stderr)
        print("Run klines_backfill.py first.", file=sys.stderr)
        return 1

    print(f"Loading klines from {DB}...")
    all_results: List[StrategyResult] = []

    for sym in SYMBOLS:
        klines = load_klines(sym)
        if not klines:
            print(f"  {sym}: no data, skipping", file=sys.stderr)
            continue
        print(f"  {sym}: {len(klines)} candles "
              f"({klines[0]['ts'] // 1000} → {klines[-1]['ts'] // 1000})")

        all_results.append(run_sma_crossover(sym, klines, fast=50, slow=200))
        all_results.append(run_time_series_momentum(sym, klines))
        all_results.append(run_breakout(sym, klines))

    print_results(all_results)
    print_summary(aggregate_by_strategy(all_results))

    print()
    print("Legend:")
    print("  Trades      = total completed round-trips")
    print("  Win%        = percentage of profitable trades")
    print("  CAGR%       = compound annual growth rate")
    print("  Sharpe      = annualized Sharpe ratio (Rf=4%)")
    print("  Sortino     = annualized Sortino ratio")
    print("  MaxDD%      = maximum drawdown")
    print("  TotalR%     = total return")
    print()
    print(f"Config: initial_capital={INITIAL_CAPITAL}, "
          f"position_size={POSITION_SIZE_PCT * 100}%, "
          f"vol_target={VOL_TARGET_ANNUAL * 100}%, "
          f"cost={TRANSACTION_COST_BPS} bps per trade")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
