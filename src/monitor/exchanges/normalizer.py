from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Mapping

from monitor.domain import (
    FundingRateSource,
    FundingSnapshot,
    PerpTicker,
    SpotTicker,
)
from monitor.utils import (
    ZERO,
    datetime_to_ms,
    to_decimal,
)

from .base import ExchangeDataError

# ---------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value

    return {}


def _get_value(
    payload: Mapping[str, Any],
    info: Mapping[str, Any],
    keys: tuple[str, ...],
) -> Any:
    """
    Return first non-None value from payload or info.
    """

    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]

    for key in keys:
        if key in info and info[key] is not None:
            return info[key]

    return None


# ---------------------------------------------------------------------
# Decimal parsing
# ---------------------------------------------------------------------


def parse_optional_decimal(value: Any) -> Decimal | None:
    """
    Parse optional Decimal value.

    Returns None if value is missing or invalid.
    """

    if value is None:
        return None

    try:
        decimal_value = to_decimal(value)
    except (TypeError, ValueError):
        return None

    if not decimal_value.is_finite():
        return None

    return decimal_value


def parse_required_positive_decimal(
    value: Any,
    field_name: str,
) -> Decimal:
    """
    Parse required positive Decimal value.
    """

    decimal_value = parse_optional_decimal(value)

    if decimal_value is None or decimal_value <= ZERO:
        raise ExchangeDataError(f"{field_name} must be positive")

    return decimal_value


# ---------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------


def parse_timestamp_value(value: Any) -> int | None:
    """
    Parse timestamp from int/float/string.

    Returns UTC milliseconds.
    """

    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        try:
            timestamp = int(value)
        except (OverflowError, ValueError):
            return None

        if timestamp <= 0:
            return None

        # Heuristic:
        # If value is too small, it is probably seconds, not milliseconds.
        if timestamp < 10_000_000_000:
            timestamp *= 1000

        return timestamp

    if isinstance(value, str):
        text = value.strip()

        if not text:
            return None

        # Numeric string
        try:
            numeric_value = float(text)
            return parse_timestamp_value(numeric_value)
        except ValueError:
            pass

        # ISO datetime string
        try:
            if text.endswith("Z"):
                text = text.replace("Z", "+00:00")

            parsed_datetime = dt.datetime.fromisoformat(text)
            return datetime_to_ms(parsed_datetime)
        except ValueError:
            return None

    return None


def parse_timestamp_from_payload(
    payload: Mapping[str, Any],
    received_at_ms: int,
) -> int:
    """
    Parse timestamp from ticker payload.

    Falls back to local received time if exchange timestamp is missing.
    """

    info = _as_mapping(payload.get("info"))

    candidate = _get_value(
        payload,
        info,
        (
            "timestamp",
            "datetime",
            "timestamp_ms",
            "time",
        ),
    )

    parsed = parse_timestamp_value(candidate)

    if parsed is not None:
        return parsed

    # Binance raw payload often uses closeTime / E / T.
    for key in ("closeTime", "E", "T"):
        parsed = parse_timestamp_value(info.get(key))

        if parsed is not None:
            return parsed

    return received_at_ms


# ---------------------------------------------------------------------
# Interval parsing
# ---------------------------------------------------------------------


def parse_interval_hours(value: Any) -> Decimal | None:
    """
    Parse funding interval.

    Supported examples:
        8
        "8"
        "8h"
        "480m"
        "28800s"
    """

    if value is None:
        return None

    if isinstance(value, bool):
        return None

    text = str(value).strip().lower()

    if not text:
        return None

    if text.endswith("h"):
        number = parse_optional_decimal(text[:-1])

        if number is None or number <= ZERO:
            return None

        return number

    if text.endswith("m"):
        number = parse_optional_decimal(text[:-1])

        if number is None or number <= ZERO:
            return None

        return number / Decimal("60")

    if text.endswith("s"):
        number = parse_optional_decimal(text[:-1])

        if number is None or number <= ZERO:
            return None

        return number / Decimal("3600")

    number = parse_optional_decimal(text)

    if number is None or number <= ZERO:
        return None

    return number


def parse_funding_interval_hours(
    raw_funding: Mapping[str, Any],
) -> Decimal | None:
    """
    Try to parse funding interval from raw funding payload.
    """

    payload = _as_mapping(raw_funding)
    info = _as_mapping(payload.get("info"))

    value = _get_value(
        payload,
        info,
        (
            "fundingIntervalHours",
            "intervalHours",
            "interval",
            "fundingInterval",
        ),
    )

    return parse_interval_hours(value)


# ---------------------------------------------------------------------
# Spot ticker normalization
# ---------------------------------------------------------------------


def normalize_spot_ticker(
    *,
    symbol: str,
    raw: Mapping[str, Any],
    received_at_ms: int,
) -> SpotTicker:
    """
    Normalize ccxt spot ticker into SpotTicker domain model.
    """

    payload = _as_mapping(raw)
    info = _as_mapping(payload.get("info"))

    bid = parse_required_positive_decimal(
        payload.get("bid"),
        "spot bid",
    )
    ask = parse_required_positive_decimal(
        payload.get("ask"),
        "spot ask",
    )

    if bid > ask:
        raise ExchangeDataError("spot bid > ask")

    timestamp_ms = parse_timestamp_from_payload(
        payload,
        received_at_ms,
    )

    last = parse_optional_decimal(
        _get_value(payload, info, ("last",)),
    )

    quote_volume_24h = parse_optional_decimal(
        _get_value(payload, info, ("quoteVolume", "quoteVolume24h")),
    )

    base_volume_24h = parse_optional_decimal(
        _get_value(payload, info, ("baseVolume", "baseVolume24h")),
    )

    bid_quantity = parse_optional_decimal(
        _get_value(
            payload,
            info,
            (
                "bidVolume",
                "bidQty",
                "bidQuantity",
            ),
        ),
    )

    ask_quantity = parse_optional_decimal(
        _get_value(
            payload,
            info,
            (
                "askVolume",
                "askQty",
                "askQuantity",
            ),
        ),
    )

    return SpotTicker(
        symbol=symbol,
        bid=bid,
        ask=ask,
        timestamp_ms=timestamp_ms,
        last=last,
        quote_volume_24h=quote_volume_24h,
        base_volume_24h=base_volume_24h,
        bid_quantity=bid_quantity,
        ask_quantity=ask_quantity,
        raw=payload,
    )


# ---------------------------------------------------------------------
# Perpetual ticker normalization
# ---------------------------------------------------------------------


def normalize_perp_ticker(
    *,
    symbol: str,
    raw: Mapping[str, Any],
    received_at_ms: int,
) -> PerpTicker:
    """
    Normalize ccxt perpetual ticker into PerpTicker domain model.
    """

    payload = _as_mapping(raw)
    info = _as_mapping(payload.get("info"))

    bid = parse_required_positive_decimal(
        payload.get("bid"),
        "perp bid",
    )
    ask = parse_required_positive_decimal(
        payload.get("ask"),
        "perp ask",
    )

    if bid > ask:
        raise ExchangeDataError("perp bid > ask")

    timestamp_ms = parse_timestamp_from_payload(
        payload,
        received_at_ms,
    )

    last = parse_optional_decimal(
        _get_value(payload, info, ("last",)),
    )

    mark_price = parse_optional_decimal(
        _get_value(
            payload,
            info,
            (
                "mark",
                "markPrice",
            ),
        ),
    )

    index_price = parse_optional_decimal(
        _get_value(
            payload,
            info,
            (
                "index",
                "indexPrice",
            ),
        ),
    )

    quote_volume_24h = parse_optional_decimal(
        _get_value(payload, info, ("quoteVolume", "quoteVolume24h")),
    )

    base_volume_24h = parse_optional_decimal(
        _get_value(payload, info, ("baseVolume", "baseVolume24h")),
    )

    open_interest = parse_optional_decimal(
        _get_value(
            payload,
            info,
            (
                "openInterest",
                "openInterestValue",
            ),
        ),
    )

    bid_quantity = parse_optional_decimal(
        _get_value(
            payload,
            info,
            (
                "bidVolume",
                "bidQty",
                "bidQuantity",
            ),
        ),
    )

    ask_quantity = parse_optional_decimal(
        _get_value(
            payload,
            info,
            (
                "askVolume",
                "askQty",
                "askQuantity",
            ),
        ),
    )

    return PerpTicker(
        symbol=symbol,
        bid=bid,
        ask=ask,
        timestamp_ms=timestamp_ms,
        last=last,
        mark_price=mark_price,
        index_price=index_price,
        quote_volume_24h=quote_volume_24h,
        base_volume_24h=base_volume_24h,
        open_interest=open_interest,
        bid_quantity=bid_quantity,
        ask_quantity=ask_quantity,
        raw=payload,
    )


# ---------------------------------------------------------------------
# Funding normalization
# ---------------------------------------------------------------------


def normalize_funding_snapshot(
    *,
    cycle_id: str,
    symbol_name: str,
    perp_symbol: str,
    raw_funding: Mapping[str, Any],
    received_at_ms: int,
    default_funding_interval_hours: Decimal,
    use_predicted_funding: bool,
) -> FundingSnapshot:
    """
    Normalize ccxt funding response into FundingSnapshot domain model.
    """

    payload = _as_mapping(raw_funding)
    info = _as_mapping(payload.get("info"))

    predicted_funding_rate = parse_optional_decimal(
        _get_value(
            payload,
            info,
            (
                "fundingRate",
                "predictedFundingRate",
                "nextFundingRate",
            ),
        ),
    )

    last_funding_rate = parse_optional_decimal(
        _get_value(
            payload,
            info,
            (
                "lastFundingRate",
                "previousFundingRate",
            ),
        ),
    )

    if use_predicted_funding and predicted_funding_rate is not None:
        effective_funding_rate = predicted_funding_rate
        funding_rate_source = FundingRateSource.PREDICTED
    elif last_funding_rate is not None:
        effective_funding_rate = last_funding_rate
        funding_rate_source = FundingRateSource.LAST
    elif predicted_funding_rate is not None:
        effective_funding_rate = predicted_funding_rate
        funding_rate_source = FundingRateSource.PREDICTED
    else:
        raise ExchangeDataError("funding rate is missing")

    funding_interval_hours = parse_funding_interval_hours(payload)

    if funding_interval_hours is None or funding_interval_hours <= ZERO:
        funding_interval_hours = parse_optional_decimal(
            default_funding_interval_hours,
        )

    if funding_interval_hours is None or funding_interval_hours <= ZERO:
        raise ExchangeDataError("funding interval is unknown")

    next_funding_timestamp_ms = parse_timestamp_value(
        _get_value(
            payload,
            info,
            (
                "nextFundingTimestamp",
                "nextFundingTime",
                "nextFundingTimestampMs",
                # ccxt иногда мапит nextFundingTime сюда
                "fundingTimestamp",
            ),
        ),
    )

    last_funding_timestamp_ms = parse_timestamp_value(
        _get_value(
            payload,
            info,
            (
                # ccxt unified
                "previousFundingTimestamp",
                "previousFundingTime",
                "lastFundingTimestamp",
                "lastFundingTime",
                "lastFundingTimestampMs",
                # Binance raw
                "prevFundingTime",
                "prevFundingTimestamp",
            ),
        ),
    )

    return FundingSnapshot(
        cycle_id=cycle_id,
        symbol_name=symbol_name,
        effective_funding_rate=effective_funding_rate,
        funding_rate_source=funding_rate_source,
        funding_interval_hours=funding_interval_hours,
        received_at_ms=received_at_ms,
        last_funding_rate=last_funding_rate,
        predicted_funding_rate=predicted_funding_rate,
        last_funding_timestamp_ms=last_funding_timestamp_ms,
        next_funding_timestamp_ms=next_funding_timestamp_ms,
        raw=payload,
    )
