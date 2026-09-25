"""Tests for AdminSubscriptionNotifyService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.admin_subscription_notify_service import AdminSubscriptionNotifyService


@pytest.fixture
def mock_user():
    return SimpleNamespace(
        id=123,
        telegram_id=456789,
        language='ru',
        email='user@example.com',
        email_verified=True,
    )


@pytest.fixture
def mock_subscription():
    return SimpleNamespace(
        id=99,
        tariff_id=1,
        traffic_limit_gb=50,
        device_limit=2,
        end_date=datetime.now(UTC) + timedelta(days=30),
        tariff=SimpleNamespace(name='Премиум'),
    )


@pytest.mark.asyncio
async def test_notify_subscription_granted(mock_user, mock_subscription):
    mock_db = AsyncMock()

    with patch.object(AdminSubscriptionNotifyService, '_send_tg_message', new_callable=AsyncMock) as mock_send_tg, \
         patch('app.cabinet.routes.websocket.notify_user_subscription_activated', new_callable=AsyncMock) as mock_ws:
        await AdminSubscriptionNotifyService.notify_subscription_granted(
            db=mock_db,
            user=mock_user,
            subscription=mock_subscription,
            days=30,
        )

        mock_send_tg.assert_called_once()
        mock_ws.assert_called_once()
        call_args = mock_send_tg.call_args[0]
        assert call_args[0] == mock_user
        assert 'Вам выдана подписка' in call_args[1]
        assert 'Премиум' in call_args[1]
        assert '30 дн.' in call_args[1]


@pytest.mark.asyncio
async def test_notify_subscription_extended(mock_user, mock_subscription):
    mock_db = AsyncMock()

    with patch.object(AdminSubscriptionNotifyService, '_send_tg_message', new_callable=AsyncMock) as mock_send_tg, \
         patch('app.cabinet.routes.websocket.notify_user_subscription_renewed', new_callable=AsyncMock) as mock_ws:
        await AdminSubscriptionNotifyService.notify_subscription_extended(
            db=mock_db,
            user=mock_user,
            subscription=mock_subscription,
            days=15,
        )

        mock_send_tg.assert_called_once()
        mock_ws.assert_called_once()
        call_args = mock_send_tg.call_args[0]
        assert 'Ваша подписка «Премиум» продлена' in call_args[1]
        assert '15 дн.' in call_args[1]


@pytest.mark.asyncio
async def test_notify_subscription_cancelled(mock_user, mock_subscription):
    mock_db = AsyncMock()

    with patch.object(AdminSubscriptionNotifyService, '_send_tg_message', new_callable=AsyncMock) as mock_send_tg, \
         patch('app.cabinet.routes.websocket.notify_user_subscription_expired', new_callable=AsyncMock) as mock_ws:
        await AdminSubscriptionNotifyService.notify_subscription_cancelled(
            db=mock_db,
            user=mock_user,
            subscription=mock_subscription,
            reason='Тестовая отмена',
        )

        mock_send_tg.assert_called_once()
        mock_ws.assert_called_once()
        call_args = mock_send_tg.call_args[0]
        assert 'Ваша подписка «Премиум» аннулирована' in call_args[1]
        assert 'Тестовая отмена' in call_args[1]
