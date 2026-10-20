from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.services import active_invoice
from app.config import settings
from app.database.models import PaymentMethod, Transaction, User, YooKassaPayment
from app.services import invoice_creation, invoice_notifications as service
from app.services.payment_verification_service import PendingPayment
from tests.fixtures.sqlite_memory import memory_session


def invoice():
    return PendingPayment(
        PaymentMethod.YOOKASSA,
        5,
        'provider-id',
        12345,
        'pending',
        False,
        datetime.now(UTC),
        SimpleNamespace(id=7, username='<Alice>', full_name=None),
        SimpleNamespace(metadata_json={'purpose': 'topup'}, is_paid=False, status='pending'),
    )


def test_message_has_clear_invoice_context_without_payment_url():
    record = invoice()
    text = service.invoice_message(
        record.user,
        event='Счёт создан',
        amount_kopeks=12345,
        method='yookassa',
        invoice_id=5,
        source='cabinet',
        purpose='Оплата тарифа «VPN» (30 дн.)',
        method_title='YooKassa',
    )
    assert len(text.splitlines()) == 6
    assert 'yookassa:5' in text
    assert '&lt;Alice&gt;' in text
    assert '123.45 ₽' in text
    assert 'Кабинет' in text
    assert 'Оплата тарифа «VPN» (30 дн.)' in text
    assert 'YooKassa' in text
    assert 'https://' not in text


@pytest.mark.asyncio
async def test_creation_preserves_metadata_and_reuse_keeps_original_source(monkeypatch):
    record = invoice()
    db = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(active_invoice, 'get_active_invoice_record', AsyncMock(return_value=record))
    monkeypatch.setattr(invoice_creation, 'send_invoice_event', send)
    token = service.invoice_source.set('telegram_miniapp')
    try:
        await invoice_creation.record_invoice_created(db, record.user)
        service.invoice_source.set('cabinet')
        await invoice_creation.record_invoice_created(db, record.user)
    finally:
        service.invoice_source.reset(token)
    assert record.payment.metadata_json == {'purpose': 'topup', service.SOURCE_KEY: 'telegram_miniapp'}
    db.commit.assert_awaited_once()
    send.assert_awaited_once_with(record, '💳 Счёт создан')


@pytest.mark.asyncio
async def test_stars_attribution_survives_cancelled_invoice(invoice_redis, monkeypatch):
    user = SimpleNamespace(id=7)
    payment_service = SimpleNamespace(create_stars_invoice=AsyncMock(return_value='https://t.me/$link'))
    monkeypatch.setattr(active_invoice, 'get_active_invoice_record', AsyncMock(return_value=None))
    await invoice_creation.create_managed_stars_invoice(object(), user, payment_service, 10000, 50)
    record = await invoice_creation.get_stars_invoice(user)
    monkeypatch.setattr(active_invoice, 'get_active_invoice_record', AsyncMock(return_value=record))
    send = AsyncMock()
    monkeypatch.setattr(invoice_creation, 'send_invoice_event', send)
    token = service.invoice_source.set('cabinet')
    try:
        await invoice_creation.record_invoice_created(object(), user)
    finally:
        service.invoice_source.reset(token)
    await invoice_creation.clear_stars_invoice(user.id, record.identifier)
    assert await invoice_creation.get_stars_invoice(user) is None
    assert await service.paid_invoice_details(
        None,
        SimpleNamespace(id=88),
        stars_payload=record.identifier,
    ) == (record.local_id, 'cabinet', 'Пополнение баланса')
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_payment_identity_and_source_are_read_from_linked_invoice(monkeypatch):
    tables = [User.__table__, Transaction.__table__, YooKassaPayment.__table__]
    async with memory_session(monkeypatch, tables) as db:
        user = User(id=7, telegram_id=700, balance_kopeks=0)
        transaction = Transaction(
            id=88,
            user_id=7,
            type='deposit',
            amount_kopeks=12345,
            payment_method='yookassa',
            is_completed=True,
        )
        payment = YooKassaPayment(
            id=5,
            user_id=7,
            yookassa_payment_id='provider-id',
            amount_kopeks=12345,
            status='succeeded',
            transaction_id=88,
            metadata_json={service.SOURCE_KEY: 'telegram_miniapp'},
            description='Оплата тарифа «LTE» (30 дн.)',
        )
        db.add_all([user, transaction, payment])
        await db.commit()
        assert await service.paid_invoice_details(db, transaction) == (
            5,
            'telegram_miniapp',
            'Оплата тарифа «LTE» (30 дн.)',
        )


def test_paid_message_identifies_user_and_preserves_kopeks():
    user = SimpleNamespace(
        id=7,
        full_name='Alice <Admin>',
        username='alice',
        telegram_id=700,
        email='alice+<test>@example.com',
        balance_kopeks=12400,
    )
    text = service.invoice_message(
        user,
        event='✅ Счёт оплачен',
        amount_kopeks=12345,
        method='yookassa',
        invoice_id=5,
        source='telegram_bot',
        old_balance=55,
        transaction_id=88,
    )
    assert len(text.splitlines()) == 9
    assert 'Alice &lt;Admin&gt; (@alice)' in text
    assert 'User #7' in text and 'Telegram ID: <code>700</code>' in text
    assert 'alice+&lt;test&gt;@example.com' in text
    assert 'Баланс: 0.55 ₽ → 124 ₽' in text
    assert 'Транзакция: <code>#88</code>' in text


@pytest.mark.asyncio
async def test_cancel_notifies_only_after_commit_and_only_once(monkeypatch):
    record = invoice()
    db = AsyncMock()
    db.add = lambda _: None
    monkeypatch.setattr(active_invoice, 'get_payment_record', AsyncMock(return_value=record))
    monkeypatch.setattr(active_invoice, 'send_invoice_cancelled_telegram_message', AsyncMock())

    async def notified(*args):
        db.commit.assert_awaited_once()

    send = AsyncMock(side_effect=notified)
    monkeypatch.setattr(invoice_creation, 'send_invoice_event', send)
    await active_invoice.cancel_pending_payment(db, record.user, 'yookassa', 5)
    await active_invoice.cancel_pending_payment(db, record.user, 'yookassa', 5)
    send.assert_awaited_once_with(record, '❌ Счёт отменён')


@pytest.mark.asyncio
async def test_notification_transport_failure_does_not_fail_invoice(monkeypatch):
    record = invoice()
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'BOT_TOKEN', '123456:test')
    from app import bot_factory

    bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock(side_effect=RuntimeError('close failed'))))
    monkeypatch.setattr(bot_factory, 'create_bot', lambda: bot)
    from app.services.admin_notification_service import AdminNotificationService

    monkeypatch.setattr(AdminNotificationService, '_send_message', AsyncMock(side_effect=RuntimeError('offline')))
    assert await invoice_creation.send_invoice_event(record, 'Счёт создан') is False


@pytest.mark.asyncio
async def test_created_event_sends_purpose_and_admin_link(monkeypatch):
    from app.services.admin_notification_service import AdminNotificationService

    record = invoice()
    record.payment.description = 'Оплата тарифа «LTE» (30 дн.)'
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example')
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(AdminNotificationService, '_send_message', send)
    assert await invoice_creation.send_invoice_event(record, 'Счёт создан', bot=SimpleNamespace())
    text = send.call_args.args[0]
    assert 'Оплата тарифа «LTE» (30 дн.)' in text and 'YooKassa' in text
    assert send.call_args.kwargs['reply_markup'].inline_keyboard[0][0].url == 'https://cabinet.example/admin/users/7'


@pytest.mark.asyncio
async def test_paid_event_sends_balance_change_transaction_and_purpose(monkeypatch):
    from app.services.admin_notification_service import AdminNotificationService

    user = SimpleNamespace(id=7, full_name='Alice', username='alice', balance_kopeks=12400)
    transaction = SimpleNamespace(id=88, amount_kopeks=12345, payment_method='yookassa')
    admin = AdminNotificationService(SimpleNamespace())
    monkeypatch.setattr(admin, '_is_enabled', lambda: True)
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example')
    monkeypatch.setattr(service, 'paid_invoice_details', AsyncMock(return_value=(5, 'cabinet', 'Оплата тарифа «VPN»')))
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(admin, '_send_message', send)
    assert await admin.send_balance_topup_notification(
        user,
        transaction,
        55,
        topup_status='paid',
        referrer_info='Нет',
        subscription=None,
        promo_group=None,
    )
    text = send.call_args.args[0]
    assert 'Оплата тарифа «VPN»' in text and '0.55 ₽ → 124 ₽' in text
    assert 'Транзакция: <code>#88</code>' in text
    assert send.call_args.kwargs['reply_markup'].inline_keyboard[0][0].url.endswith('/admin/users/7')


@pytest.mark.asyncio
async def test_cabinet_wrapper_scopes_source_to_one_request(invoice_redis):
    @invoice_creation.serialized_invoice_creation
    async def endpoint(user, raw_request=None):
        return service.invoice_source.get()

    user = SimpleNamespace(id=7)
    request = SimpleNamespace(headers={'X-Client-Source': 'telegram_miniapp'})
    assert await endpoint(user, request) == 'telegram_miniapp'
    assert service.invoice_source.get() == 'telegram_bot'
    assert await endpoint(user) == 'cabinet'
