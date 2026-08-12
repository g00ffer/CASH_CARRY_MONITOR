"""
Tests for monitor.signals (cooldown, state, engine)
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from monitor.domain import (
    AlertState,
    SignalDecision,
    SignalState,
)
from monitor.signals import (
    InMemorySignalStateStore,
    SignalEngineParams,
    SignalEvaluationInput,
    create_alert_state,
    evaluate_signal,
    update_alert_state,
)
from monitor.signals.cooldown import (
    calc_cooldown_remaining_sec,
    hysteresis_threshold,
    is_cooldown_active,
    meets_threshold_with_hysteresis,
)


# ======================================================================
# Constants and helpers
# ======================================================================

# Close to market_snapshot.received_at_ms (1710000000300)
# so that snapshot_age < max_snapshot_age_ms (15000)
NOW_MS = 1710000001000


def _make_passing_evaluation_input(
    market_snapshot,
    funding_snapshot,
    basis_metrics,
    funding_yield_metrics,
    cost_metrics,
    net_yield_metrics,
    quality_report,
) -> SignalEvaluationInput:
    """
    Build evaluation input that passes all checks.
    Overrides net_yield_metrics with passing values.
    """
    passing_net_yield = replace(
        net_yield_metrics,
        net_horizon=Decimal("0.002"),   # > min_net_horizon (0.001)
        net_annual=Decimal("0.10"),     # > min_net_annual (0.08)
    )
    return SignalEvaluationInput(
        cycle_id="test-cycle-001",
        symbol_name="BTC_CARRY",
        now_ms=NOW_MS,
        quality_report=quality_report,
        market_snapshot=market_snapshot,
        funding_snapshot=funding_snapshot,
        basis_metrics=basis_metrics,
        funding_yield_metrics=funding_yield_metrics,
        cost_metrics=cost_metrics,
        net_yield_metrics=passing_net_yield,
    )


def _make_active_state(
    *,
    confirmations: int = 5,
    last_alert_ts_ms: int | None = None,
) -> AlertState:
    """Helper to create an active signal state."""
    return AlertState(
        symbol_name="BTC_CARRY",
        state=SignalState.SIGNAL_ACTIVE,
        consecutive_confirmations=confirmations,
        last_alert_ts_ms=last_alert_ts_ms,
        last_signal_started_ts_ms=NOW_MS - 5000 * 1000,
        last_net_annual=Decimal("0.10"),
        last_reasons=(),
        updated_at_ms=NOW_MS - 10000,
    )


# ======================================================================
# Tests for cooldown.py
# ======================================================================

class TestCalcCooldownRemainingSec:
    def test_no_last_alert(self):
        result = calc_cooldown_remaining_sec(
            last_alert_ts_ms=None,
            now_ms=1000000,
            cooldown_sec=3600,
        )
        assert result == 0

    def test_zero_cooldown(self):
        result = calc_cooldown_remaining_sec(
            last_alert_ts_ms=1000000,
            now_ms=2000000,
            cooldown_sec=0,
        )
        assert result == 0

    def test_cooldown_active(self):
        # Last alert 1000 seconds ago, cooldown 3600 seconds
        # Remaining: 3600 - 1000 = 2600 seconds
        result = calc_cooldown_remaining_sec(
            last_alert_ts_ms=1000000,
            now_ms=1000000 + 1000 * 1000,
            cooldown_sec=3600,
        )
        assert result == 2600

    def test_cooldown_expired(self):
        # Last alert 4000 seconds ago, cooldown 3600 seconds
        result = calc_cooldown_remaining_sec(
            last_alert_ts_ms=1000000,
            now_ms=1000000 + 4000 * 1000,
            cooldown_sec=3600,
        )
        assert result == 0

    def test_future_timestamp_returns_full_cooldown(self):
        result = calc_cooldown_remaining_sec(
            last_alert_ts_ms=2000000,
            now_ms=1000000,
            cooldown_sec=3600,
        )
        assert result == 3600


class TestIsCooldownActive:
    def test_active(self):
        assert is_cooldown_active(
            last_alert_ts_ms=1000000,
            now_ms=1000000 + 1000 * 1000,
            cooldown_sec=3600,
        ) is True

    def test_expired(self):
        assert is_cooldown_active(
            last_alert_ts_ms=1000000,
            now_ms=1000000 + 4000 * 1000,
            cooldown_sec=3600,
        ) is False

    def test_no_alert(self):
        assert is_cooldown_active(
            last_alert_ts_ms=None,
            now_ms=1000000,
            cooldown_sec=3600,
        ) is False


class TestHysteresisThreshold:
    def test_not_active(self):
        result = hysteresis_threshold(
            base_threshold=Decimal("0.08"),
            hysteresis=Decimal("0.01"),
            is_active=False,
        )
        assert result == Decimal("0.08")

    def test_active_reduces_threshold(self):
        result = hysteresis_threshold(
            base_threshold=Decimal("0.08"),
            hysteresis=Decimal("0.01"),
            is_active=True,
        )
        assert result == Decimal("0.07")

    def test_zero_hysteresis(self):
        result = hysteresis_threshold(
            base_threshold=Decimal("0.08"),
            hysteresis=Decimal("0"),
            is_active=True,
        )
        assert result == Decimal("0.08")


class TestMeetsThresholdWithHysteresis:
    def test_above_threshold(self):
        assert meets_threshold_with_hysteresis(
            value=Decimal("0.09"),
            base_threshold=Decimal("0.08"),
            hysteresis=Decimal("0.01"),
            is_active=False,
        ) is True

    def test_below_threshold(self):
        assert meets_threshold_with_hysteresis(
            value=Decimal("0.07"),
            base_threshold=Decimal("0.08"),
            hysteresis=Decimal("0.01"),
            is_active=False,
        ) is False

    def test_in_hysteresis_zone_active(self):
        # Value 0.075 is between 0.07 and 0.08
        # Signal is active -> effective threshold is 0.07 -> passes
        assert meets_threshold_with_hysteresis(
            value=Decimal("0.075"),
            base_threshold=Decimal("0.08"),
            hysteresis=Decimal("0.01"),
            is_active=True,
        ) is True

    def test_in_hysteresis_zone_not_active(self):
        # Value 0.075 is between 0.07 and 0.08
        # Signal is not active -> effective threshold is 0.08 -> fails
        assert meets_threshold_with_hysteresis(
            value=Decimal("0.075"),
            base_threshold=Decimal("0.08"),
            hysteresis=Decimal("0.01"),
            is_active=False,
        ) is False


# ======================================================================
# Tests for state.py
# ======================================================================

class TestCreateAlertState:
    def test_initial_state(self):
        state = create_alert_state(
            symbol_name="BTC_CARRY",
            now_ms=1000000,
        )
        assert state.symbol_name == "BTC_CARRY"
        assert state.state == SignalState.NORMAL
        assert state.consecutive_confirmations == 0
        assert state.last_alert_ts_ms is None
        assert state.last_signal_started_ts_ms is None
        assert state.last_net_annual is None
        assert state.last_reasons == ()
        assert state.updated_at_ms == 1000000


class TestInMemorySignalStateStore:
    def test_get_nonexistent(self):
        store = InMemorySignalStateStore()
        assert store.get("BTC_CARRY") is None

    def test_upsert_and_get(self):
        store = InMemorySignalStateStore()
        state = create_alert_state(
            symbol_name="BTC_CARRY",
            now_ms=1000000,
        )
        store.upsert(state)
        retrieved = store.get("BTC_CARRY")
        assert retrieved is not None
        assert retrieved.symbol_name == "BTC_CARRY"

    def test_all_returns_all_states(self):
        store = InMemorySignalStateStore()
        state1 = create_alert_state(symbol_name="BTC_CARRY", now_ms=1000000)
        state2 = create_alert_state(symbol_name="ETH_CARRY", now_ms=1000000)
        store.upsert(state1)
        store.upsert(state2)
        all_states = store.all()
        assert len(all_states) == 2

    def test_upsert_overwrites_existing(self):
        store = InMemorySignalStateStore()
        state1 = create_alert_state(symbol_name="BTC_CARRY", now_ms=1000000)
        store.upsert(state1)
        state2 = replace(state1, state=SignalState.SIGNAL_ACTIVE)
        store.upsert(state2)
        retrieved = store.get("BTC_CARRY")
        assert retrieved.state == SignalState.SIGNAL_ACTIVE


class TestUpdateAlertState:
    def _make_decision(
        self,
        *,
        should_alert: bool = False,
        state: SignalState = SignalState.NORMAL,
        confirmations: int = 0,
    ) -> SignalDecision:
        return SignalDecision(
            cycle_id="test-cycle",
            symbol_name="BTC_CARRY",
            timestamp_ms=2000000,
            state=state,
            should_alert=should_alert,
            reasons=(),
            passed_checks=(),
            consecutive_confirmations=confirmations,
            cooldown_remaining_sec=0,
            metrics=None,
        )

    def test_should_alert_updates_last_alert_ts(self):
        decision = self._make_decision(
            should_alert=True,
            state=SignalState.SIGNAL_ACTIVE,
            confirmations=3,
        )
        new_state = update_alert_state(
            current=None,
            decision=decision,
            now_ms=2000000,
        )
        assert new_state.last_alert_ts_ms == 2000000

    def test_no_alert_preserves_last_alert_ts(self):
        current = create_alert_state(
            symbol_name="BTC_CARRY",
            now_ms=1000000,
        )
        # Simulate a previous alert
        current = replace(current, last_alert_ts_ms=1500000)
        decision = self._make_decision(should_alert=False)
        new_state = update_alert_state(
            current=current,
            decision=decision,
            now_ms=2000000,
        )
        assert new_state.last_alert_ts_ms == 1500000

    def test_confirmations_updated(self):
        decision = self._make_decision(confirmations=5)
        new_state = update_alert_state(
            current=None,
            decision=decision,
            now_ms=2000000,
        )
        assert new_state.consecutive_confirmations == 5

    def test_state_updated(self):
        decision = self._make_decision(
            state=SignalState.SIGNAL_ACTIVE,
            confirmations=3,
        )
        new_state = update_alert_state(
            current=None,
            decision=decision,
            now_ms=2000000,
        )
        assert new_state.state == SignalState.SIGNAL_ACTIVE


# ======================================================================
# Tests for engine.py — Quality Gate
# ======================================================================

class TestEvaluateSignalQualityGate:
    def test_failed_quality_report_returns_data_invalid(
        self,
        quality_report_failed,
        market_snapshot,
        funding_snapshot,
        basis_metrics,
        funding_yield_metrics,
        cost_metrics,
        net_yield_metrics,
        signal_params,
    ):
        evaluation_input = _make_passing_evaluation_input(
            market_snapshot=market_snapshot,
            funding_snapshot=funding_snapshot,
            basis_metrics=basis_metrics,
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=net_yield_metrics,
            quality_report=quality_report_failed,
        )
        decision = evaluate_signal(
            evaluation_input=evaluation_input,
            current_state=None,
            params=signal_params,
        )
        assert decision.state == SignalState.DATA_INVALID
        assert decision.should_alert is False
        assert decision.consecutive_confirmations == 0
        assert len(decision.reasons) > 0

    def test_failed_quality_report_resets_confirmations(
        self,
        quality_report_failed,
        market_snapshot,
        funding_snapshot,
        basis_metrics,
        funding_yield_metrics,
        cost_metrics,
        net_yield_metrics,
        signal_params,
    ):
        # Even if previous state had confirmations, they should reset
        current_state = _make_active_state(confirmations=5)
        evaluation_input = _make_passing_evaluation_input(
            market_snapshot=market_snapshot,
            funding_snapshot=funding_snapshot,
            basis_metrics=basis_metrics,
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=net_yield_metrics,
            quality_report=quality_report_failed,
        )
        decision = evaluate_signal(
            evaluation_input=evaluation_input,
            current_state=current_state,
            params=signal_params,
        )
        assert decision.state == SignalState.DATA_INVALID
        assert decision.consecutive_confirmations == 0


# ======================================================================
# Tests for engine.py — Confirmations
# ======================================================================

class TestEvaluateSignalConfirmations:
    def test_first_confirmation_watching(
        self,
        market_snapshot,
        funding_snapshot,
        basis_metrics,
        funding_yield_metrics,
        cost_metrics,
        net_yield_metrics,
        quality_report_ok,
        signal_params,
    ):
        evaluation_input = _make_passing_evaluation_input(
            market_snapshot=market_snapshot,
            funding_snapshot=funding_snapshot,
            basis_metrics=basis_metrics,
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=net_yield_metrics,
            quality_report=quality_report_ok,
        )
        decision = evaluate_signal(
            evaluation_input=evaluation_input,
            current_state=None,
            params=signal_params,
        )
        assert decision.state == SignalState.WATCHING
        assert decision.should_alert is False
        assert decision.consecutive_confirmations == 1
        assert decision.reasons == ()

    def test_enough_confirmations_signal_active(
        self,
        market_snapshot,
        funding_snapshot,
        basis_metrics,
        funding_yield_metrics,
        cost_metrics,
        net_yield_metrics,
        quality_report_ok,
        signal_params,
    ):
        # Previous state with 2 confirmations (min_consecutive_confirmations=3)
        current_state = AlertState(
            symbol_name="BTC_CARRY",
            state=SignalState.WATCHING,
            consecutive_confirmations=2,
            last_alert_ts_ms=None,
            last_signal_started_ts_ms=None,
            last_net_annual=None,
            last_reasons=(),
            updated_at_ms=NOW_MS - 10000,
        )
        evaluation_input = _make_passing_evaluation_input(
            market_snapshot=market_snapshot,
            funding_snapshot=funding_snapshot,
            basis_metrics=basis_metrics,
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=net_yield_metrics,
            quality_report=quality_report_ok,
        )
        decision = evaluate_signal(
            evaluation_input=evaluation_input,
            current_state=current_state,
            params=signal_params,
        )
        assert decision.state == SignalState.SIGNAL_ACTIVE
        assert decision.should_alert is True
        assert decision.consecutive_confirmations == 3
        assert decision.reasons == ()

    def test_confirmations_reset_on_failure(
        self,
        market_snapshot,
        funding_snapshot,
        basis_metrics,
        funding_yield_metrics,
        cost_metrics,
        net_yield_metrics,
        quality_report_ok,
        signal_params,
    ):
        # Previous state with 2 confirmations, but now net_annual is negative
        current_state = AlertState(
            symbol_name="BTC_CARRY",
            state=SignalState.WATCHING,
            consecutive_confirmations=2,
            last_alert_ts_ms=None,
            last_signal_started_ts_ms=None,
            last_net_annual=None,
            last_reasons=(),
            updated_at_ms=NOW_MS - 10000,
        )
        # Use original net_yield_metrics with negative values
        evaluation_input = SignalEvaluationInput(
            cycle_id="test-cycle",
            symbol_name="BTC_CARRY",
            now_ms=NOW_MS,
            quality_report=quality_report_ok,
            market_snapshot=market_snapshot,
            funding_snapshot=funding_snapshot,
            basis_metrics=basis_metrics,
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=net_yield_metrics,  # negative values
        )
        decision = evaluate_signal(
            evaluation_input=evaluation_input,
            current_state=current_state,
            params=signal_params,
        )
        assert decision.consecutive_confirmations == 0
        assert decision.state == SignalState.NORMAL


# ======================================================================
# Tests for engine.py — Thresholds
# ======================================================================

class TestEvaluateSignalThresholds:
    def test_net_annual_below_threshold(
        self,
        market_snapshot,
        funding_snapshot,
        basis_metrics,
        funding_yield_metrics,
        cost_metrics,
        net_yield_metrics,
        quality_report_ok,
        signal_params,
    ):
        # Use original net_yield_metrics with negative values
        evaluation_input = SignalEvaluationInput(
            cycle_id="test-cycle",
            symbol_name="BTC_CARRY",
            now_ms=NOW_MS,
            quality_report=quality_report_ok,
            market_snapshot=market_snapshot,
            funding_snapshot=funding_snapshot,
            basis_metrics=basis_metrics,
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=net_yield_metrics,  # negative values
        )
        decision = evaluate_signal(
            evaluation_input=evaluation_input,
            current_state=None,
            params=signal_params,
        )
        assert "net_annual_below_threshold" in decision.reasons
        assert "net_horizon_below_threshold" in decision.reasons
        assert decision.should_alert is False

    def test_funding_rate_below_threshold(
        self,
        market_snapshot,
        funding_snapshot,
        basis_metrics,
        funding_yield_metrics,
        cost_metrics,
        net_yield_metrics,
        quality_report_ok,
        signal_params,
    ):
        # Set funding rate below threshold (0.00001 < 0.00005)
        low_funding_snapshot = replace(
            funding_snapshot,
            effective_funding_rate=Decimal("0.00001"),
        )
        evaluation_input = _make_passing_evaluation_input(
            market_snapshot=market_snapshot,
            funding_snapshot=low_funding_snapshot,
            basis_metrics=basis_metrics,
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=net_yield_metrics,
            quality_report=quality_report_ok,
        )
        decision = evaluate_signal(
            evaluation_input=evaluation_input,
            current_state=None,
            params=signal_params,
        )
        assert "funding_rate_below_threshold" in decision.reasons
        assert decision.should_alert is False

    def test_negative_funding_blocked(
        self,
        market_snapshot,
        funding_snapshot,
        basis_metrics,
        funding_yield_metrics,
        cost_metrics,
        net_yield_metrics,
        quality_report_ok,
        signal_params,
    ):
        negative_funding_snapshot = replace(
            funding_snapshot,
            effective_funding_rate=Decimal("-0.0001"),
        )
        evaluation_input = _make_passing_evaluation_input(
            market_snapshot=market_snapshot,
            funding_snapshot=negative_funding_snapshot,
            basis_metrics=basis_metrics,
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=net_yield_metrics,
            quality_report=quality_report_ok,
        )
        decision = evaluate_signal(
            evaluation_input=evaluation_input,
            current_state=None,
            params=signal_params,
        )
        assert "funding_not_positive" in decision.reasons
        assert decision.should_alert is False

    def test_all_checks_passed_reasons_empty(
        self,
        market_snapshot,
        funding_snapshot,
        basis_metrics,
        funding_yield_metrics,
        cost_metrics,
        net_yield_metrics,
        quality_report_ok,
        signal_params,
    ):
        evaluation_input = _make_passing_evaluation_input(
            market_snapshot=market_snapshot,
            funding_snapshot=funding_snapshot,
            basis_metrics=basis_metrics,
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=net_yield_metrics,
            quality_report=quality_report_ok,
        )
        decision = evaluate_signal(
            evaluation_input=evaluation_input,
            current_state=None,
            params=signal_params,
        )
        assert decision.reasons == ()
        assert len(decision.passed_checks) > 0


# ======================================================================
# Tests for engine.py — Hysteresis
# ======================================================================

class TestEvaluateSignalHysteresis:
    def test_hysteresis_keeps_active_signal(
        self,
        market_snapshot,
        funding_snapshot,
        basis_metrics,
        funding_yield_metrics,
        cost_metrics,
        net_yield_metrics,
        quality_report_ok,
        signal_params,
    ):
        """
        When signal is already active, threshold is reduced by hysteresis.
        net_annual = 0.075 is below min_net_annual (0.08) but above
        min_net_annual - hysteresis (0.07), so active signal should persist.
        """
        current_state = _make_active_state(
            confirmations=5,
            last_alert_ts_ms=NOW_MS - 4000 * 1000,  # Cooldown expired
        )
        # net_annual = 0.075, between 0.07 and 0.08
        hysteresis_net_yield = replace(
            net_yield_metrics,
            net_horizon=Decimal("0.002"),
            net_annual=Decimal("0.075"),
        )
        evaluation_input = SignalEvaluationInput(
            cycle_id="test-cycle",
            symbol_name="BTC_CARRY",
            now_ms=NOW_MS,
            quality_report=quality_report_ok,
            market_snapshot=market_snapshot,
            funding_snapshot=funding_snapshot,
            basis_metrics=basis_metrics,
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=hysteresis_net_yield,
        )
        decision = evaluate_signal(
            evaluation_input=evaluation_input,
            current_state=current_state,
            params=signal_params,
        )
        # Signal should still be active due to hysteresis
        assert "net_annual_below_threshold" not in decision.reasons
        assert decision.consecutive_confirmations == 6

    def test_no_hysteresis_for_new_signal(
        self,
        market_snapshot,
        funding_snapshot,
        basis_metrics,
        funding_yield_metrics,
        cost_metrics,
        net_yield_metrics,
        quality_report_ok,
        signal_params,
    ):
        """
        When signal is not active, threshold is NOT reduced.
        net_annual = 0.075 is below min_net_annual (0.08),
        so new signal should NOT be triggered.
        """
        hysteresis_net_yield = replace(
            net_yield_metrics,
            net_horizon=Decimal("0.002"),
            net_annual=Decimal("0.075"),
        )
        evaluation_input = SignalEvaluationInput(
            cycle_id="test-cycle",
            symbol_name="BTC_CARRY",
            now_ms=NOW_MS,
            quality_report=quality_report_ok,
            market_snapshot=market_snapshot,
            funding_snapshot=funding_snapshot,
            basis_metrics=basis_metrics,
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=hysteresis_net_yield,
        )
        decision = evaluate_signal(
            evaluation_input=evaluation_input,
            current_state=None,  # No previous state
            params=signal_params,
        )
        assert "net_annual_below_threshold" in decision.reasons
        assert decision.should_alert is False


# ======================================================================
# Tests for engine.py — Cooldown
# ======================================================================

class TestEvaluateSignalCooldown:
    def test_cooldown_prevents_alert(
        self,
        market_snapshot,
        funding_snapshot,
        basis_metrics,
        funding_yield_metrics,
        cost_metrics,
        net_yield_metrics,
        quality_report_ok,
        signal_params,
    ):
        # Last alert 1000 seconds ago, cooldown 3600 seconds
        current_state = _make_active_state(
            confirmations=5,
            last_alert_ts_ms=NOW_MS - 1000 * 1000,
        )
        evaluation_input = _make_passing_evaluation_input(
            market_snapshot=market_snapshot,
            funding_snapshot=funding_snapshot,
            basis_metrics=basis_metrics,
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=net_yield_metrics,
            quality_report=quality_report_ok,
        )
        decision = evaluate_signal(
            evaluation_input=evaluation_input,
            current_state=current_state,
            params=signal_params,
        )
        assert decision.state == SignalState.COOLDOWN
        assert decision.should_alert is False
        assert decision.cooldown_remaining_sec > 0

    def test_cooldown_expired_allows_alert(
        self,
        market_snapshot,
        funding_snapshot,
        basis_metrics,
        funding_yield_metrics,
        cost_metrics,
        net_yield_metrics,
        quality_report_ok,
        signal_params,
    ):
        # Last alert 4000 seconds ago, cooldown 3600 seconds (expired)
        # But was_active=True and repeat_alert_while_active=False
        # so should_alert should be False
        current_state = _make_active_state(
            confirmations=5,
            last_alert_ts_ms=NOW_MS - 4000 * 1000,
        )
        evaluation_input = _make_passing_evaluation_input(
            market_snapshot=market_snapshot,
            funding_snapshot=funding_snapshot,
            basis_metrics=basis_metrics,
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=net_yield_metrics,
            quality_report=quality_report_ok,
        )
        decision = evaluate_signal(
            evaluation_input=evaluation_input,
            current_state=current_state,
            params=signal_params,
        )
        assert decision.cooldown_remaining_sec == 0
        # was_active=True, repeat_alert_while_active=False -> no repeat
        assert decision.should_alert is False


# ======================================================================
# Tests for engine.py — Funding Suppression
# ======================================================================

class TestEvaluateSignalFundingSuppression:
    def test_funding_imminent(
        self,
        market_snapshot,
        funding_snapshot,
        basis_metrics,
        funding_yield_metrics,
        cost_metrics,
        net_yield_metrics,
        quality_report_ok,
        signal_params,
    ):
        # Next funding in 5 minutes (within 10-minute suppression window)
        funding_imminent = replace(
            funding_snapshot,
            next_funding_timestamp_ms=NOW_MS + 5 * 60 * 1000,
        )
        evaluation_input = _make_passing_evaluation_input(
            market_snapshot=market_snapshot,
            funding_snapshot=funding_imminent,
            basis_metrics=basis_metrics,
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=net_yield_metrics,
            quality_report=quality_report_ok,
        )
        decision = evaluate_signal(
            evaluation_input=evaluation_input,
            current_state=None,
            params=signal_params,
        )
        assert "funding_imminent" in decision.reasons
        assert decision.should_alert is False

    def test_funding_just_passed(
        self,
        market_snapshot,
        funding_snapshot,
        basis_metrics,
        funding_yield_metrics,
        cost_metrics,
        net_yield_metrics,
        quality_report_ok,
        signal_params,
    ):
        # Funding passed 2 minutes ago (within 10-minute suppression window)
        funding_passed = replace(
            funding_snapshot,
            next_funding_timestamp_ms=NOW_MS - 2 * 60 * 1000,
        )
        evaluation_input = _make_passing_evaluation_input(
            market_snapshot=market_snapshot,
            funding_snapshot=funding_passed,
            basis_metrics=basis_metrics,
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=net_yield_metrics,
            quality_report=quality_report_ok,
        )
        decision = evaluate_signal(
            evaluation_input=evaluation_input,
            current_state=None,
            params=signal_params,
        )
        assert "funding_just_passed" in decision.reasons
        assert decision.should_alert is False

    def test_funding_far_away_no_suppression(
        self,
        market_snapshot,
        funding_snapshot,
        basis_metrics,
        funding_yield_metrics,
        cost_metrics,
        net_yield_metrics,
        quality_report_ok,
        signal_params,
    ):
        # Next funding in 4 hours (outside 10-minute suppression window)
        funding_far = replace(
            funding_snapshot,
            next_funding_timestamp_ms=NOW_MS + 4 * 60 * 60 * 1000,
        )
        evaluation_input = _make_passing_evaluation_input(
            market_snapshot=market_snapshot,
            funding_snapshot=funding_far,
            basis_metrics=basis_metrics,
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=net_yield_metrics,
            quality_report=quality_report_ok,
        )
        decision = evaluate_signal(
            evaluation_input=evaluation_input,
            current_state=None,
            params=signal_params,
        )
        assert "funding_imminent" not in decision.reasons
        assert "funding_just_passed" not in decision.reasons
        assert "funding_window_ok" in decision.passed_checks

    def test_no_next_funding_timestamp_no_suppression(
        self,
        market_snapshot,
        funding_snapshot,
        basis_metrics,
        funding_yield_metrics,
        cost_metrics,
        net_yield_metrics,
        quality_report_ok,
        signal_params,
    ):
        # No next funding timestamp -> no suppression
        funding_no_ts = replace(
            funding_snapshot,
            next_funding_timestamp_ms=None,
        )
        evaluation_input = _make_passing_evaluation_input(
            market_snapshot=market_snapshot,
            funding_snapshot=funding_no_ts,
            basis_metrics=basis_metrics,
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=net_yield_metrics,
            quality_report=quality_report_ok,
        )
        decision = evaluate_signal(
            evaluation_input=evaluation_input,
            current_state=None,
            params=signal_params,
        )
        assert "funding_imminent" not in decision.reasons
        assert "funding_just_passed" not in decision.reasons
        assert "funding_window_ok" in decision.passed_checks