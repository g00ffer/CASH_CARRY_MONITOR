"""Tests for monitor.utils.retry (remaining branches)"""
from __future__ import annotations

import asyncio

import pytest

from monitor.utils.retry import (
    DEFAULT_RETRY_POLICY,
    EXCHANGE_RETRY_POLICY,
    NON_CRITICAL_RETRY_POLICY,
    TELEGRAM_RETRY_POLICY,
    RetryPolicy,
    default_should_retry,
    retry_async,
    retry_sync,
)


class TestDefaultShouldRetry:
    def test_timeout_error(self):
        assert default_should_retry(TimeoutError()) is True

    def test_connection_error(self):
        assert default_should_retry(ConnectionError()) is True

    def test_cancelled_error(self):
        assert default_should_retry(asyncio.CancelledError()) is False

    def test_value_error_not_retried(self):
        assert default_should_retry(ValueError("bad value")) is False

    def test_key_error_not_retried(self):
        assert default_should_retry(KeyError("missing")) is False

    def test_message_contains_timeout(self):
        assert default_should_retry(RuntimeError("request timeout")) is True

    def test_message_contains_rate_limit(self):
        assert default_should_retry(RuntimeError("rate limit exceeded")) is True

    def test_message_contains_too_many_requests(self):
        assert default_should_retry(RuntimeError("too many requests")) is True

    def test_message_contains_connection(self):
        assert default_should_retry(RuntimeError("connection refused")) is True

    def test_message_contains_unavailable(self):
        assert default_should_retry(RuntimeError("service unavailable")) is True

    def test_generic_message_not_retried(self):
        assert default_should_retry(RuntimeError("something else")) is False

    def test_class_name_contains_timeout(self):
        class CustomTimeoutError(Exception):
            pass
        assert default_should_retry(CustomTimeoutError()) is True


class TestRetryPolicyValidation:
    def test_zero_attempts_raises(self):
        with pytest.raises(ValueError, match="max_attempts must be >= 1"):
            RetryPolicy(max_attempts=0)

    def test_negative_initial_delay_raises(self):
        with pytest.raises(ValueError, match="initial_delay_ms must be >= 0"):
            RetryPolicy(initial_delay_ms=-1)

    def test_negative_max_delay_raises(self):
        with pytest.raises(ValueError, match="max_delay_ms must be >= 0"):
            RetryPolicy(max_delay_ms=-1)

    def test_backoff_below_one_raises(self):
        with pytest.raises(ValueError, match="backoff_factor must be >= 1"):
            RetryPolicy(backoff_factor=0.5)

    def test_negative_jitter_raises(self):
        with pytest.raises(ValueError, match="jitter_ms must be >= 0"):
            RetryPolicy(jitter_ms=-1)


class TestRetryPolicyDelay:
    def test_exponential_backoff(self):
        policy = RetryPolicy(
            initial_delay_ms=100,
            max_delay_ms=10000,
            backoff_factor=2.0,
            jitter_ms=0,
        )
        assert policy.delay_ms(1) == 100
        assert policy.delay_ms(2) == 200
        assert policy.delay_ms(3) == 400

    def test_capped_by_max_delay(self):
        policy = RetryPolicy(
            initial_delay_ms=100,
            max_delay_ms=500,
            backoff_factor=2.0,
            jitter_ms=0,
        )
        assert policy.delay_ms(10) == 500

    def test_attempt_below_one_normalized(self):
        policy = RetryPolicy(initial_delay_ms=100, jitter_ms=0)
        assert policy.delay_ms(0) == 100


class TestRetryPolicyCanRetry:
    def test_retryable_error(self):
        policy = RetryPolicy()
        assert policy.can_retry(ConnectionError()) is True

    def test_non_retryable_error(self):
        policy = RetryPolicy()
        assert policy.can_retry(ValueError("bad")) is False

    def test_custom_should_retry(self):
        policy = RetryPolicy(should_retry=lambda e: isinstance(e, ValueError))
        assert policy.can_retry(ValueError("bad")) is True
        assert policy.can_retry(ConnectionError()) is False

    def test_exception_not_in_retry_on_exceptions(self):
        policy = RetryPolicy(retry_on_exceptions=(ConnectionError,))
        assert policy.can_retry(ValueError("bad")) is False


class TestPresets:
    def test_default_policy_exists(self):
        assert DEFAULT_RETRY_POLICY.max_attempts >= 1

    def test_exchange_policy(self):
        assert EXCHANGE_RETRY_POLICY.max_attempts >= 1

    def test_telegram_policy(self):
        assert TELEGRAM_RETRY_POLICY.max_attempts >= 1

    def test_non_critical_policy(self):
        assert NON_CRITICAL_RETRY_POLICY.max_attempts >= 1


class TestRetrySync:
    def test_success_first_try(self):
        result = retry_sync(lambda: "ok", policy=RetryPolicy(max_attempts=3, initial_delay_ms=1, jitter_ms=0))
        assert result == "ok"

    def test_success_after_retries(self):
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("fail")
            return "ok"

        policy = RetryPolicy(max_attempts=3, initial_delay_ms=1, jitter_ms=0)
        result = retry_sync(flaky, policy=policy)
        assert result == "ok"
        assert call_count == 3

    def test_all_attempts_exhausted(self):
        def always_fail():
            raise ConnectionError("always fail")

        policy = RetryPolicy(max_attempts=2, initial_delay_ms=1, jitter_ms=0)
        with pytest.raises(ConnectionError, match="always fail"):
            retry_sync(always_fail, policy=policy)

    def test_non_retryable_raises_immediately(self):
        call_count = 0

        def fail_value():
            nonlocal call_count
            call_count += 1
            raise ValueError("bad")

        policy = RetryPolicy(max_attempts=3, initial_delay_ms=1, jitter_ms=0)
        with pytest.raises(ValueError, match="bad"):
            retry_sync(fail_value, policy=policy)
        assert call_count == 1

    def test_on_retry_callback(self):
        retries_seen = []

        def on_retry(exc, attempt, delay):
            retries_seen.append(attempt)

        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("fail")
            return "ok"

        policy = RetryPolicy(max_attempts=3, initial_delay_ms=1, jitter_ms=0)
        retry_sync(flaky, policy=policy, on_retry=on_retry)
        assert retries_seen == [1]


class TestRetryAsync:
    @pytest.mark.asyncio
    async def test_success_first_try(self):
        async def ok():
            return "ok"

        result = await retry_async(ok, policy=RetryPolicy(max_attempts=3, initial_delay_ms=1, jitter_ms=0))
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_success_after_retries(self):
        call_count = 0

        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("fail")
            return "ok"

        policy = RetryPolicy(max_attempts=3, initial_delay_ms=1, jitter_ms=0)
        result = await retry_async(flaky, policy=policy)
        assert result == "ok"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_all_attempts_exhausted(self):
        async def always_fail():
            raise ConnectionError("always fail")

        policy = RetryPolicy(max_attempts=2, initial_delay_ms=1, jitter_ms=0)
        with pytest.raises(ConnectionError, match="always fail"):
            await retry_async(always_fail, policy=policy)

    @pytest.mark.asyncio
    async def test_non_retryable_raises_immediately(self):
        call_count = 0

        async def fail_value():
            nonlocal call_count
            call_count += 1
            raise ValueError("bad")

        policy = RetryPolicy(max_attempts=3, initial_delay_ms=1, jitter_ms=0)
        with pytest.raises(ValueError, match="bad"):
            await retry_async(fail_value, policy=policy)
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_async_on_retry_callback(self):
        retries_seen = []

        async def on_retry(exc, attempt, delay):
            retries_seen.append(attempt)

        call_count = 0

        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("fail")
            return "ok"

        policy = RetryPolicy(max_attempts=3, initial_delay_ms=1, jitter_ms=0)
        await retry_async(flaky, policy=policy, on_retry=on_retry)
        assert retries_seen == [1]