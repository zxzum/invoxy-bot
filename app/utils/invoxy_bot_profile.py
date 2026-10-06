"""Профиль бота Invoxy: меню команд, описание и кнопка «Кабинет» рядом с полем ввода.

Глобально (при старте) — русский по умолчанию и английский по language_code. Для конкретного
чата — после выбора языка пользователем: Telegram иначе выбирает команды по языку приложения,
а не по сохранённому в боте. Ошибки Telegram логируются и не пробрасываются.
"""

from __future__ import annotations

import structlog
from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, MenuButtonWebApp, WebAppInfo

from app.keyboards.invoxy_reply import SUPPORTED_LANGUAGES, normalize_language
from app.localization.texts import get_texts
from app.utils.chat_menu_button import _resolve_menu_button_url
from app.utils.invoxy_links import website_url


logger = structlog.get_logger(__name__)

DESCRIPTION_LIMIT = 512
SHORT_DESCRIPTION_LIMIT = 120

_COMMAND_KEYS: tuple[tuple[str, str], ...] = (
    ('start', 'INVOXY_CMD_START'),
    ('tariff', 'INVOXY_CMD_TARIFF'),
    ('renew', 'INVOXY_CMD_RENEW'),
    ('cabinet', 'INVOXY_CMD_CABINET'),
    ('site', 'INVOXY_CMD_SITE'),
    ('support', 'INVOXY_CMD_SUPPORT'),
    ('language', 'INVOXY_CMD_LANGUAGE'),
)


def commands_for(language: str | None) -> list[BotCommand]:
    texts = get_texts(normalize_language(language))
    site = website_url()
    return [
        BotCommand(command=command, description=texts.t(key))
        for command, key in _COMMAND_KEYS
        if command != 'site' or site
    ]


def description_for(language: str | None) -> str:
    texts = get_texts(normalize_language(language))
    site = website_url()
    text = texts.t('INVOXY_BOT_DESCRIPTION').format(site_line=f'\n{site}' if site else '')
    if len(text) > DESCRIPTION_LIMIT:  # слишком длинная ссылка — без неё, но в лимите
        text = texts.t('INVOXY_BOT_DESCRIPTION').format(site_line='')
    return text[:DESCRIPTION_LIMIT]


def short_description_for(language: str | None) -> str:
    return get_texts(normalize_language(language)).t('INVOXY_BOT_SHORT')[:SHORT_DESCRIPTION_LIMIT]


async def _safe(label: str, call) -> None:
    try:
        await call
    except Exception as error:
        logger.warning('Не удалось обновить профиль бота', step=label, error=error)


async def configure_bot_profile(bot: Bot) -> None:
    """Команды и описание бота: без language_code — русский (по умолчанию), плюс 'en'."""
    for language in SUPPORTED_LANGUAGES:
        code = None if language == 'ru' else language
        await _safe('commands', bot.set_my_commands(commands_for(language), language_code=code))
        await _safe('description', bot.set_my_description(description=description_for(language), language_code=code))
        await _safe(
            'short_description',
            bot.set_my_short_description(short_description=short_description_for(language), language_code=code),
        )


async def apply_user_profile(bot: Bot, chat_id: int, language: str | None) -> None:
    """Команды и кнопка меню конкретного чата на языке пользователя."""
    lang = normalize_language(language)
    await _safe(
        'chat_commands',
        bot.set_my_commands(commands_for(lang), scope=BotCommandScopeChat(chat_id=chat_id)),
    )
    url = _resolve_menu_button_url()
    if not url:
        return
    button = MenuButtonWebApp(text=get_texts(lang).t('INVOXY_BTN_CABINET'), web_app=WebAppInfo(url=url))
    await _safe('chat_menu_button', bot.set_chat_menu_button(chat_id=chat_id, menu_button=button))
