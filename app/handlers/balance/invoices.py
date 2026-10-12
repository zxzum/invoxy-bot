"""Open invoices: pay, check, cancel and browse older invoices."""

import html

from aiogram import types
from fastapi import HTTPException

from app.cabinet.services.active_invoice import (
    get_invoice_expires_at,
    get_payment_url,
    is_invoice_active,
    resolve_purpose,
)
from app.database.models import PaymentMethod
from app.services.invoice_creation import get_stars_invoice, invoice_creation_lock
from app.services.payment_verification_service import list_recent_pending_payments, method_display_name
from app.utils.timezone import format_local_datetime


async def active_invoices(db, user):
    records = await list_recent_pending_payments(db, user_id=user.id)
    stars = await get_stars_invoice(user)
    if stars:
        records.append(stars)
    return sorted(
        (record for record in records if is_invoice_active(record)), key=lambda record: record.created_at, reverse=True
    )


def invoice_screen(record, index=0, count=1, confirm_cancel=False):
    purpose, _ = resolve_purpose(record)
    text = (
        f'💳 <b>Неоплаченный счёт{f" {index + 1} из {count}" if count > 1 else ""}</b>\n\n'
        f'{html.escape(purpose)}\n'
        f'Сумма: <b>{record.amount_kopeks / 100:.2f} ₽</b>\n'
        f'{html.escape(method_display_name(record.method))}\n'
        f'Создан: {format_local_datetime(record.created_at, "%d.%m %H:%M")}\n'
        f'Действует до {format_local_datetime(get_invoice_expires_at(record), "%H:%M")}\n\n'
        'Оплатите или отмените этот счёт перед созданием нового.\n'
        'Возврат в меню не отменяет счёт.'
    )
    rows = []
    url = get_payment_url(record)
    if url:
        rows.append([types.InlineKeyboardButton(text='💳 Оплатить счёт', url=url)])
    suffix = f'{record.method.value}:{record.local_id}'
    if record.method != PaymentMethod.TELEGRAM_STARS:
        rows.append([types.InlineKeyboardButton(text='🔄 Проверить оплату', callback_data=f'invoice:check:{suffix}')])
    rows.append(
        [
            types.InlineKeyboardButton(
                text='Подтвердить отмену' if confirm_cancel else 'Отменить счёт',
                callback_data=f'invoice:{"cancel" if confirm_cancel else "confirm"}:{suffix}',
            )
        ]
    )
    if confirm_cancel:
        text += '\n\n<b>Отменить счёт?</b> Если вы уже заплатили, дождитесь зачисления. Подтверждённая оплата не потеряется.'
        rows.append([types.InlineKeyboardButton(text='Оставить счёт', callback_data=f'invoice:view:{index}')])
    if count > 1:
        nav = []
        if index > 0:
            nav.append(types.InlineKeyboardButton(text='← Предыдущий', callback_data=f'invoice:view:{index - 1}'))
        if index + 1 < count:
            nav.append(types.InlineKeyboardButton(text='Следующий →', callback_data=f'invoice:view:{index + 1}'))
        if nav:
            rows.append(nav)
    rows.append([types.InlineKeyboardButton(text='💰 Баланс', callback_data='menu_balance')])
    return text, types.InlineKeyboardMarkup(inline_keyboard=rows)


async def show_open_invoice(message, db, user, edit=False):
    records = await active_invoices(db, user)
    if not records:
        return False
    text, keyboard = invoice_screen(records[0], count=len(records))
    response = await (message.edit_text if edit else message.answer)(text, reply_markup=keyboard, parse_mode='HTML')
    return response or True


async def create_guarded_payment(message, db, user, create):
    """Check under the same lock as cabinet creation, including stale buttons."""
    try:
        async with invoice_creation_lock(user.id):
            if await show_open_invoice(message, db, user):
                return False, None
            return True, await create()
    except HTTPException as error:
        if error.status_code == 409 and await show_open_invoice(message, db, user):
            return False, None
        await message.answer(
            str(error.detail),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='Повторить пополнение', callback_data='balance_topup')],
                    [types.InlineKeyboardButton(text='Поддержка', callback_data='menu_support')],
                ]
            ),
        )
        return False, None


async def handle_invoice(callback, db_user, db, state):
    from app.cabinet.routes.balance import cancel_user_pending_payment, check_payment_status

    parts = callback.data.split(':')
    action = parts[1]
    records = await active_invoices(db, db_user)
    index = 0
    feedback = None
    try:
        if action == 'view':
            index = max(0, min(int(parts[2]), len(records) - 1))
        else:
            method, payment_id = parts[2], int(parts[3])
            index = next(
                (
                    i
                    for i, record in enumerate(records)
                    if record.method.value == method and record.local_id == payment_id
                ),
                -1,
            )
            if index < 0:
                await callback.answer('Счёт уже закрыт или недоступен.', show_alert=True)
                return
            if action == 'check':
                result = await check_payment_status(method=method, payment_id=payment_id, user=db_user, db=db)
                if result.payment and result.payment.is_paid:
                    feedback = '✅ Оплата подтверждена.'
                elif not result.success:
                    feedback = result.message or 'Не удалось проверить оплату. Попробуйте позже.'
                else:
                    feedback = 'Оплата ещё не поступила.'
            elif action == 'cancel':
                result = await cancel_user_pending_payment(method=method, payment_id=payment_id, user=db_user, db=db)
                feedback = '✅ Оплата уже подтверждена.' if result.is_paid else 'Счёт отменён.'
            if action in {'check', 'cancel'}:
                records = await active_invoices(db, db_user)
                index = min(index, len(records) - 1)
    except (HTTPException, ValueError) as error:
        await callback.answer(str(getattr(error, 'detail', 'Некорректный счёт.')), show_alert=True)
        return
    if records:
        text, keyboard = invoice_screen(records[index], index, len(records), confirm_cancel=action == 'confirm')
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
        await callback.answer(feedback, show_alert=bool(feedback))
    else:
        await state.clear()
        await callback.message.edit_text(
            f'{feedback or "Неоплаченных счетов нет."}\n\nМожно создать новый счёт в разделе пополнения.',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='Пополнить баланс', callback_data='balance_topup')],
                    [types.InlineKeyboardButton(text='💰 Баланс', callback_data='menu_balance')],
                ]
            ),
        )
        await callback.answer()
