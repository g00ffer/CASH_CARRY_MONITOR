from __future__ import annotations

from decimal import Decimal

from monitor.utils import (
    to_decimal,
)


def calc_cooldown_remaining_sec(
    last_alert_ts_ms: int | None,
    now_ms: int,
    cooldown_sec: int,
) -> int:
    """
    Calculate remaining cooldown in seconds.

    Returns 0 if cooldown is not active.
    """

    if last_alert_ts_ms is None:
        return 0

    if cooldown_sec <= 0:
        return 0

    elapsed_ms = int(now_ms) - int(last_alert_ts_ms)

    # If last alert timestamp is in the future, treat cooldown as active.
    if elapsed_ms < 0:
        return int(cooldown_sec)

    cooldown_ms = int(cooldown_sec) * 1000
    remaining_ms = cooldown_ms - elapsed_ms

    if remaining_ms <= 0:
        return 0

    # Round up to whole seconds.
    return (remaining_ms + 999) // 1000


def is_cooldown_active(
    last_alert_ts_ms: int | None,
    now_ms: int,
    cooldown_sec: int,
) -> bool:
    """
    Return True if cooldown is active.
    """

    return calc_cooldown_remaining_sec(
        last_alert_ts_ms=last_alert_ts_ms,
        now_ms=now_ms,
        cooldown_sec=cooldown_sec,
    ) > 0


def hysteresis_threshold(
    base_threshold: Decimal,
    hysteresis: Decimal,
    is_active: bool,
) -> Decimal:
    """
    Return effective threshold with hysteresis.

    If signal is already active, turning it off requires metric to fall
    below:

        base_threshold - hysteresis

    Example:
        base_threshold = 8%
        hysteresis = 1%

        signal turns on at >= 8%
        signal turns off only below 7%
    """

    base_threshold_decimal = to_decimal(base_threshold)
    hysteresis_decimal = to_decimal(hysteresis)

    if is_active:
        return base_threshold_decimal - hysteresis_decimal

    return base_threshold_decimal


def meets_threshold_with_hysteresis(
    value: Decimal,
    base_threshold: Decimal,
    hysteresis: Decimal,
    is_active: bool,
) -> bool:
    """
    Check whether value meets threshold with hysteresis.
    """

    threshold = hysteresis_threshold(
        base_threshold=base_threshold,
        hysteresis=hysteresis,
        is_active=is_active,
    )

    return to_decimal(value) >= threshold
