# src/monitor/notifications/__init__.py
from .formatter import (
    format_error_message,
    format_heartbeat_message,
    format_signal_message,
    format_warning_message,
)
from .rate_limiter import (
    NotificationRateLimiter,
    RateLimiterParams,
)
from .telegram import (
    AlertDeliveryResult,
    TelegramAuthError,
    TelegramNotifier,
    TelegramNotifierParams,
    TelegramRateLimitError,
    TelegramRequestError,
    mask_token_in_text,
)

# Backward compatibility: app.py uses NotificationResult
NotificationResult = AlertDeliveryResult

__all__ = [
    # formatter
    "format_error_message",
    "format_heartbeat_message",
    "format_signal_message",
    "format_warning_message",
    # rate limiter
    "NotificationRateLimiter",
    "RateLimiterParams",
    # telegram
    "AlertDeliveryResult",
    "NotificationResult",
    "TelegramAuthError",
    "TelegramNotifier",
    "TelegramNotifierParams",
    "TelegramRateLimitError",
    "TelegramRequestError",
    "mask_token_in_text",
]