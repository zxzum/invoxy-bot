"""Handler for RollyPay balance top-up."""

import html

import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.keyboards.topup_amounts import get_topup_amount_keyboard
from app.localization.texts import get_texts
from app.services.payment_method_config_service import get_enabled_methods_for_user
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


def _check_topup_restriction(db_user: User, texts) -> InlineKeyboardMarkup | None:
    """Проверяет ограничение на пополнение. Возвращает клавиатуру если ограничен, иначе None."""
    if not getattr(db_user, 'restriction_topup', False):
        return None

    keyboard = []
    support_url = settings.get_support_contact_url()
    if support_url:
        keyboard.append([InlineKeyboardButton(text='\U0001f198 Обжаловать', url=support_url)])
    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def _create_rollypay_payment_and_respond(
    message_or_callback,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    edit_message: bool = False,
    payment_method: str | None = None,
):
    """
    Common logic for creating RollyPay payment and sending response.
    """
    texts = get_texts(db_user.language)
    amount_rub = amount_kopeks / 100

    # Create payment
    payment_service = PaymentService()

    description = settings.PAYMENT_BALANCE_TEMPLATE.format(
        service_name=settings.PAYMENT_SERVICE_NAME,
        description='Пополнение баланса',
    )

    from .invoices import create_guarded_payment

    async def create():
        return await payment_service.create_rollypay_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description=description,
            email=getattr(db_user, 'email', None),
            language=db_user.language,
            payment_method_type=payment_method,
        )

    attempted, result = await create_guarded_payment(message_or_callback, db, db_user, create)
    if not attempted:
        return

    if not result:
        error_text = texts.t(
            'PAYMENT_CREATE_ERROR',
            'Не удалось создать платёж. Попробуйте позже.',
        )
        if edit_message:
            await message_or_callback.edit_text(
                error_text,
                reply_markup=get_back_keyboard(db_user.language, callback_data='topup_rollypay'),
                parse_mode='HTML',
            )
        else:
            await message_or_callback.answer(
                error_text,
                reply_markup=get_back_keyboard(db_user.language, callback_data='topup_rollypay'),
                parse_mode='HTML',
            )
        return

    from .invoices import show_open_invoice

    await show_open_invoice(message_or_callback, db, db_user, edit=edit_message)

    logger.info('RollyPay payment created', telegram_id=db_user.telegram_id, amount_rub=amount_rub)


@error_handler
async def process_rollypay_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
):
    """
    Process payment amount directly.
    """
    texts = get_texts(db_user.language)

    restriction_kb = _check_topup_restriction(db_user, texts)
    if restriction_kb:
        reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
        await message.answer(
            f'\U0001f6ab <b>Пополнение ограничено</b>\n\n{reason}',
            parse_mode='HTML',
            reply_markup=restriction_kb,
        )
        await state.clear()
        return

    # Validate amount
    min_amount = settings.ROLLYPAY_MIN_AMOUNT_KOPEKS
    max_amount = settings.ROLLYPAY_MAX_AMOUNT_KOPEKS

    if amount_kopeks < min_amount:
        await message.answer(
            texts.t(
                'PAYMENT_AMOUNT_TOO_LOW',
                'Минимальная сумма пополнения: {min_amount}\u20bd',
            ).format(min_amount=min_amount // 100),
            reply_markup=get_back_keyboard(db_user.language, callback_data='topup_rollypay'),
            parse_mode='HTML',
        )
        return

    if amount_kopeks > max_amount:
        await message.answer(
            texts.t(
                'PAYMENT_AMOUNT_TOO_HIGH',
                'Максимальная сумма пополнения: {max_amount}\u20bd',
            ).format(max_amount=max_amount // 100),
            reply_markup=get_back_keyboard(db_user.language, callback_data='topup_rollypay'),
            parse_mode='HTML',
        )
        return

    data = await state.get_data()
    payment_method = data.get('rollypay_method')
    options = await _rollypay_options(db, db_user)
    if payment_method not in {option['id'] for option in options}:
        await message.answer(
            texts.t('ROLLYPAY_SELECT_PAYMENT_METHOD', 'Выберите способ оплаты в платёжной системе №1.'),
            reply_markup=get_back_keyboard(db_user.language, callback_data='topup_rollypay'),
        )
        return
    await state.clear()

    await _create_rollypay_payment_and_respond(
        message_or_callback=message,
        db_user=db_user,
        db=db,
        amount_kopeks=amount_kopeks,
        edit_message=False,
        payment_method=payment_method,
    )


@error_handler
async def start_rollypay_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """
    Start RollyPay top-up process with an explicit payment-method choice.
    """
    texts = get_texts(db_user.language)

    restriction_kb = _check_topup_restriction(db_user, texts)
    if restriction_kb:
        reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
        await callback.message.edit_text(
            f'\U0001f6ab <b>Пополнение ограничено</b>\n\n{reason}',
            parse_mode='HTML',
            reply_markup=restriction_kb,
        )
        await callback.answer()
        return

    options = await _rollypay_options(db, db_user)
    if not options:
        await callback.answer(
            texts.t('ROLLYPAY_UNAVAILABLE', 'Платёжная система №1 временно недоступна.'), show_alert=True
        )
        return
    await state.update_data(payment_method='rollypay', rollypay_method=None)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=option['name'], callback_data=f'rollypay_method_{option["id"]}')]
            for option in options
        ]
        + [[InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')]]
    )
    from app.utils.photo_message import edit_or_answer_photo

    await edit_or_answer_photo(
        callback, texts.t('ROLLYPAY_SELECT_PAYMENT_METHOD', 'Выберите способ оплаты в платёжной системе №1:'), keyboard
    )
    await callback.answer()


async def _rollypay_options(db: AsyncSession, user: User) -> list[dict]:
    if not settings.is_rollypay_enabled():
        return []
    methods = await get_enabled_methods_for_user(db, user=user)
    return next((method.get('options') or [] for method in methods if method['id'] == 'rollypay'), [])


@error_handler
async def select_rollypay_method(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    if getattr(db_user, 'restriction_topup', False):
        await callback.answer(texts.t('TOPUP_RESTRICTED', 'Пополнение ограничено администратором.'), show_alert=True)
        return
    method = callback.data.removeprefix('rollypay_method_')
    options = await _rollypay_options(db, db_user)
    if method not in {option['id'] for option in options}:
        await callback.answer(texts.t('PAYMENT_METHOD_UNAVAILABLE', 'Этот способ оплаты недоступен.'), show_alert=True)
        return
    data = await state.get_data()
    await state.update_data(payment_method='rollypay', rollypay_method=method)
    await state.set_state(BalanceStates.waiting_for_amount)
    pending_amount = int(data.get('rollypay_pending_amount') or 0)
    await state.update_data(rollypay_pending_amount=None)
    await callback.answer()
    if pending_amount > 0:
        await process_rollypay_payment_amount(callback.message, db_user, db, pending_amount, state)
        return

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(payment_method='rollypay')

    min_amount = settings.ROLLYPAY_MIN_AMOUNT_KOPEKS // 100
    max_amount = settings.ROLLYPAY_MAX_AMOUNT_KOPEKS // 100
    display_name = settings.get_rollypay_display_name()

    keyboard = await get_topup_amount_keyboard('rollypay', db_user.language, back_callback='topup_rollypay')

    from app.utils.photo_message import edit_or_answer_photo

    await edit_or_answer_photo(
        callback,
        texts.t(
            'ROLLYPAY_ENTER_AMOUNT',
            '\U0001f4b3 <b>Пополнение через {name}</b>\n\n'
            'Введите сумму пополнения в рублях.\n\n'
            'Минимум: {min_amount}\u20bd\n'
            'Максимум: {max_amount}\u20bd',
        ).format(
            name=html.escape(display_name),
            min_amount=min_amount,
            max_amount=f'{max_amount:,}'.replace(',', ' '),
        ),
        keyboard,
    )
