from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class RateLimiterParams:
    """
    Telegram anti-flood limiter.

    Stage 1 uses simple sliding window limiter.
    """

    max_messages_per_hour: int = 20
    window_ms: int = 3_600_000

    def __post_init__(self) -> None:
        if self.max_messages_per_hour <= 0:
            raise ValueError("max_messages_per_hour must be > 0")

        if self.window_ms <= 0:
            raise ValueError("window_ms must be > 0")


class NotificationRateLimiter:
    """
    In-memory sliding window rate limiter.

    The limiter is intentionally simple for Stage 1.
    It protects against Telegram flooding and repeated alerts.
    """

    def __init__(
        self,
        params: RateLimiterParams | None = None,
    ) -> None:
        self._params = params or RateLimiterParams()
        self._events: dict[str, deque[int]] = defaultdict(deque)

    def _cleanup(
        self,
        key: str,
        now_ms: int,
    ) -> None:
        events = self._events[key]
        cutoff_ms = int(now_ms) - self._params.window_ms

        while events and events[0] <= cutoff_ms:
            events.popleft()

    def allow(
        self,
        key: str,
        now_ms: int,
    ) -> bool:
        """
        Try to consume one message allowance for key.

        Returns True if message is allowed.
        """

        self._cleanup(key, now_ms)

        events = self._events[key]

        if len(events) < self._params.max_messages_per_hour:
            events.append(int(now_ms))
            return True

        return False

    def remaining(
        self,
        key: str,
        now_ms: int,
    ) -> int:
        """
        Number of messages still allowed in current window.
        """

        self._cleanup(key, now_ms)

        used = len(self._events[key])
        remaining = self._params.max_messages_per_hour - used

        return max(0, remaining)

    def reset(
        self,
        key: str | None = None,
    ) -> None:
        """
        Reset limiter state.

        If key is None, reset all keys.
        """

        if key is None:
            self._events.clear()
        else:
            self._events.pop(key, None)
