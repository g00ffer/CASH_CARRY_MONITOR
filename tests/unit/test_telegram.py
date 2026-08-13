"""Tests for monitor.notifications.telegram with mock httpx"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from monitor.domain import AlertDeliveryStatus
from monitor.notifications import NotificationRateLimiter
from monitor.notifications.telegram import (
    TelegramNotifier,
    TelegramNotifierParams,
)


# ======================================================================
# Fake httpx.AsyncClient
# ======================================================================

@dataclass
class FakeResponse:
    status_code: int
    json_data: dict | None = None
    text_data: str = ""

    def json(self) -> dict:
        return self.json_data or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeAsyncClient:
    """
    In-memory fake of httpx.AsyncClient.
    Configurable responses for post() calls.
    """

    def __init__(
        self,
        *,
        responses: list[FakeResponse | Exception] | None = None,
        default_status: int = 200,
    ) -> None:
        self._responses = list(responses or [])
        self._default_status = default_status
        self.post_calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.post_calls.append({"url": url, "kwargs": kwargs})
        if self._responses:
            response = self._responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return FakeResponse(
            status_code=self._default_status,
            json_data={"ok": True, "result": {"message_id": 123}},
        )

    async def aclose(self) -> None:
        return None


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture
def fake_client():
    return FakeAsyncClient()


@pytest.fixture
def telegram_params():
    return TelegramNotifierParams(
        token="123456:ABCDEF",
        chat_id="987654321",
        enabled=True,
        timeout_ms=1000,
        retry_attempts=3,
        send_signal=True,
        send_warning=True,
        send_error=True,
        send_heartbeat=True,
    )


@pytest.fixture
def disabled_params():
    return TelegramNotifierParams(
        token="123456:ABCDEF",
        chat_id="987654321",
        enabled=False,
    )


@pytest.fixture
def send_signal_disabled_params():
    return TelegramNotifierParams(
        token="123456:ABCDEF",
        chat_id="987654321",
        enabled=True,
        send_signal=False,
    )


# ======================================================================
# Tests: successful delivery
# ======================================================================

class TestTelegramSuccess:
    @pytest.mark.asyncio
    async def test_send_signal_success(
        self, telegram_params, fake_client, monkeypatch,
    ):
        monkeypatch.setattr(
            "monitor.notifications.telegram.httpx.AsyncClient",
            lambda *a, **kw: fake_client,
        )
        notifier = TelegramNotifier(params=telegram_params)
        try:
            result = await notifier.send_signal(
                text="CARRY SIGNAL BTC_CARRY",
                now_ms=1710000001000,
            )
            assert result.status == AlertDeliveryStatus.SENT
            assert result.delivered is True
            assert len(fake_client.post_calls) >= 1
            # URL должен вести к sendMessage для нашего токена
            call = fake_client.post_calls[0]
            assert "123456:ABCDEF" in call["url"]
            assert "sendMessage" in call["url"]
        finally:
            await notifier.close()

    @pytest.mark.asyncio
    async def test_send_warning_success(
        self, telegram_params, fake_client, monkeypatch,
    ):
        monkeypatch.setattr(
            "monitor.notifications.telegram.httpx.AsyncClient",
            lambda *a, **kw: fake_client,
        )
        notifier = TelegramNotifier(params=telegram_params)
        try:
            result = await notifier.send_warning(
                text="WARNING: stale data",
                now_ms=1710000001000,
            )
            assert result.status == AlertDeliveryStatus.SENT
        finally:
            await notifier.close()

    @pytest.mark.asyncio
    async def test_send_heartbeat_success(
        self, telegram_params, fake_client, monkeypatch,
    ):
        monkeypatch.setattr(
            "monitor.notifications.telegram.httpx.AsyncClient",
            lambda *a, **kw: fake_client,
        )
        notifier = TelegramNotifier(params=telegram_params)
        try:
            result = await notifier.send_heartbeat(
                text="HEARTBEAT ok",
                now_ms=1710000001000,
            )
            assert result.status == AlertDeliveryStatus.SENT
        finally:
            await notifier.close()


# ======================================================================
# Tests: suppression
# ======================================================================

class TestTelegramSuppression:
    @pytest.mark.asyncio
    async def test_disabled_suppresses(self, disabled_params):
        notifier = TelegramNotifier(params=disabled_params)
        try:
            result = await notifier.send_signal(
                text="test", now_ms=1710000001000,
            )
            assert result.status == AlertDeliveryStatus.SUPPRESSED
            assert result.delivered is False
            assert result.suppressed_reason is not None
        finally:
            await notifier.close()

    @pytest.mark.asyncio
    async def test_send_signal_disabled_suppresses(
        self, send_signal_disabled_params, fake_client, monkeypatch,
    ):
        monkeypatch.setattr(
            "monitor.notifications.telegram.httpx.AsyncClient",
            lambda *a, **kw: fake_client,
        )
        notifier = TelegramNotifier(params=send_signal_disabled_params)
        try:
            result = await notifier.send_signal(
                text="test", now_ms=1710000001000,
            )
            assert result.status == AlertDeliveryStatus.SUPPRESSED
            # Никаких реальных HTTP-запросов
            assert len(fake_client.post_calls) == 0
        finally:
            await notifier.close()

    @pytest.mark.asyncio
    async def test_rate_limiter_suppresses(
        self, telegram_params, fake_client, monkeypatch,
    ):
        # Rate limiter: 1 сообщение в час
        limiter = NotificationRateLimiter(
            max_messages_per_hour=1,
            window_ms=3_600_000,
        )
        monkeypatch.setattr(
            "monitor.notifications.telegram.httpx.AsyncClient",
            lambda *a, **kw: fake_client,
        )
        notifier = TelegramNotifier(
            params=telegram_params, rate_limiter=limiter,
        )
        try:
            # Первое сообщение проходит
            r1 = await notifier.send_signal(
                text="first", now_ms=1710000001000,
            )
            assert r1.status == AlertDeliveryStatus.SENT

            # Второе в течение часа — SUPPRESSED
            r2 = await notifier.send_signal(
                text="second", now_ms=1710000002000,
            )
            assert r2.status == AlertDeliveryStatus.SUPPRESSED
            assert r2.delivered is False
            # Только один реальный запрос к Telegram
            assert len(fake_client.post_calls) == 1
        finally:
            await notifier.close()


# ======================================================================
# Tests: error handling & retry
# ======================================================================

class TestTelegramErrors:
    @pytest.mark.asyncio
    async def test_http_500_returns_failed(
        self, telegram_params, monkeypatch,
    ):
        # Первый ответ 500, второй 200 — retry должен сработать
        fake = FakeAsyncClient(responses=[
            FakeResponse(status_code=500),
            FakeResponse(status_code=200, json_data={"ok": True}),
        ])
        monkeypatch.setattr(
            "monitor.notifications.telegram.httpx.AsyncClient",
            lambda *a, **kw: fake,
        )
        # Короткий retry, чтобы тест был быстрым
        params = TelegramNotifierParams(
            token="123456:ABCDEF",
            chat_id="987654321",
            retry_attempts=3,
            timeout_ms=100,
        )
        notifier = TelegramNotifier(params=params)
        try:
            result = await notifier.send_signal(
                text="test", now_ms=1710000001000,
            )
            # После retry — успешно
            assert result.status == AlertDeliveryStatus.SENT
            assert len(fake.post_calls) == 2
        finally:
            await notifier.close()

    @pytest.mark.asyncio
    async def test_all_retries_exhausted_returns_failed(
        self, telegram_params, monkeypatch,
    ):
        # Все ответы 500 → после retries → FAILED
        fake = FakeAsyncClient(responses=[
            FakeResponse(status_code=500),
            FakeResponse(status_code=500),
            FakeResponse(status_code=500),
        ])
        monkeypatch.setattr(
            "monitor.notifications.telegram.httpx.AsyncClient",
            lambda *a, **kw: fake,
        )
        params = TelegramNotifierParams(
            token="123456:ABCDEF",
            chat_id="987654321",
            retry_attempts=3,
            timeout_ms=100,
        )
        notifier = TelegramNotifier(params=params)
        try:
            result = await notifier.send_signal(
                text="test", now_ms=1710000001000,
            )
            assert result.status == AlertDeliveryStatus.FAILED
            assert result.delivered is False
            assert result.error_message is not None
            assert len(fake.post_calls) == 3
        finally:
            await notifier.close()

    @pytest.mark.asyncio
    async def test_token_not_leaked_in_error_message(
        self, telegram_params, monkeypatch,
    ):
        # Все ответы 500 с ошибкой — error_message НЕ должен содержать токен
        secret_token = "123456:VERY_SECRET_BOT_TOKEN_XYZ"
        fake = FakeAsyncClient(responses=[
            FakeResponse(status_code=500),
            FakeResponse(status_code=500),
            FakeResponse(status_code=500),
        ])
        monkeypatch.setattr(
            "monitor.notifications.telegram.httpx.AsyncClient",
            lambda *a, **kw: fake,
        )
        params = TelegramNotifierParams(
            token=secret_token,
            chat_id="987654321",
            retry_attempts=3,
            timeout_ms=100,
        )
        notifier = TelegramNotifier(params=params)
        try:
            result = await notifier.send_signal(
                text="test", now_ms=1710000001000,
            )
            assert result.status == AlertDeliveryStatus.FAILED
            msg = result.error_message or ""
            assert secret_token not in msg, (
                "Telegram bot token leaked into error message"
            )
        finally:
            await notifier.close()

    @pytest.mark.asyncio
    async def test_timeout_returns_failed(
        self, telegram_params, monkeypatch,
    ):
        fake = FakeAsyncClient(responses=[
            asyncio.TimeoutError("connect timed out"),
            asyncio.TimeoutError("connect timed out"),
            asyncio.TimeoutError("connect timed out"),
        ])
        monkeypatch.setattr(
            "monitor.notifications.telegram.httpx.AsyncClient",
            lambda *a, **kw: fake,
        )
        params = TelegramNotifierParams(
            token="123456:ABCDEF",
            chat_id="987654321",
            retry_attempts=3,
            timeout_ms=100,
        )
        notifier = TelegramNotifier(params=params)
        try:
            result = await notifier.send_signal(
                text="test", now_ms=1710000001000,
            )
            assert result.status == AlertDeliveryStatus.FAILED
            assert result.error_message is not None
        finally:
            await notifier.close()


# ======================================================================
# Tests: TelegramNotifierParams validation
# ======================================================================

class TestTelegramNotifierParams:
    def test_required_fields(self):
        params = TelegramNotifierParams(
            token="abc",
            chat_id="123",
        )
        assert params.token == "abc"
        assert params.chat_id == "123"
        assert params.enabled is True
        assert params.send_signal is True

    def test_all_flags_disabled(self):
        params = TelegramNotifierParams(
            token="abc",
            chat_id="123",
            enabled=False,
            send_signal=False,
            send_warning=False,
            send_error=False,
            send_heartbeat=False,
        )
        assert params.enabled is False
        assert params.send_signal is False