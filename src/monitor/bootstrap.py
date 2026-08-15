from __future__ import annotations

import os

from monitor.calculators import (
    calc_effective_cost_amortization_hours,
    calc_effective_holding_hours,
)
from monitor.config import Settings, load_settings
from monitor.data import QualityParams
from monitor.domain import (
    CarryInstrument,
    ExpectedExitBasisMode,
    StrategyDirection,
    YieldBase,
)
from monitor.exchanges import BinanceClient, BybitClient
from monitor.notifications import (
    TelegramNotifier,
    TelegramNotifierParams,
)
from monitor.persistence import (
    AlertRepository,
    Database,
    DatabaseParams,
    LoggingParams,
    SnapshotRepository,
    get_logger,
    log_event,
    setup_logging,
)
from monitor.signals import (
    InMemorySignalStateStore,
    SignalEngineParams,
)
from monitor.utils import (
    bps_to_decimal,
    pct_to_decimal,
    to_decimal,
)
from .app import (
    CostParams,
    MonitorApp,
    YieldParams,
)


# ---------------------------------------------------------------------
# Config -> domain mappers
# ---------------------------------------------------------------------


def _instrument_from_symbol_config(symbol) -> CarryInstrument:
    return CarryInstrument(
        name=symbol.name,
        exchange=symbol.exchange,
        base=symbol.base,
        quote=symbol.quote,
        spot_symbol=symbol.spot_symbol,
        perp_symbol=symbol.perp_symbol,
        direction=StrategyDirection(symbol.direction),
        notional_usd=to_decimal(symbol.notional_usd),
        enabled=symbol.enabled,
    )


def _quality_params_from_settings(settings: Settings) -> QualityParams:
    return QualityParams(
        max_snapshot_age_ms=settings.quality.max_snapshot_age_ms,
        max_spot_perp_time_diff_ms=(
            settings.quality.max_spot_perp_time_diff_ms
        ),
        max_spread=bps_to_decimal(settings.quality.max_spread_bps),
        min_quote_volume_24h=to_decimal(
            settings.quality.min_quote_volume_24h,
        ),
        require_valid_funding_interval=(
            settings.quality.require_valid_funding_interval
        ),
        require_predicted_funding=(
            settings.quality.require_predicted_funding
        ),
        max_price_jump_pct=pct_to_decimal(
            settings.quality.max_price_jump_pct,
        ),
        future_timestamp_tolerance_ms=5000,
    )


def _signal_params_from_settings(settings: Settings) -> SignalEngineParams:
    return SignalEngineParams(
        min_net_annual=pct_to_decimal(
            settings.signals.min_net_annual_pct,
        ),
        min_net_horizon=pct_to_decimal(
            settings.signals.min_net_horizon_pct,
        ),
        min_funding_rate_per_interval=pct_to_decimal(
            settings.signals.min_funding_rate_pct_per_interval,
        ),
        require_positive_funding=(
            settings.signals.require_positive_funding
        ),
        require_predicted_funding=(
            settings.quality.require_predicted_funding
        ),
        min_consecutive_confirmations=(
            settings.signals.min_consecutive_confirmations
        ),
        cooldown_sec=settings.signals.cooldown_sec,
        hysteresis=pct_to_decimal(settings.signals.hysteresis_pct),
        max_snapshot_age_ms=settings.signals.max_snapshot_age_ms,
        max_spread=bps_to_decimal(settings.quality.max_spread_bps),
        suppress_minutes_before_funding=(
            settings.signals.suppress_minutes_before_funding
        ),
        suppress_minutes_after_funding=(
            settings.signals.suppress_minutes_after_funding
        ),
        repeat_alert_while_active=False,
    )


def _cost_params_from_settings(settings: Settings) -> CostParams:
    if settings.fees.execution_mode == "taker":
        spot_fee = pct_to_decimal(settings.fees.spot_taker_fee_pct)
        perp_fee = pct_to_decimal(settings.fees.perp_taker_fee_pct)
    else:
        spot_fee = pct_to_decimal(settings.fees.spot_maker_fee_pct)
        perp_fee = pct_to_decimal(settings.fees.perp_maker_fee_pct)
    return CostParams(
        spot_fee=spot_fee,
        perp_fee=perp_fee,
        slippage_entry=bps_to_decimal(settings.costs.slippage_entry_bps),
        slippage_exit=bps_to_decimal(settings.costs.slippage_exit_bps),
        spread_buffer=bps_to_decimal(settings.costs.spread_buffer_bps),
        borrow_rate_annual=pct_to_decimal(
            settings.yield_model.borrow_rate_annual_pct,
        ),
        opportunity_cost_annual=pct_to_decimal(
            settings.yield_model.opportunity_cost_annual_pct,
        ),
    )


def _yield_params_from_settings(settings: Settings) -> YieldParams:
    holding_hours = to_decimal(settings.yield_model.holding_hours)
    cost_amortization_hours = to_decimal(
        settings.yield_model.cost_amortization_hours,
    )
    min_cost_amortization_hours = to_decimal(
        settings.yield_model.min_cost_amortization_hours,
    )
    effective_holding_hours = calc_effective_holding_hours(
        holding_hours=holding_hours,
        min_cost_amortization_hours=min_cost_amortization_hours,
    )
    effective_cost_amortization_hours = (
        calc_effective_cost_amortization_hours(
            holding_hours=holding_hours,
            cost_amortization_hours=cost_amortization_hours,
            min_cost_amortization_hours=min_cost_amortization_hours,
        )
    )
    return YieldParams(
        holding_hours=effective_holding_hours,
        cost_amortization_hours=effective_cost_amortization_hours,
        include_basis_convergence=(
            settings.yield_model.include_basis_convergence
        ),
        basis_haircut=to_decimal(settings.yield_model.basis_haircut),
        expected_exit_basis_mode=ExpectedExitBasisMode(
            settings.yield_model.expected_exit_basis_mode,
        ),
        yield_base=YieldBase(settings.yield_model.yield_base),
    )


# ---------------------------------------------------------------------
# Exchange client factory
# ---------------------------------------------------------------------


def _build_exchange_client(settings: Settings):
    """
    Build exchange client based on settings.exchange.id.

    Supported:
        - binance
        - bybit
    """
    exchange_id = str(
        getattr(settings.exchange, "id", "binance"),
    ).strip().lower()

    common_kwargs = dict(
        timeout_ms=settings.exchange.timeout_ms,
        retries=settings.exchange.retries,
        retry_backoff_ms=settings.exchange.retry_backoff_ms,
        sandbox=settings.exchange.sandbox,
    )

    if exchange_id == "binance":
        return BinanceClient(
            **common_kwargs,
            api_key=os.getenv("BINANCE_API_KEY") or None,
            api_secret=os.getenv("BINANCE_API_SECRET") or None,
            max_weight_per_minute=(
                settings.exchange.max_weight_per_minute
            ),
        )

    if exchange_id == "bybit":
        return BybitClient(
            **common_kwargs,
            api_key=os.getenv("BYBIT_API_KEY") or None,
            api_secret=os.getenv("BYBIT_API_SECRET") or None,
        )

    raise ValueError(f"unknown exchange id: {exchange_id!r}")


# ---------------------------------------------------------------------
# Build application
# ---------------------------------------------------------------------


def build_app() -> MonitorApp:
    """
    Build fully wired MonitorApp.

    Steps:
        1. Load config.
        2. Setup logging.
        3. Build persistence.
        4. Build exchange client.
        5. Build params.
        6. Build notifier.
        7. Assemble MonitorApp.
    """
    settings_path = os.getenv(
        "MONITOR_SETTINGS_PATH",
        "config/settings.yaml",
    )
    symbols_path = os.getenv(
        "MONITOR_SYMBOLS_PATH",
        "config/symbols.yaml",
    )
    settings = load_settings(
        settings_path=settings_path,
        symbols_path=symbols_path,
    )
    setup_logging(
        LoggingParams(
            level=settings.logging.level,
            format=settings.logging.format,
            file_path=settings.logging.file_path,
            rotation=settings.logging.rotation,
            retention_days=settings.logging.retention_days,
            console=settings.logging.console,
        ),
    )
    logger = get_logger("monitor.bootstrap")
    log_event(
        logger,
        event="config_loaded",
        payload={
            "config_version": settings.meta.config_version,
            "environment": settings.meta.environment,
            "symbols_count": len(settings.symbols),
        },
    )
    if settings.storage.mode != "sqlite":
        raise ValueError(
            "Stage 1 application layer supports only sqlite storage mode",
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    database = Database(
        DatabaseParams(
            sqlite_path=settings.storage.sqlite_path,
            save_raw_responses=settings.storage.save_raw_responses,
            retention_days=settings.storage.retention_days,
        ),
    )
    snapshot_repository = SnapshotRepository(
        database=database,
        save_raw_responses=settings.storage.save_raw_responses,
    )
    alert_repository = AlertRepository(database=database)

    # ------------------------------------------------------------------
    # Instruments
    # ------------------------------------------------------------------
    instruments = tuple(
        _instrument_from_symbol_config(symbol)
        for symbol in settings.symbols
    )

    # ------------------------------------------------------------------
    # Exchange client
    # ------------------------------------------------------------------
    exchange_client = _build_exchange_client(settings)

    # ------------------------------------------------------------------
    # Params
    # ------------------------------------------------------------------
    quality_params = _quality_params_from_settings(settings)
    signal_params = _signal_params_from_settings(settings)
    cost_params = _cost_params_from_settings(settings)
    yield_params = _yield_params_from_settings(settings)

    # ------------------------------------------------------------------
    # Signal state
    # ------------------------------------------------------------------
    state_store = InMemorySignalStateStore()

    # ------------------------------------------------------------------
    # Telegram notifier
    # ------------------------------------------------------------------
    telegram_params = TelegramNotifierParams(
        token=os.getenv(settings.telegram.token_env, ""),
        chat_id=os.getenv(settings.telegram.chat_id_env, ""),
        enabled=settings.telegram.enabled,
        timeout_ms=settings.telegram.timeout_ms,
        retry_attempts=settings.telegram.retry_attempts,
        max_messages_per_hour=settings.telegram.max_messages_per_hour,
        send_signal=settings.telegram.send_signal,
        send_warning=settings.telegram.send_data_errors,
        send_error=settings.telegram.send_data_errors,
        send_heartbeat=settings.telegram.send_heartbeat,
        parse_mode=None,
        disable_web_page_preview=True,
    )
    notifier = TelegramNotifier(telegram_params)

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    return MonitorApp(
        settings=settings,
        database=database,
        exchange_client=exchange_client,
        notifier=notifier,
        instruments=instruments,
        quality_params=quality_params,
        signal_params=signal_params,
        cost_params=cost_params,
        yield_params=yield_params,
        state_store=state_store,
        snapshot_repository=snapshot_repository,
        alert_repository=alert_repository,
    )