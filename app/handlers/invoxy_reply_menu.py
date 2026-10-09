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
from app.keyboards.inline import get_support_keyboard
from app.keyboards.invoxy_reply import build_reply_keyboard, normalize_language, resolve_reply_action
from app.localization.texts import get_texts
from app.services.support_settings_service import SupportSettingsService
from app.states import RegistrationStates
from app.utils.cache import cache
from app.utils.invoxy_bot_profile import apply_user_profile
from app.utils.invoxy_keyboard_marker import set_marker
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
    # reply-клавиатуру можно заменить только новым сообщением
    try:
        await callback.bot.send_message(
            chat_id=chat_id, text=texts.t('INVOXY_KEYBOARD_READY'), reply_markup=build_reply_keyboard(code)
        )
        await set_marker(db_user.telegram_id, code)
        await apply_user_profile(callback.bot, chat_id, code)
    except Exception as error:
        logger.warning('Не удалось отправить клавиатуру после смены языка', error=error)
    await callback.answer()


def register_handlers(dp: Dispatcher) -> None:
    not_registering = ~StateFilter(RegistrationStates)
    dp.message.register(handle_command, Command(*_COMMAND_ACTIONS), not_registering)
    dp.message.register(handle_reply_button, _is_reply_button, not_registering)
    dp.callback_query.register(handle_renew_page, F.data.startswith('rnw:'))
    dp.callback_query.register(handle_language_choice, F.data.startswith('ilang:'))
