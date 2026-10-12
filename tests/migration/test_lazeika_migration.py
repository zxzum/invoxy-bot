"""Tests for Lazeika VPN to Invoxy VPN migration."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.database.models import SubscriptionStatus
from app.services.migration import send_migration_notification
from app.services.migration.lazeika_client import LazeikaClient
from app.services.migration.migration_service import (
    MigrationCandidate,
    check_migration_eligibility,
    execute_user_migration,
)
from app.services.migration.tariff_mapping import resolve_target_tariff
from app.services.migration.trial_migration_notifier import notify_all_eligible_trial_users


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
        assert candidate.panel_user_id == '183'
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
        referred_by_id=None,
        balance_kopeks=0,
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
        panel_user_id='183',
        expires_at_str='25.10.2026',
        new_end_date=datetime.now(UTC) + timedelta(days=25),
        balance_rub=150.0,
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
        patch('app.services.migration.migration_service.get_subscription_by_user_id', new_callable=AsyncMock, return_value=None),
        patch('app.services.migration.migration_service.generate_unique_short_id', new_callable=AsyncMock, return_value='abc123'),
        patch.object(LazeikaClient, 'annul_subscription', new_callable=AsyncMock) as mock_annul,
    ):
        mock_check.return_value = candidate
        mock_annul.return_value = True

        result = await execute_user_migration(mock_db, user, telegram_id=123456789)

        assert result.success is True
        assert result.tariff_name == 'Премиум LTE'
        assert result.total_days == 25
        assert result.device_limit == 10
        assert result.balance_transferred_rub == 150.0
        assert result.balance_kopeks == 15000
        assert user.balance_kopeks == 15000
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


@pytest.mark.asyncio
async def test_execute_user_migration_already_migrated_graceful():
    """Verify that if user already has an active paid subscription, migration returns gracefully."""
    mock_db = AsyncMock()
    user = SimpleNamespace(
        id=42,
        telegram_id=123456789,
        username='cooluser',
        email='cool@example.com',
        remnawave_id=183,
    )
    existing_paid_sub = SimpleNamespace(
        id=10,
        tariff_id=3,
        is_trial=False,
        status='active',
        end_date=datetime.now(UTC) + timedelta(days=15),
        device_limit=5,
        traffic_limit_gb=600,
        whitelist_traffic_limit_gb=50,
        subscription_url='https://sub.invoxy.my/test',
    )
    mock_tariff = SimpleNamespace(id=3, name='Стандарт LTE')

    with (
        patch('app.services.migration.migration_service.check_migration_eligibility', new_callable=AsyncMock, return_value=None),
        patch('app.services.migration.migration_service.get_active_subscriptions_by_user_id', new_callable=AsyncMock, return_value=[existing_paid_sub]),
        patch('app.services.migration.migration_service.get_tariff_by_id', new_callable=AsyncMock, return_value=mock_tariff),
    ):
        result = await execute_user_migration(mock_db, user, telegram_id=123456789)
        assert result.success is True
        assert result.tariff_name == 'Стандарт LTE'
        assert result.total_days >= 14
        assert result.device_limit == 5
        assert 'уже успешно перенесена' in result.message


@pytest.mark.asyncio
async def test_resolve_target_tariff_rejects_trials_and_unknown():
    """Verify resolve_target_tariff returns None for unknown or trial names."""
    mock_db = AsyncMock()
    assert await resolve_target_tariff(mock_db, '') is None
    assert await resolve_target_tariff(mock_db, '   ') is None
    assert await resolve_target_tariff(mock_db, 'Триал') is None
    assert await resolve_target_tariff(mock_db, 'Пробный') is None
    assert await resolve_target_tariff(mock_db, 'Пробный период') is None
    assert await resolve_target_tariff(mock_db, 'Неизвестный тариф') is None


@pytest.mark.asyncio
async def test_check_migration_rejects_trial_subscriptions():
    """Verify check_migration_eligibility rejects subscriptions that are trials."""
    mock_db = AsyncMock()

    # Case 1: Empty tariffName and empty trafficLimitBytes (classic Lazeika trial)
    trial_context_1 = {
        'found': True,
        'client': {'id': 'laz_c1', 'telegramId': '558134111', 'isBlocked': False},
        'subscriptions': [
            {
                'id': 'laz_s1',
                'tariffName': '',
                'status': 'ACTIVE',
                'expiresAt': (datetime.now(UTC) + timedelta(days=2)).isoformat(),
                'remnawaveUuid': '311',
                'deviceLimit': 1,
                'trafficLimitBytes': '',
                'localTrafficLimitBytes': '',
            }
        ],
    }

    # Case 2: Named 'Пробный'
    trial_context_2 = {
        'found': True,
        'client': {'id': 'laz_c2', 'telegramId': '558134112', 'isBlocked': False},
        'subscriptions': [
            {
                'id': 'laz_s2',
                'tariffName': 'Пробный',
                'status': 'ACTIVE',
                'expiresAt': (datetime.now(UTC) + timedelta(days=2)).isoformat(),
                'remnawaveUuid': '312',
                'deviceLimit': 1,
                'trafficLimitBytes': '10737418240',
            }
        ],
    }

    # Case 3: isTrial = True
    trial_context_3 = {
        'found': True,
        'client': {'id': 'laz_c3', 'telegramId': '558134113', 'isBlocked': False},
        'subscriptions': [
            {
                'id': 'laz_s3',
                'tariffName': 'Стандарт',
                'status': 'ACTIVE',
                'isTrial': True,
                'expiresAt': (datetime.now(UTC) + timedelta(days=2)).isoformat(),
                'remnawaveUuid': '313',
                'deviceLimit': 5,
                'trafficLimitBytes': '805306368000',
            }
        ],
    }

    with patch.object(LazeikaClient, 'get_client_context', new_callable=AsyncMock) as mock_get:
        mock_get.return_value = trial_context_1
        assert await check_migration_eligibility(mock_db, telegram_id=558134111) is None

        mock_get.return_value = trial_context_2
        assert await check_migration_eligibility(mock_db, telegram_id=558134112) is None

        mock_get.return_value = trial_context_3
        assert await check_migration_eligibility(mock_db, telegram_id=558134113) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('trial_fields', [
    {'tariffName': ''},
    {'tariffName': 'Пробная подписка'},
    {'tariffName': 'Стандарт', 'isTrial': True},
    {'tariffName': 'Стандарт', 'trialId': 'trial_1'},
])
async def test_trial_cannot_be_offered_or_executed(trial_fields):
    """A direct execute request must not bypass the automatic trial exclusion."""
    from fastapi import HTTPException

    from app.cabinet.routes.migration import check_migration, execute_migration_endpoint
    from app.handlers.migration import check_and_prompt_lazeika_migration

    db = AsyncMock()
    user = SimpleNamespace(id=42, telegram_id=123456789, email='user@example.com')
    message = MagicMock()
    message.answer = AsyncMock()
    context = {
        'found': True,
        'client': {'id': 'client_1'},
        'subscriptions': [{
            'id': 'subscription_1',
            'status': 'ACTIVE',
            'expiresAt': (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            'remnawaveUuid': '183',
            'trafficLimitBytes': '10737418240',
            **trial_fields,
        }],
    }
    with (
        patch.object(LazeikaClient, 'get_client_context', AsyncMock(return_value=context)),
        patch('app.services.migration.migration_service.get_active_subscriptions_by_user_id',
              AsyncMock(return_value=[])),
        patch('app.services.migration.migration_service.RemnaWaveService') as panel,
        patch.object(LazeikaClient, 'annul_subscription', AsyncMock()) as annul,
    ):
        assert await check_migration(user, db) == {'eligible': False}
        assert not await check_and_prompt_lazeika_migration(message, AsyncMock(), db, user=user)
        message.answer.assert_not_awaited()
        with pytest.raises(HTTPException) as error:
            await execute_migration_endpoint(user, db)
        assert error.value.status_code == 400
        assert 'Пробный период не переносится' in error.value.detail
        panel.assert_not_called()
        annul.assert_not_awaited()
        db.commit.assert_not_awaited()
        db.add.assert_not_called()


@pytest.mark.asyncio
async def test_paid_subscription_after_trial_gets_bonus():
    """A previous trial must not prevent transferring a subsequent paid subscription."""
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = None
    db.execute.return_value = result
    context = {
        'found': True,
        'client': {'id': 'client_1'},
        'subscriptions': [
            {'id': 'trial_1', 'status': 'ACTIVE', 'tariffName': 'Стандарт', 'trialId': 'trial_1'},
            {
                'id': 'paid_1', 'status': 'ACTIVE', 'tariffName': 'Стандарт', 'trialId': None,
                'isTrial': False, 'remnawaveUuid': '183', 'trafficLimitBytes': '10737418240',
                'expiresAt': (datetime.now(UTC) + timedelta(days=10)).isoformat(),
            },
        ],
    }
    with patch.object(LazeikaClient, 'get_client_context', AsyncMock(return_value=context)):
        candidate = await check_migration_eligibility(db, telegram_id=123456789)
    assert candidate.subscription_id == 'paid_1'
    assert candidate.bonus_days == 5


@pytest.mark.asyncio
async def test_check_migration_eligibility_squad_empty_traffic_limit_bytes():
    """Squad tariffs with empty trafficLimitBytes but valid localTrafficLimitBytes must be accepted."""
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    mock_db.execute.return_value = mock_result

    # Exact payload structure returned for user 735721146 (Gena)
    gena_context = {
        'found': True,
        'client': {
            'id': 'cmrnrm35e00n7myuujb4ru3vs',
            'telegramId': '735721146',
            'email': 'gazaryan.g@mail.ru',
            'balance': 0,
            'isBlocked': False,
        },
        'subscriptions': [
            {
                'id': 'cmrnrqvcn00ndmyuujqid3fc7',
                'tariffName': 'Премиум',
                'status': 'ACTIVE',
                'expiresAt': (datetime.now(UTC) + timedelta(days=137)).isoformat(),
                'remnawaveUuid': '87',
                'deviceLimit': 5,
                'trafficLimitBytes': '',
                'localTrafficLimitBytes': '161061273600',
            }
        ],
    }

    with patch.object(LazeikaClient, 'get_client_context', new_callable=AsyncMock) as mock_get:
        mock_get.return_value = gena_context
        candidate = await check_migration_eligibility(mock_db, telegram_id=735721146)
        assert candidate is not None
        assert candidate.lazeika_tariff_name == 'Премиум'
        assert candidate.target_tariff_name == 'Премиум LTE'
        assert candidate.panel_user_id == '87'
        assert candidate.device_limit == 10  # max(5, target_tariff.device_limit=10)
        assert candidate.remaining_days in (136, 137)
        assert candidate.bonus_days == 5
        assert candidate.total_days == candidate.remaining_days + 5


@pytest.mark.asyncio
async def test_send_migration_notification_success():
    """send_migration_notification sends message with offer and bonus days when eligible."""
    mock_db = AsyncMock()
    mock_bot = AsyncMock()

    now = datetime.now(UTC)
    candidate = MigrationCandidate(
        client_id='laz_c1',
        subscription_id='laz_s1',
        lazeika_tariff_name='Премиум',
        target_tariff_id=4,
        target_tariff_name='Премиум LTE',
        remaining_days=137,
        bonus_days=5,
        total_days=142,
        device_limit=10,
        traffic_limit_gb=1000,
        whitelist_traffic_limit_gb=150,
        panel_user_id='87',
        expires_at_str=(now + timedelta(days=137)).isoformat(),
        new_end_date=now + timedelta(days=142),
    )

    with patch('app.services.migration.trial_migration_notifier.check_migration_eligibility', new_callable=AsyncMock) as mock_check:
        mock_check.return_value = candidate

        success = await send_migration_notification(mock_bot, mock_db, telegram_id=735721146)
        assert success is True
        mock_bot.send_message.assert_awaited_once()
        call_kwargs = mock_bot.send_message.await_args.kwargs
        assert call_kwargs['chat_id'] == 735721146
        assert 'перенос подписки из лазейка впн' in call_kwargs['text'].lower()
        assert '5 дней' in call_kwargs['text']
        assert 'Премиум LTE' in call_kwargs['text']
        assert call_kwargs['reply_markup'] is not None


@pytest.mark.asyncio
async def test_send_migration_notification_ineligible():
    """send_migration_notification returns False and does not send message if not eligible."""
    mock_db = AsyncMock()
    mock_bot = AsyncMock()

    with patch('app.services.migration.trial_migration_notifier.check_migration_eligibility', new_callable=AsyncMock) as mock_check:
        mock_check.return_value = None

        success = await send_migration_notification(mock_bot, mock_db, telegram_id=123456)
        assert success is False
        mock_bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_all_eligible_trial_users_flow():
    """notify_all_eligible_trial_users processes trial users and tallies stats."""
    mock_db = AsyncMock()
    mock_bot = AsyncMock()

    user1 = SimpleNamespace(id=1, telegram_id=735721146)
    user2 = SimpleNamespace(id=2, telegram_id=999999999)

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [user1, user2]
    mock_db.execute.return_value = mock_result

    now = datetime.now(UTC)
    candidate = MigrationCandidate(
        client_id='laz_c1',
        subscription_id='laz_s1',
        lazeika_tariff_name='Премиум',
        target_tariff_id=4,
        target_tariff_name='Премиум LTE',
        remaining_days=137,
        bonus_days=5,
        total_days=142,
        device_limit=10,
        traffic_limit_gb=1000,
        whitelist_traffic_limit_gb=150,
        panel_user_id='87',
        expires_at_str=(now + timedelta(days=137)).isoformat(),
        new_end_date=now + timedelta(days=142),
    )

    with (
        patch('app.services.migration.trial_migration_notifier.check_migration_eligibility', new_callable=AsyncMock) as mock_check,
        patch('app.services.migration.trial_migration_notifier.send_migration_notification', new_callable=AsyncMock) as mock_send,
    ):
        # First user has candidate, second does not
        mock_check.side_effect = [candidate, None]
        mock_send.return_value = True

        stats = await notify_all_eligible_trial_users(mock_bot, mock_db, delay_between_messages_sec=0.01)
        assert stats['total_checked'] == 2
        assert stats['eligible_found'] == 1
        assert stats['notified_success'] == 1
        assert stats['errors'] == 0
