"""Tests for SignalEngineParams validation (remaining engine.py branches)"""
from __future__ import annotations

from decimal import Decimal

import pytest

from monitor.signals import SignalEngineParams


def _valid_params(**overrides) -> SignalEngineParams:
    defaults = dict(
        min_net_annual=Decimal("0.08"),
        min_net_horizon=Decimal("0.001"),
        min_funding_rate_per_interval=Decimal("0.00005"),
        require_positive_funding=True,
        require_predicted_funding=False,
        min_consecutive_confirmations=3,
        cooldown_sec=3600,
        hysteresis=Decimal("0.01"),
        max_snapshot_age_ms=15000,
        max_spread=Decimal("0.001"),
        suppress_minutes_before_funding=10,
        suppress_minutes_after_funding=10,
    )
    defaults.update(overrides)
    return SignalEngineParams(**defaults)


class TestSignalEngineParamsValidation:
    def test_valid_params(self):
        params = _valid_params()
        assert params.min_net_annual == Decimal("0.08")

    def test_zero_confirmations_raises(self):
        with pytest.raises(ValueError, match="min_consecutive_confirmations must be >= 1"):
            _valid_params(min_consecutive_confirmations=0)

    def test_negative_cooldown_raises(self):
        with pytest.raises(ValueError, match="cooldown_sec must be >= 0"):
            _valid_params(cooldown_sec=-1)

    def test_zero_snapshot_age_raises(self):
        with pytest.raises(ValueError, match="max_snapshot_age_ms must be > 0"):
            _valid_params(max_snapshot_age_ms=0)

    def test_negative_suppress_before_raises(self):
        with pytest.raises(ValueError, match="suppress_minutes_before_funding must be >= 0"):
            _valid_params(suppress_minutes_before_funding=-1)

    def test_negative_suppress_after_raises(self):
        with pytest.raises(ValueError, match="suppress_minutes_after_funding must be >= 0"):
            _valid_params(suppress_minutes_after_funding=-1)

    def test_negative_min_net_annual_raises(self):
        with pytest.raises(ValueError, match="min_net_annual must be >= 0"):
            _valid_params(min_net_annual=Decimal("-0.01"))

    def test_negative_min_net_horizon_raises(self):
        with pytest.raises(ValueError, match="min_net_horizon must be >= 0"):
            _valid_params(min_net_horizon=Decimal("-0.01"))

    def test_negative_min_funding_rate_raises(self):
        with pytest.raises(ValueError, match="min_funding_rate_per_interval must be >= 0"):
            _valid_params(min_funding_rate_per_interval=Decimal("-0.001"))

    def test_negative_hysteresis_raises(self):
        with pytest.raises(ValueError, match="hysteresis must be >= 0"):
            _valid_params(hysteresis=Decimal("-0.01"))

    def test_negative_max_spread_raises(self):
        with pytest.raises(ValueError, match="max_spread must be >= 0"):
            _valid_params(max_spread=Decimal("-0.001"))

    def test_repeat_alert_while_active_default_false(self):
        params = _valid_params()
        assert params.repeat_alert_while_active is False

    def test_repeat_alert_while_active_true(self):
        params = _valid_params(repeat_alert_while_active=True)
        assert params.repeat_alert_while_active is True