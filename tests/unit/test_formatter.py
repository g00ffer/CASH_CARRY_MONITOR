"""Tests for monitor.notifications.formatter"""
from __future__ import annotations

from decimal import Decimal

import pytest

from monitor.notifications.formatter import (
    _fmt_decimal,
    _fmt_pct,
    _fmt_price,
    _fmt_timestamp,
    format_error_message,
    format_heartbeat_message,
    format_signal_message,
    format_warning_message,
)
from monitor.domain import SignalState

NOW_MS = 1710000001000


class TestFmtPct:
    def test_normal(self):
        assert _fmt_pct(Decimal("0.1095")) == "10.95%"

    def test_small_value(self):
        result = _fmt_pct(Decimal("0.0001"), places=4)
        assert result == "0.0100%"

    def test_none(self):
        assert _fmt_pct(None) == "n/a"

    def test_zero(self):
        assert _fmt_pct(Decimal("0")) == "0.00%"

    def test_negative(self):
        result = _fmt_pct(Decimal("-0.05"))
        assert result == "-5.00%"

    def test_string_input(self):
        result = _fmt_pct("0.1095")
        assert result == "10.95%"

    def test_int_input(self):
        result = _fmt_pct(1)
        assert result == "100.00%"


class TestFmtPrice:
    def test_normal(self):
        assert _fmt_price(Decimal("65000.5")) == "65000.50"

    def test_none(self):
        assert _fmt_price(None) == "n/a"

    def test_zero(self):
        assert _fmt_price(Decimal("0")) == "0.00"


class TestFmtDecimal:
    def test_normal(self):
        result = _fmt_decimal(Decimal("8"), places=0)
        assert result == "8"

    def test_none(self):
        assert _fmt_decimal(None) == "n/a"

    def test_fractional(self):
        result = _fmt_decimal(Decimal("7.46"), places=2)
        assert result == "7.46"


class TestFmtTimestamp:
    def test_normal(self):
        result = _fmt_timestamp(1710000000000)
        assert "2024-03-09" in result

    def test_none(self):
        assert _fmt_timestamp(None) == "n/a"


class TestFormatSignalMessage:
    def test_full_message(
        self,
        signal_decision_active,
        market_snapshot,
        funding_snapshot,
        basis_metrics,
        cost_metrics,
        net_yield_metrics,
        quality_report_ok,
    ):
        message = format_signal_message(
            symbol_name="BTC_CARRY",
            decision=signal_decision_active,
            market_snapshot=market_snapshot,
            funding_snapshot=funding_snapshot,
            basis_metrics=basis_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=net_yield_metrics,
            quality_report=quality_report_ok,
            config_version="1.0.0",
        )
        assert "CARRY SIGNAL" in message
        assert "BTC_CARRY" in message
        assert "Spot bid/ask:" in message
        assert "Perp bid/ask:" in message
        assert "Entry basis:" in message
        assert "Funding rate:" in message
        assert "Net annual:" in message
        assert "Warnings: none" in message
        assert "Config: 1.0.0" in message

    def test_minimal_message(self, signal_decision_active):
        message = format_signal_message(
            symbol_name="BTC_CARRY",
            decision=signal_decision_active,
        )
        assert "CARRY SIGNAL" in message
        assert "BTC_CARRY" in message

    def test_with_warnings(
        self,
        signal_decision_active,
    ):
        from monitor.domain import DataIssue, QualityReport
        from monitor.domain.enums import DataIssueCode, DataIssueSeverity

        warning_report = QualityReport(
            is_ok=True,
            checked_at_ms=NOW_MS,
            errors=(),
            warnings=(
                DataIssue(
                    code=DataIssueCode.PRICE_JUMP,
                    severity=DataIssueSeverity.WARNING,
                    message="price jumped",
                ),
            ),
        )
        message = format_signal_message(
            symbol_name="BTC_CARRY",
            decision=signal_decision_active,
            quality_report=warning_report,
        )
        assert "price jumped" in message


class TestFormatWarningMessage:
    def test_full(self):
        message = format_warning_message(
            message="data quality check failed",
            now_ms=NOW_MS,
            symbol_name="ETH_CARRY",
            cycle_id="cycle-001",
            details="stale snapshot",
            config_version="1.0.0",
        )
        assert "WARNING" in message
        assert "ETH_CARRY" in message
        assert "cycle-001" in message
        assert "data quality check failed" in message
        assert "stale snapshot" in message

    def test_minimal(self):
        message = format_warning_message(
            message="something wrong",
            now_ms=NOW_MS,
        )
        assert "WARNING" in message
        assert "something wrong" in message


class TestFormatErrorMessage:
    def test_full(self):
        message = format_error_message(
            message="exchange request failed",
            now_ms=NOW_MS,
            symbol_name="BTC_CARRY",
            cycle_id="cycle-001",
            details="timeout",
            config_version="1.0.0",
        )
        assert "ERROR" in message
        assert "exchange request failed" in message
        assert "timeout" in message

    def test_minimal(self):
        message = format_error_message(
            message="critical failure",
            now_ms=NOW_MS,
        )
        assert "ERROR" in message


class TestFormatHeartbeatMessage:
    def test_full(self):
        message = format_heartbeat_message(
            now_ms=NOW_MS,
            service_name="cash-carry-monitor",
            symbols_count=2,
            alerts_sent_count=5,
            last_error=None,
            config_version="1.0.0",
        )
        assert "HEARTBEAT" in message
        assert "cash-carry-monitor" in message
        assert "Symbols monitored: 2" in message
        assert "Alerts sent: 5" in message
        assert "Last error: none" in message

    def test_with_last_error(self):
        message = format_heartbeat_message(
            now_ms=NOW_MS,
            last_error="connection timeout",
        )
        assert "Last error: connection timeout" in message

    def test_minimal(self):
        message = format_heartbeat_message(now_ms=NOW_MS)
        assert "HEARTBEAT" in message