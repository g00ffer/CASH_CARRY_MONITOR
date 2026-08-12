"""Tests for monitor.config.loader"""
from __future__ import annotations

import pytest

from monitor.config.loader import (
    ConfigError,
    _read_yaml_file,
    _validate_runtime_secrets,
    load_settings,
)
from monitor.config.schema import Settings, TelegramSettings

VALID_SETTINGS = """
meta:
  config_version: "1.0.0"
  environment: dev
exchange:
  id: binance
polling:
  market_interval_ms: 10000
quality:
  max_snapshot_age_ms: 15000
fees:
  spot_taker_fee_pct: 0.10
costs:
  slippage_entry_bps: 2.0
yield_model:
  holding_hours: 168
signals:
  min_net_annual_pct: 8.0
telegram:
  enabled: true
  token_env: TELEGRAM_BOT_TOKEN
  chat_id_env: TELEGRAM_CHAT_ID
logging:
  level: INFO
storage:
  mode: sqlite
  sqlite_path: data/monitor.sqlite
"""

VALID_SYMBOLS = """
symbols:
  - name: BTC_CARRY
    enabled: true
    exchange: binance
    base: BTC
    quote: USDT
    spot_symbol: BTC/USDT
    perp_symbol: BTC/USDT:USDT
    direction: long_spot_short_perp
    notional_usd: 10000
"""


@pytest.fixture
def config_files(tmp_path):
    settings_file = tmp_path / "settings.yaml"
    symbols_file = tmp_path / "symbols.yaml"
    settings_file.write_text(VALID_SETTINGS)
    symbols_file.write_text(VALID_SYMBOLS)
    return settings_file, symbols_file


@pytest.fixture
def telegram_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456789")


class TestLoadSettings:
    def test_successful_load(self, config_files, telegram_env):
        settings_file, symbols_file = config_files
        settings = load_settings(settings_file, symbols_file)
        assert settings.meta.config_version == "1.0.0"
        assert len(settings.symbols) == 1
        assert settings.symbols[0].name == "BTC_CARRY"

    def test_missing_settings_file(self, tmp_path, telegram_env):
        symbols_file = tmp_path / "symbols.yaml"
        symbols_file.write_text(VALID_SYMBOLS)
        with pytest.raises(ConfigError, match="config file not found"):
            load_settings(tmp_path / "nonexistent.yaml", symbols_file)

    def test_missing_symbols_file(self, tmp_path, telegram_env):
        settings_file = tmp_path / "settings.yaml"
        settings_file.write_text(VALID_SETTINGS)
        with pytest.raises(ConfigError, match="config file not found"):
            load_settings(settings_file, tmp_path / "nonexistent.yaml")

    def test_invalid_yaml(self, tmp_path, telegram_env):
        settings_file = tmp_path / "settings.yaml"
        settings_file.write_text("{{invalid yaml")
        symbols_file = tmp_path / "symbols.yaml"
        symbols_file.write_text(VALID_SYMBOLS)
        with pytest.raises(ConfigError, match="failed to parse YAML"):
            load_settings(settings_file, symbols_file)

    def test_symbols_in_settings_raises(self, tmp_path, telegram_env):
        settings_with_symbols = VALID_SETTINGS + "\nsymbols: []"
        settings_file = tmp_path / "settings.yaml"
        settings_file.write_text(settings_with_symbols)
        symbols_file = tmp_path / "symbols.yaml"
        symbols_file.write_text(VALID_SYMBOLS)
        with pytest.raises(ConfigError, match="symbols must be defined only"):
            load_settings(settings_file, symbols_file)

    def test_telegram_disabled_no_secrets_needed(self, tmp_path):
        settings_no_tg = VALID_SETTINGS.replace("enabled: true", "enabled: false")
        settings_file = tmp_path / "settings.yaml"
        settings_file.write_text(settings_no_tg)
        symbols_file = tmp_path / "symbols.yaml"
        symbols_file.write_text(VALID_SYMBOLS)
        settings = load_settings(settings_file, symbols_file)
        assert settings.telegram.enabled is False

    def test_telegram_enabled_missing_secrets_raises(self, config_files, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        # Предотвращаем чтение реального .env файла
        monkeypatch.setattr("monitor.config.loader.load_dotenv", lambda *args, **kwargs: None)
        settings_file, symbols_file = config_files
        with pytest.raises(ConfigError, match="environment variables are missing"):
            load_settings(settings_file, symbols_file)


class TestReadYamlFile:
    def test_valid_file(self, tmp_path):
        f = tmp_path / "test.yaml"
        f.write_text("key: value")
        result = _read_yaml_file(f)
        assert result == {"key": "value"}

    def test_empty_file(self, tmp_path):
        f = tmp_path / "test.yaml"
        f.write_text("")
        result = _read_yaml_file(f)
        assert result == {}

    def test_missing_file(self, tmp_path):
        with pytest.raises(ConfigError, match="config file not found"):
            _read_yaml_file(tmp_path / "nonexistent.yaml")


class TestValidateRuntimeSecrets:
    def test_telegram_disabled_skips(self):
        # Создаём dummy-символ, чтобы список не был пустым
        dummy_symbol = SymbolConfig(
            name="BTC_CARRY",
            base="BTC",
            quote="USDT",
            spot_symbol="BTC/USDT",
            perp_symbol="BTC/USDT:USDT",
        )
        settings = Settings(
            telegram=TelegramSettings(enabled=False),
            symbols=[dummy_symbol],  # ← Добавили символ
        )
        # Should not raise even without env vars
        _validate_runtime_secrets(settings)