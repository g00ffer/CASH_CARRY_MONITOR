from __future__ import annotations

from decimal import Decimal

from monitor.domain import (
    CarryInstrument,
    FundingSnapshot,
)
from monitor.exchanges import (
    ExchangeClient,
    ExchangeDataError,
)


async def fetch_funding_snapshot(
    *,
    client: ExchangeClient,
    instrument: CarryInstrument,
    cycle_id: str,
    use_predicted_funding: bool,
    default_funding_interval_hours: Decimal,
    now_ms: int | None = None,
) -> FundingSnapshot:
    """
    Fetch funding snapshot for instrument perpetual.

    This service delegates exchange-specific work to exchange client,
    but keeps domain-level checks and consistent interface.
    """

    if not instrument.enabled:
        raise ExchangeDataError(
            f"instrument {instrument.name} is disabled",
        )

    funding_snapshot = await client.fetch_funding_snapshot(
        cycle_id=cycle_id,
        symbol_name=instrument.name,
        perp_symbol=instrument.perp_symbol,
        use_predicted_funding=use_predicted_funding,
        default_funding_interval_hours=default_funding_interval_hours,
    )

    if funding_snapshot.symbol_name != instrument.name:
        raise ExchangeDataError(
            f"unexpected funding symbol_name returned for {instrument.name}: "
            f"expected {instrument.name}, got {funding_snapshot.symbol_name}",
        )

    return funding_snapshot
