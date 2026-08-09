from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")


ShouldRetry = Callable[[Exception], bool]
OnRetry = Callable[[Exception, int, int], Any]


# ---------------------------------------------------------------------
# Default retryable error detection
# ---------------------------------------------------------------------


_RETRYABLE_CLASS_NAME_PARTS = (
    "timeout",
    "timedout",
    "network",
    "connection",
    "ratelimit",
    "rate_limit",
    "toomanyrequests",
    "unavailable",
    "temporary",
    "temp",
    "gateway",
    "badgateway",
    "serviceunavailable",
    "reset",
    "refused",
    "closed",
    "econnreset",
)

_RETRYABLE_MESSAGE_PARTS = (
    "timeout",
    "timed out",
    "network",
    "connection",
    "rate limit",
    "too many requests",
    "temporarily",
    "temporary",
    "unavailable",
    "service unavailable",
    "bad gateway",
    "connection reset",
    "connection refused",
    "connection closed",
)


def default_should_retry(exc: Exception) -> bool:
    """
    Default heuristic for retryable errors.

    It retries common network/timeouts/rate-limit-like errors and avoids
    retrying obvious logical errors such as ValueError or KeyError.
    """

    if isinstance(exc, asyncio.CancelledError):
        return False

    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True

    exception_name = type(exc).__name__.lower()
    exception_message = str(exc).lower()

    if any(part in exception_name for part in _RETRYABLE_CLASS_NAME_PARTS):
        return True

    if any(part in exception_message for part in _RETRYABLE_MESSAGE_PARTS):
        return True

    return False


# ---------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryPolicy:
    """
    Retry policy with exponential backoff and jitter.
    """

    max_attempts: int = 3
    initial_delay_ms: int = 200
    max_delay_ms: int = 5000
    backoff_factor: float = 2.0
    jitter_ms: int = 100

    retry_on_exceptions: tuple[type[Exception], ...] = (Exception,)
    should_retry: ShouldRetry | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

        if self.initial_delay_ms < 0:
            raise ValueError("initial_delay_ms must be >= 0")

        if self.max_delay_ms < 0:
            raise ValueError("max_delay_ms must be >= 0")

        if self.backoff_factor < 1:
            raise ValueError("backoff_factor must be >= 1")

        if self.jitter_ms < 0:
            raise ValueError("jitter_ms must be >= 0")

    def delay_ms(self, attempt: int) -> int:
        """
        Delay in milliseconds before next retry.

        attempt is 1-based.
        """

        if attempt < 1:
            attempt = 1

        exponent = attempt - 1
        base_delay = float(self.initial_delay_ms) * (
            float(self.backoff_factor) ** exponent
        )
        base_delay = min(base_delay, float(self.max_delay_ms))

        jitter = 0
        if self.jitter_ms > 0:
            jitter = random.randint(0, self.jitter_ms)

        return int(base_delay + jitter)

    def can_retry(self, exc: Exception) -> bool:
        """
        Check whether exception is retryable according to policy.
        """

        if not isinstance(exc, self.retry_on_exceptions):
            return False

        if self.should_retry is not None:
            return bool(self.should_retry(exc))

        return default_should_retry(exc)


# ---------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------


DEFAULT_RETRY_POLICY = RetryPolicy()

EXCHANGE_RETRY_POLICY = RetryPolicy(
    max_attempts=3,
    initial_delay_ms=500,
    max_delay_ms=5000,
    backoff_factor=2.0,
    jitter_ms=250,
)

TELEGRAM_RETRY_POLICY = RetryPolicy(
    max_attempts=3,
    initial_delay_ms=1000,
    max_delay_ms=10000,
    backoff_factor=2.0,
    jitter_ms=500,
)

NON_CRITICAL_RETRY_POLICY = RetryPolicy(
    max_attempts=2,
    initial_delay_ms=200,
    max_delay_ms=1000,
    backoff_factor=2.0,
    jitter_ms=100,
)


# ---------------------------------------------------------------------
# Sync retry
# ---------------------------------------------------------------------


def retry_sync(
    func: Callable[..., T],
    *args: Any,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    on_retry: OnRetry | None = None,
    **kwargs: Any,
) -> T:
    """
    Retry a synchronous function according to retry policy.
    """

    attempt = 1

    while True:
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            if attempt >= policy.max_attempts or not policy.can_retry(exc):
                raise

            delay = policy.delay_ms(attempt)

            if on_retry is not None:
                on_retry(exc, attempt, delay)

            time.sleep(delay / 1000)
            attempt += 1


# ---------------------------------------------------------------------
# Async retry
# ---------------------------------------------------------------------


async def retry_async(
    func: Callable[..., Awaitable[T]],
    *args: Any,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    on_retry: OnRetry | None = None,
    **kwargs: Any,
) -> T:
    """
    Retry an async function according to retry policy.
    """

    attempt = 1

    while True:
        try:
            return await func(*args, **kwargs)
        except Exception as exc:
            if attempt >= policy.max_attempts or not policy.can_retry(exc):
                raise

            delay = policy.delay_ms(attempt)

            if on_retry is not None:
                callback_result = on_retry(exc, attempt, delay)

                if asyncio.iscoroutine(callback_result):
                    await callback_result

            await asyncio.sleep(delay / 1000)
            attempt += 1
