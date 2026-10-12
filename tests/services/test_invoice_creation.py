import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import balance
from app.cabinet.services import active_invoice
from app.database.models import PaymentMethod
from app.handlers.balance import invoices
from app.services import invoice_creation
from app.services.payment_verification_service import PendingPayment


@pytest.fixture
def user():
    return SimpleNamespace(id=41, telegram_id=410, username='test', language='ru')


def pending(user, method=PaymentMethod.PLATEGA, id=1):
    return PendingPayment(
        method,
        id,
        f'p-{id}',
        10000,
        'pending',
        False,
        datetime.now(UTC),
        user,
        SimpleNamespace(redirect_url='https://pay.example.test', description='Пополнение баланса'),
    )


@pytest.mark.asyncio
async def test_two_bot_clicks_create_one_invoice(invoice_redis, user, monkeypatch):
    records = []
    monkeypatch.setattr(
        invoices, 'list_recent_pending_payments', AsyncMock(side_effect=lambda *a, **kw: records.copy())
    )
    message = SimpleNamespace(answer=AsyncMock())

    async def create():
        await asyncio.sleep(0)
        records.append(pending(user))
        return {'local_payment_id': 1}

    first, second = await asyncio.gather(
        *[invoices.create_guarded_payment(message, object(), user, create) for _ in range(2)]
    )
    assert len(records) == 1
    assert sorted([first[0], second[0]]) == [False, True]
    keyboard = message.answer.await_args.kwargs['reply_markup']
    assert any(button.text == 'Отменить счёт' for row in keyboard.inline_keyboard for button in row)


@pytest.mark.asyncio
async def test_bot_and_cabinet_share_creation_lock(invoice_redis, user):
    inside = 0
    maximum = 0

    @invoice_creation.serialized_invoice_creation
    async def endpoint(user):
        nonlocal inside, maximum
        inside += 1
        maximum = max(maximum, inside)
        await asyncio.sleep(0)
        inside -= 1

    async def bot():
        async with invoice_creation.invoice_creation_lock(user.id):
            await endpoint.__wrapped__(user)

    await asyncio.gather(endpoint(user=user), bot())
    assert maximum == 1


@pytest.mark.asyncio
async def test_redis_failure_never_calls_provider(user, monkeypatch):
    monkeypatch.setattr(invoice_creation.cache, '_connected', False)
    create = AsyncMock()
    message = SimpleNamespace(answer=AsyncMock())
    attempted, _ = await invoices.create_guarded_payment(message, object(), user, create)
    assert attempted is False
    create.assert_not_awaited()
    assert 'недоступно' in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_stars_reused_between_clients_and_cancelled_link_removed(invoice_redis, user, monkeypatch):
    async def active(db, user_id):
        return await invoice_creation.get_stars_invoice(user)

    monkeypatch.setattr(active_invoice, 'get_active_invoice_record', active)
    service = SimpleNamespace(create_stars_invoice=AsyncMock(return_value='https://t.me/$invoice'))
    first = await invoice_creation.create_managed_stars_invoice(object(), user, service, 10000, 50)
    second = await invoice_creation.create_managed_stars_invoice(object(), user, service, 10000, 50)
    assert first == second
    service.create_stars_invoice.assert_awaited_once()
    record = await invoice_creation.get_stars_invoice(user)
    assert record.local_id < 0
    assert record.amount_kopeks == 10000
    with pytest.raises(HTTPException) as error:
        await invoice_creation.create_managed_stars_invoice(object(), user, service, 20000, 100)
    assert error.value.status_code == 409
    await invoice_creation.clear_stars_invoice(user.id, record.identifier)
    assert await invoice_creation.get_stars_invoice(user) is None


@pytest.mark.asyncio
async def test_late_stars_payment_does_not_remove_new_invoice(invoice_redis, user):
    await invoice_creation.cache.set(f'invoice:stars:{user.id}', {'payload': 'new'})
    await invoice_creation.clear_stars_invoice(user.id, 'old')
    assert await invoice_creation.cache.get(f'invoice:stars:{user.id}') == {'payload': 'new'}


@pytest.mark.asyncio
async def test_active_only_filters_before_pagination(invoice_redis, user, monkeypatch):
    inactive = pending(user, id=1)
    inactive.status = 'canceled'
    first, second = pending(user, id=2), pending(user, id=3)
    monkeypatch.setattr(balance, 'list_recent_pending_payments', AsyncMock(return_value=[inactive, first, second]))
    result = await balance.get_pending_payments(page=2, per_page=1, active_only=True, user=user, db=object())
    assert result.total == 2
    assert result.pages == 2
    assert [item.id for item in result.items] == [3]


def test_provider_expiry_and_terminal_status_do_not_block_creation(user):
    record = pending(user)
    record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert not active_invoice.is_invoice_active(record)


@pytest.mark.asyncio
async def test_invoice_actions_never_check_another_users_invoice(invoice_redis, user, monkeypatch):
    monkeypatch.setattr(invoices, 'list_recent_pending_payments', AsyncMock(return_value=[]))
    check = AsyncMock()
    monkeypatch.setattr(balance, 'check_payment_status', check)
    callback = SimpleNamespace(data='invoice:check:platega:99', answer=AsyncMock())
    await invoices.handle_invoice(callback, user, object(), object())
    check.assert_not_awaited()
    assert callback.answer.await_args.kwargs['show_alert'] is True


@pytest.mark.asyncio
async def test_cancel_refreshes_under_row_lock_before_writing(user, monkeypatch):
    record = pending(user)
    record.payment.is_paid = False
    db = AsyncMock()

    async def settled_while_checking(payment, **kwargs):
        assert kwargs['with_for_update'] is True
        payment.is_paid = True

    db.refresh.side_effect = settled_while_checking
    monkeypatch.setattr(active_invoice, 'get_payment_record', AsyncMock(return_value=record))
    with pytest.raises(HTTPException) as error:
        await active_invoice.cancel_pending_payment(db, user, 'platega', 1)
    assert error.value.status_code == 409
    db.commit.assert_not_awaited()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cancel_waits_for_settlement_and_preserves_paid_status(postgres_database, monkeypatch):
    from app.database.crud import rollypay
    from app.database.models import RollyPayPayment, User
    from tests.fixtures.postgres_db import postgres_sessions, wait_for_lock_waiter

    tables = [RollyPayPayment.__table__, User.__table__]
    async with postgres_sessions(postgres_database, tables, count=3) as (settlement, cancellation, watcher):
        user = User(telegram_id=99000041, first_name='Test', language='ru', balance_kopeks=0)
        settlement.add(user)
        await settlement.commit()
        payment = await rollypay.create_rollypay_payment(
            settlement,
            user_id=user.id,
            order_id='ux-cancel-race',
            amount_kopeks=10000,
            description='Пополнение баланса',
            payment_url='https://pay.example.test',
        )
        record = await active_invoice.get_payment_record(cancellation, PaymentMethod.ROLLYPAY, payment.id)
        assert record and not record.is_paid
        monkeypatch.setattr(active_invoice, 'get_payment_record', AsyncMock(return_value=record))
        locked = await rollypay.get_rollypay_payment_by_id_for_update(settlement, payment.id)
        locked.is_paid = True
        locked.status = 'success'
        await settlement.flush()

        task = asyncio.create_task(
            active_invoice.cancel_pending_payment(cancellation, record.user, 'rollypay', payment.id)
        )
        try:
            await wait_for_lock_waiter(watcher)
            assert not task.done()
            await settlement.commit()
            with pytest.raises(HTTPException) as error:
                await task
            assert error.value.status_code == 409
            await cancellation.rollback()
            final = await rollypay.get_rollypay_payment_by_id(watcher, payment.id)
            assert final.is_paid and final.status == 'success'
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_restricted_account_cannot_create_stars_invoice(invoice_redis, user, monkeypatch):
    from app.cabinet.schemas.balance import StarsInvoiceRequest

    user.restriction_topup = True
    bot = AsyncMock()
    monkeypatch.setattr(balance, 'create_bot', bot)
    with pytest.raises(HTTPException) as error:
        await balance.create_stars_invoice(request=StarsInvoiceRequest(amount_kopeks=10000), user=user, db=object())
    assert error.value.status_code == 403
    bot.assert_not_called()


@pytest.mark.asyncio
async def test_failed_status_check_explains_failure_instead_of_claiming_update(invoice_redis, user, monkeypatch):
    record = pending(user)
    monkeypatch.setattr(invoices, 'list_recent_pending_payments', AsyncMock(return_value=[record]))
    monkeypatch.setattr(
        balance,
        'check_payment_status',
        AsyncMock(
            return_value=SimpleNamespace(
                payment=None,
                success=False,
                message='Платёжная система временно недоступна.',
            )
        ),
    )
    callback = SimpleNamespace(
        data='invoice:check:platega:1', answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock())
    )
    await invoices.handle_invoice(callback, user, object(), SimpleNamespace(clear=AsyncMock()))
    assert callback.answer.await_args.args[0] == 'Платёжная система временно недоступна.'
    assert callback.answer.await_args.kwargs['show_alert'] is True


@pytest.mark.asyncio
async def test_cancel_paid_invoice_reports_payment_and_offers_next_action(invoice_redis, user, monkeypatch):
    monkeypatch.setattr(invoices, 'list_recent_pending_payments', AsyncMock(side_effect=[[pending(user)], []]))
    monkeypatch.setattr(balance, 'cancel_user_pending_payment', AsyncMock(return_value=SimpleNamespace(is_paid=True)))
    callback = SimpleNamespace(
        data='invoice:cancel:platega:1', answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock())
    )
    state = SimpleNamespace(clear=AsyncMock())
    await invoices.handle_invoice(callback, user, object(), state)
    assert 'Оплата уже подтверждена' in callback.message.edit_text.await_args.args[0]
    assert 'отменён' not in callback.message.edit_text.await_args.args[0]
    state.clear.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'payload,current_payload,deleted',
    [
        ('balance_41_10000_invoice1', 'balance_41_10000_invoice2', False),
        ('balance_41_10000_invoice1', 'balance_41_10000_invoice1', True),
    ],
)
async def test_stars_completion_only_cleans_its_own_message(user, monkeypatch, payload, current_payload, deleted):
    from app.handlers import stars_payments

    monkeypatch.setattr(stars_payments, 'get_user_by_telegram_id', AsyncMock(return_value=user))
    service = SimpleNamespace(
        process_stars_payment=AsyncMock(return_value=True), build_topup_success_keyboard=AsyncMock(return_value=None)
    )
    monkeypatch.setattr(stars_payments, 'PaymentService', lambda bot: service)
    message = SimpleNamespace(
        successful_payment=SimpleNamespace(
            total_amount=50, invoice_payload=payload, telegram_payment_charge_id='test-charge'
        ),
        from_user=SimpleNamespace(id=user.telegram_id),
        chat=SimpleNamespace(id=user.telegram_id),
        bot=SimpleNamespace(delete_message=AsyncMock()),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={
                'stars_invoice_message_id': 99,
                'stars_invoice_chat_id': user.telegram_id,
                'stars_invoice_payload': current_payload,
            }
        ),
        update_data=AsyncMock(),
    )
    await stars_payments.handle_successful_payment(message, object(), state)
    service.process_stars_payment.assert_awaited_once()
    assert message.bot.delete_message.called is deleted
    assert state.update_data.called is deleted
