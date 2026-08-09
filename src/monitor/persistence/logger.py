from __future__ import annotations

import datetime as dt
import json
import logging
import logging.handlers
from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------
# JSON encoder
# ---------------------------------------------------------------------


class _JsonEncoder(json.JSONEncoder):
    """
    JSON encoder for structured logs.

    Supports:
    - Decimal
    - Enum
    - datetime/date
    - dataclasses
    - exceptions
    """

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return str(obj)

        if isinstance(obj, Enum):
            return obj.value

        if isinstance(obj, dt.datetime):
            return obj.isoformat()

        if isinstance(obj, dt.date):
            return obj.isoformat()

        if isinstance(obj, Exception):
            return repr(obj)

        if is_dataclass(obj):
            return asdict(obj)

        try:
            return super().default(obj)
        except TypeError:
            return str(obj)


# ---------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """
    Format log records as JSON lines.

    Supports optional extra fields:
    - cycle_id
    - symbol_name
    - event
    - payload
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(
                record.created,
                tz=dt.timezone.utc,
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key in ("cycle_id", "symbol_name", "event", "payload"):
            value = getattr(record, key, None)

            if value is not None:
                payload[key] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(
            payload,
            cls=_JsonEncoder,
            ensure_ascii=False,
        )


# ---------------------------------------------------------------------
# Logging params
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class LoggingParams:
    """
    Logging configuration.

    Usually built from settings.logging in bootstrap.
    """

    level: str = "INFO"
    format: str = "json"
    file_path: str = "logs/monitor.log"
    rotation: str = "daily"
    retention_days: int = 30
    console: bool = True

    def __post_init__(self) -> None:
        if self.format not in ("json", "text"):
            raise ValueError("logging format must be json or text")

        if self.rotation not in ("daily", "hourly", "none"):
            raise ValueError("logging rotation must be daily, hourly or none")

        if self.retention_days < 1:
            raise ValueError("retention_days must be >= 1")


# ---------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------


def setup_logging(params: LoggingParams) -> None:
    """
    Configure root logger.
    This should be called once in bootstrap.
    """
    numeric_level = logging.getLevelName(params.level.upper())
    if not isinstance(numeric_level, int):
        raise ValueError(f"invalid logging level: {params.level}")

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Remove existing handlers to make setup idempotent.
    root_logger.handlers.clear()

    file_path = Path(params.file_path).expanduser()
    file_path.parent.mkdir(parents=True, exist_ok=True)

    if params.rotation == "daily":
        file_handler: logging.Handler = logging.handlers.TimedRotatingFileHandler(
            filename=file_path,
            when="midnight",
            backupCount=params.retention_days,
            encoding="utf-8",
        )
    elif params.rotation == "hourly":
        file_handler = logging.handlers.TimedRotatingFileHandler(
            filename=file_path,
            when="H",
            backupCount=params.retention_days * 24,
            encoding="utf-8",
        )
    else:
        file_handler = logging.FileHandler(
            filename=file_path,
            encoding="utf-8",
        )

    if params.format == "json":
        file_formatter: logging.Formatter = JsonFormatter()
    else:
        file_formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
        )

    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)

    if params.console:
        console_handler = logging.StreamHandler()
        if params.format == "json":
            console_formatter: logging.Formatter = JsonFormatter()
        else:
            console_formatter = logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
            )
        console_handler.setFormatter(console_formatter)
        root_logger.addHandler(console_handler)

    # ------------------------------------------------------------------
    # Suppress httpx INFO logs (they contain full URLs with tokens)
    # ------------------------------------------------------------------
    _configure_httpx_logging()


def _configure_httpx_logging() -> None:
    """
    Configure httpx logger to prevent token leakage.

    httpx logs full request URLs at INFO level, which includes
    the Telegram bot token. We suppress INFO logs and add a
    masking filter for WARNING/ERROR messages.
    """
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.setLevel(logging.WARNING)
    httpx_logger.addFilter(_TokenMaskingFilter())

    # Also suppress httpcore (used internally by httpx)
    httpcore_logger = logging.getLogger("httpcore")
    httpcore_logger.setLevel(logging.WARNING)
    httpcore_logger.addFilter(_TokenMaskingFilter())


class _TokenMaskingFilter(logging.Filter):
    """
    Filter that masks Telegram bot tokens in log messages.

    Telegram token format: digits:alphanumeric_string
    Example: 8701172674:AAHOgoE48bdEzBYcZLZpDGk9BCpQuePFEbE
    """

    import re as _re
    _TOKEN_PATTERN = _re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._TOKEN_PATTERN.sub(
                "bot***:***", record.msg
            )
        if record.args:
            # Mask in formatted args too
            if isinstance(record.args, tuple):
                record.args = tuple(
                    self._TOKEN_PATTERN.sub("bot***:***", str(a))
                    if isinstance(a, str) else a
                    for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: self._TOKEN_PATTERN.sub("bot***:***", str(v))
                    if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
        return True

def get_logger(name: str) -> logging.Logger:
    """
    Get named logger.
    """

    return logging.getLogger(name)


# ---------------------------------------------------------------------
# Structured event helper
# ---------------------------------------------------------------------


def log_event(
    logger: logging.Logger,
    *,
    event: str,
    level: int = logging.INFO,
    message: str | None = None,
    cycle_id: str | None = None,
    symbol_name: str | None = None,
    payload: Any | None = None,
    exc_info: Any | None = None,
) -> None:
    """
    Log structured event.

    Example:
        log_event(
            logger,
            event="signal_decision",
            cycle_id=cycle_id,
            symbol_name="BTC_CARRY",
            payload={...},
        )
    """

    extra: dict[str, Any] = {
        "event": event,
    }

    if cycle_id is not None:
        extra["cycle_id"] = cycle_id

    if symbol_name is not None:
        extra["symbol_name"] = symbol_name

    if payload is not None:
        extra["payload"] = payload

    logger.log(
        level,
        message or event,
        extra=extra,
        exc_info=exc_info,
    )
