"""Telegram bot handlers for migrating users from Lazeika VPN (temporary)."""

import structlog
from aiogram import Bot, F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.user import create_user_no_commit, get_user_by_telegram_id
from app.database.models import User
from app.services.migration import (
    build_migration_offer_text,
    check_migration_eligibility,
    execute_user_migration,
    get_migration_offer_keyboard,
    send_migration_notification,
)


logger = structlog.get_logger(__name__)

migration_router = Router(name='migration')


def get_migration_success_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text='📱 В главное меню',
                    callback_data='back_to_menu',
                )
            ]
        ]
    )


async def check_and_prompt_lazeika_migration(
    message: types.Message,
    state: FSMContext,
    db: AsyncSession,
    user: User | None = None,
    telegram_id: int | None = None,
) -> bool:
    """Checks if the user has an active subscription in Lazeika VPN and offers transfer.

    Returns True if an offer was displayed, False otherwise.
    """
    target_tg_id = (
        telegram_id
        or (user.telegram_id if user and user.telegram_id else None)
        or (message.chat.id if message and message.chat and getattr(message.chat, 'type', None) == 'private' else None)
        or (message.from_user.id if message and message.from_user else None)
    )
    if not target_tg_id:
        return False

    try:
        candidate = await check_migration_eligibility(db, telegram_id=target_tg_id, user=user)
        if not candidate:
            return False

        text = build_migration_offer_text(candidate)

        await message.answer(
            text,
            reply_markup=get_migration_offer_keyboard(),
            parse_mode='HTML',
        )
        return True
    except Exception as e:
        logger.warning('Failed to check and prompt Lazeika migration', telegram_id=target_tg_id, error=str(e))
        return False


_migration_in_progress: set[int] = set()


@migration_router.callback_query(F.data == 'lazeika_migrate:execute')
async def handle_migration_execute(
    callback: types.CallbackQuery,
    state: FSMContext,
    db: AsyncSession,
):
    """Executes the migration when the user confirms transfer."""
    telegram_id = callback.from_user.id
    if telegram_id in _migration_in_progress:
        await callback.answer('⏳ Перенос уже выполняется, пожалуйста, подождите...', show_alert=True)
        return

    _migration_in_progress.add(telegram_id)
    try:
        await callback.answer('⏳ Выполняем перенос подписки...')

        # Ensure user exists in Invoxy DB
        user = await get_user_by_telegram_id(db, telegram_id)
        fsm_data = await state.get_data() or {}
        ref_code = fsm_data.get('referral_code')

        if not user:
            user = await create_user_no_commit(
                db,
                telegram_id=telegram_id,
                username=callback.from_user.username,
                first_name=callback.from_user.first_name,
                last_name=callback.from_user.last_name,
                language=callback.from_user.language_code or 'ru',
            )
            await db.commit()
            await db.refresh(user)

        # Attach referrer if user was invited via referral link
        if user.referred_by_id is None:
            try:
                from app.services.referral_service import attach_referrer_if_missing
                await attach_referrer_if_missing(
                    db,
                    user,
                    referral_code=ref_code,
                    bot=callback.bot,
                    source='bot_migration',
                )
            except Exception as e:
                logger.warning('Failed to attach referrer during bot migration', error=str(e))

        # Check if user already has an active paid subscription
        user_subs = getattr(user, 'subscriptions', None) or []
        has_active_paid = any(s.is_active and not s.is_trial for s in user_subs)
        if has_active_paid:
            await state.clear()
            already_text = (
                '🎉 <b>Ваша подписка уже успешно перенесена!</b>\n\n'
                'Все ваши подключённые устройства и ключи работают в обычном режиме.\n'
                'Управление подпиской доступно в главном меню.'
            )
            try:
                await callback.message.edit_text(
                    already_text,
                    reply_markup=get_migration_success_keyboard(),
                    parse_mode='HTML',
                )
            except Exception:
                await callback.message.answer(
                    already_text,
                    reply_markup=get_migration_success_keyboard(),
                    parse_mode='HTML',
                )
            return

        # Immediately edit message to loading state so buttons cannot be clicked repeatedly
        try:
            await callback.message.edit_text(
                '⏳ <b>Выполняем перенос вашей подписки...</b>\n\n'
                'Пожалуйста, подождите несколько секунд. Мы переносим ваши параметры подключения и начисляем бонус 🎁',
                parse_mode='HTML',
            )
        except Exception:
            pass

        result = await execute_user_migration(
            db,
            user,
            telegram_id=telegram_id,
            source='telegram',
        )

        balance_success = ''
        if result.balance_transferred_rub > 0:
            balance_success = (
                f'💰 Перенесён баланс: <b>+{result.balance_transferred_rub:.2f} ₽</b> (зачислен на ваш счёт)\n'
            )

        success_text = (
            '🎉 <b>Подписка успешно перенесена!</b>\n\n'
            'Теперь всё управление осуществляется через этого бота: <b>@invoxy_bot</b>\n'
            '🎁 Начислен бонус: <b>+5 дней</b>!\n'
            f'{balance_success}'
            '⚡️ Лимиты трафика сброшены!\n\n'
            '📋 <b>Параметры вашей подписки:</b>\n'
            f'• Тариф: <b>{result.tariff_name}</b>\n'
            f'• Срок доступа: <b>{result.total_days}</b> дн. (до {result.end_date_str})\n'
            f'• Лимит устройств: <b>{result.device_limit}</b>\n\n'
            '<i>Все ваши подключённые устройства и ключи продолжают работать без перенастройки!</i>'
        )

        await state.clear()
        try:
            await callback.message.edit_text(
                success_text,
                reply_markup=get_migration_success_keyboard(),
                parse_mode='HTML',
            )
        except Exception:
            await callback.message.answer(
                success_text,
                reply_markup=get_migration_success_keyboard(),
                parse_mode='HTML',
            )
    except ValueError as val_err:
        logger.warning(
            'Migration candidate not found or already migrated',
            telegram_id=telegram_id,
            reason=str(val_err),
        )
        info_text = (
            'ℹ️ <b>Подписка не найдена или уже перенесена</b>\n\n'
            'Активная подписка в Лазейка ВПН не обнаружена, либо перенос уже был завершён ранее.\n\n'
            'Если вы считаете, что произошла ошибка, обратитесь в нашу поддержку.'
        )
        await state.clear()
        try:
            await callback.message.edit_text(
                info_text,
                reply_markup=get_migration_success_keyboard(),
                parse_mode='HTML',
            )
        except Exception:
            await callback.message.answer(
                info_text,
                reply_markup=get_migration_success_keyboard(),
                parse_mode='HTML',
            )
    except Exception as e:
        logger.error(
            'Migration execution failed via Telegram bot', telegram_id=telegram_id, error=str(e), exc_info=True
        )
        error_text = (
            '❌ <b>Не удалось выполнить перенос</b>\n\n'
            'Произошла временная ошибка при обработке переноса. Пожалуйста, попробуйте еще раз через минуту или обратитесь в поддержку.'
        )
        try:
            await callback.message.edit_text(
                error_text,
                reply_markup=get_migration_offer_keyboard(),
                parse_mode='HTML',
            )
        except Exception:
            await callback.message.answer(
                error_text,
                reply_markup=get_migration_offer_keyboard(),
                parse_mode='HTML',
            )
    finally:
        _migration_in_progress.discard(telegram_id)


@migration_router.callback_query(F.data == 'lazeika_migrate:skip')
async def handle_migration_skip(
    callback: types.CallbackQuery,
    state: FSMContext,
    db: AsyncSession,
):
    """User skipped the migration offer."""
    await callback.answer()
    telegram_id = callback.from_user.id
    user = await get_user_by_telegram_id(db, telegram_id)
    if not user:
        user = await create_user_no_commit(
            db,
            telegram_id=telegram_id,
            username=callback.from_user.username,
            first_name=callback.from_user.first_name,
            last_name=callback.from_user.last_name,
            language=callback.from_user.language_code or 'ru',
        )
        await db.commit()
        await db.refresh(user)

    await state.clear()
    from app.handlers.menu import handle_back_to_menu
    await handle_back_to_menu(callback, state, user, db)


@migration_router.callback_query(F.data == 'lazeika_migrate:check')
async def handle_migration_check_callback(
    callback: types.CallbackQuery,
    state: FSMContext,
    db: AsyncSession,
):
    """User manually requested migration check via button."""
    telegram_id = callback.from_user.id
    user = await get_user_by_telegram_id(db, telegram_id)
    candidate = await check_migration_eligibility(db, telegram_id=telegram_id, user=user)
    if not candidate:
        await callback.answer(
            'ℹ️ Активная подписка в Лазейка ВПН для вашего аккаунта не найдена или уже перенесена.',
            show_alert=True,
        )
        return

    text = build_migration_offer_text(candidate)
    await callback.message.answer(
        text,
        reply_markup=get_migration_offer_keyboard(),
        parse_mode='HTML',
    )
    await callback.answer()


@migration_router.message(Command('migrate', 'lazeika'))
async def handle_migration_command(
    message: types.Message,
    state: FSMContext,
    db: AsyncSession,
):
    """Command /migrate or /lazeika to check and initiate migration."""
    telegram_id = message.from_user.id
    user = await get_user_by_telegram_id(db, telegram_id)
    candidate = await check_migration_eligibility(db, telegram_id=telegram_id, user=user)
    if not candidate:
        await message.answer(
            'ℹ️ <b>Перенос из Лазейка ВПН</b>\n\n'
            'Активная подписка в Лазейка ВПН для вашего Telegram ID не найдена, либо уже была перенесена ранее.\n\n'
            'Если вы считаете, что произошла ошибка, пожалуйста, обратитесь в поддержку.',
            parse_mode='HTML',
        )
        return

    text = build_migration_offer_text(candidate)
    await message.answer(
        text,
        reply_markup=get_migration_offer_keyboard(),
        parse_mode='HTML',
    )


def register_handlers(dp) -> None:
    dp.include_router(migration_router)

