"""Tests for monitor.config.schema (Pydantic validation)"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from monitor.config.schema import (
    CostsSettings,
    ExchangeSettings,
    FeesSettings,
    LoggingSettings,
    MetaSettings,
    PollingSettings,
    QualitySettings,
    SignalSettings,
    StorageSettings,
    SymbolConfig,
    SymbolsFile,
    TelegramSettings,
    YieldModelSettings,
)


class TestMetaSettings:
    def test_defaults(self):
        meta = MetaSettings()
        assert meta.config_version == "1.0.0"
        assert meta.environment == "dev"

    def test_invalid_environment(self):
        with pytest.raises(ValidationError):
            MetaSettings(environment="invalid")


class TestExchangeSettings:
    def test_defaults(self):
        ex = ExchangeSettings()
        assert ex.id == "binance"
        assert ex.timeout_ms == 5000

    def test_zero_timeout_raises(self):
        with pytest.raises(ValidationError):
            ExchangeSettings(timeout_ms=0)

    def test_negative_retries_raises(self):
        with pytest.raises(ValidationError):
            ExchangeSettings(retries=-1)


class TestPollingSettings:
    def test_defaults(self):
        p = PollingSettings()
        assert p.market_interval_ms == 10000

    def test_zero_interval_raises(self):
        with pytest.raises(ValidationError):
            PollingSettings(market_interval_ms=0)


class TestQualitySettings:
    def test_defaults(self):
        q = QualitySettings()
        assert q.max_snapshot_age_ms == 15000

    def test_negative_spread_raises(self):
        with pytest.raises(ValidationError):
            QualitySettings(max_spread_bps=-1.0)


class TestFeesSettings:
    def test_defaults(self):
        f = FeesSettings()
        assert f.execution_mode == "taker"

    def test_fee_too_high_raises(self):
        with pytest.raises(ValidationError):
            FeesSettings(spot_taker_fee_pct=10.0)

    def test_negative_fee_raises(self):
        with pytest.raises(ValidationError):
            FeesSettings(spot_taker_fee_pct=-0.1)


class TestCostsSettings:
    def test_defaults(self):
        c = CostsSettings()
        assert c.slippage_entry_bps == 2.0

    def test_negative_slippage_raises(self):
        with pytest.raises(ValidationError):
            CostsSettings(slippage_entry_bps=-1.0)


class TestYieldModelSettings:
    def test_defaults(self):
        y = YieldModelSettings()
        assert y.holding_hours == 168
        assert y.include_basis_convergence is False
        assert y.basis_haircut == 0.0

    def test_holding_below_min_amortization_raises(self):
        with pytest.raises(ValidationError, match="holding_hours cannot be less"):
            YieldModelSettings(
                holding_hours=10,
                min_cost_amortization_hours=24,
            )

    def test_haircut_nonzero_without_convergence_raises(self):
        with pytest.raises(ValidationError, match="basis_haircut must be 0"):
            YieldModelSettings(
                include_basis_convergence=False,
                basis_haircut=0.5,
            )

    def test_haircut_with_convergence_ok(self):
        y = YieldModelSettings(
            include_basis_convergence=True,
            basis_haircut=0.5,
        )
        assert y.basis_haircut == 0.5

    def test_haircut_above_one_raises(self):
        with pytest.raises(ValidationError):
            YieldModelSettings(
                include_basis_convergence=True,
                basis_haircut=1.5,
            )


class TestSignalSettings:
    def test_defaults(self):
        s = SignalSettings()
        assert s.min_net_annual_pct == 8.0
        assert s.min_consecutive_confirmations == 3

    def test_zero_confirmations_raises(self):
        with pytest.raises(ValidationError):
            SignalSettings(min_consecutive_confirmations=0)

    def test_negative_cooldown_raises(self):
        with pytest.raises(ValidationError):
            SignalSettings(cooldown_sec=-1)


class TestTelegramSettings:
    def test_defaults(self):
        t = TelegramSettings()
        assert t.token_env == "TELEGRAM_BOT_TOKEN"

    def test_invalid_env_name_raises(self):
        with pytest.raises(ValidationError, match="environment variable name"):
            TelegramSettings(token_env="invalid-name")

    def test_lowercase_env_name_raises(self):
        with pytest.raises(ValidationError):
            TelegramSettings(chat_id_env="telegram_chat_id")


class TestLoggingSettings:
    def test_defaults(self):
        l = LoggingSettings()
        assert l.level == "INFO"
        assert l.format == "json"

    def test_invalid_format_raises(self):
        with pytest.raises(ValidationError):
            LoggingSettings(format="xml")


class TestStorageSettings:
    def test_defaults(self):
        s = StorageSettings()
        assert s.mode == "sqlite"

    def test_zero_retention_raises(self):
        with pytest.raises(ValidationError):
            StorageSettings(retention_days=0)


class TestSymbolConfig:
    def test_valid(self):
        s = SymbolConfig(
            name="BTC_CARRY",
            base="BTC",
            quote="USDT",
            spot_symbol="BTC/USDT",
            perp_symbol="BTC/USDT:USDT",
        )
        assert s.name == "BTC_CARRY"
        assert s.enabled is True

    def test_lowercase_name_uppercased(self):
        s = SymbolConfig(
            name="btc_carry",
            base="BTC",
            quote="USDT",
            spot_symbol="BTC/USDT",
            perp_symbol="BTC/USDT:USDT",
        )
        assert s.name == "BTC_CARRY"

    def test_invalid_name_chars_raises(self):
        with pytest.raises(ValidationError, match="symbol name"):
            SymbolConfig(
                name="BTC-CARRY!",
                base="BTC",
                quote="USDT",
                spot_symbol="BTC/USDT",
                perp_symbol="BTC/USDT:USDT",
            )

    def test_spot_symbol_without_slash_raises(self):
        with pytest.raises(ValidationError, match="base/quote format"):
            SymbolConfig(
                name="BTC_CARRY",
                base="BTC",
                quote="USDT",
                spot_symbol="BTCUSDT",
                perp_symbol="BTC/USDT:USDT",
            )

    def test_numeric_base_raises(self):
        with pytest.raises(ValidationError, match="letters only"):
            SymbolConfig(
                name="TEST",
                base="123",
                quote="USDT",
                spot_symbol="123/USDT",
                perp_symbol="123/USDT:USDT",
            )