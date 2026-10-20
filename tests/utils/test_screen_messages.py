from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.notification_types import NotificationType
from app.utils import photo_message, screen_banners


@pytest.mark.asyncio
async def test_banner_keeps_caption_keyboard_and_caches_file_id(monkeypatch):
    monkeypatch.setattr(photo_message, 'get_screen_banner', lambda _: 'original-image')
    monkeypatch.setattr(screen_banners, '_file_id_cache', {})
    sent = SimpleNamespace(photo=[SimpleNamespace(file_id='cached-photo')])
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=sent), send_message=AsyncMock())
    keyboard = object()
    result = await photo_message.send_screen_message(bot, 7, '<b>Оплата</b>', banner='payment_pending', reply_markup=keyboard)
    assert result is sent
    bot.send_photo.assert_awaited_once_with(
        chat_id=7, photo='original-image', caption='<b>Оплата</b>', reply_markup=keyboard, parse_mode='HTML',
    )
    bot.send_message.assert_not_awaited()
    assert screen_banners._file_id_cache['payment_pending'] == 'cached-photo'


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['missing', 'long', 'failed', 'disabled'])
async def test_text_fallback_keeps_original_text_and_buttons(monkeypatch, case):
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', case != 'disabled')
    if case != 'disabled':
        monkeypatch.setattr(photo_message, 'get_screen_banner', lambda _: None if case == 'missing' else 'image')
    text = 'a' * 1025 if case == 'long' else '<b>Поддержка</b>'
    keyboard = object()
    bot = SimpleNamespace(send_photo=AsyncMock(side_effect=OSError('unreadable')), send_message=AsyncMock())
    await photo_message.send_screen_message(bot, 7, text, banner='support', reply_markup=keyboard)
    bot.send_message.assert_awaited_once_with(chat_id=7, text=text, reply_markup=keyboard, parse_mode='HTML')
    if case == 'failed':
        bot.send_photo.assert_awaited_once()
    else:
        bot.send_photo.assert_not_awaited()


@pytest.mark.parametrize(('event', 'banner'), [
    (NotificationType.PAYMENT_INVOICE_CREATED, 'payment_pending'),
    (NotificationType.PAYMENT_INVOICE_CANCELLED, 'invoice_cancelled'),
    (NotificationType.BALANCE_TOPUP, 'paid_successful'),
    (NotificationType.AUTOPAY_FAILED, 'payment_failed'),
    (NotificationType.TICKET_REPLY, 'support'),
    (NotificationType.REFERRAL_BONUS, 'bonus'),
    (NotificationType.GRACE_ACCESS_GRANTED, 'grace_access'),
    (NotificationType.GRACE_ACCESS_ENDED, 'subscription_inactive'),
    (NotificationType.DAILY_DEBIT, None),
])
def test_notification_banner_matches_actual_event(event, banner):
    assert screen_banners.notification_banner(event, {}) == banner


def test_only_backup_login_reminder_uses_security_banner():
    assert screen_banners.notification_banner(NotificationType.USER_REMINDER, {'builtin_key': 'link_auth_method'}) == 'account_security'
    assert screen_banners.notification_banner(NotificationType.USER_REMINDER, {'reminder_id': 1}) is None
    assert screen_banners.notification_banner(NotificationType.SUBSCRIPTION_ACTIVATED, {'is_trial': True}) == 'trial'


@pytest.mark.asyncio
async def test_central_delivery_selects_security_banner(monkeypatch):
    from app.services.notification_delivery_service import NotificationDeliveryService

    send = AsyncMock()
    monkeypatch.setattr(photo_message, 'send_screen_message', send)
    service = NotificationDeliveryService()
    assert await service._send_telegram_notification(
        SimpleNamespace(telegram_id=7), NotificationType.USER_REMINDER,
        {'builtin_key': 'link_auth_method'}, SimpleNamespace(), 'Добавьте почту', 'keyboard',
    )
    assert send.call_args.kwargs['banner'] == 'account_security'
    assert send.call_args.kwargs['reply_markup'] == 'keyboard'


@pytest.mark.asyncio
@pytest.mark.parametrize(('action', 'is_paid', 'banner'), [
    ('cancel', False, 'invoice_cancelled'), ('cancel', True, 'paid_successful'), ('check', True, 'paid_successful'),
])
async def test_invoice_callback_edits_same_message_and_keeps_actions(monkeypatch, action, is_paid, banner):
    from app.cabinet.routes import balance
    from app.database.models import PaymentMethod
    from app.handlers.balance import invoices

    record = SimpleNamespace(method=PaymentMethod.YOOKASSA, local_id=5)
    monkeypatch.setattr(invoices, 'active_invoices', AsyncMock(side_effect=[[record], []]))
    monkeypatch.setattr(invoices, 'get_screen_banner', lambda kind: f'image:{kind}')
    monkeypatch.setattr(balance, 'cancel_user_pending_payment', AsyncMock(return_value=SimpleNamespace(is_paid=is_paid)))
    monkeypatch.setattr(balance, 'check_payment_status', AsyncMock(return_value=SimpleNamespace(payment=SimpleNamespace(is_paid=True))))
    callback = SimpleNamespace(data=f'invoice:{action}:yookassa:5', message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock())
    await invoices.handle_invoice(callback, SimpleNamespace(id=7), object(), state)
    callback.message.edit_text.assert_awaited_once()
    sent = callback.message.edit_text.call_args.kwargs
    assert sent['media_kind'] == banner and sent['media'] == f'image:{banner}'
    assert [b.callback_data for row in sent['reply_markup'].inline_keyboard for b in row] == ['balance_topup', 'menu_balance']


@pytest.mark.asyncio
async def test_explicit_banner_upgrades_existing_text_without_extra_message(monkeypatch):
    from app.utils import message_patch

    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', True)
    monkeypatch.setattr(message_patch, '_cache_banner_file_id', lambda *_: None)
    message = SimpleNamespace(photo=[], text='old text', edit_media=AsyncMock(), delete=AsyncMock(), answer=AsyncMock())
    keyboard = object()
    await message_patch._edit_with_photo(message, '<b>Счёт</b>', media='banner', media_kind='payment_pending', reply_markup=keyboard)
    message.edit_media.assert_awaited_once()
    media = message.edit_media.call_args.args[0]
    assert media.media == 'banner' and media.caption == '<b>Счёт</b>'
    assert message.edit_media.call_args.kwargs['reply_markup'] is keyboard
    message.delete.assert_not_awaited()
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_long_existing_text_stays_text_when_banner_requested(monkeypatch):
    from app.utils import message_patch

    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', True)
    edit = AsyncMock()
    monkeypatch.setattr(message_patch, '_text_edit', edit)
    message = SimpleNamespace(photo=[], text='old text', edit_media=AsyncMock(), delete=AsyncMock())
    await message_patch._edit_with_photo(message, 'a' * 1025, media='banner', media_kind='support')
    message.edit_media.assert_not_awaited()
    message.delete.assert_not_awaited()
    edit.assert_awaited_once_with(message, 'a' * 1025)
