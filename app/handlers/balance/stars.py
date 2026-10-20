import html

import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext

from app.config import settings
from app.database.models import User
from app.external.telegram_stars import TelegramStarsService
from app.keyboards.topup_amounts import get_topup_amount_keyboard
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from app.utils.decorators import error_handler
from app.utils.screen_banners import get_screen_banner


logger = structlog.get_logger(__name__)


@error_handler
async def start_stars_payment(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)

    if not settings.TELEGRAM_STARS_ENABLED:
        await callback.answer('❌ Пополнение через Stars временно недоступно', show_alert=True)
        return

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append([types.InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await callback.message.edit_text(
            f'🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\n'
            'Если вы считаете это ошибкой, вы можете обжаловать решение.',
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
            media=get_screen_banner('account_status'),
            media_kind='account_status',
        )
        await callback.answer()
        return

    message_text = texts.TOP_UP_AMOUNT

    keyboard = await get_topup_amount_keyboard('stars', db_user.language, back_callback='balance_topup')

    await callback.message.edit_text(
        message_text, reply_markup=keyboard, media=get_screen_banner('balance'), media_kind='balance'
    )

    await state.update_data(
        stars_prompt_message_id=callback.message.message_id,
        stars_prompt_chat_id=callback.message.chat.id,
    )

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(payment_method='stars')
    await callback.answer()


@error_handler
async def process_stars_payment_amount(message: types.Message, db_user: User, amount_kopeks: int, state: FSMContext):
    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append([types.InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await message.answer(
            f'🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\n'
            'Если вы считаете это ошибкой, вы можете обжаловать решение.',
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode='HTML',
        )
        await state.clear()
        return

    texts = get_texts(db_user.language)

    if not settings.TELEGRAM_STARS_ENABLED:
        await message.answer('⚠️ Оплата Stars временно недоступна')
        return

    try:
        amount_rubles = amount_kopeks / 100
        stars_amount = TelegramStarsService.calculate_stars_from_rubles(amount_rubles)

        payment_service = PaymentService(message.bot)
        from app.database.database import AsyncSessionLocal
        from app.services.invoice_creation import create_managed_stars_invoice, get_stars_invoice

        from .invoices import create_guarded_payment, show_open_invoice

        async with AsyncSessionLocal() as db:
            async def create():
                return await create_managed_stars_invoice(db, db_user, payment_service, amount_kopeks, stars_amount)

            attempted, _ = await create_guarded_payment(message, db, db_user, create)
            if not attempted:
                await state.clear()
                return
            state_data = await state.get_data()
            prompt_id = state_data.get('stars_prompt_message_id')
            prompt_chat = state_data.get('stars_prompt_chat_id', message.chat.id)
            try:
                await message.delete()
                if prompt_id and prompt_id != message.message_id:
                    await message.bot.delete_message(prompt_chat, prompt_id)
            except Exception as error:
                logger.warning('Could not remove Stars amount prompt', error=str(error))
            invoice_message = await show_open_invoice(message, db, db_user)
            record = await get_stars_invoice(db_user)
            await state.update_data(
                stars_invoice_message_id=invoice_message.message_id,
                stars_invoice_chat_id=invoice_message.chat.id,
                stars_invoice_payload=record.identifier,
            )
        await state.set_state(None)

    except Exception as e:
        logger.error('Ошибка создания Stars invoice', error=e)
        await message.answer('⚠️ Ошибка создания платежа')
