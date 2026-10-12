"""Роутинг reply-клавиатуры Invoxy, команд-алиасов и переключения языка.

Экраны строятся в invoxy_screens. Экран с баннером уходит через Message.answer(media=...,
media_kind=...) — так же, как в остальных хендлерах; текстовые экраны — через bot.send_message,
потому что Message.answer в режиме логотипа превращает любой ответ в фото.
"""

from __future__ import annotations

import structlog
from aiogram import Dispatcher, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user import update_user
from app.database.models import User
from app.handlers.invoxy_screens import (
    Screen,
    available_languages,
    build_cabinet_screen,
    build_language_picker,
    build_renew_screen,
    build_site_screen,
    build_tariff_screen,
)
from app.keyboards.inline import get_main_menu_keyboard_async, get_support_keyboard
from app.keyboards.invoxy_reply import normalize_language, resolve_reply_action
from app.localization.texts import get_texts
from app.services.main_menu_button_service import MainMenuButtonService
from app.services.subscription_checkout_service import has_subscription_checkout_draft, should_offer_checkout_resume
from app.services.support_settings_service import SupportSettingsService
from app.services.user_cart_service import user_cart_service
from app.states import RegistrationStates
from app.utils.cache import cache
from app.utils.invoxy_bot_profile import apply_user_profile
from app.utils.invoxy_main_menu import more_keyboard
from app.utils.photo_message import edit_or_answer_photo
from app.utils.screen_banners import get_screen_banner


logger = structlog.get_logger(__name__)

LOCK_TTL_SECONDS = 2

_COMMAND_ACTIONS = ('tariff', 'renew', 'cabinet', 'site', 'support', 'language')


async def first_hit(user_id: int, action: str, ttl: int = LOCK_TTL_SECONDS) -> bool:
    """Защита от двойного нажатия: True, если за последние ttl секунд такого действия не было.

    Без Redis (или при его сбое) не блокируем — лучше повтор, чем «мёртвая» кнопка.
    """
    if not getattr(cache, '_connected', False):
        return True
    try:
        return bool(await cache.setnx(f'invoxy:lock:{user_id}:{action}', 1, expire=ttl))
    except Exception:
        return True


async def _send_text(message: types.Message, text: str, markup=None) -> None:
    await message.bot.send_message(chat_id=message.chat.id, text=text, reply_markup=markup, parse_mode='HTML')


async def _send_screen(message: types.Message, screen: Screen) -> None:
    if screen.banner:
        await message.answer(
            screen.text,
            reply_markup=screen.markup,
            parse_mode='HTML',
            media=get_screen_banner(screen.banner),
            media_kind=screen.banner,
        )
    else:
        await _send_text(message, screen.text, screen.markup)


async def _send_support(message: types.Message, language: str) -> None:
    await _send_text(
        message,
        SupportSettingsService.get_support_info_text(language),
        get_support_keyboard(language),
    )


async def dispatch_action(message: types.Message, db: AsyncSession, db_user: User, action: str) -> None:
    language = normalize_language(db_user.language)
    try:
        if action == 'tariff':
            await _send_screen(message, await build_tariff_screen(db, db_user, language))
        elif action == 'renew':
            await _send_screen(message, await build_renew_screen(db, db_user, language))
        elif action == 'cabinet':
            await _send_screen(message, build_cabinet_screen(language))
        elif action == 'site':
            # build_site_screen сам отвечает «ссылка не настроена», если SERVICE_WEBSITE_URL пуст.
            await _send_screen(message, build_site_screen(language))
        elif action == 'support':
            await _send_support(message, language)
        elif action == 'language':
            await _send_screen(message, build_language_picker(language))
    except Exception as error:
        logger.error('Ошибка в действии reply-меню', action=action, error=error, exc_info=True)
        texts = get_texts(language)
        markup = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.t('INVOXY_BTN_MAIN_MENU'), callback_data='back_to_menu')]
            ]
        )
        await _send_text(message, texts.t('INVOXY_ERROR_GENERIC'), markup)


async def _clear_state(state: FSMContext) -> None:
    current = await state.get_state()
    if current and not current.startswith(f'{RegistrationStates.__name__}:'):
        await state.clear()


async def _is_reply_button(message: types.Message) -> bool:
    return resolve_reply_action(message.text) is not None


async def handle_reply_button(message: types.Message, state: FSMContext, db: AsyncSession, db_user: User | None = None):
    action = resolve_reply_action(message.text)
    if action is None or db_user is None:
        return
    if not await first_hit(db_user.telegram_id, action):
        return
    await _clear_state(state)
    await dispatch_action(message, db, db_user, action)


async def handle_command(message: types.Message, state: FSMContext, db: AsyncSession, db_user: User | None = None):
    if db_user is None:
        return
    command = (message.text or '').split()[0].lstrip('/').split('@')[0].lower()
    if command not in _COMMAND_ACTIONS:
        return
    if not await first_hit(db_user.telegram_id, command):
        return
    await _clear_state(state)
    await dispatch_action(message, db, db_user, command)


def _parse_page(data: str | None) -> int | None:
    try:
        page = int((data or '').split(':', 1)[1])
    except (IndexError, ValueError):
        return None
    return page if page >= 0 else None


async def handle_renew_page(callback: types.CallbackQuery, db: AsyncSession, db_user: User | None = None):
    page = _parse_page(callback.data)
    if db_user is None or page is None or not isinstance(callback.message, types.Message):
        await callback.answer()
        return
    screen = await build_renew_screen(db, db_user, normalize_language(db_user.language), page=page)
    try:
        await callback.message.edit_text(
            screen.text,
            reply_markup=screen.markup,
            parse_mode='HTML',
            media=get_screen_banner(screen.banner) if screen.banner else None,
            media_kind=screen.banner,
        )
    except Exception as error:
        if 'message is not modified' not in str(error).lower():
            logger.warning('Не удалось обновить страницу продления', error=error)
    await callback.answer()


async def handle_language_choice(callback: types.CallbackQuery, db: AsyncSession, db_user: User | None = None):
    if db_user is None:
        await callback.answer()
        return
    current = normalize_language(db_user.language)
    code = (callback.data or '').split(':', 1)[-1].strip().lower()
    if code not in available_languages():
        await callback.answer(get_texts(current).t('INVOXY_LANG_UNAVAILABLE'), show_alert=True)
        return
    if code == current:
        await callback.answer(get_texts(current).t('INVOXY_LANG_SAME'))
        return
    if not await first_hit(db_user.telegram_id, 'lang'):
        await callback.answer()
        return

    await update_user(db, db_user, language=code)
    texts = get_texts(code)
    chat_id = callback.message.chat.id
    main_menu = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.t('INVOXY_BTN_MAIN_MENU'), callback_data='back_to_menu')]
        ]
    )
    try:
        await callback.message.edit_text(texts.t('INVOXY_LANG_CHANGED'), reply_markup=main_menu, parse_mode='HTML')
    except Exception as error:
        logger.warning('Не удалось обновить сообщение выбора языка', error=error)
    try:
        await apply_user_profile(callback.bot, chat_id, code)
    except Exception as error:
        logger.warning('Не удалось обновить профиль после смены языка', error=error)
    await callback.answer()


def register_handlers(dp: Dispatcher) -> None:
    not_registering = ~StateFilter(RegistrationStates)
    dp.message.register(handle_command, Command(*_COMMAND_ACTIONS), not_registering)
    dp.message.register(handle_reply_button, _is_reply_button, not_registering)
    dp.callback_query.register(handle_renew_page, F.data.startswith('rnw:'))
    dp.callback_query.register(handle_language_choice, F.data.startswith('ilang:'))
    dp.callback_query.register(handle_more_menu, F.data == 'invoxy_more')


async def handle_more_menu(callback: types.CallbackQuery, db: AsyncSession, db_user: User | None = None):
    if db_user is None or not isinstance(callback.message, types.Message):
        await callback.answer()
        return
    texts = get_texts(db_user.language)
    is_admin = settings.is_admin(db_user.telegram_id)
    active = any(sub.is_active or sub.actual_status == 'limited' for sub in getattr(db_user, 'subscriptions', []) or [])
    custom = await MainMenuButtonService.get_buttons_for_user(
        db, is_admin=is_admin, has_active_subscription=active, subscription_is_active=active
    )
    draft = await has_subscription_checkout_draft(db_user.id)
    saved = await user_cart_service.has_user_cart(db_user.id)
    full = await get_main_menu_keyboard_async(
        db=db,
        user=db_user,
        language=db_user.language,
        is_admin=is_admin,
        is_moderator=(not is_admin) and SupportSettingsService.is_moderator(db_user.telegram_id),
        has_had_paid_subscription=db_user.has_had_paid_subscription,
        has_active_subscription=active,
        subscription_is_active=active,
        balance_kopeks=db_user.balance_kopeks,
        subscription=db_user.subscription,
        show_resume_checkout=should_offer_checkout_resume(db_user, draft),
        has_saved_cart=saved,
        custom_buttons=custom,
        show_all=True,
    )
    from app.utils.message_patch import get_logo_media
    from app.utils.screen_banners import get_screen_banner

    await edit_or_answer_photo(
        callback,
        texts.t('INVOXY_HOME_MORE'),
        more_keyboard(full, texts),
        media=get_screen_banner('main') or get_logo_media(),
        media_kind='main',
    )
    await callback.answer()
