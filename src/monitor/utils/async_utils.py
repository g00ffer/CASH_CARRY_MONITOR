from __future__ import annotations

import asyncio
from inspect import isawaitable
from typing import Any, Awaitable, Callable, Iterable, Mapping, TypeVar

from .time import utc_now_ms

T = TypeVar("T")


# ---------------------------------------------------------------------
# Sleep / timeout
# ---------------------------------------------------------------------


async def sleep_ms(ms: int | float) -> None:
    """
    Async sleep for given number of milliseconds.
    """

    if ms <= 0:
        return

    await asyncio.sleep(float(ms) / 1000.0)


async def wait_for_ms(
    awaitable: Awaitable[T],
    timeout_ms: int | float | None,
) -> T:
    """
    Await awaitable with timeout in milliseconds.

    If timeout_ms is None or <= 0, await without timeout.
    """

    if timeout_ms is None or timeout_ms <= 0:
        return await awaitable

    return await asyncio.wait_for(
        awaitable,
        timeout=float(timeout_ms) / 1000.0,
    )


# ---------------------------------------------------------------------
# Gather helpers
# ---------------------------------------------------------------------


async def gather_dict(
    coros: Mapping[str, Awaitable[Any]],
) -> dict[str, Any]:
    """
    Gather named awaitables.

    Returns dict with same keys. Values may be results or exceptions.
    """

    keys = list(coros.keys())
    awaitables = [coros[key] for key in keys]

    results = await asyncio.gather(
        *awaitables,
        return_exceptions=True,
    )

    return dict(zip(keys, results))


def split_results(
    results: Iterable[Any],
) -> tuple[list[Any], list[BaseException]]:
    """
    Split gather results into successful values and exceptions.
    """

    ok: list[Any] = []
    errors: list[BaseException] = []

    for result in results:
        if isinstance(result, BaseException):
            errors.append(result)
        else:
            ok.append(result)

    return ok, errors


# ---------------------------------------------------------------------
# Stop event helpers
# ---------------------------------------------------------------------


async def wait_for_stop(
    stop_event: asyncio.Event | None,
    timeout_ms: int | float | None,
) -> bool:
    """
    Wait until stop event is set or timeout happens.

    Returns True if stop event is set, False if timeout happened.
    """

    if timeout_ms is None:
        if stop_event is None:
            raise ValueError("timeout_ms cannot be None when stop_event is None")

        await stop_event.wait()
        return True

    if stop_event is None:
        await sleep_ms(timeout_ms)
        return False

    if stop_event.is_set():
        return True

    if timeout_ms <= 0:
        return stop_event.is_set()

    try:
        await asyncio.wait_for(
            stop_event.wait(),
            timeout=float(timeout_ms) / 1000.0,
        )
        return True
    except asyncio.TimeoutError:
        return False


def next_tick_delay_ms(
    interval_ms: int,
    started_at_ms: int,
    now_ms: int | None = None,
) -> int:
    """
    Calculate how long to sleep until next polling tick.
    """

    now = utc_now_ms() if now_ms is None else int(now_ms)
    elapsed = now - int(started_at_ms)

    return max(0, int(interval_ms) - elapsed)


# ---------------------------------------------------------------------
# Periodic runner
# ---------------------------------------------------------------------


async def maybe_await(value: Any) -> Any:
    """
    Await value if it is awaitable, otherwise return as-is.
    """

    if isawaitable(value):
        return await value

    return value


async def run_periodic(
    interval_ms: int,
    task: Callable[[], Awaitable[None]],
    stop_event: asyncio.Event | None = None,
    run_immediately: bool = True,
    catch_exceptions: bool = False,
    on_error: Callable[[Exception], Any] | None = None,
) -> None:
    """
    Run async task periodically with fixed interval.

    If task execution takes longer than interval, next execution starts
    immediately after previous execution finishes.

    If catch_exceptions is False, first task exception stops the loop.

    If catch_exceptions is True, task exceptions are suppressed and optionally
    passed to on_error.
    """

    if interval_ms <= 0:
        raise ValueError("interval_ms must be positive")

    first_iteration = True

    while True:
        if stop_event is not None and stop_event.is_set():
            break

        started_at_ms = utc_now_ms()

        if not first_iteration or run_immediately:
            try:
                await task()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not catch_exceptions:
                    raise

                if on_error is not None:
                    await maybe_await(on_error(exc))

        first_iteration = False

        elapsed_ms = utc_now_ms() - started_at_ms
        delay_ms = max(0, interval_ms - elapsed_ms)

        stopped = await wait_for_stop(stop_event, delay_ms)

        if stopped:
            break
