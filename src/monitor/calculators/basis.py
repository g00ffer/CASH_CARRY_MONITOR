from __future__ import annotations

from decimal import Decimal

from monitor.domain import BasisMetrics, MarketSnapshot
from monitor.domain.enums import ExpectedExitBasisMode
from monitor.utils import (
    ONE,
    ZERO,
    to_decimal,
    utc_now_ms,
)


def calc_mid_price(
    bid: Decimal,
    ask: Decimal,
) -> Decimal:
    """
    Calculate mid price from bid and ask.

    If bid or ask is non-positive, return zero.
    Such cases should normally be blocked earlier by data quality checks.
    """

    bid_decimal = to_decimal(bid)
    ask_decimal = to_decimal(ask)

    if bid_decimal <= ZERO or ask_decimal <= ZERO:
        return ZERO

    return (bid_decimal + ask_decimal) / Decimal("2")


def calc_spread_ratio(
    bid: Decimal,
    ask: Decimal,
) -> Decimal:
    """
    Calculate spread as decimal fraction:

        spread = ask / bid - 1

    Example:
    bid = 100
    ask = 100.10

    spread = 0.001 = 0.1%
    """

    bid_decimal = to_decimal(bid)
    ask_decimal = to_decimal(ask)

    if bid_decimal <= ZERO or ask_decimal <= ZERO:
        return ZERO

    return ask_decimal / bid_decimal - ONE


def calc_basis_mid(
    spot_mid: Decimal,
    perp_mid: Decimal,
) -> Decimal:
    """
    Mid basis:

        basis_mid = P_mid / S_mid - 1

    Used for observation/analytics.
    """

    spot_mid_decimal = to_decimal(spot_mid)
    perp_mid_decimal = to_decimal(perp_mid)

    if spot_mid_decimal <= ZERO:
        return ZERO

    return perp_mid_decimal / spot_mid_decimal - ONE


def calc_basis_entry(
    spot_ask: Decimal,
    perp_bid: Decimal,
) -> Decimal:
    """
    Executable entry basis for long spot / short perpetual.

    Strategy entry:
    - buy spot at ask
    - sell perpetual at bid

        basis_entry = P_bid / S_ask - 1
    """

    spot_ask_decimal = to_decimal(spot_ask)
    perp_bid_decimal = to_decimal(perp_bid)

    if spot_ask_decimal <= ZERO or perp_bid_decimal <= ZERO:
        return ZERO

    return perp_bid_decimal / spot_ask_decimal - ONE


def select_expected_exit_basis(
    mode: ExpectedExitBasisMode,
    basis_entry: Decimal,
    historical_median_basis: Decimal | None = None,
) -> Decimal:
    """
    Select expected exit basis assumption.

    Modes:
    - ENTRY:
        expected_exit_basis = basis_entry
        basis convergence PnL = 0
        conservative

    - ZERO:
        expected_exit_basis = 0
        assumes full convergence
        aggressive for perpetual

    - HISTORICAL_MEDIAN:
        expected_exit_basis = historical median basis
        if historical median is not provided, fallback to entry basis
    """

    basis_entry_decimal = to_decimal(basis_entry)

    if mode == ExpectedExitBasisMode.ENTRY:
        return basis_entry_decimal

    if mode == ExpectedExitBasisMode.ZERO:
        return ZERO

    if mode == ExpectedExitBasisMode.HISTORICAL_MEDIAN:
        if historical_median_basis is None:
            return basis_entry_decimal

        return to_decimal(historical_median_basis)

    raise ValueError(f"unsupported expected exit basis mode: {mode}")


def calc_basis_metrics(
    market_snapshot: MarketSnapshot,
    calculated_at_ms: int | None = None,
) -> BasisMetrics:
    """
    Calculate basis metrics from market snapshot.
    """

    calculated_at = (
        utc_now_ms()
        if calculated_at_ms is None
        else int(calculated_at_ms)
    )

    spot_mid = market_snapshot.spot.mid_price
    perp_mid = market_snapshot.perp.mid_price

    return BasisMetrics(
        basis_mid=calc_basis_mid(
            spot_mid=spot_mid,
            perp_mid=perp_mid,
        ),
        basis_entry=calc_basis_entry(
            spot_ask=market_snapshot.spot.ask,
            perp_bid=market_snapshot.perp.bid,
        ),
        spot_mid=spot_mid,
        perp_mid=perp_mid,
        spot_spread=calc_spread_ratio(
            bid=market_snapshot.spot.bid,
            ask=market_snapshot.spot.ask,
        ),
        perp_spread=calc_spread_ratio(
            bid=market_snapshot.perp.bid,
            ask=market_snapshot.perp.ask,
        ),
        calculated_at_ms=calculated_at,
    )
