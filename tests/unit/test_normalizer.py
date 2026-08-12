"""
Tests for monitor.exchanges.normalizer
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from monitor.exchanges.normalizer import (
    normalize_funding_snapshot,
    normalize_perp_ticker,
    normalize_spot_ticker,
    parse_funding_interval_hours,
    parse_interval_hours,
    parse_optional_decimal,
    parse_required_positive_decimal,
    parse_timestamp_from_payload,
    parse_timestamp_value,
)
from monitor.exchanges.base import ExchangeDataError
from monitor.domain.enums import FundingRateSource


# ======================================================================
# parse_optional_decimal
# ======================================================================

class TestParseOptionalDecimal:
    def test_none_returns_none(self):
        assert parse_optional_decimal(None) is None

    def test_string_value(self):
        result = parse_optional_decimal("65000.5")
        assert result == Decimal("65000.5")

    def test_int_value(self):
        result = parse_optional_decimal(65000)
        assert result == Decimal("65000")

    def test_float_value(self):
        result = parse_optional_decimal(65000.5)
        assert result == Decimal("65000.5")

    def test_scientific_notation_string(self):
        result = parse_optional_decimal("6.127e-05")
        assert result == Decimal("0.00006127")

    def test_invalid_string_returns_none(self):
        assert parse_optional_decimal("not_a_number") is None

    def test_empty_string_returns_none(self):
        assert parse_optional_decimal("") is None

    def test_infinity_returns_none(self):
        assert parse_optional_decimal(float("inf")) is None

    def test_nan_returns_none(self):
        assert parse_optional_decimal(float("nan")) is None

    def test_negative_value(self):
        result = parse_optional_decimal("-0.0001")
        assert result == Decimal("-0.0001")

    def test_zero_value(self):
        result = parse_optional_decimal("0")
        assert result == Decimal("0")


# ======================================================================
# parse_required_positive_decimal
# ======================================================================

class TestParseRequiredPositiveDecimal:
    def test_valid_positive(self):
        result = parse_required_positive_decimal("65000.5", "price")
        assert result == Decimal("65000.5")

    def test_none_raises(self):
        with pytest.raises(ExchangeDataError, match="price must be positive"):
            parse_required_positive_decimal(None, "price")

    def test_zero_raises(self):
        with pytest.raises(ExchangeDataError, match="price must be positive"):
            parse_required_positive_decimal("0", "price")

    def test_negative_raises(self):
        with pytest.raises(ExchangeDataError, match="price must be positive"):
            parse_required_positive_decimal("-100", "price")

    def test_invalid_string_raises(self):
        with pytest.raises(ExchangeDataError, match="price must be positive"):
            parse_required_positive_decimal("abc", "price")


# ======================================================================
# parse_timestamp_value
# ======================================================================

class TestParseTimestampValue:
    def test_milliseconds_passthrough(self):
        result = parse_timestamp_value(1710000000000)
        assert result == 1710000000000

    def test_seconds_converted_to_ms(self):
        # Value < 10_000_000_000 is treated as seconds
        result = parse_timestamp_value(1710000000)
        assert result == 1710000000000

    def test_none_returns_none(self):
        assert parse_timestamp_value(None) is None

    def test_bool_returns_none(self):
        assert parse_timestamp_value(True) is None

    def test_zero_returns_none(self):
        assert parse_timestamp_value(0) is None

    def test_negative_returns_none(self):
        assert parse_timestamp_value(-100) is None

    def test_iso_string_with_z(self):
        result = parse_timestamp_value("2024-03-09T12:00:00.000Z")
        assert result is not None
        assert result == 1710000000000

    def test_iso_string_with_offset(self):
        result = parse_timestamp_value("2024-03-09T12:00:00.000+00:00")
        assert result is not None
        assert result == 1710000000000

    def test_numeric_string(self):
        result = parse_timestamp_value("1710000000000")
        assert result == 1710000000000

    def test_empty_string_returns_none(self):
        assert parse_timestamp_value("") is None

    def test_float_value(self):
        result = parse_timestamp_value(1710000000000.0)
        assert result == 1710000000000


# ======================================================================
# parse_timestamp_from_payload
# ======================================================================

class TestParseTimestampFromPayload:
    def test_from_timestamp_field(self):
        payload = {"timestamp": 1710000000000}
        result = parse_timestamp_from_payload(payload, received_at_ms=999)
        assert result == 1710000000000

    def test_from_datetime_field(self):
        payload = {"datetime": "2024-03-09T12:00:00.000Z"}
        result = parse_timestamp_from_payload(payload, received_at_ms=999)
        assert result == 1710000000000

    def test_from_info_closeTime(self):
        payload = {"info": {"closeTime": 1710000000000}}
        result = parse_timestamp_from_payload(payload, received_at_ms=999)
        assert result == 1710000000000

    def test_from_info_E(self):
        payload = {"info": {"E": 1710000000000}}
        result = parse_timestamp_from_payload(payload, received_at_ms=999)
        assert result == 1710000000000

    def test_fallback_to_received_at(self):
        payload = {}
        result = parse_timestamp_from_payload(payload, received_at_ms=999)
        assert result == 999

    def test_timestamp_priority_over_info(self):
        payload = {
            "timestamp": 1710000000000,
            "info": {"closeTime": 1710000001000},
        }
        result = parse_timestamp_from_payload(payload, received_at_ms=999)
        assert result == 1710000000000


# ======================================================================
# parse_interval_hours
# ======================================================================

class TestParseIntervalHours:
    def test_integer(self):
        assert parse_interval_hours(8) == Decimal("8")

    def test_string_number(self):
        assert parse_interval_hours("8") == Decimal("8")

    def test_hours_suffix(self):
        assert parse_interval_hours("8h") == Decimal("8")

    def test_minutes_suffix(self):
        assert parse_interval_hours("480m") == Decimal("8")

    def test_seconds_suffix(self):
        assert parse_interval_hours("28800s") == Decimal("8")

    def test_none_returns_none(self):
        assert parse_interval_hours(None) is None

    def test_empty_string_returns_none(self):
        assert parse_interval_hours("") is None

    def test_zero_returns_none(self):
        assert parse_interval_hours("0") is None

    def test_negative_returns_none(self):
        assert parse_interval_hours("-8h") is None

    def test_bool_returns_none(self):
        assert parse_interval_hours(True) is None

    def test_fractional_hours(self):
        assert parse_interval_hours("0.5") == Decimal("0.5")


# ======================================================================
# parse_funding_interval_hours
# ======================================================================

class TestParseFundingIntervalHours:
    def test_from_payload_top_level(self):
        payload = {"fundingIntervalHours": 8}
        result = parse_funding_interval_hours(payload)
        assert result == Decimal("8")

    def test_from_info_intervalHours(self):
        payload = {"info": {"intervalHours": "4"}}
        result = parse_funding_interval_hours(payload)
        assert result == Decimal("4")

    def test_from_info_interval(self):
        payload = {"info": {"interval": "8h"}}
        result = parse_funding_interval_hours(payload)
        assert result == Decimal("8")

    def test_missing_returns_none(self):
        payload = {"info": {}}
        result = parse_funding_interval_hours(payload)
        assert result is None

    def test_empty_payload_returns_none(self):
        result = parse_funding_interval_hours({})
        assert result is None


# ======================================================================
# normalize_spot_ticker
# ======================================================================

class TestNormalizeSpotTicker:
    def test_from_fixture(self, raw_spot_ticker):
        result = normalize_spot_ticker(
            symbol="BTC/USDT",
            raw=raw_spot_ticker,
            received_at_ms=1710000000500,
        )
        assert result.symbol == "BTC/USDT"
        assert result.bid == Decimal("65000")
        assert result.ask == Decimal("65001")
        assert result.timestamp_ms == 1710000000000
        assert result.last == Decimal("65000.5")
        assert result.base_volume_24h == Decimal("12345.67")
        assert result.quote_volume_24h == Decimal("803702067")
        assert result.bid_quantity == Decimal("1.5")
        assert result.ask_quantity == Decimal("0.8")

    def test_mid_price_calculated(self, raw_spot_ticker):
        result = normalize_spot_ticker(
            symbol="BTC/USDT",
            raw=raw_spot_ticker,
            received_at_ms=1710000000500,
        )
        expected_mid = (Decimal("65000") + Decimal("65001")) / Decimal("2")
        assert result.mid_price == expected_mid

    def test_bid_greater_than_ask_raises(self):
        raw = {"bid": "102", "ask": "100", "timestamp": 1710000000000}
        with pytest.raises(ExchangeDataError, match="spot bid > ask"):
            normalize_spot_ticker(
                symbol="BTC/USDT",
                raw=raw,
                received_at_ms=1710000000500,
            )

    def test_missing_bid_raises(self):
        raw = {"ask": "100", "timestamp": 1710000000000}
        with pytest.raises(ExchangeDataError, match="spot bid must be positive"):
            normalize_spot_ticker(
                symbol="BTC/USDT",
                raw=raw,
                received_at_ms=1710000000500,
            )

    def test_missing_ask_raises(self):
        raw = {"bid": "100", "timestamp": 1710000000000}
        with pytest.raises(ExchangeDataError, match="spot ask must be positive"):
            normalize_spot_ticker(
                symbol="BTC/USDT",
                raw=raw,
                received_at_ms=1710000000500,
            )

    def test_zero_bid_raises(self):
        raw = {"bid": "0", "ask": "100", "timestamp": 1710000000000}
        with pytest.raises(ExchangeDataError, match="spot bid must be positive"):
            normalize_spot_ticker(
                symbol="BTC/USDT",
                raw=raw,
                received_at_ms=1710000000500,
            )

    def test_negative_ask_raises(self):
        raw = {"bid": "100", "ask": "-5", "timestamp": 1710000000000}
        with pytest.raises(ExchangeDataError, match="spot ask must be positive"):
            normalize_spot_ticker(
                symbol="BTC/USDT",
                raw=raw,
                received_at_ms=1710000000500,
            )

    def test_missing_timestamp_uses_received_at(self):
        raw = {"bid": "100", "ask": "101"}
        result = normalize_spot_ticker(
            symbol="BTC/USDT",
            raw=raw,
            received_at_ms=1710000000500,
        )
        assert result.timestamp_ms == 1710000000500


# ======================================================================
# normalize_perp_ticker
# ======================================================================

class TestNormalizePerpTicker:
    def test_from_fixture(self, raw_perp_ticker):
        result = normalize_perp_ticker(
            symbol="BTC/USDT:USDT",
            raw=raw_perp_ticker,
            received_at_ms=1710000000500,
        )
        assert result.symbol == "BTC/USDT:USDT"
        assert result.bid == Decimal("65030")
        assert result.ask == Decimal("65032")
        assert result.timestamp_ms == 1710000000200
        assert result.last == Decimal("65031")

    def test_bid_greater_than_ask_raises(self):
        raw = {"bid": "65035", "ask": "65030", "timestamp": 1710000000200}
        with pytest.raises(ExchangeDataError, match="perp bid > ask"):
            normalize_perp_ticker(
                symbol="BTC/USDT:USDT",
                raw=raw,
                received_at_ms=1710000000500,
            )

    def test_missing_bid_raises(self):
        raw = {"ask": "65030", "timestamp": 1710000000200}
        with pytest.raises(ExchangeDataError, match="perp bid must be positive"):
            normalize_perp_ticker(
                symbol="BTC/USDT:USDT",
                raw=raw,
                received_at_ms=1710000000500,
            )

    def test_mark_price_parsed(self):
        raw = {
            "bid": "65030",
            "ask": "65032",
            "timestamp": 1710000000200,
            "info": {"markPrice": "65031.5"},
        }
        result = normalize_perp_ticker(
            symbol="BTC/USDT:USDT",
            raw=raw,
            received_at_ms=1710000000500,
        )
        assert result.mark_price == Decimal("65031.5")

    def test_index_price_parsed(self):
        raw = {
            "bid": "65030",
            "ask": "65032",
            "timestamp": 1710000000200,
            "info": {"indexPrice": "65025.0"},
        }
        result = normalize_perp_ticker(
            symbol="BTC/USDT:USDT",
            raw=raw,
            received_at_ms=1710000000500,
        )
        assert result.index_price == Decimal("65025.0")


# ======================================================================
# normalize_funding_snapshot
# ======================================================================

class TestNormalizeFundingSnapshot:
    def test_predicted_funding_used(self, raw_funding):
        # Add fundingRate for predicted mode
        raw_funding["fundingRate"] = "0.00010000"
        raw_funding["info"]["fundingIntervalHours"] = 8
        result = normalize_funding_snapshot(
            cycle_id="test_cycle",
            symbol_name="BTC_CARRY",
            perp_symbol="BTC/USDT:USDT",
            raw_funding=raw_funding,
            received_at_ms=1710000000500,
            default_funding_interval_hours=Decimal("8"),
            use_predicted_funding=True,
        )
        assert result.cycle_id == "test_cycle"
        assert result.symbol_name == "BTC_CARRY"
        assert result.effective_funding_rate == Decimal("0.0001")
        assert result.funding_rate_source == FundingRateSource.PREDICTED
        assert result.funding_interval_hours == Decimal("8")
        assert result.last_funding_rate == Decimal("0.00008")
        assert result.predicted_funding_rate == Decimal("0.0001")

    def test_fallback_to_last_funding(self):
        raw = {
            "lastFundingRate": "0.00005",
            "timestamp": 1710000000200,
            "info": {},
        }
        result = normalize_funding_snapshot(
            cycle_id="test_cycle",
            symbol_name="BTC_CARRY",
            perp_symbol="BTC/USDT:USDT",
            raw_funding=raw,
            received_at_ms=1710000000500,
            default_funding_interval_hours=Decimal("8"),
            use_predicted_funding=True,
        )
        assert result.effective_funding_rate == Decimal("0.00005")
        assert result.funding_rate_source == FundingRateSource.LAST
        assert result.funding_interval_hours == Decimal("8")

    def test_missing_funding_rate_raises(self):
        raw = {"timestamp": 1710000000200, "info": {}}
        with pytest.raises(ExchangeDataError, match="funding rate is missing"):
            normalize_funding_snapshot(
                cycle_id="test_cycle",
                symbol_name="BTC_CARRY",
                perp_symbol="BTC/USDT:USDT",
                raw_funding=raw,
                received_at_ms=1710000000500,
                default_funding_interval_hours=Decimal("8"),
                use_predicted_funding=False,
            )

    def test_default_interval_used(self):
        raw = {
            "lastFundingRate": "0.0001",
            "timestamp": 1710000000200,
            "info": {},
        }
        result = normalize_funding_snapshot(
            cycle_id="test_cycle",
            symbol_name="BTC_CARRY",
            perp_symbol="BTC/USDT:USDT",
            raw_funding=raw,
            received_at_ms=1710000000500,
            default_funding_interval_hours=Decimal("4"),
            use_predicted_funding=False,
        )
        assert result.funding_interval_hours == Decimal("4")

    def test_next_funding_timestamp_parsed(self, raw_funding):
        raw_funding["lastFundingRate"] = "0.0001"
        result = normalize_funding_snapshot(
            cycle_id="test_cycle",
            symbol_name="BTC_CARRY",
            perp_symbol="BTC/USDT:USDT",
            raw_funding=raw_funding,
            received_at_ms=1710000000500,
            default_funding_interval_hours=Decimal("8"),
            use_predicted_funding=False,
        )
        # From fixture: info.nextFundingTime = 1710028800000
        assert result.next_funding_timestamp_ms == 1710028800000

    def test_negative_funding_rate_allowed(self):
        """Negative funding is valid data (backwardation), just blocks signal."""
        raw = {
            "lastFundingRate": "-0.0001",
            "timestamp": 1710000000200,
            "info": {},
        }
        result = normalize_funding_snapshot(
            cycle_id="test_cycle",
            symbol_name="BTC_CARRY",
            perp_symbol="BTC/USDT:USDT",
            raw_funding=raw,
            received_at_ms=1710000000500,
            default_funding_interval_hours=Decimal("8"),
            use_predicted_funding=False,
        )
        assert result.effective_funding_rate == Decimal("-0.0001")
        assert result.funding_rate_source == FundingRateSource.LAST

    def test_interval_from_payload_overrides_default(self):
        raw = {
            "lastFundingRate": "0.0001",
            "timestamp": 1710000000200,
            "fundingIntervalHours": 4,
            "info": {},
        }
        result = normalize_funding_snapshot(
            cycle_id="test_cycle",
            symbol_name="BTC_CARRY",
            perp_symbol="BTC/USDT:USDT",
            raw_funding=raw,
            received_at_ms=1710000000500,
            default_funding_interval_hours=Decimal("8"),
            use_predicted_funding=False,
        )
        assert result.funding_interval_hours == Decimal("4")