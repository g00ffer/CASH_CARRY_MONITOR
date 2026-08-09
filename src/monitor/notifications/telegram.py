from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from monitor.domain import (
    AlertDeliveryStatus,
    AlertType,
)
from monitor.notifications.rate_limiter import (
    NotificationRateLimiter,
    RateLimiterParams,
)
from monitor.persistence.logger import log_event
from monitor.utils import (
    RetryPolicy,
    retry_async,
    utc_now_ms,
)
from monitor.utils.retry import default_should_retry

logger = logging.getLogger(__name__)


# ======================================================================
# Token masking (defined locally because it is not in monitor.utils)
# ======================================================================

_TOKEN_PATTERN = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,50}\b")


def mask_token_in_text(text: str, token: str | None = None) -> str:
    """
    Mask Telegram bot tokens in text to prevent leakage into logs.
    """
    if not text:
        return text
    if token and token.strip() and token in text:
        text = text.replace(token, "bot***:***")
    return _TOKEN_PATTERN.sub("bot***:***", text)


# ======================================================================
# Alert delivery result
# ======================================================================


@dataclass(frozen=True, slots=True, kw_only=True)
class AlertDeliveryResult:
    """
    Result of an alert delivery attempt.
    Defined locally because it is specific to the notification layer.
    """

    alert_id: str
    alert_type: AlertType
    status: AlertDeliveryStatus
    delivered: bool
    suppressed_reason: str | None = None
    error_message: str | None = None


# ======================================================================
# Exceptions
# ======================================================================


class TelegramAuthError(Exception):
    """Raised when Telegram returns 401/403 (bad token or chat)."""


class TelegramRequestError(Exception):
    """Raised when Telegram returns non-200 status."""


class TelegramRateLimitError(TelegramRequestError):
    """
    Raised when Telegram API returns 429 Too Many Requests.
    This is a distinct exception so that RetryPolicy can explicitly
    retry on rate limits instead of relying on fragile text matching.
    """


# ======================================================================
# Params
# ======================================================================


@dataclass(frozen=True, slots=True, kw_only=True)
class TelegramNotifierParams:
    """
    Telegram settings.
    Secrets must be provided from environment variables.
    Do not store token/chat id in YAML config.
    """

    token: str
    chat_id: str
    enabled: bool = True
    timeout_ms: int = 5000
    retry_attempts: int = 3
    max_messages_per_hour: int = 20
    send_signal: bool = True
    send_warning: bool = True
    send_error: bool = True
    send_heartbeat: bool = True
    parse_mode: str | None = None
    disable_web_page_preview: bool = True
    proxy: str | None = None

    def __post_init__(self) -> None:
        if self.enabled:
            if not self.token.strip():
                raise ValueError("telegram token is required when enabled")
            if not self.chat_id.strip():
                raise ValueError("telegram chat_id is required when enabled")
        if self.timeout_ms <= 0:
            raise ValueError("timeout_ms must be > 0")
        if self.retry_attempts < 0:
            raise ValueError("retry_attempts must be >= 0")
        if self.max_messages_per_hour <= 0:
            raise ValueError("max_messages_per_hour must be > 0")

    def __repr__(self) -> str:
        return (
            "TelegramNotifierParams("
            "token='***', "
            "chat_id='***', "
            f"enabled={self.enabled!r}, "
            f"timeout_ms={self.timeout_ms!r}, "
            f"retry_attempts={self.retry_attempts!r}, "
            f"max_messages_per_hour={self.max_messages_per_hour!r}, "
            f"proxy={'***' if self.proxy else None!r}"
            ")"
        )


# ======================================================================
# Retry predicate
# ======================================================================


def _telegram_should_retry(exc: Exception) -> bool:
    """
    Custom retry predicate for Telegram.
    Explicitly retries on 429 rate limit (TelegramRateLimitError),
    falls back to default_should_retry for everything else.
    """
    if isinstance(exc, TelegramRateLimitError):
        return True
    return default_should_retry(exc)


# ======================================================================
# Notifier
# ======================================================================


class TelegramNotifier:
    """
    Telegram Bot API notifier.
    """

    def __init__(
        self,
        params: TelegramNotifierParams,
        rate_limiter: NotificationRateLimiter | None = None,
    ) -> None:
        self._params = params

        client_kwargs: dict[str, Any] = {
            "timeout": params.timeout_ms / 1000,
        }
        if params.proxy and params.proxy.strip():
            client_kwargs["proxy"] = params.proxy.strip()

        self._client = httpx.AsyncClient(**client_kwargs)

        self._rate_limiter = rate_limiter or NotificationRateLimiter(
            RateLimiterParams(
                max_messages_per_hour=params.max_messages_per_hour,
            ),
        )

        self._retry_policy = RetryPolicy(
            max_attempts=max(1, params.retry_attempts),
            initial_delay_ms=1000,
            max_delay_ms=10000,
            backoff_factor=2.0,
            jitter_ms=500,
            should_retry=_telegram_should_retry,
        )

    # ------------------------------------------------------------------
    # Public API: generic send
    # ------------------------------------------------------------------

    async def send_alert(
        self,
        *,
        alert_type: AlertType,
        text: str,
        alert_id: str | None = None,
        now_ms: int | None = None,
        cycle_id: str | None = None,
        symbol_name: str | None = None,
        **kwargs: Any,
    ) -> AlertDeliveryResult:
        """Send an alert. Returns AlertDeliveryResult."""
        import uuid as _uuid

        if alert_id is None:
            alert_id = str(_uuid.uuid4())
        if now_ms is None:
            now_ms = utc_now_ms()

        # Config check
        if not self._is_enabled(alert_type):
            log_event(
                logger,
                event="alert_suppressed_by_config",
                level=logging.DEBUG,
                cycle_id=cycle_id,
                symbol_name=symbol_name,
                payload={
                    "alert_type": alert_type.value,
                    "alert_id": alert_id,
                },
            )
            return AlertDeliveryResult(
                alert_id=alert_id,
                alert_type=alert_type,
                status=AlertDeliveryStatus.SUPPRESSED,
                delivered=False,
                suppressed_reason="disabled_by_config",
                error_message=None,
            )

        # Rate limiter (pass now_ms!)
        if not self._rate_limiter.allow(alert_id, now_ms):
            log_event(
                logger,
                event="alert_suppressed_by_rate_limiter",
                level=logging.WARNING,
                cycle_id=cycle_id,
                symbol_name=symbol_name,
                payload={
                    "alert_type": alert_type.value,
                    "alert_id": alert_id,
                },
            )
            return AlertDeliveryResult(
                alert_id=alert_id,
                alert_type=alert_type,
                status=AlertDeliveryStatus.SUPPRESSED,
                delivered=False,
                suppressed_reason="rate_limited",
                error_message=None,
            )

        # Validate text
        if not text or not text.strip():
            return AlertDeliveryResult(
                alert_id=alert_id,
                alert_type=alert_type,
                status=AlertDeliveryStatus.FAILED,
                delivered=False,
                suppressed_reason=None,
                error_message="empty alert text",
            )

        # Split and send
        chunks = self._split_message(text)
        last_error: str | None = None

        for chunk in chunks:
            result = await self._send_text(
                text=chunk,
                alert_id=alert_id,
                alert_type=alert_type,
                cycle_id=cycle_id,
                symbol_name=symbol_name,
            )
            if not result.delivered:
                last_error = result.error_message
                break

        if last_error:
            return AlertDeliveryResult(
                alert_id=alert_id,
                alert_type=alert_type,
                status=AlertDeliveryStatus.FAILED,
                delivered=False,
                suppressed_reason=None,
                error_message=last_error,
            )

        return AlertDeliveryResult(
            alert_id=alert_id,
            alert_type=alert_type,
            status=AlertDeliveryStatus.SENT,
            delivered=True,
            suppressed_reason=None,
            error_message=None,
        )

    # ------------------------------------------------------------------
    # Public API: convenience wrappers (match app.py call sites)
    # ------------------------------------------------------------------

    async def send_heartbeat(
        self,
        *,
        text: str,
        now_ms: int | None = None,
        alert_id: str | None = None,
        cycle_id: str | None = None,
        symbol_name: str | None = None,
        **kwargs: Any,
    ) -> AlertDeliveryResult:
        """Send a heartbeat."""
        return await self.send_alert(
            alert_type=AlertType.HEARTBEAT,
            text=text,
            alert_id=alert_id,
            now_ms=now_ms,
            cycle_id=cycle_id,
            symbol_name=symbol_name,
        )

    async def send_signal(
        self,
        *,
        text: str,
        now_ms: int | None = None,
        alert_id: str | None = None,
        cycle_id: str | None = None,
        symbol_name: str | None = None,
        **kwargs: Any,
    ) -> AlertDeliveryResult:
        """Send a signal."""
        return await self.send_alert(
            alert_type=AlertType.SIGNAL,
            text=text,
            alert_id=alert_id,
            now_ms=now_ms,
            cycle_id=cycle_id,
            symbol_name=symbol_name,
        )

    async def send_warning(
        self,
        *,
        text: str,
        now_ms: int | None = None,
        alert_id: str | None = None,
        cycle_id: str | None = None,
        symbol_name: str | None = None,
        **kwargs: Any,
    ) -> AlertDeliveryResult:
        """Send a warning."""
        return await self.send_alert(
            alert_type=AlertType.WARNING,
            text=text,
            alert_id=alert_id,
            now_ms=now_ms,
            cycle_id=cycle_id,
            symbol_name=symbol_name,
        )

    async def send_error(
        self,
        *,
        text: str,
        now_ms: int | None = None,
        alert_id: str | None = None,
        cycle_id: str | None = None,
        symbol_name: str | None = None,
        **kwargs: Any,
    ) -> AlertDeliveryResult:
        """Send an error alert."""
        return await self.send_alert(
            alert_type=AlertType.ERROR,
            text=text,
            alert_id=alert_id,
            now_ms=now_ms,
            cycle_id=cycle_id,
            symbol_name=symbol_name,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the HTTP client gracefully."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _is_enabled(self, alert_type: AlertType) -> bool:
        """Check if the alert type is enabled in config."""
        if not self._params.enabled:
            return False
        match alert_type:
            case AlertType.SIGNAL:
                return self._params.send_signal
            case AlertType.WARNING:
                return self._params.send_warning
            case AlertType.ERROR:
                return self._params.send_error
            case AlertType.HEARTBEAT:
                return self._params.send_heartbeat
            case _:
                return True

    def _split_message(self, text: str) -> list[str]:
        """Split message into chunks of max 4096 chars (Telegram limit)."""
        max_len = 4096
        if len(text) <= max_len:
            return [text]

        chunks: list[str] = []
        remaining = text

        while remaining:
            if len(remaining) <= max_len:
                chunks.append(remaining)
                break

            split_pos = remaining.rfind("\n", 0, max_len)
            if split_pos == -1 or split_pos < max_len // 2:
                split_pos = max_len

            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos:].lstrip("\n")

        return chunks

    async def _send_text(
        self,
        *,
        text: str,
        alert_id: str,
        alert_type: AlertType,
        cycle_id: str | None = None,
        symbol_name: str | None = None,
    ) -> AlertDeliveryResult:
        """Send a single text chunk to Telegram with retry."""
        url = (
            f"https://api.telegram.org/"
            f"bot{self._params.token}/sendMessage"
        )

        payload: dict[str, Any] = {
            "chat_id": self._params.chat_id,
            "text": text,
            "disable_web_page_preview": (
                self._params.disable_web_page_preview
            ),
        }
        if self._params.parse_mode:
            payload["parse_mode"] = self._params.parse_mode

        async def _post() -> AlertDeliveryResult:
            response = await self._client.post(url, json=payload)

            if response.status_code == 429:
                retry_after = 5
                try:
                    body = response.json()
                    retry_after = body.get("parameters", {}).get(
                        "retry_after", 5
                    )
                except Exception:
                    pass
                raise TelegramRateLimitError(
                    f"Telegram API rate limit: status=429, "
                    f"retry_after={retry_after}s",
                )

            if response.status_code in (401, 403):
                raise TelegramAuthError(
                    f"Telegram auth error: "
                    f"status={response.status_code}, "
                    f"body={response.text[:500]}",
                )

            if response.status_code != 200:
                raise TelegramRequestError(
                    f"Telegram API error: "
                    f"status={response.status_code}, "
                    f"body={response.text[:500]}",
                )

            return AlertDeliveryResult(
                alert_id=alert_id,
                alert_type=alert_type,
                status=AlertDeliveryStatus.SENT,
                delivered=True,
                suppressed_reason=None,
                error_message=None,
            )

        try:
            # KEY FIX: use retry_async instead of RetryPolicy.execute
            result = await retry_async(_post, policy=self._retry_policy)
            return result
        except TelegramAuthError as exc:
            error_msg = self._safe_error_message(exc)
            log_event(
                logger,
                event="telegram_auth_error",
                level=logging.ERROR,
                cycle_id=cycle_id,
                symbol_name=symbol_name,
                payload={
                    "alert_id": alert_id,
                    "alert_type": alert_type.value,
                    "error_message": error_msg,
                },
            )
            return AlertDeliveryResult(
                alert_id=alert_id,
                alert_type=alert_type,
                status=AlertDeliveryStatus.FAILED,
                delivered=False,
                suppressed_reason=None,
                error_message=error_msg,
            )
        except Exception as exc:
            error_msg = self._safe_error_message(exc)
            log_event(
                logger,
                event="telegram_send_failed",
                level=logging.ERROR,
                cycle_id=cycle_id,
                symbol_name=symbol_name,
                payload={
                    "alert_id": alert_id,
                    "alert_type": alert_type.value,
                    "error_message": error_msg,
                },
            )
            return AlertDeliveryResult(
                alert_id=alert_id,
                alert_type=alert_type,
                status=AlertDeliveryStatus.FAILED,
                delivered=False,
                suppressed_reason=None,
                error_message=error_msg,
            )

    def _safe_error_message(self, exc: Exception) -> str:
        """Create error message with token masked."""
        raw = str(exc)
        return mask_token_in_text(raw, self._params.token)