from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from monitor.domain import (
    DataIssue,
    DataIssueCode,
    DataIssueSeverity,
    FundingSnapshot,
    MarketSnapshot,
    QualityReport,
)
from monitor.utils import (
    ONE,
    ZERO,
    to_decimal,
)

# ---------------------------------------------------------------------
# Quality parameters
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class QualityParams:
    """
    Data quality thresholds.

    All ratio-like values are decimal fractions.

    Example:
        max_spread = Decimal("0.001")  # 0.10% = 10 bps
        max_price_jump_pct = Decimal("0.05")  # 5%
    """

    max_snapshot_age_ms: int
    max_spot_perp_time_diff_ms: int
    max_spread: Decimal

    min_quote_volume_24h: Decimal | None = None

    require_valid_funding_interval: bool = True
    require_predicted_funding: bool = False

    max_price_jump_pct: Decimal | None = None

    future_timestamp_tolerance_ms: int = 1000

    def __post_init__(self) -> None:
        if self.max_snapshot_age_ms <= 0:
            raise ValueError("max_snapshot_age_ms must be > 0")

        if self.max_spot_perp_time_diff_ms < 0:
            raise ValueError("max_spot_perp_time_diff_ms must be >= 0")

        if to_decimal(self.max_spread) < ZERO:
            raise ValueError("max_spread must be >= 0")

        if (
            self.min_quote_volume_24h is not None
            and to_decimal(self.min_quote_volume_24h) < ZERO
        ):
            raise ValueError("min_quote_volume_24h must be >= 0")

        if (
            self.max_price_jump_pct is not None
            and to_decimal(self.max_price_jump_pct) < ZERO
        ):
            raise ValueError("max_price_jump_pct must be >= 0")

        if self.future_timestamp_tolerance_ms < 0:
            raise ValueError("future_timestamp_tolerance_ms must be >= 0")


# ---------------------------------------------------------------------
# Issue helpers
# ---------------------------------------------------------------------


def _error(
    code: DataIssueCode,
    message: str,
    field_name: str | None = None,
) -> DataIssue:
    return DataIssue(
        code=code,
        severity=DataIssueSeverity.ERROR,
        message=message,
        field_name=field_name,
    )


def _warning(
    code: DataIssueCode,
    message: str,
    field_name: str | None = None,
) -> DataIssue:
    return DataIssue(
        code=code,
        severity=DataIssueSeverity.WARNING,
        message=message,
        field_name=field_name,
    )


# ---------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------


def _spread_ratio(
    bid: Decimal,
    ask: Decimal,
) -> Decimal:
    """
    Spread as decimal fraction:

        spread = ask / bid - 1
    """

    if bid <= ZERO or ask <= ZERO:
        return ZERO

    return ask / bid - ONE


def _relative_change(
    current: Decimal,
    previous: Decimal,
) -> Decimal:
    """
    Relative change:

        abs(current / previous - 1)
    """

    if previous <= ZERO:
        return ZERO

    return abs(current / previous - ONE)


# ---------------------------------------------------------------------
# Ticker checks
# ---------------------------------------------------------------------


def _check_ticker(
    ticker,
    market_type: str,
    params: QualityParams,
    now_ms: int,
) -> tuple[list[DataIssue], list[DataIssue]]:
    """
    Common checks for spot/perp ticker.
    """

    errors: list[DataIssue] = []
    warnings: list[DataIssue] = []

    timestamp_field = f"{market_type}.timestamp_ms"

    timestamp_age_ms = int(now_ms) - int(ticker.timestamp_ms)

    if timestamp_age_ms < -params.future_timestamp_tolerance_ms:
        errors.append(
            _error(
                code=DataIssueCode.FUTURE_TIMESTAMP,
                message=f"{market_type} timestamp is in the future",
                field_name=timestamp_field,
            )
        )
    elif timestamp_age_ms > params.max_snapshot_age_ms:
        errors.append(
            _error(
                code=DataIssueCode.STALE_SNAPSHOT,
                message=f"{market_type} timestamp is too old",
                field_name=timestamp_field,
            )
        )

    if ticker.bid <= ZERO or ticker.ask <= ZERO:
        errors.append(
            _error(
                code=DataIssueCode.NON_POSITIVE_PRICE,
                message=f"{market_type} bid/ask must be positive",
                field_name=f"{market_type}.bid/ask",
            )
        )
    elif ticker.bid >= ticker.ask:
        errors.append(
            _error(
                code=DataIssueCode.INVALID_BID_ASK,
                message=f"{market_type} bid must be less than ask",
                field_name=f"{market_type}.bid/ask",
            )
        )
    else:
        spread = _spread_ratio(
            bid=ticker.bid,
            ask=ticker.ask,
        )

        if spread > params.max_spread:
            errors.append(
                _error(
                    code=DataIssueCode.SPREAD_TOO_WIDE,
                    message=f"{market_type} spread is too wide",
                    field_name=f"{market_type}.spread",
                )
            )

    if market_type == "spot" and params.min_quote_volume_24h is not None:
        if (
            ticker.quote_volume_24h is not None
            and ticker.quote_volume_24h < params.min_quote_volume_24h
        ):
            errors.append(
                _error(
                    code=DataIssueCode.LOW_LIQUIDITY,
                    message="spot quote volume is below required minimum",
                    field_name="spot.quote_volume_24h",
                )
            )

    return errors, warnings


# ---------------------------------------------------------------------
# Market quality
# ---------------------------------------------------------------------


def check_market_quality(
    *,
    market_snapshot: MarketSnapshot,
    params: QualityParams,
    now_ms: int,
    previous_market_snapshot: MarketSnapshot | None = None,
) -> QualityReport:
    """
    Check market snapshot quality.
    """

    errors: list[DataIssue] = []
    warnings: list[DataIssue] = []

    # ------------------------------------------------------------------
    # Local received time staleness
    # ------------------------------------------------------------------

    received_age_ms = int(now_ms) - int(market_snapshot.received_at_ms)

    if received_age_ms < -params.future_timestamp_tolerance_ms:
        errors.append(
            _error(
                code=DataIssueCode.FUTURE_TIMESTAMP,
                message="market snapshot received_at is in the future",
                field_name="market_snapshot.received_at_ms",
            )
        )
    elif received_age_ms > params.max_snapshot_age_ms:
        errors.append(
            _error(
                code=DataIssueCode.STALE_SNAPSHOT,
                message="market snapshot received_at is too old",
                field_name="market_snapshot.received_at_ms",
            )
        )

    # ------------------------------------------------------------------
    # Spot checks
    # ------------------------------------------------------------------

    spot_errors, spot_warnings = _check_ticker(
        ticker=market_snapshot.spot,
        market_type="spot",
        params=params,
        now_ms=now_ms,
    )

    errors.extend(spot_errors)
    warnings.extend(spot_warnings)

    # ------------------------------------------------------------------
    # Perp checks
    # ------------------------------------------------------------------

    perp_errors, perp_warnings = _check_ticker(
        ticker=market_snapshot.perp,
        market_type="perp",
        params=params,
        now_ms=now_ms,
    )

    errors.extend(perp_errors)
    warnings.extend(perp_warnings)

    # ------------------------------------------------------------------
    # Spot/perp timestamp mismatch
    # ------------------------------------------------------------------
    spot_perp_diff_ms = market_snapshot.spot_perp_time_diff_ms
    if spot_perp_diff_ms > params.max_spot_perp_time_diff_ms:
        # Для fetch_ticker это нормальное поведение, так как timestamp 
        # отражает время последней сделки, а не время обновления BBO.
        # Переводим в WARNING, чтобы не блокировать сигнал.
        warnings.append(
            _warning(
                code=DataIssueCode.TIMESTAMP_MISMATCH,
                message=(
                    f"spot and perp timestamps differ by {spot_perp_diff_ms} ms "
                    f"(limit {params.max_spot_perp_time_diff_ms} ms). "
                    f"Expected for fetch_ticker (last trade time vs BBO)."
                ),
                field_name="market_snapshot.spot_perp_time_diff_ms",
            )
        )

    # ------------------------------------------------------------------
    # Optional price jump checks
    # ------------------------------------------------------------------

    if (
        previous_market_snapshot is not None
        and params.max_price_jump_pct is not None
    ):
        max_jump = to_decimal(params.max_price_jump_pct)

        spot_jump = _relative_change(
            current=market_snapshot.spot.mid_price,
            previous=previous_market_snapshot.spot.mid_price,
        )

        if spot_jump > max_jump:
            warnings.append(
                _warning(
                    code=DataIssueCode.PRICE_JUMP,
                    message="spot mid price jumped too much between snapshots",
                    field_name="spot.mid_price",
                )
            )

        perp_jump = _relative_change(
            current=market_snapshot.perp.mid_price,
            previous=previous_market_snapshot.perp.mid_price,
        )

        if perp_jump > max_jump:
            warnings.append(
                _warning(
                    code=DataIssueCode.PRICE_JUMP,
                    message="perp mid price jumped too much between snapshots",
                    field_name="perp.mid_price",
                )
            )

    return QualityReport(
        is_ok=len(errors) == 0,
        checked_at_ms=int(now_ms),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------
# Funding quality
# ---------------------------------------------------------------------


def check_funding_quality(
    *,
    funding_snapshot: FundingSnapshot,
    params: QualityParams,
    now_ms: int,
) -> QualityReport:
    """
    Check funding snapshot quality.
    """

    errors: list[DataIssue] = []
    warnings: list[DataIssue] = []

    # ------------------------------------------------------------------
    # Received time staleness
    # ------------------------------------------------------------------

    received_age_ms = int(now_ms) - int(funding_snapshot.received_at_ms)

    if received_age_ms < -params.future_timestamp_tolerance_ms:
        errors.append(
            _error(
                code=DataIssueCode.FUTURE_TIMESTAMP,
                message="funding snapshot received_at is in the future",
                field_name="funding_snapshot.received_at_ms",
            )
        )
    elif received_age_ms > params.max_snapshot_age_ms:
        errors.append(
            _error(
                code=DataIssueCode.STALE_SNAPSHOT,
                message="funding snapshot received_at is too old",
                field_name="funding_snapshot.received_at_ms",
            )
        )

    # ------------------------------------------------------------------
    # Funding interval
    # ------------------------------------------------------------------

    if params.require_valid_funding_interval:
        if funding_snapshot.funding_interval_hours <= ZERO:
            errors.append(
                _error(
                    code=DataIssueCode.FUNDING_INTERVAL_UNKNOWN,
                    message="funding interval is unknown or invalid",
                    field_name="funding_snapshot.funding_interval_hours",
                )
            )
    else:
        if funding_snapshot.funding_interval_hours <= ZERO:
            warnings.append(
                _warning(
                    code=DataIssueCode.FUNDING_INTERVAL_UNKNOWN,
                    message="funding interval is unknown or invalid",
                    field_name="funding_snapshot.funding_interval_hours",
                )
            )

    # ------------------------------------------------------------------
    # Predicted funding availability
    # ------------------------------------------------------------------

    if params.require_predicted_funding:
        if funding_snapshot.predicted_funding_rate is None:
            errors.append(
                _error(
                    code=DataIssueCode.FUNDING_UNKNOWN,
                    message="predicted funding rate is required but missing",
                    field_name="funding_snapshot.predicted_funding_rate",
                )
            )

    # ------------------------------------------------------------------
    # Next funding timestamp sanity
    # ------------------------------------------------------------------

    if funding_snapshot.next_funding_timestamp_ms is None:
        warnings.append(
            _warning(
                code=DataIssueCode.FUNDING_TIMESTAMP_INVALID,
                message="next funding timestamp is missing",
                field_name="funding_snapshot.next_funding_timestamp_ms",
            )
        )
    else:
        # Allow 10 seconds tolerance after funding settlement.
        # Binance may not update nextFundingTime immediately.
        funding_settlement_tolerance_ms = 10_000
        if (
            funding_snapshot.next_funding_timestamp_ms is not None
            and funding_snapshot.next_funding_timestamp_ms
            <= int(now_ms) - funding_settlement_tolerance_ms
        ):
            warnings.append(
                _warning(
                    code=DataIssueCode.FUNDING_TIMESTAMP_INVALID,
                    message="next funding timestamp is in the past",
                    field_name="funding_snapshot.next_funding_timestamp_ms",
                )
            )

    # ------------------------------------------------------------------
    # Last funding timestamp sanity
    # ------------------------------------------------------------------

    if funding_snapshot.last_funding_timestamp_ms is not None:
        if (
            funding_snapshot.last_funding_timestamp_ms
            > int(now_ms) + params.future_timestamp_tolerance_ms
        ):
            warnings.append(
                _warning(
                    code=DataIssueCode.FUNDING_TIMESTAMP_INVALID,
                    message="last funding timestamp is in the future",
                    field_name="funding_snapshot.last_funding_timestamp_ms",
                )
            )

    return QualityReport(
        is_ok=len(errors) == 0,
        checked_at_ms=int(now_ms),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------
# Combined symbol quality
# ---------------------------------------------------------------------


def merge_quality_reports(
    *reports: QualityReport,
    checked_at_ms: int,
) -> QualityReport:
    """
    Merge multiple quality reports into one.
    """

    errors: list[DataIssue] = []
    warnings: list[DataIssue] = []

    for report in reports:
        errors.extend(report.errors)
        warnings.extend(report.warnings)

    return QualityReport(
        is_ok=len(errors) == 0,
        checked_at_ms=int(checked_at_ms),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def check_symbol_quality(
    *,
    market_snapshot: MarketSnapshot | None,
    funding_snapshot: FundingSnapshot | None,
    params: QualityParams,
    now_ms: int,
    previous_market_snapshot: MarketSnapshot | None = None,
) -> QualityReport:
    """
    Combined quality check for one symbol.

    If market_snapshot or funding_snapshot is None, an exchange/data error
    is added to resulting quality report.
    """

    reports: list[QualityReport] = []

    if market_snapshot is None:
        reports.append(
            QualityReport(
                is_ok=False,
                checked_at_ms=int(now_ms),
                errors=(
                    _error(
                        code=DataIssueCode.EXCHANGE_ERROR,
                        message="market snapshot is missing",
                        field_name="market_snapshot",
                    ),
                ),
                warnings=(),
            )
        )
    else:
        reports.append(
            check_market_quality(
                market_snapshot=market_snapshot,
                params=params,
                now_ms=now_ms,
                previous_market_snapshot=previous_market_snapshot,
            )
        )

    if funding_snapshot is None:
        reports.append(
            QualityReport(
                is_ok=False,
                checked_at_ms=int(now_ms),
                errors=(
                    _error(
                        code=DataIssueCode.EXCHANGE_ERROR,
                        message="funding snapshot is missing",
                        field_name="funding_snapshot",
                    ),
                ),
                warnings=(),
            )
        )
    else:
        reports.append(
            check_funding_quality(
                funding_snapshot=funding_snapshot,
                params=params,
                now_ms=now_ms,
            )
        )

    return merge_quality_reports(
        *reports,
        checked_at_ms=int(now_ms),
    )


# ---------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------


def quality_report_from_error(
    *,
    message: str,
    checked_at_ms: int,
    code: DataIssueCode = DataIssueCode.EXCHANGE_ERROR,
    field_name: str | None = None,
) -> QualityReport:
    """
    Create failed quality report from one error.
    """

    return QualityReport(
        is_ok=False,
        checked_at_ms=int(checked_at_ms),
        errors=(
            _error(
                code=code,
                message=message,
                field_name=field_name,
            ),
        ),
        warnings=(),
    )


def quality_report_from_errors(
    *,
    messages: Iterable[str],
    checked_at_ms: int,
    code: DataIssueCode = DataIssueCode.EXCHANGE_ERROR,
) -> QualityReport:
    """
    Create failed quality report from multiple error messages.
    """
    errors = tuple(
        _error(
            code=code,
            message=message,
        )
        for message in messages
    )
    return QualityReport(
        is_ok=len(errors) == 0,  # <-- Убедись, что тут не захардкожено False
        checked_at_ms=int(checked_at_ms),
        errors=errors,
        warnings=(),
    )
