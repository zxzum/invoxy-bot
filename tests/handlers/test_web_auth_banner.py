from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, User as TelegramUser

from app.database.models import UserStatus
from app.handlers import start
from app.services.web_auth_service import WEB_AUTH_TOKEN_MIN_LENGTH


@pytest.mark.asyncio
async def test_web_auth_success_keeps_banner_and_updates_caption():
    telegram_user = TelegramUser(id=123, is_bot=False, first_name='Test')
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=123, type='private'),
        from_user=telegram_user,
        photo=[PhotoSize(file_id='banner', file_unique_id='banner', width=1, height=1)],
    )
    callback = CallbackQuery(
        id='query',
        from_user=telegram_user,
        chat_instance='chat',
        data=f'webauth_confirm:{"x" * WEB_AUTH_TOKEN_MIN_LENGTH}',
        message=message,
    )
    user = SimpleNamespace(id=7, status=UserStatus.ACTIVE.value, language='ru')

    with (
        patch.object(CallbackQuery, 'answer', AsyncMock()),
        patch.object(Message, 'edit_caption', AsyncMock()) as edit_caption,
        patch.object(Message, 'edit_text', AsyncMock()) as edit_text,
        patch.object(start, 'get_user_by_telegram_id', AsyncMock(return_value=user)),
        patch.object(start, 'link_web_auth_token', AsyncMock(return_value=True)),
    ):
        await start.process_webauth_confirm(callback, object())

    edit_caption.assert_awaited_once()
    assert 'Авторизация в кабинете подтверждена' in edit_caption.await_args.kwargs['caption']
    edit_text.assert_not_awaited()
