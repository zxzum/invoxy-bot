"""Tests for AdminNotificationService subscription audit diff, RemnaWave sync verification,
and traffic reset notifications.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.admin_notification_service import (
    AdminNotificationService,
    NotificationCategory,
)


@pytest.fixture
def admin_service() -> AdminNotificationService:
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=True)
    service = AdminNotificationService(bot)
    service.chat_id = -100123456
    service.enabled = True
    return service


def test_build_subscription_diff_block_trial_conversion(admin_service: AdminNotificationService):
    old_state = {
        'tariff_id': 5,
        'traffic_limit_gb': 10,
        'whitelist_traffic_limit_gb': 0,
        'traffic_used_gb': 10.73,
        'whitelist_traffic_used_bytes': 0,
        'end_date': datetime(2026, 10, 10, 12, 0, tzinfo=UTC),
        'status': 'limited',
        'is_trial': True,
        'device_limit': 1,
    }
    subscription = SimpleNamespace(
        id=54,
        tariff_id=3,
        traffic_limit_gb=600,
        whitelist_traffic_limit_gb=50,
        traffic_used_gb=0.0,
        whitelist_traffic_used_bytes=0,
        end_date=datetime(2027, 4, 2, 12, 0, tzinfo=UTC),
        status='active',
        is_trial=False,
        device_limit=2,
    )

    diff_lines = admin_service._build_subscription_diff_block_sync(
        subscription=subscription,
        old_state=old_state,
        period_days=180,
        old_tariff_name='Пробный',
        new_tariff_name='Стандарт LTE',
    )
    diff_text = '\n'.join(diff_lines)

    assert 'Пробный' in diff_text
    assert 'Стандарт LTE' in diff_text
    assert '10 ГБ → 600 ГБ' in diff_text
    assert '0 ГБ → 50 ГБ' in diff_text
    assert '10.73 ГБ → 0.0 ГБ' in diff_text
    assert 'limited' in diff_text
    assert 'active' in diff_text
    assert 'Да → Нет' in diff_text
    assert '1 → 2' in diff_text


def test_remnawave_verification_detects_mismatch(admin_service: AdminNotificationService):
    subscription = SimpleNamespace(
        id=54,
        traffic_limit_gb=600,
        remnawave_id=319,
        remnawave_short_uuid='WaArs6_2g0CB5jxn',
    )
    # Stale 10 GB in RemnaWave while DB expects 600 GB
    rw_info = {
        'id': 319,
        'status': 'LIMITED',
        'traffic_limit_bytes': 10 * (1024**3),
    }

    lines = admin_service._format_remnawave_verification(subscription, rw_info)
    text = '\n'.join(lines)

    assert 'РАССИНХРОН ЛИМИТА' in text
    assert '600 ГБ' in text
    assert '10.0 ГБ' in text
    assert 'LIMITED' in text


def test_remnawave_verification_matches(admin_service: AdminNotificationService):
    subscription = SimpleNamespace(
        id=54,
        traffic_limit_gb=600,
        remnawave_id=319,
        remnawave_short_uuid='WaArs6_2g0CB5jxn',
    )
    rw_info = {
        'id': 319,
        'status': 'ACTIVE',
        'traffic_limit_bytes': 600 * (1024**3),
    }

    lines = admin_service._format_remnawave_verification(subscription, rw_info)
    text = '\n'.join(lines)

    assert 'совпадает с БД' in text
    assert '600.0 ГБ' in text
    assert 'ACTIVE' in text
    assert 'РАССИНХРОН' not in text


@pytest.mark.asyncio
async def test_send_traffic_reset_notification(admin_service: AdminNotificationService, monkeypatch):
    user = SimpleNamespace(
        id=56,
        telegram_id=5212250705,
        username='sanya_vpn',
        full_name='Саня',
        email='truvalev123@gmail.com',
        balance_kopeks=50000,
        referred_by_id=None,
    )
    subscription = SimpleNamespace(
        id=54,
        tariff_id=3,
        traffic_limit_gb=600,
        whitelist_traffic_limit_gb=50,
        traffic_used_gb=0.0,
        whitelist_traffic_used_bytes=0,
        end_date=datetime(2027, 4, 2, 12, 0, tzinfo=UTC),
        status='active',
        is_trial=False,
        remnawave_id=319,
        remnawave_short_uuid='WaArs6_2g0CB5jxn',
        device_limit=2,
    )

    db = AsyncMock()

    # Mock tariff name
    async def fake_get_tariff_name(db_sess, sub):
        return 'Стандарт LTE'

    monkeypatch.setattr(admin_service, '_get_tariff_name', fake_get_tariff_name)

    # Mock remnawave verification
    async def fake_verify(sub):
        return {'id': 319, 'status': 'ACTIVE', 'traffic_limit_bytes': 600 * (1024**3)}

    monkeypatch.setattr(admin_service, '_verify_remnawave_state', fake_verify)

    sent_messages = []

    async def fake_send_message(text, reply_markup=None, *, category=None, **kwargs):
        sent_messages.append({'text': text, 'keyboard': reply_markup, 'category': category})
        return True

    monkeypatch.setattr(admin_service, '_send_message', fake_send_message)

    success = await admin_service.send_traffic_reset_notification(
        db=db,
        user=user,
        subscription=subscription,
        reset_scope='regular',
        cleared_gb=10.73,
        price_kopeks=15000,
        payment_method='Баланс',
    )

    assert success is True
    assert len(sent_messages) == 1
    msg = sent_messages[0]
    assert 'СБРОС ОСНОВНОГО ТРАФИКА' in msg['text']
    assert 'Саня' in msg['text']
    assert '5212250705' in msg['text']
    assert 'truvalev123@gmail.com' in msg['text']
    assert '10.73 ГБ' in msg['text']
    assert 'совпадает с БД' in msg['text']
    assert msg['category'] == NotificationCategory.ADDONS


@pytest.mark.asyncio
async def test_send_subscription_purchase_notification_includes_diff_and_remnawave(
    admin_service: AdminNotificationService, monkeypatch
):
    user = SimpleNamespace(
        id=56,
        telegram_id=5212250705,
        username='sanya_vpn',
        full_name='Саня',
        email='truvalev123@gmail.com',
        balance_kopeks=50000,
        referred_by_id=None,
        has_had_paid_subscription=True,
    )
    old_state = {
        'tariff_id': 5,
        'traffic_limit_gb': 10,
        'whitelist_traffic_limit_gb': 0,
        'traffic_used_gb': 10.73,
        'whitelist_traffic_used_bytes': 0,
        'end_date': datetime(2026, 10, 10, 12, 0, tzinfo=UTC),
        'status': 'limited',
        'is_trial': True,
        'device_limit': 1,
    }
    subscription = SimpleNamespace(
        id=54,
        tariff_id=3,
        traffic_limit_gb=600,
        whitelist_traffic_limit_gb=50,
        traffic_used_gb=0.0,
        whitelist_traffic_used_bytes=0,
        end_date=datetime(2027, 4, 2, 12, 0, tzinfo=UTC),
        status='active',
        is_trial=False,
        remnawave_id=319,
        remnawave_short_uuid='WaArs6_2g0CB5jxn',
        device_limit=2,
        connected_squads=[],
        _previous_state=old_state,
    )

    db = AsyncMock()

    async def fake_get_tariff_name(db_sess, sub):
        return 'Стандарт LTE'

    async def fake_get_tariff_name_by_id(db_sess, tid):
        if tid == 5:
            return 'Пробный'
        return 'Стандарт LTE'

    monkeypatch.setattr(admin_service, '_get_tariff_name', fake_get_tariff_name)
    monkeypatch.setattr(admin_service, '_get_tariff_name_by_id', fake_get_tariff_name_by_id)

    async def fake_verify(sub):
        return {'id': 319, 'status': 'ACTIVE', 'traffic_limit_bytes': 600 * (1024**3)}

    monkeypatch.setattr(admin_service, '_verify_remnawave_state', fake_verify)

    sent_messages = []

    async def fake_send_message(text, reply_markup=None, *, category=None, **kwargs):
        sent_messages.append({'text': text, 'keyboard': reply_markup, 'category': category})
        return True

    monkeypatch.setattr(admin_service, '_send_message', fake_send_message)

    success = await admin_service.send_subscription_purchase_notification(
        db=db,
        user=user,
        subscription=subscription,
        transaction=None,
        period_days=180,
        was_trial_conversion=True,
        amount_kopeks=120000,
    )

    assert success is True
    assert len(sent_messages) == 1
    msg = sent_messages[0]
    assert 'КОНВЕРСИЯ ИЗ ТРИАЛА' in msg['text']
    assert 'Саня' in msg['text']
    assert 'truvalev123@gmail.com' in msg['text']
    assert 'Пробный' in msg['text']
    assert 'Стандарт LTE' in msg['text']
    assert '10 ГБ → 600 ГБ' in msg['text']
    assert '0 ГБ → 50 ГБ' in msg['text']
    assert '10.73 ГБ → 0.0 ГБ' in msg['text']
    assert 'Да → Нет' in msg['text']
    assert 'совпадает с БД' in msg['text']
    assert msg['category'] == NotificationCategory.PURCHASES


@pytest.mark.asyncio
async def test_send_traffic_reset_notification_lte_and_admin(
    admin_service: AdminNotificationService, monkeypatch
):
    user = SimpleNamespace(
        id=56,
        telegram_id=5212250705,
        username='sanya_vpn',
        full_name='Саня',
        email='truvalev123@gmail.com',
        balance_kopeks=50000,
        referred_by_id=None,
    )
    admin_user = SimpleNamespace(
        id=1,
        username='admin_boss',
        full_name='Admin Boss',
    )
    subscription = SimpleNamespace(
        id=54,
        tariff_id=3,
        traffic_limit_gb=600,
        whitelist_traffic_limit_gb=50,
        traffic_used_gb=0.0,
        whitelist_traffic_used_bytes=0,
        end_date=datetime(2027, 4, 2, 12, 0, tzinfo=UTC),
        status='active',
        is_trial=False,
        remnawave_id=319,
        remnawave_short_uuid='WaArs6_2g0CB5jxn',
        device_limit=2,
    )
    db = AsyncMock()

    async def fake_get_tariff_name(db_sess, sub):
        return 'Стандарт LTE'

    monkeypatch.setattr(admin_service, '_get_tariff_name', fake_get_tariff_name)

    async def fake_verify(sub):
        return {'id': 319, 'status': 'ACTIVE', 'traffic_limit_bytes': 600 * (1024**3)}

    monkeypatch.setattr(admin_service, '_verify_remnawave_state', fake_verify)

    sent_messages = []

    async def fake_send_message(text, reply_markup=None, *, category=None, **kwargs):
        sent_messages.append({'text': text, 'keyboard': reply_markup, 'category': category})
        return True

    monkeypatch.setattr(admin_service, '_send_message', fake_send_message)

    # Test LTE reset
    await admin_service.send_traffic_reset_notification(
        db=db,
        user=user,
        subscription=subscription,
        reset_scope='lte',
        cleared_gb=25.0,
        price_kopeks=15000,
        payment_method='Баланс',
    )
    assert len(sent_messages) == 1
    assert 'СБРОС ТРАФИКА LTE' in sent_messages[0]['text']
    assert '25.00 ГБ' in sent_messages[0]['text']

    # Test Admin reset
    await admin_service.send_traffic_reset_notification(
        db=db,
        user=user,
        subscription=subscription,
        reset_scope='admin',
        cleared_gb=10.5,
        price_kopeks=0,
        payment_method='Администратор',
        admin_user=admin_user,
    )
    assert len(sent_messages) == 2
    assert 'АДМИН-СБРОС ТРАФИКА (Admin Boss)' in sent_messages[1]['text']
    assert '10.50 ГБ' in sent_messages[1]['text']


@pytest.mark.asyncio
async def test_send_subscription_update_notification_includes_rw_audit(
    admin_service: AdminNotificationService, monkeypatch
):
    user = SimpleNamespace(
        id=56,
        telegram_id=5212250705,
        username='sanya_vpn',
        full_name='Саня',
        email='truvalev123@gmail.com',
        balance_kopeks=50000,
        referred_by_id=None,
    )
    subscription = SimpleNamespace(
        id=54,
        tariff_id=3,
        traffic_limit_gb=650,
        whitelist_traffic_limit_gb=50,
        traffic_used_gb=0.0,
        whitelist_traffic_used_bytes=0,
        end_date=datetime(2027, 4, 2, 12, 0, tzinfo=UTC),
        status='active',
        is_trial=False,
        remnawave_id=319,
        remnawave_short_uuid='WaArs6_2g0CB5jxn',
        device_limit=2,
    )
    db = AsyncMock()

    async def fake_get_tariff_name(db_sess, sub):
        return 'Стандарт LTE'

    monkeypatch.setattr(admin_service, '_get_tariff_name', fake_get_tariff_name)

    async def fake_verify(sub):
        return {'id': 319, 'status': 'ACTIVE', 'traffic_limit_bytes': 650 * (1024**3)}

    monkeypatch.setattr(admin_service, '_verify_remnawave_state', fake_verify)

    sent_messages = []

    async def fake_send_message(text, reply_markup=None, *, category=None, **kwargs):
        sent_messages.append({'text': text, 'keyboard': reply_markup, 'category': category})
        return True

    monkeypatch.setattr(admin_service, '_send_message', fake_send_message)

    success = await admin_service.send_subscription_update_notification(
        db=db,
        user=user,
        subscription=subscription,
        update_type='traffic',
        old_value=600,
        new_value=650,
        price_paid=10000,
    )

    assert success is True
    assert len(sent_messages) == 1
    msg = sent_messages[0]
    assert 'ДОКУПКА ТРАФИКА' in msg['text']
    assert 'truvalev123@gmail.com' in msg['text']
    assert 'User: #56' in msg['text']
    assert 'Подписка: #54' in msg['text']
    assert 'совпадает с БД' in msg['text']
    assert msg['keyboard'] is not None
