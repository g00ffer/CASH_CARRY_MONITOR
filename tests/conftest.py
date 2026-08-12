"""
Base fixtures for all unit tests.

Run tests from project root:
    PYTHONPATH=src pytest tests/unit/ -v
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from monitor.domain import (
    BasisMetrics,
    CarryInstrument,
    CostMetrics,
    FundingSnapshot,
    FundingYieldMetrics,
    MarketSnapshot,
    NetYieldMetrics,
    PerpTicker,
    QualityReport,
    SignalDecision,
    SignalMetricsSummary,
    SignalState,
    SpotTicker,
)
from monitor.domain.enums import (
    ExpectedExitBasisMode,
    FundingRateSource,
    SignalState as SignalStateEnum,
    StrategyDirection,
    YieldBase,
)
from monitor.data import QualityParams
from monitor.signals import SignalEngineParams

# ======================================================================
# Paths
# ======================================================================

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ======================================================================
# JSON fixture loader
# ======================================================================

def load_json_fixture(name: str) -> dict:
    """Load a JSON fixture file by name."""
    fixture_path = FIXTURES_DIR / name
    with open(fixture_path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


# ======================================================================
# Standard decimal values for tests
# ======================================================================

@pytest.fixture
def zero() -> Decimal:
    return Decimal("0")


@pytest.fixture
def one() -> Decimal:
    return Decimal("1")


@pytest.fixture
def hours_per_year() -> Decimal:
    return Decimal("8760")


# ======================================================================
# CarryInstrument fixtures
# ======================================================================

@pytest.fixture
def carry_instrument() -> CarryInstrument:
    """Standard BTC carry instrument for tests."""
    return CarryInstrument(
        name="BTC_CARRY",
        exchange="binance",
        base="BTC",
        quote="USDT",
        spot_symbol="BTC/USDT",
        perp_symbol="BTC/USDT:USDT",
        direction=StrategyDirection.LONG_SPOT_SHORT_PERP,
        notional_usd=Decimal("10000"),
        enabled=True,
    )


# ======================================================================
# Spot / Perp ticker fixtures
# ======================================================================

@pytest.fixture
def spot_ticker() -> SpotTicker:
    """Standard spot ticker: BTC/USDT with bid=65000, ask=65001."""
    return SpotTicker(
        symbol="BTC/USDT",
        bid=Decimal("65000"),
        ask=Decimal("65001"),
        timestamp_ms=1710000000000,
        last=Decimal("65000.5"),
        quote_volume_24h=Decimal("803702067"),
        base_volume_24h=Decimal("12345.67"),
    )


@pytest.fixture
def perp_ticker() -> PerpTicker:
    """Standard perp ticker: BTC/USDT:USDT with bid=65030, ask=65032."""
    return PerpTicker(
        symbol="BTC/USDT:USDT",
        bid=Decimal("65030"),
        ask=Decimal("65032"),
        timestamp_ms=1710000000200,
        last=Decimal("65031"),
        mark_price=Decimal("65031"),
        index_price=Decimal("65025"),
        quote_volume_24h=Decimal("6432587654"),
        base_volume_24h=Decimal("98765.43"),
    )


@pytest.fixture
def market_snapshot(
    spot_ticker: SpotTicker,
    perp_ticker: PerpTicker,
) -> MarketSnapshot:
    """Combined market snapshot for BTC_CARRY."""
    return MarketSnapshot(
        cycle_id="test_cycle_001",
        symbol_name="BTC_CARRY",
        spot=spot_ticker,
        perp=perp_ticker,
        received_at_ms=1710000000300,
    )


# ======================================================================
# Funding fixtures
# ======================================================================

@pytest.fixture
def funding_snapshot() -> FundingSnapshot:
    """
    Standard funding snapshot.
    effective_funding_rate = 0.0001 (0.01% per 8h interval)
    funding_annual = 0.0001 * (8760 / 8) = 0.1095 = 10.95%
    """
    return FundingSnapshot(
        cycle_id="test_cycle_001",
        symbol_name="BTC_CARRY",
        effective_funding_rate=Decimal("0.0001"),
        funding_rate_source=FundingRateSource.PREDICTED,
        funding_interval_hours=Decimal("8"),
        received_at_ms=1710000000300,
        last_funding_rate=Decimal("0.00008"),
        predicted_funding_rate=Decimal("0.0001"),
        next_funding_timestamp_ms=1710028800000,
    )


# ======================================================================
# Metrics fixtures
# ======================================================================

@pytest.fixture
def basis_metrics() -> BasisMetrics:
    """Pre-calculated basis metrics for standard tickers."""
    return BasisMetrics(
        basis_mid=Decimal("0.00046923076923076923076923077"),
        basis_entry=Decimal("0.00044614629097521484130967063"),
        spot_mid=Decimal("65000.5"),
        perp_mid=Decimal("65031"),
        spot_spread=Decimal("0.00001538461538461538461538462"),
        perp_spread=Decimal("0.00003075464412432585100368554"),
        calculated_at_ms=1710000000300,
    )


@pytest.fixture
def funding_yield_metrics() -> FundingYieldMetrics:
    """Pre-calculated funding yield metrics."""
    return FundingYieldMetrics(
        funding_rate_per_interval=Decimal("0.0001"),
        funding_interval_hours=Decimal("8"),
        periods_per_year=Decimal("1095"),
        funding_annual=Decimal("0.1095"),
        holding_hours=Decimal("168"),
        pro_rata_funding_events=Decimal("21"),
        funding_horizon=Decimal("0.0021"),
        calculated_at_ms=1710000000300,
    )


@pytest.fixture
def cost_metrics() -> CostMetrics:
    """
    Pre-calculated cost metrics.
    one_time_costs = 0.0036 (0.36%)
    """
    return CostMetrics(
        spot_fee_round_trip=Decimal("0.002"),
        perp_fee_round_trip=Decimal("0.001"),
        slippage_round_trip=Decimal("0.0004"),
        spread_buffer=Decimal("0.0002"),
        one_time_costs=Decimal("0.0036"),
        borrow_cost_horizon=Decimal("0"),
        opportunity_cost_horizon=Decimal("0"),
        total_costs_horizon=Decimal("0.0036"),
        one_time_costs_annualized=Decimal("0.1877142857142857142857142857"),
        total_costs_annualized=Decimal("0.1877142857142857142857142857"),
        cost_amortization_hours=Decimal("168"),
        calculated_at_ms=1710000000300,
    )


@pytest.fixture
def net_yield_metrics() -> NetYieldMetrics:
    """Pre-calculated net yield metrics for conservative mode."""
    return NetYieldMetrics(
        holding_hours=Decimal("168"),
        cost_amortization_hours=Decimal("168"),
        funding_horizon=Decimal("0.0021"),
        basis_convergence_pnl=Decimal("0"),
        gross_horizon=Decimal("0.0021"),
        total_costs_horizon=Decimal("0.0036"),
        net_horizon=Decimal("-0.0015"),
        net_annual=Decimal("-0.07821428571428571428571428571"),
        include_basis_convergence=False,
        yield_base=YieldBase.NOTIONAL,
        calculated_at_ms=1710000000300,
        net_annual_on_equity=None,
    )


# ======================================================================
# Quality fixtures
# ======================================================================

@pytest.fixture
def quality_params() -> QualityParams:
    """Standard quality parameters for tests."""
    return QualityParams(
        max_snapshot_age_ms=15000,
        max_spot_perp_time_diff_ms=5000,
        max_spread=Decimal("0.001"),  # 0.1% = 10 bps
        min_quote_volume_24h=Decimal("1000000"),
        require_valid_funding_interval=True,
        require_predicted_funding=False,
        max_price_jump_pct=Decimal("0.05"),
        future_timestamp_tolerance_ms=5000,
    )


@pytest.fixture
def quality_report_ok() -> QualityReport:
    """Quality report that passes all checks."""
    return QualityReport(
        is_ok=True,
        checked_at_ms=1710000000300,
        errors=(),
        warnings=(),
    )


@pytest.fixture
def quality_report_failed() -> QualityReport:
    """Quality report that fails checks."""
    from monitor.domain import DataIssue
    from monitor.domain.enums import DataIssueCode, DataIssueSeverity

    return QualityReport(
        is_ok=False,
        checked_at_ms=1710000000300,
        errors=(
            DataIssue(
                code=DataIssueCode.STALE_SNAPSHOT,
                severity=DataIssueSeverity.ERROR,
                message="spot timestamp is too old",
                field_name="spot.timestamp_ms",
            ),
        ),
        warnings=(),
    )


# ======================================================================
# Signal engine fixtures
# ======================================================================

@pytest.fixture
def signal_params() -> SignalEngineParams:
    """Standard signal engine parameters."""
    return SignalEngineParams(
        min_net_annual=Decimal("0.08"),          # 8%
        min_net_horizon=Decimal("0.001"),        # 0.1%
        min_funding_rate_per_interval=Decimal("0.00005"),  # 0.005%
        require_positive_funding=True,
        require_predicted_funding=False,
        min_consecutive_confirmations=3,
        cooldown_sec=3600,
        hysteresis=Decimal("0.01"),              # 1%
        max_snapshot_age_ms=15000,
        max_spread=Decimal("0.001"),             # 0.1% = 10 bps
        suppress_minutes_before_funding=10,
        suppress_minutes_after_funding=10,
        repeat_alert_while_active=False,
    )


@pytest.fixture
def signal_decision_normal() -> SignalDecision:
    """Signal decision in NORMAL state, no alert."""
    return SignalDecision(
        cycle_id="test_cycle_001",
        symbol_name="BTC_CARRY",
        timestamp_ms=1710000000300,
        state=SignalStateEnum.NORMAL,
        should_alert=False,
        reasons=("net_annual_below_threshold",),
        passed_checks=("quality_ok", "snapshot_fresh"),
        consecutive_confirmations=0,
        cooldown_remaining_sec=0,
        metrics=None,
    )


@pytest.fixture
def signal_decision_active() -> SignalDecision:
    """Signal decision in SIGNAL_ACTIVE state, should alert."""
    metrics_summary = SignalMetricsSummary(
        funding_rate_per_interval=Decimal("0.0001"),
        funding_annual=Decimal("0.1095"),
        funding_horizon=Decimal("0.0021"),
        basis_entry=Decimal("0.0004"),
        one_time_costs=Decimal("0.0036"),
        total_costs_horizon=Decimal("0.0036"),
        net_horizon=Decimal("0.0015"),
        net_annual=Decimal("0.0850"),
    )
    return SignalDecision(
        cycle_id="test_cycle_001",
        symbol_name="BTC_CARRY",
        timestamp_ms=1710000000300,
        state=SignalStateEnum.SIGNAL_ACTIVE,
        should_alert=True,
        reasons=(),
        passed_checks=(
            "quality_ok",
            "snapshot_fresh",
            "spread_ok",
            "funding_interval_known",
            "funding_positive",
            "funding_rate_ok",
            "net_horizon_ok",
            "net_annual_ok",
            "funding_window_ok",
        ),
        consecutive_confirmations=3,
        cooldown_remaining_sec=0,
        metrics=metrics_summary,
    )


# ======================================================================
# Raw JSON fixture loaders (for normalizer tests)
# ======================================================================

@pytest.fixture
def raw_spot_ticker() -> dict:
    """Load raw Binance spot ticker JSON fixture."""
    return load_json_fixture("binance_spot_ticker.json")


@pytest.fixture
def raw_perp_ticker() -> dict:
    """Load raw Binance perp ticker JSON fixture."""
    return load_json_fixture("binance_perp_ticker.json")


@pytest.fixture
def raw_funding() -> dict:
    """Load raw Binance funding JSON fixture."""
    return load_json_fixture("binance_funding.json")