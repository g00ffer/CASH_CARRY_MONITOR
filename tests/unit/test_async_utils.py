"""Tests for monitor.utils.async_utils"""
from __future__ import annotations

import asyncio

import pytest

from monitor.utils import next_tick_delay_ms, utc_now_ms, wait_for_stop
from monitor.utils.async_utils import gather_dict, run_periodic, sleep_ms


class TestGatherDict:
    @pytest.mark.asyncio
    async def test_returns_named_results(self):
        async def a():
            return 1

        async def b():
            return "x"

        result = await gather_dict({"a": a(), "b": b()})
        assert result == {"a": 1, "b": "x"}

    @pytest.mark.asyncio
    async def test_empty_mapping(self):
        result = await gather_dict({})
        assert result == {}

    @pytest.mark.asyncio
    async def test_exceptions_returned_not_raised(self):
        async def ok():
            return 1

        async def bad():
            raise ValueError("boom")

        result = await gather_dict({"ok": ok(), "bad": bad()})
        assert result["ok"] == 1
        assert isinstance(result["bad"], ValueError)

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self):
        async def ok():
            return 1

        async def cancelled():
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await gather_dict({"ok": ok(), "cancelled": cancelled()})

    @pytest.mark.asyncio
    async def test_keys_preserved(self):
        async def make(v):
            return v

        result = await gather_dict(
            {"spot": make("s"), "perp": make("p")},
        )
        assert set(result.keys()) == {"spot", "perp"}


class TestWaitForStop:
    @pytest.mark.asyncio
    async def test_returns_after_timeout(self):
        stop = asyncio.Event()
        await wait_for_stop(stop_event=stop, timeout_ms=50)
        assert not stop.is_set()

    @pytest.mark.asyncio
    async def test_returns_immediately_when_set(self):
        stop = asyncio.Event()
        stop.set()
        await wait_for_stop(stop_event=stop, timeout_ms=10_000)
        assert stop.is_set()


class TestSleepMs:
    @pytest.mark.asyncio
    async def test_sleep_returns(self):
        await sleep_ms(10)


class TestNextTickDelay:
    def test_normal_delay(self):
        started = utc_now_ms() - 3000
        delay = next_tick_delay_ms(interval_ms=10000, started_at_ms=started)
        assert 6000 <= delay <= 8000

    def test_overrun_returns_non_negative(self):
        started = utc_now_ms() - 15000
        delay = next_tick_delay_ms(interval_ms=10000, started_at_ms=started)
        assert delay >= 0


class TestRunPeriodic:
    @pytest.mark.asyncio
    async def test_runs_until_stop(self):
        stop = asyncio.Event()
        calls = []

        async def task():
            calls.append(1)
            if len(calls) >= 3:
                stop.set()

        await run_periodic(
            interval_ms=10,
            task=task,
            stop_event=stop,
            run_immediately=True,
        )
        assert len(calls) >= 3

    @pytest.mark.asyncio
    async def test_catch_exceptions_true(self):
        stop = asyncio.Event()
        calls = []

        async def flaky():
            calls.append(1)
            if len(calls) >= 2:
                stop.set()
            raise RuntimeError("transient")

        await run_periodic(
            interval_ms=10,
            task=flaky,
            stop_event=stop,
            catch_exceptions=True,
        )
        assert len(calls) >= 2