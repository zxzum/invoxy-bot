"""Tests for White Internet (LTE / whitelist) in admin user card."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.cabinet.routes import admin_users as m
from app.cabinet.schemas.users import (
    UpdateSubscriptionRequest,
    UserSubscriptionInfo,
)


@pytest.mark.asyncio
async def test_build_subscription_info_async_returns_whitelist_fields(monkeypatch):
    """Builder fills whitelist traffic fields, calculations, and purchases."""
    now = datetime.now(UTC)
    sub = SimpleNamespace(
        id=10,
        user_id=1,
        status='active',
        is_trial=False,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=29),
        traffic_limit_gb=50,
        traffic_used_gb=10.0,
        device_limit=2,
        tariff_id=None,
        autopay_enabled=False,
        purchased_traffic_gb=0,
        whitelist_traffic_limit_gb=10,
        whitelist_traffic_used_bytes=5 * (1024**3),  # 5 GB
        whitelist_traffic_purchased_gb=3,
        whitelist_traffic_reset_at=now + timedelta(days=15),
        connected_squads=['regular-squad', 'whitelist-squad-123'],
    )

    db = AsyncMock()
    mock_tp_res = MagicMock()
    mock_tp_res.scalars.return_value.all.return_value = []
    p1 = SimpleNamespace(
        id=101,
        subscription_id=10,
        traffic_gb=3,
        expires_at=now + timedelta(days=15),
        created_at=now - timedelta(days=1),
    )
    mock_wtp_res = MagicMock()
    mock_wtp_res.scalars.return_value.all.return_value = [p1]

    db.execute.side_effect = [mock_tp_res, mock_wtp_res]

    monkeypatch.setattr(m.settings, 'WHITELIST_TRAFFIC_ACCOUNTING_ENABLED', True)
    monkeypatch.setattr(m.settings, 'WHITELIST_SQUAD_UUID', 'whitelist-squad-123')
    monkeypatch.setattr(type(m.settings), 'is_platega_recurrent_enabled', lambda self: False, raising=False)

    info: UserSubscriptionInfo = await m._build_subscription_info_async(db, sub)

    assert info.whitelist_traffic_limit_gb == 10
    assert info.whitelist_traffic_used_gb == 5.0
    assert info.whitelist_traffic_used_percent == 50.0
    assert info.whitelist_traffic_purchased_gb == 3
    assert info.whitelist_exhausted is False
    assert info.whitelist_squad_attached is True
    assert len(info.whitelist_traffic_purchases) == 1
    assert info.whitelist_traffic_purchases[0].id == 101
    assert info.whitelist_traffic_purchases[0].traffic_gb == 3


@pytest.mark.asyncio
async def test_build_subscription_info_async_exhausted_flag(monkeypatch):
    """When used >= limit, whitelist_exhausted is True."""
    now = datetime.now(UTC)
    sub = SimpleNamespace(
        id=11,
        user_id=1,
        status='active',
        is_trial=False,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=29),
        traffic_limit_gb=50,
        traffic_used_gb=10.0,
        device_limit=2,
        tariff_id=None,
        autopay_enabled=False,
        purchased_traffic_gb=0,
        whitelist_traffic_limit_gb=5,
        whitelist_traffic_used_bytes=6 * (1024**3),  # 6 GB > 5 GB
        whitelist_traffic_purchased_gb=0,
        whitelist_traffic_reset_at=None,
        connected_squads=[],
    )

    db = AsyncMock()
    mock_empty = MagicMock()
    mock_empty.scalars.return_value.all.return_value = []
    db.execute.side_effect = [mock_empty, mock_empty]

    monkeypatch.setattr(type(m.settings), 'WHITELIST_TRAFFIC_ACCOUNTING_ENABLED', False, raising=False)
    monkeypatch.setattr(type(m.settings), 'is_platega_recurrent_enabled', lambda self: False, raising=False)

    info = await m._build_subscription_info_async(db, sub)
    assert info.whitelist_exhausted is True
    assert info.whitelist_squad_attached is None  # accounting disabled


@pytest.mark.asyncio
async def test_add_whitelist_traffic_action(monkeypatch):
    """Adding whitelist traffic invokes add_whitelist_subscription_traffic and syncs panel."""
    sub = SimpleNamespace(
        id=20,
        user_id=2,
        status='active',
        is_active=True,
        is_trial=False,
        whitelist_traffic_limit_gb=5,
        whitelist_traffic_used_bytes=0,
        connected_squads=['regular'],
    )
    user = SimpleNamespace(id=2, remnawave_id=200, subscriptions=[sub])

    monkeypatch.setattr(m, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(m.settings), 'is_multi_tariff_enabled', lambda self: False, raising=False)
    monkeypatch.setattr(m.settings, 'WHITELIST_SQUAD_UUID', 'whitelist-squad-uuid')

    mock_add_traffic = AsyncMock(return_value=sub)
    monkeypatch.setattr('app.database.crud.subscription.add_whitelist_subscription_traffic', mock_add_traffic)

    mock_sync = AsyncMock()
    monkeypatch.setattr(m, '_sync_whitelist_squads_to_panel', mock_sync)

    mock_info = MagicMock(spec=UserSubscriptionInfo)
    monkeypatch.setattr(m, '_build_subscription_info_async', AsyncMock(return_value=mock_info))

    db = AsyncMock()

    result = await m.update_user_subscription(
        user_id=2,
        request=UpdateSubscriptionRequest(action='add_whitelist_traffic', traffic_gb=10),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is True
    assert 'Added 10 GB White Internet' in result.message
    # Squad added to connected_squads
    assert 'whitelist-squad-uuid' in sub.connected_squads
    mock_add_traffic.assert_awaited_once_with(db, sub, 10, enforce_monthly_limit=False)
    mock_sync.assert_awaited_once_with(sub, user)


def test_add_whitelist_traffic_requires_positive_gb():
    """traffic_gb must be >= 1 at schema validation level."""
    with pytest.raises(ValidationError):
        UpdateSubscriptionRequest(action='add_whitelist_traffic', traffic_gb=0)


@pytest.mark.asyncio
async def test_remove_whitelist_traffic_action(monkeypatch):
    """Removing whitelist traffic purchase decrements counters and syncs panel."""
    sub = SimpleNamespace(
        id=30,
        user_id=3,
        status='active',
        is_active=True,
        is_trial=False,
        whitelist_traffic_limit_gb=15,
        whitelist_traffic_purchased_gb=10,
        whitelist_traffic_reset_at=datetime.now(UTC),
        connected_squads=['whitelist-squad'],
    )
    user = SimpleNamespace(id=3, remnawave_id=300, subscriptions=[sub])

    purchase = SimpleNamespace(
        id=55,
        subscription_id=30,
        traffic_gb=5,
    )

    monkeypatch.setattr(m, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(m.settings), 'is_multi_tariff_enabled', lambda self: False, raising=False)

    db = AsyncMock()
    mock_purchase_res = MagicMock()
    mock_purchase_res.scalar_one_or_none.return_value = purchase

    mock_remaining_res = MagicMock()
    mock_remaining_res.scalars.return_value.all.return_value = []

    db.execute.side_effect = [mock_purchase_res, mock_remaining_res]

    mock_sync = AsyncMock()
    monkeypatch.setattr(m, '_sync_whitelist_squads_to_panel', mock_sync)
    mock_info = MagicMock(spec=UserSubscriptionInfo)
    monkeypatch.setattr(m, '_build_subscription_info_async', AsyncMock(return_value=mock_info))

    result = await m.update_user_subscription(
        user_id=3,
        request=UpdateSubscriptionRequest(action='remove_whitelist_traffic', traffic_purchase_id=55),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is True
    assert 'Removed 5 GB White Internet' in result.message
    assert sub.whitelist_traffic_limit_gb == 10
    assert sub.whitelist_traffic_purchased_gb == 5
    assert sub.whitelist_traffic_reset_at is None
    db.delete.assert_awaited_once_with(purchase)
    mock_sync.assert_awaited_once_with(sub, user)


@pytest.mark.asyncio
async def test_reset_whitelist_used_action(monkeypatch):
    """Resetting whitelist used bytes zeroes the counter and syncs panel."""
    sub = SimpleNamespace(
        id=40,
        user_id=4,
        status='active',
        is_active=True,
        is_trial=False,
        whitelist_traffic_limit_gb=10,
        whitelist_traffic_used_bytes=10 * (1024**3),
        connected_squads=['whitelist-squad'],
    )
    user = SimpleNamespace(id=4, remnawave_id=400, subscriptions=[sub])

    monkeypatch.setattr(m, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(m.settings), 'is_multi_tariff_enabled', lambda self: False, raising=False)

    mock_sync = AsyncMock()
    monkeypatch.setattr(m, '_sync_whitelist_squads_to_panel', mock_sync)
    mock_info = MagicMock(spec=UserSubscriptionInfo)
    monkeypatch.setattr(m, '_build_subscription_info_async', AsyncMock(return_value=mock_info))

    db = AsyncMock()

    result = await m.update_user_subscription(
        user_id=4,
        request=UpdateSubscriptionRequest(action='reset_whitelist_used'),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is True
    assert sub.whitelist_traffic_used_bytes == 0
    mock_sync.assert_awaited_once_with(sub, user)


@pytest.mark.asyncio
async def test_adjust_whitelist_used_set_exact(monkeypatch):
    """Adjusting whitelist used bytes with exact GB updates bytes, syncs panel, and validates inputs."""
    from fastapi import HTTPException

    sub = SimpleNamespace(
        id=50,
        user_id=5,
        status='active',
        is_active=True,
        is_trial=False,
        whitelist_traffic_limit_gb=10,
        whitelist_traffic_used_bytes=1 * (1024**3),
        connected_squads=['whitelist-squad'],
    )
    user = SimpleNamespace(id=5, remnawave_id=500, subscriptions=[sub])

    monkeypatch.setattr(m, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(m.settings), 'is_multi_tariff_enabled', lambda self: False, raising=False)

    mock_sync = AsyncMock()
    monkeypatch.setattr(m, '_sync_whitelist_squads_to_panel', mock_sync)
    mock_info = MagicMock(spec=UserSubscriptionInfo)
    monkeypatch.setattr(m, '_build_subscription_info_async', AsyncMock(return_value=mock_info))

    db = AsyncMock()
    mock_snap_res = MagicMock()
    mock_snap_res.scalar_one_or_none.return_value = MagicMock()  # existing snapshot
    db.execute.return_value = mock_snap_res

    # 1. Exact set
    result = await m.update_user_subscription(
        user_id=5,
        request=UpdateSubscriptionRequest(action='adjust_whitelist_used', whitelist_traffic_used_gb=4.5),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is True
    assert sub.whitelist_traffic_used_bytes == int(4.5 * (1024**3))
    assert '4.50 GB' in result.message
    mock_sync.assert_awaited_once_with(sub, user)

    # 2. Neither whitelist_traffic_used_gb nor whitelist_traffic_delta_gb provided -> HTTPException(400)
    with pytest.raises(HTTPException) as exc_info:
        await m.update_user_subscription(
            user_id=5,
            request=UpdateSubscriptionRequest(action='adjust_whitelist_used'),
            admin=SimpleNamespace(id=1),
            db=db,
        )
    assert exc_info.value.status_code == 400
    assert 'Either whitelist_traffic_used_gb or whitelist_traffic_delta_gb is required' in exc_info.value.detail

    # 3. Negative whitelist_traffic_used_gb rejected by schema (ge=0)
    with pytest.raises(ValidationError):
        UpdateSubscriptionRequest(action='adjust_whitelist_used', whitelist_traffic_used_gb=-0.5)


@pytest.mark.asyncio
async def test_adjust_whitelist_used_delta_add_and_subtract(monkeypatch):
    """Adjusting whitelist used bytes with positive and negative deltas."""
    sub = SimpleNamespace(
        id=51,
        user_id=5,
        status='active',
        is_active=True,
        is_trial=False,
        whitelist_traffic_limit_gb=10,
        whitelist_traffic_used_bytes=5 * (1024**3),
        connected_squads=['whitelist-squad'],
    )
    user = SimpleNamespace(id=5, remnawave_id=500, subscriptions=[sub])

    monkeypatch.setattr(m, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(m.settings), 'is_multi_tariff_enabled', lambda self: False, raising=False)

    mock_sync = AsyncMock()
    monkeypatch.setattr(m, '_sync_whitelist_squads_to_panel', mock_sync)
    mock_info = MagicMock(spec=UserSubscriptionInfo)
    monkeypatch.setattr(m, '_build_subscription_info_async', AsyncMock(return_value=mock_info))

    db = AsyncMock()
    mock_snap_res = MagicMock()
    mock_snap_res.scalar_one_or_none.return_value = MagicMock()
    db.execute.return_value = mock_snap_res

    # 1. Delta add (+2.5 GB) -> 7.5 GB
    result_add = await m.update_user_subscription(
        user_id=5,
        request=UpdateSubscriptionRequest(action='adjust_whitelist_used', whitelist_traffic_delta_gb=2.5),
        admin=SimpleNamespace(id=1),
        db=db,
    )
    assert result_add.success is True
    assert sub.whitelist_traffic_used_bytes == int(7.5 * (1024**3))
    assert '7.50 GB' in result_add.message

    # 2. Delta subtract (-3.0 GB) -> 4.5 GB
    result_sub = await m.update_user_subscription(
        user_id=5,
        request=UpdateSubscriptionRequest(action='adjust_whitelist_used', whitelist_traffic_delta_gb=-3.0),
        admin=SimpleNamespace(id=1),
        db=db,
    )
    assert result_sub.success is True
    assert sub.whitelist_traffic_used_bytes == int(4.5 * (1024**3))
    assert '4.50 GB' in result_sub.message

    # 3. Delta subtract exceeding current usage (-10.0 GB) -> clamped to 0
    result_clamp = await m.update_user_subscription(
        user_id=5,
        request=UpdateSubscriptionRequest(action='adjust_whitelist_used', whitelist_traffic_delta_gb=-10.0),
        admin=SimpleNamespace(id=1),
        db=db,
    )
    assert result_clamp.success is True
    assert sub.whitelist_traffic_used_bytes == 0
    assert '0.00 GB' in result_clamp.message


@pytest.mark.asyncio
async def test_adjust_whitelist_used_ensures_snapshot_created(monkeypatch):
    """Ensures WhitelistTrafficUsageSnapshot is created if missing for the current month."""
    from app.database.models import WhitelistTrafficUsageSnapshot
    from app.utils.traffic_topup_limits import get_local_month_period_key

    sub = SimpleNamespace(
        id=60,
        user_id=6,
        status='active',
        is_active=True,
        is_trial=False,
        whitelist_traffic_limit_gb=10,
        whitelist_traffic_used_bytes=0,
        connected_squads=['whitelist-squad'],
    )
    user = SimpleNamespace(id=6, remnawave_id=600, subscriptions=[sub])

    monkeypatch.setattr(m, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(m.settings), 'is_multi_tariff_enabled', lambda self: False, raising=False)

    mock_sync = AsyncMock()
    monkeypatch.setattr(m, '_sync_whitelist_squads_to_panel', mock_sync)
    mock_info = MagicMock(spec=UserSubscriptionInfo)
    monkeypatch.setattr(m, '_build_subscription_info_async', AsyncMock(return_value=mock_info))

    # Case 1: No snapshot exists in DB -> created
    db = AsyncMock()
    db.add = MagicMock()
    mock_snap_res_none = MagicMock()
    mock_snap_res_none.scalar_one_or_none.return_value = None
    db.execute.return_value = mock_snap_res_none

    result = await m.update_user_subscription(
        user_id=6,
        request=UpdateSubscriptionRequest(action='adjust_whitelist_used', whitelist_traffic_used_gb=1.0),
        admin=SimpleNamespace(id=1),
        db=db,
    )
    assert result.success is True
    db.add.assert_called_once()
    added_obj = db.add.call_args[0][0]
    assert isinstance(added_obj, WhitelistTrafficUsageSnapshot)
    assert added_obj.subscription_id == 60
    assert added_obj.panel_user_id == 600
    assert added_obj.measured_bytes == 0
    assert added_obj.period_key == get_local_month_period_key(datetime.now(UTC))

    # Case 2: Snapshot already exists -> not added again
    db.reset_mock()
    mock_snap_res_exists = MagicMock()
    mock_snap_res_exists.scalar_one_or_none.return_value = MagicMock()
    db.execute.return_value = mock_snap_res_exists

    result_existing = await m.update_user_subscription(
        user_id=6,
        request=UpdateSubscriptionRequest(action='adjust_whitelist_used', whitelist_traffic_used_gb=2.0),
        admin=SimpleNamespace(id=1),
        db=db,
    )
    assert result_existing.success is True
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_reset_main_traffic_action(monkeypatch):
    """Resetting main traffic clears traffic_used_gb, resets panel traffic, and reactivates LIMITED status."""
    from app.database.models import SubscriptionStatus

    sub = SimpleNamespace(
        id=70,
        user_id=7,
        status=SubscriptionStatus.LIMITED.value,
        is_active=True,
        is_trial=False,
        traffic_used_gb=45.5,
        connected_squads=[],
    )
    user = SimpleNamespace(id=7, remnawave_id=700, subscriptions=[sub])

    monkeypatch.setattr(m, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(m.settings), 'is_multi_tariff_enabled', lambda self: False, raising=False)

    mock_info = MagicMock(spec=UserSubscriptionInfo)
    monkeypatch.setattr(m, '_build_subscription_info_async', AsyncMock(return_value=mock_info))

    mock_api = AsyncMock()
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_api
    mock_cm.__aexit__.return_value = None

    mock_service = MagicMock()
    mock_service.is_configured = True
    mock_service.get_api_client.return_value = mock_cm

    monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', lambda: mock_service)

    db = AsyncMock()

    result = await m.update_user_subscription(
        user_id=7,
        request=UpdateSubscriptionRequest(action='reset_main_traffic'),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is True
    assert 'Main traffic usage reset to 0 in database and RemnaWave' in result.message
    assert sub.traffic_used_gb == 0.0
    assert sub.status == SubscriptionStatus.ACTIVE.value
    mock_api.reset_user_traffic.assert_awaited_once_with(700)
    mock_api.enable_user.assert_awaited_once_with(700)
    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once_with(sub)

    # Also test reset_traffic alias
    sub.traffic_used_gb = 10.0
    result_alias = await m.update_user_subscription(
        user_id=7,
        request=UpdateSubscriptionRequest(action='reset_traffic'),
        admin=SimpleNamespace(id=1),
        db=db,
    )
    assert result_alias.success is True
    assert sub.traffic_used_gb == 0.0


def test_permission_users_subscription_registered():
    """users:subscription must be in the permissions registry."""
    from app.services.permission_service import get_all_permissions

    assert 'users:subscription' in get_all_permissions()
