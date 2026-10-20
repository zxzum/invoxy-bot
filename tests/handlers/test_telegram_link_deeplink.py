from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User as TelegramUser

from app.database.models import UserStatus
from app.handlers import start
from app.services.telegram_link_service import TG_LINK_TOKEN_MIN_LENGTH


@pytest.mark.asyncio
async def test_linktg_confirm_direct_link():
    telegram_user = TelegramUser(id=999, is_bot=False, first_name='Direct', username='direct_tg')
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=999, type='private'),
        from_user=telegram_user,
    )
    token = 'x' * TG_LINK_TOKEN_MIN_LENGTH
    callback = CallbackQuery(
        id='query',
        from_user=telegram_user,
        chat_instance='chat',
        data=f'linktg_confirm:{token}',
        message=message,
    )
    target_user = SimpleNamespace(
        id=65,
        email='user@example.com',
        telegram_id=None,
        username=None,
        first_name=None,
        last_name=None,
        status=UserStatus.ACTIVE.value,
        updated_at=None,
    )

    db_mock = AsyncMock()

    with (
        patch.object(CallbackQuery, 'answer', AsyncMock()),
        patch.object(Message, 'edit_text', AsyncMock()) as edit_text,
        patch.object(start, 'get_telegram_link_token', AsyncMock(return_value={'status': 'pending', 'user_id': 65})),
        patch.object(start, 'get_user_by_id', AsyncMock(return_value=target_user)),
        patch.object(start, 'get_user_by_telegram_id', AsyncMock(return_value=None)),
        patch.object(start, 'set_telegram_link_token_status', AsyncMock(return_value=True)) as set_status,
    ):
        await start.process_linktg_confirm(callback, db_mock)

    assert target_user.telegram_id == 999
    assert target_user.username == 'direct_tg'
    db_mock.commit.assert_awaited_once()
    set_status.assert_awaited_once_with(token, 'linked', telegram_id=999)
    edit_text.assert_awaited_once()
    assert 'Telegram успешно привязан' in edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_linktg_confirm_merge_required():
    telegram_user = TelegramUser(id=888, is_bot=False, first_name='ExistingTg')
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=888, type='private'),
        from_user=telegram_user,
    )
    token = 'y' * TG_LINK_TOKEN_MIN_LENGTH
    callback = CallbackQuery(
        id='query',
        from_user=telegram_user,
        chat_instance='chat',
        data=f'linktg_confirm:{token}',
        message=message,
    )
    target_user = SimpleNamespace(id=65, email='user@example.com', telegram_id=None, status=UserStatus.ACTIVE.value)
    existing_user = SimpleNamespace(id=63, telegram_id=888, status=UserStatus.ACTIVE.value)

    db_mock = AsyncMock()

    with (
        patch.object(CallbackQuery, 'answer', AsyncMock()),
        patch.object(Message, 'edit_text', AsyncMock()) as edit_text,
        patch.object(start, 'get_telegram_link_token', AsyncMock(return_value={'status': 'pending', 'user_id': 65})),
        patch.object(start, 'get_user_by_id', AsyncMock(return_value=target_user)),
        patch.object(start, 'get_user_by_telegram_id', AsyncMock(return_value=existing_user)),
        patch(
            'app.cabinet.auth.merge_service.create_merge_token',
            AsyncMock(return_value='test_merge_token_123'),
        ),
        patch.object(start, 'set_telegram_link_token_status', AsyncMock(return_value=True)) as set_status,
    ):
        await start.process_linktg_confirm(callback, db_mock)

    # Should not directly mutate target_user.telegram_id without merge
    assert target_user.telegram_id is None
    set_status.assert_awaited_once_with(
        token, 'merge_required', merge_token='test_merge_token_123', telegram_id=888
    )
    edit_text.assert_awaited_once()
    assert 'Обнаружен существующий аккаунт Telegram' in edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_linktg_deny():
    telegram_user = TelegramUser(id=777, is_bot=False, first_name='DenyUser')
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=777, type='private'),
        from_user=telegram_user,
    )
    callback = CallbackQuery(
        id='query',
        from_user=telegram_user,
        chat_instance='chat',
        data='linktg_deny',
        message=message,
    )

    with (
        patch.object(CallbackQuery, 'answer', AsyncMock()),
        patch.object(Message, 'edit_text', AsyncMock()) as edit_text,
    ):
        await start.process_linktg_confirm(callback, AsyncMock())

    edit_text.assert_awaited_once()
    assert 'Привязка Telegram отменена' in edit_text.await_args.args[0]
