from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import types

from app.middlewares import invoxy_reply_keyboard as mw
from app.utils import invoxy_keyboard_marker as marker


class _FakeCache:
    def __init__(self, connected: bool = True) -> None:
        self._connected = connected
        self.keys: set[str] = set()

    async def setnx(self, key, value, expire=None) -> bool:
        if key in self.keys:
            return False
        self.keys.add(key)
        return True

    async def delete(self, key) -> None:
        self.keys.discard(key)


@pytest.fixture
def cache(monkeypatch: pytest.MonkeyPatch) -> _FakeCache:
    fake = _FakeCache()
    monkeypatch.setattr(marker, 'cache', fake)
    monkeypatch.setattr(mw, 'apply_user_profile', AsyncMock())
    return fake


def _event(user_id: int = 42) -> AsyncMock:
    event = AsyncMock(spec=types.Message)
    event.from_user = SimpleNamespace(id=user_id, is_bot=False)
    event.chat = SimpleNamespace(id=user_id, type='private')
    return event


def _data(user=None, bot=None) -> dict:
    return {'db_user': user, 'db': AsyncMock(), 'bot': bot or AsyncMock()}


def _user(language: str = 'ru') -> SimpleNamespace:
    return SimpleNamespace(id=1, telegram_id=42, language=language)


async def _run(event, data, handler=None):
    handler = handler or AsyncMock(return_value='result')
    return await mw.InvoxyReplyKeyboardMiddleware()(handler, event, data), handler


async def test_removes_keyboard_once_per_language(cache) -> None:
    data = _data(_user())
    result, _ = await _run(_event(), data)
    assert result == 'result'
    sent = data['bot'].send_message.await_args.kwargs
    assert sent['chat_id'] == 42
    assert 'под сообщением' in sent['text']
    assert isinstance(sent['reply_markup'], types.ReplyKeyboardRemove)
    assert sent['reply_markup'].remove_keyboard is True
    mw.apply_user_profile.assert_awaited_once()
    again = _data(_user())
    await _run(_event(), again)
    again['bot'].send_message.assert_not_awaited()


async def test_english_user_gets_english_removal_hint(cache) -> None:
    data = _data(_user('en'))
    await _run(_event(), data)
    sent = data['bot'].send_message.await_args.kwargs
    assert 'buttons below the message' in sent['text']
    assert sent['reply_markup'].remove_keyboard is True


async def test_removal_marker_prevents_resend(cache) -> None:
    assert await marker.claim_marker(42, 'en')
    data = _data(_user('en'))
    await _run(_event(), data)
    data['bot'].send_message.assert_not_awaited()


async def test_redis_down_sends_nothing(cache) -> None:
    cache._connected = False
    data = _data(_user())
    await _run(_event(), data)
    data['bot'].send_message.assert_not_awaited()


async def test_unregistered_user_gets_nothing(cache, monkeypatch) -> None:
    monkeypatch.setattr(mw, 'get_user_by_telegram_id', AsyncMock(return_value=None))
    data = _data(None)
    await _run(_event(), data)
    data['bot'].send_message.assert_not_awaited()


async def test_user_registered_during_the_update_is_found_afterwards(cache, monkeypatch) -> None:
    monkeypatch.setattr(mw, 'get_user_by_telegram_id', AsyncMock(return_value=_user()))
    data = _data(None)
    await _run(_event(), data)
    data['bot'].send_message.assert_awaited_once()


async def test_handler_error_propagates_and_nothing_is_sent(cache) -> None:
    data = _data(_user())
    with pytest.raises(RuntimeError):
        await _run(_event(), data, AsyncMock(side_effect=RuntimeError('boom')))
    data['bot'].send_message.assert_not_awaited()


async def test_send_failure_is_swallowed_and_result_returned(cache) -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError('telegram down')
    result, _ = await _run(_event(), _data(_user(), bot))
    assert result == 'result'


async def test_bots_and_non_message_events_are_ignored(cache) -> None:
    data = _data(_user())
    event = _event()
    event.from_user = SimpleNamespace(id=7, is_bot=True)
    await _run(event, data)
    await _run(SimpleNamespace(), data)
    data['bot'].send_message.assert_not_awaited()


async def test_old_install_marker_does_not_skip_removal(cache) -> None:
    cache.keys.add('invoxy:rk:42:ru')
    data = _data(_user())
    await _run(_event(), data)
    assert data['bot'].send_message.await_args.kwargs['reply_markup'].remove_keyboard


async def test_removal_send_failure_retries_next_update(cache) -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError('telegram down')
    await _run(_event(), _data(_user(), bot))
    data = _data(_user())
    await _run(_event(), data)
    data['bot'].send_message.assert_awaited_once()


async def test_group_messages_are_ignored(cache) -> None:
    event = _event()
    event.chat.type = 'group'
    data = _data(_user())
    await _run(event, data)
    data['bot'].send_message.assert_not_awaited()


async def test_inline_callback_removes_old_keyboard(cache) -> None:
    event = AsyncMock(spec=types.CallbackQuery)
    event.from_user = SimpleNamespace(id=42, is_bot=False)
    event.message = _event()
    data = _data(_user())
    await _run(event, data)
    assert data['bot'].send_message.await_args.kwargs['reply_markup'].remove_keyboard
