from __future__ import annotations

from monitor.domain import (
    CarryInstrument,
    MarketSnapshot,
)
from monitor.exchanges import (
    ExchangeClient,
    ExchangeDataError,
    ExchangeRequestError,
)
from monitor.utils import (
    gather_dict,
    utc_now_ms,
)


async def fetch_market_snapshot(
    *,
    client: ExchangeClient,
    instrument: CarryInstrument,
    cycle_id: str,
    now_ms: int | None = None,
) -> MarketSnapshot:
    """
    Fetch spot and perp tickers in parallel and build MarketSnapshot.

    This service does not calculate yield metrics.
    It only fetches and assembles market data.
    """

    if not instrument.enabled:
        raise ExchangeDataError(
            f"instrument {instrument.name} is disabled",
        )

    requests = {
        "spot": client.fetch_spot_ticker(instrument.spot_symbol),
        "perp": client.fetch_perp_ticker(instrument.perp_symbol),
    }

    results = await gather_dict(requests)

    errors: list[str] = []

    for request_name, result in results.items():
        if isinstance(result, BaseException):
            errors.append(f"{request_name}: {result}")

    if errors:
        raise ExchangeRequestError(
            f"failed to fetch market snapshot for {instrument.name}: "
            + "; ".join(errors),
        )

    spot = results["spot"]
    perp = results["perp"]

    if isinstance(spot, BaseException):
        raise ExchangeRequestError(
            f"failed to fetch spot ticker for {instrument.name}: {spot}",
        )

    if isinstance(perp, BaseException):
        raise ExchangeRequestError(
            f"failed to fetch perp ticker for {instrument.name}: {perp}",
        )

    if spot.symbol != instrument.spot_symbol:
        raise ExchangeDataError(
            f"unexpected spot symbol returned for {instrument.name}: "
            f"expected {instrument.spot_symbol}, got {spot.symbol}",
        )

    if perp.symbol != instrument.perp_symbol:
        raise ExchangeDataError(
            f"unexpected perp symbol returned for {instrument.name}: "
            f"expected {instrument.perp_symbol}, got {perp.symbol}",
        )

    received_at_ms = utc_now_ms()

    return MarketSnapshot(
        cycle_id=cycle_id,
        symbol_name=instrument.name,
        spot=spot,
        perp=perp,
        received_at_ms=received_at_ms,
    )
