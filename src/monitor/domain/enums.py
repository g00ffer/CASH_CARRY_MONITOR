from enum import Enum


class StrategyDirection(str, Enum):
    """
    Strategy direction.

    For Stage 1 we only support classic cash-and-carry:
    long spot, short perpetual.
    """

    LONG_SPOT_SHORT_PERP = "long_spot_short_perp"


class MarketType(str, Enum):
    SPOT = "spot"
    PERPETUAL = "perpetual"


class SignalState(str, Enum):
    """
    High-level signal state for a symbol.
    """

    NORMAL = "normal"
    WATCHING = "watching"
    SIGNAL_ACTIVE = "signal_active"
    COOLDOWN = "cooldown"
    DATA_INVALID = "data_invalid"


class AlertType(str, Enum):
    """
    Notification type.

    SIGNAL, WARNING, ERROR and HEARTBEAT must not be mixed.
    """

    SIGNAL = "signal"
    WARNING = "warning"
    ERROR = "error"
    HEARTBEAT = "heartbeat"


class AlertDeliveryStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


class DataIssueCode(str, Enum):
    """
    Machine-readable data quality issue codes.
    """

    STALE_SNAPSHOT = "stale_snapshot"
    FUTURE_TIMESTAMP = "future_timestamp"
    TIMESTAMP_MISMATCH = "timestamp_mismatch"

    MISSING_BID = "missing_bid"
    MISSING_ASK = "missing_ask"
    INVALID_BID_ASK = "invalid_bid_ask"
    NON_POSITIVE_PRICE = "non_positive_price"

    SPREAD_TOO_WIDE = "spread_too_wide"
    PRICE_JUMP = "price_jump"
    LOW_LIQUIDITY = "low_liquidity"

    FUNDING_UNKNOWN = "funding_unknown"
    FUNDING_INTERVAL_UNKNOWN = "funding_interval_unknown"
    FUNDING_NOT_POSITIVE = "funding_not_positive"
    FUNDING_TIMESTAMP_INVALID = "funding_timestamp_invalid"

    EXCHANGE_ERROR = "exchange_error"
    UNKNOWN_SYMBOL = "unknown_symbol"
    CONFIG_ERROR = "config_error"


class DataIssueSeverity(str, Enum):
    WARNING = "warning"
    ERROR = "error"


class FundingRateSource(str, Enum):
    """
    Which funding rate was selected as effective for calculations.
    """

    PREDICTED = "predicted"
    LAST = "last"
    DEFAULT = "default"


class YieldBase(str, Enum):
    """
    Yield base for Stage 1.

    For Stage 1 we prefer notional yield.
    Equity yield can be added later.
    """

    NOTIONAL = "notional"
    EQUITY = "equity"


class ExpectedExitBasisMode(str, Enum):
    """
    Assumption for expected exit basis.

    For Stage 1 conservative default is ENTRY, meaning no basis
    convergence profit is assumed.
    """

    ENTRY = "entry"
    ZERO = "zero"
    HISTORICAL_MEDIAN = "historical_median"
