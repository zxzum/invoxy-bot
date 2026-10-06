"""Один раз на пользователя и язык отправляет постоянную reply-клавиатуру Invoxy.

Стоит самым внутренним middleware (после AuthMiddleware), поэтому db_user уже загружен.
Клавиатура приходит отдельным коротким сообщением после ответа на текущий апдейт — так
она появляется и у существующих пользователей, и у только что зарегистрированных, и не
требует правок в огромном cmd_start. Любая ошибка логируется и не влияет на основной ответ.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.database.crud.user import get_user_by_telegram_id
from app.keyboards.invoxy_reply import build_reply_keyboard, normalize_language
from app.localization.texts import get_texts
from app.utils.invoxy_bot_profile import apply_user_profile
from app.utils.invoxy_keyboard_marker import claim_marker


logger = structlog.get_logger(__name__)


class InvoxyReplyKeyboardMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        result = await handler(event, data)
        try:
            await self._install(event, data)
        except Exception as error:
            logger.warning('Не удалось установить reply-клавиатуру Invoxy', error=error)
        return result

    @staticmethod
    async def _install(event: TelegramObject, data: dict[str, Any]) -> None:
        if not isinstance(event, (Message, CallbackQuery)):
            return
        tg_user = event.from_user
        if tg_user is None or tg_user.is_bot:
            return
        user = data.get('db_user')
        if user is None and data.get('db') is not None:
            # Регистрация могла завершиться внутри этого апдейта.
            user = await get_user_by_telegram_id(data['db'], tg_user.id)
        if user is None:
            return
        language = normalize_language(user.language)
        if not await claim_marker(tg_user.id, language):
            return
        bot = data.get('bot') or event.bot
        texts = get_texts(language)
        await bot.send_message(
            chat_id=tg_user.id,  # личный чат: id чата совпадает с id пользователя
            text=texts.t('INVOXY_KEYBOARD_READY'),
            reply_markup=build_reply_keyboard(language),
        )
        await apply_user_profile(bot, tg_user.id, language)
