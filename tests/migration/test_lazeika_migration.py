"""Tests for Lazeika VPN to Invoxy VPN migration."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.database.models import SubscriptionStatus
from app.services.migration.lazeika_client import LazeikaClient
from app.services.migration.migration_service import (
    check_migration_eligibility,
    execute_user_migration,
)
from app.services.migration.tariff_mapping import resolve_target_tariff


@pytest.mark.asyncio
async def test_tariff_mapping_resolution():
    """Verify tariff mapping correctly resolves Lazeika tariffs."""
    mock_db = AsyncMock()
    # Mock DB return None so fallback activates
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    mock_db.execute.return_value = mock_result

    # 1. Premium -> Premium LTE
    t1 = await resolve_target_tariff(mock_db, 'Премиум')
    assert t1.name == 'Премиум LTE'
    assert t1.tariff_id == 4
    assert t1.traffic_limit_gb == 1000
    assert t1.whitelist_traffic_limit_gb == 150
    assert t1.device_limit == 10

    # 2. Standard -> Standard LTE
    t2 = await resolve_target_tariff(mock_db, 'Стандарт')
    assert t2.name == 'Стандарт LTE'
    assert t2.tariff_id == 3
    assert t2.traffic_limit_gb == 600
    assert t2.whitelist_traffic_limit_gb == 50
    assert t2.device_limit == 5

    # 3. Simple -> Base
    t3 = await resolve_target_tariff(mock_db, 'Обычный ВПН')
    assert t3.name == 'Базовый'
    assert t3.tariff_id == 2
    assert t3.traffic_limit_gb == 300
    assert t3.whitelist_traffic_limit_gb == 0
    assert t3.device_limit == 5


@pytest.mark.asyncio
async def test_check_migration_eligibility_success():
    """Verify check_migration_eligibility correctly parses candidate and adds +5 bonus days."""
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    mock_db.execute.return_value = mock_result

    mock_client_context = {
        'found': True,
        'client': {
            'id': 'lazeika_c1',
            'telegramId': '123456789',
            'email': 'user@example.com',
            'isBlocked': False,
        },
        'subscriptions': [
            {
                'id': 'lazeika_s1',
                'tariffName': 'Стандарт',
                'status': 'ACTIVE',
                'expiresAt': (datetime.now(UTC) + timedelta(days=10)).isoformat(),
                'remnawaveUuid': '183',
                'deviceLimit': 5,
                'trafficLimitBytes': '805306368000',
                'localTrafficLimitBytes': '53687091200',
            }
        ],
    }

    with patch.object(LazeikaClient, 'get_client_context', new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_client_context

        candidate = await check_migration_eligibility(mock_db, telegram_id=123456789)
        assert candidate is not None
        assert candidate.client_id == 'lazeika_c1'
        assert candidate.remnawave_uuid == '183'
        assert candidate.lazeika_tariff_name == 'Стандарт'
        assert candidate.target_tariff_name == 'Стандарт LTE'
        # 10 days remaining + 5 bonus days = 15 total days
        assert candidate.remaining_days in (9, 10)
        assert candidate.bonus_days == 5
        assert candidate.total_days == candidate.remaining_days + 5
        assert candidate.device_limit >= 5


@pytest.mark.asyncio
async def test_check_migration_skips_when_already_has_active_paid():
    """User who already has an active paid subscription in Invoxy must not be prompted to migrate."""
    mock_db = AsyncMock()
    user = SimpleNamespace(id=1, telegram_id=123456789, email=None)

    active_sub = SimpleNamespace(
        id=99,
        user_id=1,
        is_trial=False,
        status=SubscriptionStatus.ACTIVE.value,
    )

    with patch('app.services.migration.migration_service.get_active_subscriptions_by_user_id', new_callable=AsyncMock) as mock_subs:
        mock_subs.return_value = [active_sub]

        candidate = await check_migration_eligibility(mock_db, telegram_id=123456789, user=user)
        assert candidate is None


@pytest.mark.asyncio
async def test_execute_user_migration_full_flow():
    """Verify execution of migration updates Remnawave, Invoxy DB, and annuls Lazeika subscription."""
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    mock_db.execute.return_value = mock_result

    user = SimpleNamespace(
        id=42,
        telegram_id=123456789,
        username='cooluser',
        email='cool@example.com',
        remnawave_id=None,
        remnawave_uuid=None,
    )

    candidate = SimpleNamespace(
        client_id='laz_c1',
        subscription_id='laz_s1',
        lazeika_tariff_name='Премиум',
        target_tariff_id=4,
        target_tariff_name='Премиум LTE',
        remaining_days=20,
        bonus_days=5,
        total_days=25,
        device_limit=10,
        traffic_limit_gb=1000,
        whitelist_traffic_limit_gb=150,
        remnawave_uuid='183',
        expires_at_str='25.10.2026',
        new_end_date=datetime.now(UTC) + timedelta(days=25),
    )

    mock_panel_user = SimpleNamespace(
        id=183,
        username='invoxy_123456789',
        short_uuid='test-uuid-short',
        subscription_url='https://sub.invoxy.my/test-uuid-short',
    )

    mock_api = AsyncMock()
    mock_api.update_user.return_value = mock_panel_user
    mock_api.reset_user_traffic.return_value = True

    mock_remna_service = MagicMock()
    mock_remna_service.get_api_client.return_value.__aenter__.return_value = mock_api

    with (
        patch('app.services.migration.migration_service.check_migration_eligibility', new_callable=AsyncMock) as mock_check,
        patch('app.services.migration.migration_service.RemnaWaveService', return_value=mock_remna_service),
        patch('app.services.migration.migration_service.get_active_subscriptions_by_user_id', new_callable=AsyncMock) as mock_subs,
        patch.object(LazeikaClient, 'annul_subscription', new_callable=AsyncMock) as mock_annul,
    ):
        mock_check.return_value = candidate
        mock_subs.return_value = []
        mock_annul.return_value = True

        result = await execute_user_migration(mock_db, user, telegram_id=123456789)

        assert result.success is True
        assert result.tariff_name == 'Премиум LTE'
        assert result.total_days == 25
        assert result.device_limit == 10
        assert user.remnawave_id == 183

        # Verify Remnawave API was called to rename user and reset traffic
        mock_api.update_user.assert_called_once()
        call_kwargs = mock_api.update_user.call_args.kwargs
        assert call_kwargs['user_id'] == 183
        assert call_kwargs['username'] == 'invoxy_123456789'
        assert call_kwargs['hwid_device_limit'] == 10
        mock_api.reset_user_traffic.assert_called_once_with(183)

        # Verify Lazeika subscription was annulled
        mock_annul.assert_called_once_with(
            client_id='laz_c1',
            subscription_id='laz_s1',
            telegram_id=123456789,
            reason='Мигрирован в Invoxy VPN (@invoxy_bot / invoxy.my)',
        )

        # Verify DB commit
        mock_db.commit.assert_called_once()
