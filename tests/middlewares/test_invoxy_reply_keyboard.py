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

    async def set(self, key, value, expire=None) -> bool:
        self.keys.add(key)
        return True


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


async def test_installs_keyboard_once_per_language(cache) -> None:
    data = _data(_user())
    result, _ = await _run(_event(), data)
    assert result == 'result'
    sent = data['bot'].send_message.await_args.kwargs
    assert sent['chat_id'] == 42
    assert sent['text'] == 'Меню — внизу экрана.'
    assert sent['reply_markup'].keyboard[0][0].text == 'Мой тариф'
    mw.apply_user_profile.assert_awaited_once()
    again = _data(_user())
    await _run(_event(), again)
    again['bot'].send_message.assert_not_awaited()


async def test_english_user_gets_english_keyboard(cache) -> None:
    data = _data(_user('en'))
    await _run(_event(), data)
    sent = data['bot'].send_message.await_args.kwargs
    assert sent['text'] == 'The menu is at the bottom of the screen.'
    assert sent['reply_markup'].keyboard[0][0].text == 'My tariff'


async def test_marker_set_by_language_switch_prevents_resend(cache) -> None:
    await marker.set_marker(42, 'en')
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
