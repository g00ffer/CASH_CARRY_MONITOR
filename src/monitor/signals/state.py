from __future__ import annotations

from typing import Protocol, runtime_checkable

from monitor.domain import (
    AlertState,
    SignalDecision,
    SignalState,
)


@runtime_checkable
class SignalStateStore(Protocol):
    """
    Abstract storage for per-symbol signal state.

    Stage 1 implementation is in-memory.
    Later this can be backed by SQLite.
    """

    def get(self, symbol_name: str) -> AlertState | None:
        ...

    def upsert(self, state: AlertState) -> None:
        ...

    def all(self) -> list[AlertState]:
        ...


class InMemorySignalStateStore:
    """
    In-memory signal state store.

    For Stage 1 this is enough.
    State can later be duplicated into SQLite for audit/restart recovery.
    """

    def __init__(self) -> None:
        self._states: dict[str, AlertState] = {}

    def get(self, symbol_name: str) -> AlertState | None:
        return self._states.get(symbol_name)

    def upsert(self, state: AlertState) -> None:
        self._states[state.symbol_name] = state

    def all(self) -> list[AlertState]:
        return list(self._states.values())


def create_alert_state(
    *,
    symbol_name: str,
    now_ms: int,
) -> AlertState:
    """
    Create initial empty alert state.
    """

    return AlertState(
        symbol_name=symbol_name,
        state=SignalState.NORMAL,
        consecutive_confirmations=0,
        last_alert_ts_ms=None,
        last_signal_started_ts_ms=None,
        last_net_annual=None,
        last_reasons=(),
        updated_at_ms=now_ms,
    )


def update_alert_state(
    *,
    current: AlertState | None,
    decision: SignalDecision,
    now_ms: int,
) -> AlertState:
    """
    Create next immutable AlertState from signal decision.
    """

    if current is None:
        current = create_alert_state(
            symbol_name=decision.symbol_name,
            now_ms=now_ms,
        )

    last_alert_ts_ms = current.last_alert_ts_ms

    if decision.should_alert:
        last_alert_ts_ms = now_ms

    active_states = (
        SignalState.SIGNAL_ACTIVE,
        SignalState.COOLDOWN,
    )

    if decision.state in active_states:
        if current.state not in active_states:
            last_signal_started_ts_ms = now_ms
        else:
            last_signal_started_ts_ms = current.last_signal_started_ts_ms
    else:
        last_signal_started_ts_ms = None

    last_net_annual = (
        decision.metrics.net_annual
        if decision.metrics is not None
        else current.last_net_annual
    )

    return AlertState(
        symbol_name=decision.symbol_name,
        state=decision.state,
        consecutive_confirmations=decision.consecutive_confirmations,
        last_alert_ts_ms=last_alert_ts_ms,
        last_signal_started_ts_ms=last_signal_started_ts_ms,
        last_net_annual=last_net_annual,
        last_reasons=decision.reasons,
        updated_at_ms=now_ms,
    )
