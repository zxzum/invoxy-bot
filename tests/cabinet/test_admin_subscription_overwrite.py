"""Tests for subscription overwrite with double confirmation and device limits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_users
from app.cabinet.schemas.users import UpdateSubscriptionRequest


USER_ID = 42
SUB_ID = 101
PANEL_USER_ID = 8888


def _build_user(sub: SimpleNamespace | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=USER_ID,
        telegram_id=123456,
        full_name='Test User',
        username='testuser',
        email='test@example.com',
        remnawave_id=PANEL_USER_ID,
        subscriptions=[sub] if sub else [],
    )


def _build_sub(is_active: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=SUB_ID,
        user_id=USER_ID,
        remnawave_id=PANEL_USER_ID,
        is_active=is_active,
        status='active' if is_active else 'expired',
        is_trial=False,
        tariff_id=1,
        end_date=datetime.now(UTC) + timedelta(days=15) if is_active else datetime.now(UTC) - timedelta(days=1),
        traffic_limit_gb=100,
        traffic_used_gb=10.0,
        device_limit=2,
        connected_squads=['squad-1'],
        whitelist_traffic_limit_gb=0,
        whitelist_traffic_used_bytes=0,
        whitelist_traffic_purchased_gb=0,
        autopay_enabled=False,
        autopay_days_before=3,
        subscription_url='https://example.com/sub',
        subscription_crypto_link=None,
        remnawave_short_uuid='abc123',
    )


@pytest.mark.asyncio
async def test_create_subscription_without_overwrite_fails_when_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """When a user has an active subscription, creating without overwrite=True must raise 409."""
    sub = _build_sub(is_active=True)
    user = _build_user(sub)
    db = AsyncMock()

    monkeypatch.setattr(type(admin_users.settings), 'is_multi_tariff_enabled', lambda self: False)

    with patch.object(admin_users, 'get_user_by_id', AsyncMock(return_value=user)):
        request = UpdateSubscriptionRequest(action='create', days=30, overwrite=False)
        with pytest.raises(HTTPException) as exc_info:
            await admin_users.update_user_subscription(USER_ID, request, admin=SimpleNamespace(id=1), db=db)

        assert exc_info.value.status_code == 409
        assert 'Confirmation required to overwrite' in exc_info.value.detail


@pytest.mark.asyncio
async def test_create_subscription_with_overwrite_replaces_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """When overwrite=True, replace_subscription is called with provided parameters including device_limit."""
    sub = _build_sub(is_active=True)
    user = _build_user(sub)
    db = AsyncMock()

    monkeypatch.setattr(type(admin_users.settings), 'is_multi_tariff_enabled', lambda self: False)

    replaced_sub = SimpleNamespace(
        id=SUB_ID,
        user_id=USER_ID,
        remnawave_id=PANEL_USER_ID,
        is_active=True,
        status='active',
        is_trial=False,
        tariff_id=2,
        end_date=datetime.now(UTC) + timedelta(days=60),
        traffic_limit_gb=200,
        traffic_used_gb=0.0,
        device_limit=5,
        connected_squads=['squad-2'],
        whitelist_traffic_limit_gb=0,
        whitelist_traffic_used_bytes=0,
        whitelist_traffic_purchased_gb=0,
        autopay_enabled=False,
        autopay_days_before=3,
        subscription_url='https://example.com/sub2',
        subscription_crypto_link=None,
        remnawave_short_uuid='abc123',
    )

    tariff_mock = SimpleNamespace(
        id=2,
        name='Tariff 2',
        traffic_limit_gb=200,
        device_limit=3,
        allowed_squads=['squad-2'],
    )

    with (
        patch.object(admin_users, 'get_user_by_id', AsyncMock(return_value=user)),
        patch.object(admin_users, 'get_tariff_by_id', AsyncMock(return_value=tariff_mock)),
        patch('app.database.crud.subscription.replace_subscription', AsyncMock(return_value=replaced_sub)) as mock_replace,
        patch('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', AsyncMock()) as mock_platega,
        patch('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', AsyncMock()) as mock_lava,
        patch.object(admin_users, '_sync_subscription_to_panel', AsyncMock()) as mock_sync,
        patch.object(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None)),
        patch('app.services.admin_subscription_notify_service.AdminSubscriptionNotifyService.notify_subscription_granted', AsyncMock()) as mock_notify,
    ):
        request = UpdateSubscriptionRequest(
            action='create',
            tariff_id=2,
            days=60,
            device_limit=5,
            overwrite=True,
        )
        response = await admin_users.update_user_subscription(USER_ID, request, admin=SimpleNamespace(id=1), db=db)

        assert response.success is True
        mock_platega.assert_awaited_once_with(db, SUB_ID)
        mock_lava.assert_awaited_once_with(db, SUB_ID)
        mock_replace.assert_awaited_once_with(
            db=db,
            subscription=sub,
            duration_days=60,
            traffic_limit_gb=200,
            device_limit=5,
            connected_squads=['squad-2'],
            is_trial=False,
            tariff_id=2,
            update_server_counters=True,
            commit=True,
        )
        mock_sync.assert_awaited_once_with(db, user, replaced_sub)
        mock_notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_subscription_for_new_user_passes_device_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """When a user has no subscriptions, create_paid_subscription receives custom device_limit."""
    user = _build_user(None)
    db = AsyncMock()

    monkeypatch.setattr(type(admin_users.settings), 'is_multi_tariff_enabled', lambda self: False)

    new_sub = SimpleNamespace(
        id=202,
        user_id=USER_ID,
        remnawave_id=None,
        is_active=True,
        status='active',
        is_trial=False,
        tariff_id=1,
        end_date=datetime.now(UTC) + timedelta(days=30),
        traffic_limit_gb=100,
        traffic_used_gb=0.0,
        device_limit=4,
        connected_squads=[],
    )

    with (
        patch.object(admin_users, 'get_user_by_id', AsyncMock(return_value=user)),
        patch('app.database.crud.subscription.create_paid_subscription', AsyncMock(return_value=new_sub)) as mock_create,
        patch.object(admin_users, '_sync_subscription_to_panel', AsyncMock()) as mock_sync,
        patch.object(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None)),
        patch('app.services.admin_subscription_notify_service.AdminSubscriptionNotifyService.notify_subscription_granted', AsyncMock()),
    ):
        request = UpdateSubscriptionRequest(
            action='create',
            days=30,
            device_limit=4,
            overwrite=False,
        )
        response = await admin_users.update_user_subscription(USER_ID, request, admin=SimpleNamespace(id=1), db=db)

        assert response.success is True
        mock_create.assert_awaited_once_with(
            db=db,
            user_id=USER_ID,
            duration_days=30,
            traffic_limit_gb=100,
            device_limit=4,
            is_trial=False,
            tariff_id=None,
            connected_squads=[],
        )
        mock_sync.assert_awaited_once_with(db, user, new_sub)
