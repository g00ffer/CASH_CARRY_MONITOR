from __future__ import annotations

import re
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ENV_NAME_REGEX = re.compile(r"^[A-Z_][A-Z0-9_]*$")
SYMBOL_NAME_REGEX = re.compile(r"^[A-Z0-9_]+$")


class StrictModel(BaseModel):
    """
    Base model that forbids extra fields.
    This protects against typos in YAML config.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
    )


# ---------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------
class MetaSettings(StrictModel):
    config_version: str = Field(
        default="1.0.0",
        min_length=1,
    )
    environment: Literal["dev", "dry_run", "prod"] = "dev"
    calculation_version: str = Field(
        default="stage1-conservative-v1",
        min_length=1,
    )


# ---------------------------------------------------------------------
# Exchange
# ---------------------------------------------------------------------
class ExchangeSettings(StrictModel):
    id: Literal["binance", "bybit"] = "binance"
    sandbox: bool = False
    timeout_ms: int = Field(default=5000, gt=0)
    retries: int = Field(default=3, ge=0)
    retry_backoff_ms: int = Field(default=500, ge=0)
    rate_limit: bool = True
    max_weight_per_minute: Optional[int] = Field(default=None, gt=0)


# ---------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------
class PollingSettings(StrictModel):
    market_interval_ms: int = Field(default=10000, gt=0)
    funding_interval_ms: int = Field(default=60000, gt=0)
    heartbeat_interval_ms: int = Field(default=3600000, gt=0)


# ---------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------
class QualitySettings(StrictModel):
    max_snapshot_age_ms: int = Field(default=15000, gt=0)
    max_spot_perp_time_diff_ms: int = Field(default=3000, ge=0)
    max_spread_bps: float = Field(default=10.0, ge=0)
    min_quote_volume_24h: float = Field(default=1_000_000, ge=0)
    require_valid_funding_interval: bool = True
    require_predicted_funding: bool = False
    max_price_jump_pct: float = Field(default=5.0, ge=0)


# ---------------------------------------------------------------------
# Fees
# Values in percent.
# Example: 0.10 means 0.10% = 0.001 decimal.
# ---------------------------------------------------------------------
class FeesSettings(StrictModel):
    execution_mode: Literal["taker", "maker"] = "taker"
    spot_taker_fee_pct: float = Field(default=0.10, ge=0, le=5)
    spot_maker_fee_pct: float = Field(default=0.075, ge=0, le=5)
    perp_taker_fee_pct: float = Field(default=0.05, ge=0, le=5)
    perp_maker_fee_pct: float = Field(default=0.02, ge=0, le=5)
    bnb_discount: bool = False


# ---------------------------------------------------------------------
# Costs
# Slippage and spread buffer values are in basis points.
# Example: 2 means 2 bp = 0.02% = 0.0002 decimal.
# ---------------------------------------------------------------------
class CostsSettings(StrictModel):
    slippage_entry_bps: float = Field(default=2.0, ge=0)
    slippage_exit_bps: float = Field(default=2.0, ge=0)
    spread_buffer_bps: float = Field(default=2.0, ge=0)


# ---------------------------------------------------------------------
# Yield model
# ---------------------------------------------------------------------
class YieldModelSettings(StrictModel):
    holding_hours: float = Field(default=168, gt=0)
    cost_amortization_hours: float = Field(default=168, gt=0)
    min_cost_amortization_hours: float = Field(default=24, gt=0)
    include_basis_convergence: bool = False
    basis_haircut: float = Field(default=0.0, ge=0, le=1)
    expected_exit_basis_mode: Literal[
        "entry",
        "zero",
        "historical_median",
    ] = "entry"
    use_predicted_funding: bool = True
    default_funding_interval_hours: float = Field(default=8, gt=0)
    borrow_rate_annual_pct: float = Field(default=0.0, ge=0)
    opportunity_cost_annual_pct: float = Field(default=0.0, ge=0)
    yield_base: Literal["notional", "equity"] = "notional"

    @model_validator(mode="after")
    def _validate_yield_model(self) -> YieldModelSettings:
        if self.holding_hours < self.min_cost_amortization_hours:
            raise ValueError(
                "yield_model.holding_hours cannot be less than "
                "yield_model.min_cost_amortization_hours"
            )
        if self.cost_amortization_hours < self.min_cost_amortization_hours:
            raise ValueError(
                "yield_model.cost_amortization_hours cannot be less than "
                "yield_model.min_cost_amortization_hours"
            )
        if not self.include_basis_convergence and self.basis_haircut != 0.0:
            raise ValueError(
                "yield_model.basis_haircut must be 0 when "
                "include_basis_convergence is false"
            )
        return self


# ---------------------------------------------------------------------
# Margin / equity
# Optional for Stage 1, useful for future risk metrics.
# ---------------------------------------------------------------------
class MarginSettings(StrictModel):
    perp_initial_margin_fraction: float = Field(default=0.10, ge=0, le=1)
    safety_buffer_fraction: float = Field(default=0.05, ge=0, le=1)
    spot_collateral_haircut: float = Field(default=0.0, ge=0, le=1)
    use_portfolio_margin: bool = False


# ---------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------
class SignalSettings(StrictModel):
    min_net_annual_pct: float = Field(default=8.0, ge=0)
    min_net_horizon_pct: float = Field(default=0.10, ge=0)
    # Percent per funding interval.
    # Example: 0.005 means 0.005% = 0.00005 decimal.
    min_funding_rate_pct_per_interval: float = Field(default=0.005, ge=0)
    require_positive_funding: bool = True
    min_consecutive_confirmations: int = Field(default=3, ge=1)
    cooldown_sec: int = Field(default=3600, ge=0)
    hysteresis_pct: float = Field(default=1.0, ge=0)
    max_snapshot_age_ms: int = Field(default=15000, gt=0)
    suppress_minutes_before_funding: int = Field(default=10, ge=0)
    suppress_minutes_after_funding: int = Field(default=10, ge=0)

    # --- Funding stability protection (Stage 1+, off by default) ---
    funding_safety_multiplier: float = Field(default=1.0, ge=0)
    funding_lookback_minutes: int = Field(default=20, gt=0)
    require_stable_average: bool = False
    min_stable_confirmations: int = Field(default=0, ge=0)
    block_entry_if_dropping_fast: bool = False
    max_funding_drop_pct: float = Field(default=35.0, ge=0, le=100)
    exit_funding_threshold_pct: float = Field(default=0.008, ge=0)
    exit_minutes_before_funding: int = Field(default=0, ge=0)


# ---------------------------------------------------------------------
# Universe selector (dynamic pool) — Stage 1+
# ---------------------------------------------------------------------
class UniverseScoreWeights(StrictModel):
    funding: float = Field(default=0.70, ge=0, le=1)
    liquidity: float = Field(default=0.30, ge=0, le=1)

    @model_validator(mode="after")
    def _validate_weights(self) -> "UniverseScoreWeights":
        if abs(self.funding + self.liquidity - 1.0) > 1e-9:
            raise ValueError(
                "universe.score_weights: funding + liquidity must equal 1.0"
            )
        return self


class UniverseSettings(StrictModel):
    """
    Dynamic instrument pool (Universe Refresh).
    Disabled by default: static symbols.yaml remains authoritative.
    """
    enabled: bool = False
    refresh_interval_hours: int = Field(default=8, gt=0)
    max_active_symbols: int = Field(default=12, gt=0)
    min_predicted_funding_pct_per_interval: float = Field(default=0.03, ge=0)
    min_quote_volume_24h: float = Field(default=5_000_000, ge=0)
    min_open_interest_usd: Optional[float] = Field(default=None, ge=0)
    max_spread_bps: float = Field(default=15.0, ge=0)
    always_include: List[str] = Field(default_factory=list)
    score_weights: UniverseScoreWeights = Field(
        default_factory=UniverseScoreWeights,
    )
    candidate_universe_size: int = Field(default=100, gt=0)

# ---------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------
class TelegramSettings(StrictModel):
    enabled: bool = True
    # These are environment variable names, not secrets.
    token_env: str = Field(default="TELEGRAM_BOT_TOKEN", min_length=1)
    chat_id_env: str = Field(default="TELEGRAM_CHAT_ID", min_length=1)
    max_messages_per_hour: int = Field(default=20, gt=0)
    retry_attempts: int = Field(default=3, ge=0)
    timeout_ms: int = Field(default=5000, gt=0)
    send_heartbeat: bool = True
    send_data_errors: bool = True
    send_signal: bool = True

    @field_validator("token_env", "chat_id_env")
    @classmethod
    def _validate_env_name(cls, value: str) -> str:
        if not ENV_NAME_REGEX.fullmatch(value):
            raise ValueError(
                "environment variable name must match pattern "
                "[A-Z_][A-Z0-9_]*"
            )
        return value


# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------
class LoggingSettings(StrictModel):
    level: Literal[
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    ] = "INFO"
    format: Literal["json", "text"] = "json"
    file_path: str = Field(default="logs/monitor.log", min_length=1)
    rotation: Literal["daily", "hourly", "none"] = "daily"
    retention_days: int = Field(default=30, ge=1)
    console: bool = True


# ---------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------
class StorageSettings(StrictModel):
    mode: Literal["sqlite", "jsonl"] = "sqlite"
    sqlite_path: str = Field(default="data/monitor.sqlite", min_length=1)
    save_raw_responses: bool = True
    retention_days: int = Field(default=90, ge=1)
    save_snapshots: bool = True
    save_alerts: bool = True


# ---------------------------------------------------------------------
# Symbol
# ---------------------------------------------------------------------
class SymbolConfig(StrictModel):
    name: str
    enabled: bool = True
    exchange: Literal["binance", "bybit"] = "binance"
    base: str
    quote: str
    spot_symbol: str
    perp_symbol: str
    direction: Literal["long_spot_short_perp"] = "long_spot_short_perp"
    notional_usd: float = Field(default=10000, gt=0)
    tags: List[str] = Field(default_factory=list)

    @field_validator("name", "base", "quote", "spot_symbol", "perp_symbol")
    @classmethod
    def _strip_and_validate(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("field cannot be empty")
        return value

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        value = value.upper()
        if not SYMBOL_NAME_REGEX.fullmatch(value):
            raise ValueError(
                "symbol name must contain only uppercase letters, digits, "
                "and underscores"
            )
        return value

    @field_validator("base", "quote")
    @classmethod
    def _validate_asset(cls, value: str) -> str:
        value = value.upper()
        if not value.isalpha():
            raise ValueError("asset must contain letters only")
        return value

    @field_validator("spot_symbol", "perp_symbol")
    @classmethod
    def _validate_market_symbol(cls, value: str) -> str:
        value = value.upper()
        if "/" not in value:
            raise ValueError("market symbol must contain base/quote format")
        return value


# ---------------------------------------------------------------------
# Symbols file
# ---------------------------------------------------------------------
class SymbolsFile(StrictModel):
    symbols: List[SymbolConfig]


# ---------------------------------------------------------------------
# Root settings
# ---------------------------------------------------------------------
class Settings(StrictModel):
    meta: MetaSettings = Field(default_factory=MetaSettings)
    exchange: ExchangeSettings = Field(default_factory=ExchangeSettings)
    polling: PollingSettings = Field(default_factory=PollingSettings)
    quality: QualitySettings = Field(default_factory=QualitySettings)
    fees: FeesSettings = Field(default_factory=FeesSettings)
    costs: CostsSettings = Field(default_factory=CostsSettings)
    yield_model: YieldModelSettings = Field(default_factory=YieldModelSettings)
    margin: MarginSettings = Field(default_factory=MarginSettings)
    signals: SignalSettings = Field(default_factory=SignalSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    symbols: List[SymbolConfig] = Field(default_factory=list)
    universe: UniverseSettings = Field(default_factory=UniverseSettings)

    @model_validator(mode="after")
    def _validate_settings(self) -> Settings:
        if not self.symbols:
            raise ValueError("symbols list cannot be empty")
        if self.signals.max_snapshot_age_ms > self.quality.max_snapshot_age_ms:
            raise ValueError(
                "signals.max_snapshot_age_ms should not be greater than "
                "quality.max_snapshot_age_ms"
            )
        names = [symbol.name for symbol in self.symbols]
        if len(names) != len(set(names)):
            raise ValueError("symbol names must be unique")
        market_pairs = [
            (symbol.exchange, symbol.spot_symbol, symbol.perp_symbol)
            for symbol in self.symbols
        ]
        if len(market_pairs) != len(set(market_pairs)):
            raise ValueError("duplicate spot/perp symbol configuration found")
        for symbol in self.symbols:
            expected_spot_symbol = f"{symbol.base}/{symbol.quote}"
            expected_perp_symbol = f"{symbol.base}/{symbol.quote}:{symbol.quote}"
            if symbol.spot_symbol != expected_spot_symbol:
                raise ValueError(
                    f"symbol {symbol.name}: spot_symbol must be "
                    f"{expected_spot_symbol}, got {symbol.spot_symbol}"
                )
            if symbol.perp_symbol != expected_perp_symbol:
                raise ValueError(
                    f"symbol {symbol.name}: perp_symbol must be "
                    f"{expected_perp_symbol}, got {symbol.perp_symbol}"
                )
        return self