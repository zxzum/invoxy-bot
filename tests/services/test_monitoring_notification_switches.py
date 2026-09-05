from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.database.crud.notification import record_notification as record_notification_crud
from app.database.models import SentNotification
from app.services import monitoring_service
from app.services.monitoring_service import MonitoringService
from app.services.notification_settings_service import NotificationSettingsService
from tests.fixtures.sqlite_memory import memory_session


def _service():
    service = MonitoringService.__new__(MonitoringService)
    service.bot = SimpleNamespace(send_message=AsyncMock())
    return service


async def test_global_switch_stops_monitoring_notification_queries(monkeypatch):
    monkeypatch.setattr(
        NotificationSettingsService,
        'are_notifications_globally_enabled',
        classmethod(lambda cls: False),
    )
    service = _service()
    db = SimpleNamespace(execute=AsyncMock())

    await service._check_expiring_subscriptions(db)
    await service._check_trial_expiring_soon(db)
    await service._check_traffic_warnings(db)
    await service._check_low_balance_alerts(db)

    db.execute.assert_not_awaited()
    service.bot.send_message.assert_not_awaited()


async def test_expiration_state_updates_even_when_notifications_are_disabled(monkeypatch):
    monkeypatch.setattr(
        NotificationSettingsService,
        'are_notifications_globally_enabled',
        classmethod(lambda cls: False),
    )
    subscription = SimpleNamespace(id=7, user_id=42, tariff=None)
    user = SimpleNamespace(id=42, notification_settings={})
    expire_subscription = AsyncMock()

    monkeypatch.setattr(monitoring_service, 'get_expired_subscriptions', AsyncMock(return_value=[subscription]))
    monkeypatch.setattr(monitoring_service, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(
        'app.database.crud.subscription.is_recently_updated_by_webhook',
        lambda subscription: False,
    )
    monkeypatch.setattr('app.database.crud.subscription.expire_subscription', expire_subscription)

    service = _service()
    service._send_subscription_expired_notification = AsyncMock()
    service._log_monitoring_event = AsyncMock()
    db = SimpleNamespace(execute=AsyncMock())

    await service._check_expired_subscriptions(db)

    expire_subscription.assert_awaited_once_with(db, subscription)
    service._send_subscription_expired_notification.assert_not_awaited()


async def test_traffic_warnings_use_highest_reached_threshold_per_scope(monkeypatch):
    monkeypatch.setattr(
        NotificationSettingsService,
        'are_notifications_globally_enabled',
        classmethod(lambda cls: True),
    )
    user = SimpleNamespace(
        id=42,
        language='en',
        notification_settings={},
        status='active',
        telegram_id=420,
    )
    subscription = SimpleNamespace(
        id=7,
        user=user,
        traffic_limit_gb=100,
        traffic_used_gb=85.0,
        whitelist_traffic_limit_gb=100,
        whitelist_traffic_used_bytes=95 * 1024**3,
    )
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [subscription]))
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    recorded = set()

    async def was_sent(db, user_id, subscription_id, notification_type, days_before=None):
        return (notification_type, days_before) in recorded

    async def record(db, user_id, subscription_id, notification_type, days_before=None, *, commit=True):
        recorded.add((notification_type, days_before))

    send = AsyncMock(return_value=True)
    monkeypatch.setattr(monitoring_service, 'notification_sent', was_sent)
    monkeypatch.setattr(monitoring_service, 'record_notification', record)
    monkeypatch.setattr(monitoring_service.notification_delivery_service, 'send_notification', send)

    service = _service()
    await service._check_traffic_warnings(db)
    await service._check_traffic_warnings(db)

    assert send.await_count == 2
    calls = [item.kwargs for item in send.await_args_list]
    assert [item['context']['scope'] for item in calls] == ['regular', 'whitelist']
    assert calls[0]['context']['percent'] == 85.0
    assert calls[0]['context']['remaining_percent'] == 15.0
    assert '<b>15.0%</b>' in calls[0]['telegram_message']
    assert calls[1]['context']['percent'] == 95.0
    assert calls[1]['context']['remaining_percent'] == 5.0
    assert '<b>5.0%</b>' in calls[1]['telegram_message']
    assert 'White Internet' in calls[1]['telegram_message']
    assert recorded == {
        ('traffic_warning', 50),
        ('traffic_warning', 75),
        ('whitelist_traffic_warning', 50),
        ('whitelist_traffic_warning', 75),
        ('whitelist_traffic_warning', 90),
    }


async def test_traffic_warning_retries_same_threshold_after_delivery_failure(monkeypatch):
    monkeypatch.setattr(
        NotificationSettingsService,
        'are_notifications_globally_enabled',
        classmethod(lambda cls: True),
    )
    user = SimpleNamespace(
        id=42,
        language='en',
        notification_settings={},
        status='active',
        telegram_id=420,
    )
    subscription = SimpleNamespace(
        id=7,
        user=user,
        traffic_limit_gb=100,
        traffic_used_gb=85.0,
        whitelist_traffic_limit_gb=0,
        whitelist_traffic_used_bytes=0,
    )
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [subscription]))
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    recorded = set()

    async def was_sent(db, user_id, subscription_id, notification_type, days_before=None):
        return (notification_type, days_before) in recorded

    async def record(db, user_id, subscription_id, notification_type, days_before=None, *, commit=True):
        recorded.add((notification_type, days_before))

    send = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(monitoring_service, 'notification_sent', was_sent)
    monkeypatch.setattr(monitoring_service, 'record_notification', record)
    monkeypatch.setattr(monitoring_service.notification_delivery_service, 'send_notification', send)

    service = _service()
    await service._check_traffic_warnings(db)
    assert recorded == set()
    await service._check_traffic_warnings(db)

    assert send.await_count == 2
    assert [item.kwargs['context']['remaining_percent'] for item in send.await_args_list] == [15.0, 15.0]
    assert recorded == {('traffic_warning', 50), ('traffic_warning', 75)}


async def test_record_notification_is_idempotent_for_repeated_threshold(monkeypatch):
    async with memory_session(monkeypatch, [SentNotification.__table__]) as db:
        for _ in range(2):
            await record_notification_crud(db, 42, 7, 'traffic_warning', 50)

        rows = (await db.execute(select(SentNotification))).scalars().all()

    assert len(rows) == 1
