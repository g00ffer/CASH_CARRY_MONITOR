"""Tests for monitor.data.quality"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from monitor.data import QualityParams
from monitor.data.quality import (
    check_funding_quality,
    check_market_quality,
    check_symbol_quality,
    merge_quality_reports,
    quality_report_from_error,
    quality_report_from_errors,
)
from monitor.domain import (
    DataIssue,
    FundingSnapshot,
    MarketSnapshot,
    PerpTicker,
    QualityReport,
    SpotTicker,
)
from monitor.domain.enums import DataIssueCode, DataIssueSeverity, FundingRateSource

NOW_MS = 1710000001000


# ======================================================================
# QualityParams validation
# ======================================================================
class TestQualityParamsValidation:
    def test_valid_params(self):
        params = QualityParams(
            max_snapshot_age_ms=15000,
            max_spot_perp_time_diff_ms=5000,
            max_spread=Decimal("0.001"),
        )
        assert params.max_snapshot_age_ms == 15000

    def test_zero_snapshot_age_raises(self):
        with pytest.raises(ValueError, match="max_snapshot_age_ms must be > 0"):
            QualityParams(
                max_snapshot_age_ms=0,
                max_spot_perp_time_diff_ms=5000,
                max_spread=Decimal("0.001"),
            )

    def test_negative_time_diff_raises(self):
        with pytest.raises(ValueError, match="max_spot_perp_time_diff_ms must be >= 0"):
            QualityParams(
                max_snapshot_age_ms=15000,
                max_spot_perp_time_diff_ms=-1,
                max_spread=Decimal("0.001"),
            )

    def test_negative_spread_raises(self):
        with pytest.raises(ValueError, match="max_spread must be >= 0"):
            QualityParams(
                max_snapshot_age_ms=15000,
                max_spot_perp_time_diff_ms=5000,
                max_spread=Decimal("-0.001"),
            )

    def test_negative_min_volume_raises(self):
        with pytest.raises(ValueError, match="min_quote_volume_24h must be >= 0"):
            QualityParams(
                max_snapshot_age_ms=15000,
                max_spot_perp_time_diff_ms=5000,
                max_spread=Decimal("0.001"),
                min_quote_volume_24h=Decimal("-1"),
            )

    def test_negative_price_jump_raises(self):
        with pytest.raises(ValueError, match="max_price_jump_pct must be >= 0"):
            QualityParams(
                max_snapshot_age_ms=15000,
                max_spot_perp_time_diff_ms=5000,
                max_spread=Decimal("0.001"),
                max_price_jump_pct=Decimal("-1"),
            )

    def test_negative_tolerance_raises(self):
        with pytest.raises(ValueError, match="future_timestamp_tolerance_ms must be >= 0"):
            QualityParams(
                max_snapshot_age_ms=15000,
                max_spot_perp_time_diff_ms=5000,
                max_spread=Decimal("0.001"),
                future_timestamp_tolerance_ms=-1,
            )


# ======================================================================
# check_market_quality
# ======================================================================
class TestCheckMarketQuality:
    def test_valid_snapshot(self, market_snapshot, quality_params):
        report = check_market_quality(
            market_snapshot=market_snapshot,
            params=quality_params,
            now_ms=NOW_MS,
        )
        assert report.is_ok is True
        assert len(report.errors) == 0

    def test_stale_spot_timestamp(self, market_snapshot, quality_params):
        stale_spot = replace(market_snapshot.spot, timestamp_ms=NOW_MS - 20000)
        stale_snapshot = replace(market_snapshot, spot=stale_spot)
        report = check_market_quality(
            market_snapshot=stale_snapshot,
            params=quality_params,
            now_ms=NOW_MS,
        )
        assert report.is_ok is False
        codes = [e.code for e in report.errors]
        assert DataIssueCode.STALE_SNAPSHOT in codes

    def test_future_spot_timestamp(self, market_snapshot, quality_params):
        future_spot = replace(market_snapshot.spot, timestamp_ms=NOW_MS + 10000)
        future_snapshot = replace(market_snapshot, spot=future_spot)
        report = check_market_quality(
            market_snapshot=future_snapshot,
            params=quality_params,
            now_ms=NOW_MS,
        )
        assert report.is_ok is False
        codes = [e.code for e in report.errors]
        assert DataIssueCode.FUTURE_TIMESTAMP in codes

    def test_future_within_tolerance(self, market_snapshot, quality_params):
        # quality_params.future_timestamp_tolerance_ms = 5000
        slight_future = replace(market_snapshot.spot, timestamp_ms=NOW_MS + 3000)
        snapshot = replace(market_snapshot, spot=slight_future)
        report = check_market_quality(
            market_snapshot=snapshot,
            params=quality_params,
            now_ms=NOW_MS,
        )
        assert report.is_ok is True

    def test_non_positive_bid(self, market_snapshot, quality_params):
        bad_spot = replace(market_snapshot.spot, bid=Decimal("0"))
        snapshot = replace(market_snapshot, spot=bad_spot)
        report = check_market_quality(
            market_snapshot=snapshot,
            params=quality_params,
            now_ms=NOW_MS,
        )
        assert report.is_ok is False
        codes = [e.code for e in report.errors]
        assert DataIssueCode.NON_POSITIVE_PRICE in codes

    def test_bid_greater_than_ask(self, market_snapshot, quality_params):
        bad_spot = replace(
            market_snapshot.spot,
            bid=Decimal("65002"),
            ask=Decimal("65001"),
        )
        snapshot = replace(market_snapshot, spot=bad_spot)
        report = check_market_quality(
            market_snapshot=snapshot,
            params=quality_params,
            now_ms=NOW_MS,
        )
        assert report.is_ok is False
        codes = [e.code for e in report.errors]
        assert DataIssueCode.INVALID_BID_ASK in codes

    def test_wide_spread(self, market_snapshot, quality_params):
        # max_spread = 0.001, make spread ~0.0015
        wide_spot = replace(
            market_snapshot.spot,
            bid=Decimal("65000"),
            ask=Decimal("65100"),
        )
        snapshot = replace(market_snapshot, spot=wide_spot)
        report = check_market_quality(
            market_snapshot=snapshot,
            params=quality_params,
            now_ms=NOW_MS,
        )
        assert report.is_ok is False
        codes = [e.code for e in report.errors]
        assert DataIssueCode.SPREAD_TOO_WIDE in codes

    def test_low_spot_volume(self, market_snapshot, quality_params):
        # min_quote_volume_24h = 1000000
        low_vol_spot = replace(market_snapshot.spot, quote_volume_24h=Decimal("500000"))
        snapshot = replace(market_snapshot, spot=low_vol_spot)
        report = check_market_quality(
            market_snapshot=snapshot,
            params=quality_params,
            now_ms=NOW_MS,
        )
        assert report.is_ok is False
        codes = [e.code for e in report.errors]
        assert DataIssueCode.LOW_LIQUIDITY in codes

    def test_spot_perp_timestamp_mismatch(self, market_snapshot, quality_params):
        # max_spot_perp_time_diff_ms = 5000
        far_perp = replace(market_snapshot.perp, timestamp_ms=NOW_MS - 8000)
        snapshot = replace(market_snapshot, perp=far_perp)
        report = check_market_quality(
            market_snapshot=snapshot,
            params=quality_params,
            now_ms=NOW_MS,
        )
        # Теперь это WARNING, а не ERROR (перевели в прошлой сессии)
        assert report.is_ok is True  # ← Было False, стало True
        from monitor.domain.enums import DataIssueCode
        assert any(
            issue.code == DataIssueCode.TIMESTAMP_MISMATCH
            for issue in report.warnings
        )

    def test_stale_received_at(self, market_snapshot, quality_params):
        stale_snapshot = replace(market_snapshot, received_at_ms=NOW_MS - 20000)
        report = check_market_quality(
            market_snapshot=stale_snapshot,
            params=quality_params,
            now_ms=NOW_MS,
        )
        assert report.is_ok is False

    def test_future_received_at(self, market_snapshot, quality_params):
        future_snapshot = replace(market_snapshot, received_at_ms=NOW_MS + 10000)
        report = check_market_quality(
            market_snapshot=future_snapshot,
            params=quality_params,
            now_ms=NOW_MS,
        )
        assert report.is_ok is False

    def test_price_jump_warning(self, market_snapshot, quality_params):
        params_with_jump = replace(
            quality_params,
            max_price_jump_pct=Decimal("0.01"),
        )
        # Create a previous snapshot with very different price
        prev_spot = replace(market_snapshot.spot, bid=Decimal("60000"), ask=Decimal("60001"))
        prev_snapshot = replace(market_snapshot, spot=prev_spot)
        report = check_market_quality(
            market_snapshot=market_snapshot,
            params=params_with_jump,
            now_ms=NOW_MS,
            previous_market_snapshot=prev_snapshot,
        )
        # Price jump is a warning, not error
        assert report.is_ok is True
        assert len(report.warnings) > 0
        warning_codes = [w.code for w in report.warnings]
        assert DataIssueCode.PRICE_JUMP in warning_codes


# ======================================================================
# check_funding_quality
# ======================================================================
class TestCheckFundingQuality:
    def test_valid_funding(self, funding_snapshot, quality_params):
        report = check_funding_quality(
            funding_snapshot=funding_snapshot,
            params=quality_params,
            now_ms=NOW_MS,
        )
        assert report.is_ok is True

    def test_stale_funding(self, funding_snapshot, quality_params):
        stale = replace(funding_snapshot, received_at_ms=NOW_MS - 20000)
        report = check_funding_quality(
            funding_snapshot=stale,
            params=quality_params,
            now_ms=NOW_MS,
        )
        assert report.is_ok is False

    def test_future_funding(self, funding_snapshot, quality_params):
        future = replace(funding_snapshot, received_at_ms=NOW_MS + 10000)
        report = check_funding_quality(
            funding_snapshot=future,
            params=quality_params,
            now_ms=NOW_MS,
        )
        assert report.is_ok is False

    def test_zero_interval_error(self, funding_snapshot, quality_params):
        bad = replace(funding_snapshot, funding_interval_hours=Decimal("0"))
        report = check_funding_quality(
            funding_snapshot=bad,
            params=quality_params,
            now_ms=NOW_MS,
        )
        assert report.is_ok is False
        codes = [e.code for e in report.errors]
        assert DataIssueCode.FUNDING_INTERVAL_UNKNOWN in codes

    def test_zero_interval_warning_when_not_required(self, funding_snapshot, quality_params):
        params = replace(quality_params, require_valid_funding_interval=False)
        bad = replace(funding_snapshot, funding_interval_hours=Decimal("0"))
        report = check_funding_quality(
            funding_snapshot=bad,
            params=params,
            now_ms=NOW_MS,
        )
        assert report.is_ok is True
        assert len(report.warnings) > 0

    def test_predicted_funding_required_but_missing(self, funding_snapshot, quality_params):
        params = replace(quality_params, require_predicted_funding=True)
        bad = replace(funding_snapshot, predicted_funding_rate=None)
        report = check_funding_quality(
            funding_snapshot=bad,
            params=params,
            now_ms=NOW_MS,
        )
        assert report.is_ok is False
        codes = [e.code for e in report.errors]
        assert DataIssueCode.FUNDING_UNKNOWN in codes

    def test_next_funding_in_past_warning(self, funding_snapshot, quality_params):
        bad = replace(funding_snapshot, next_funding_timestamp_ms=NOW_MS - 60000)
        report = check_funding_quality(
            funding_snapshot=bad,
            params=quality_params,
            now_ms=NOW_MS,
        )
        # Warning, not error
        assert len(report.warnings) > 0

    def test_next_funding_missing_warning(self, funding_snapshot, quality_params):
        bad = replace(funding_snapshot, next_funding_timestamp_ms=None)
        report = check_funding_quality(
            funding_snapshot=bad,
            params=quality_params,
            now_ms=NOW_MS,
        )
        assert len(report.warnings) > 0

    def test_last_funding_in_future_warning(self, funding_snapshot, quality_params):
        bad = replace(funding_snapshot, last_funding_timestamp_ms=NOW_MS + 10000)
        report = check_funding_quality(
            funding_snapshot=bad,
            params=quality_params,
            now_ms=NOW_MS,
        )
        assert len(report.warnings) > 0


# ======================================================================
# merge_quality_reports
# ======================================================================
class TestMergeQualityReports:
    def test_merge_ok_reports(self):
        r1 = QualityReport(is_ok=True, checked_at_ms=NOW_MS, errors=(), warnings=())
        r2 = QualityReport(is_ok=True, checked_at_ms=NOW_MS, errors=(), warnings=())
        merged = merge_quality_reports(r1, r2, checked_at_ms=NOW_MS)
        assert merged.is_ok is True

    def test_merge_with_errors(self):
        issue = DataIssue(
            code=DataIssueCode.STALE_SNAPSHOT,
            severity=DataIssueSeverity.ERROR,
            message="stale",
        )
        r1 = QualityReport(is_ok=True, checked_at_ms=NOW_MS, errors=(), warnings=())
        r2 = QualityReport(is_ok=False, checked_at_ms=NOW_MS, errors=(issue,), warnings=())
        merged = merge_quality_reports(r1, r2, checked_at_ms=NOW_MS)
        assert merged.is_ok is False
        assert len(merged.errors) == 1


# ======================================================================
# check_symbol_quality
# ======================================================================
class TestCheckSymbolQuality:
    def test_both_valid(self, market_snapshot, funding_snapshot, quality_params):
        report = check_symbol_quality(
            market_snapshot=market_snapshot,
            funding_snapshot=funding_snapshot,
            params=quality_params,
            now_ms=NOW_MS,
        )
        assert report.is_ok is True

    def test_missing_market_snapshot(self, funding_snapshot, quality_params):
        report = check_symbol_quality(
            market_snapshot=None,
            funding_snapshot=funding_snapshot,
            params=quality_params,
            now_ms=NOW_MS,
        )
        assert report.is_ok is False
        codes = [e.code for e in report.errors]
        assert DataIssueCode.EXCHANGE_ERROR in codes

    def test_missing_funding_snapshot(self, market_snapshot, quality_params):
        report = check_symbol_quality(
            market_snapshot=market_snapshot,
            funding_snapshot=None,
            params=quality_params,
            now_ms=NOW_MS,
        )
        assert report.is_ok is False

    def test_both_missing(self, quality_params):
        report = check_symbol_quality(
            market_snapshot=None,
            funding_snapshot=None,
            params=quality_params,
            now_ms=NOW_MS,
        )
        assert report.is_ok is False
        assert len(report.errors) >= 2


# ======================================================================
# quality_report_from_error / quality_report_from_errors
# ======================================================================
class TestQualityReportFromErrors:
    def test_single_error(self):
        report = quality_report_from_error(
            message="something failed",
            checked_at_ms=NOW_MS,
        )
        assert report.is_ok is False
        assert len(report.errors) == 1
        assert report.errors[0].message == "something failed"

    def test_multiple_errors(self):
        report = quality_report_from_errors(
            messages=["error 1", "error 2", "error 3"],
            checked_at_ms=NOW_MS,
        )
        assert report.is_ok is False
        assert len(report.errors) == 3

    def test_empty_errors(self):
        report = quality_report_from_errors(
            messages=[],
            checked_at_ms=NOW_MS,
        )
        assert report.is_ok is True
        assert len(report.errors) == 0