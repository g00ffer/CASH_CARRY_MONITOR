"""Tests for monitor.notifications.rate_limiter"""
from __future__ import annotations

import pytest

from monitor.notifications.rate_limiter import (
    NotificationRateLimiter,
    RateLimiterParams,
)

NOW = 1710000000000


class TestRateLimiterParams:
    def test_valid(self):
        params = RateLimiterParams(max_messages_per_hour=20, window_ms=3600000)
        assert params.max_messages_per_hour == 20

    def test_zero_messages_raises(self):
        with pytest.raises(ValueError, match="max_messages_per_hour must be > 0"):
            RateLimiterParams(max_messages_per_hour=0)

    def test_zero_window_raises(self):
        with pytest.raises(ValueError, match="window_ms must be > 0"):
            RateLimiterParams(max_messages_per_hour=20, window_ms=0)


class TestNotificationRateLimiter:
    def test_allows_within_limit(self):
        limiter = NotificationRateLimiter(
            RateLimiterParams(max_messages_per_hour=3, window_ms=3600000)
        )
        assert limiter.allow("key", NOW) is True
        assert limiter.allow("key", NOW + 1000) is True
        assert limiter.allow("key", NOW + 2000) is True

    def test_blocks_over_limit(self):
        limiter = NotificationRateLimiter(
            RateLimiterParams(max_messages_per_hour=2, window_ms=3600000)
        )
        assert limiter.allow("key", NOW) is True
        assert limiter.allow("key", NOW + 1000) is True
        assert limiter.allow("key", NOW + 2000) is False

    def test_window_expiry(self):
        limiter = NotificationRateLimiter(
            RateLimiterParams(max_messages_per_hour=1, window_ms=3600000)
        )
        assert limiter.allow("key", NOW) is True
        assert limiter.allow("key", NOW + 1000) is False
        # After window expires, allow again
        assert limiter.allow("key", NOW + 3600001) is True

    def test_separate_keys(self):
        limiter = NotificationRateLimiter(
            RateLimiterParams(max_messages_per_hour=1, window_ms=3600000)
        )
        assert limiter.allow("key1", NOW) is True
        assert limiter.allow("key2", NOW) is True
        assert limiter.allow("key1", NOW + 1000) is False
        assert limiter.allow("key2", NOW + 1000) is False

    def test_remaining(self):
        limiter = NotificationRateLimiter(
            RateLimiterParams(max_messages_per_hour=5, window_ms=3600000)
        )
        assert limiter.remaining("key", NOW) == 5
        limiter.allow("key", NOW)
        assert limiter.remaining("key", NOW) == 4
        limiter.allow("key", NOW + 1000)
        assert limiter.remaining("key", NOW + 1000) == 3

    def test_remaining_never_negative(self):
        limiter = NotificationRateLimiter(
            RateLimiterParams(max_messages_per_hour=1, window_ms=3600000)
        )
        limiter.allow("key", NOW)
        limiter.allow("key", NOW + 1000)  # blocked
        assert limiter.remaining("key", NOW + 1000) == 0

    def test_reset_specific_key(self):
        limiter = NotificationRateLimiter(
            RateLimiterParams(max_messages_per_hour=1, window_ms=3600000)
        )
        limiter.allow("key", NOW)
        assert limiter.allow("key", NOW + 1000) is False
        limiter.reset("key")
        assert limiter.allow("key", NOW + 2000) is True

    def test_reset_all_keys(self):
        limiter = NotificationRateLimiter(
            RateLimiterParams(max_messages_per_hour=1, window_ms=3600000)
        )
        limiter.allow("key1", NOW)
        limiter.allow("key2", NOW)
        limiter.reset()
        assert limiter.allow("key1", NOW + 1000) is True
        assert limiter.allow("key2", NOW + 1000) is True

    def test_reset_nonexistent_key(self):
        limiter = NotificationRateLimiter()
        limiter.reset("nonexistent")  # Should not raise

    def test_default_params(self):
        limiter = NotificationRateLimiter()
        for i in range(20):
            assert limiter.allow("key", NOW + i) is True
        assert limiter.allow("key", NOW + 21) is False