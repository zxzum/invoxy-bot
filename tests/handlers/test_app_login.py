from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import types

from app.handlers import app_login
from app.services import app_client_service as svc


def _message() -> types.Message:
    message = AsyncMock(spec=types.Message)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    return message


@pytest.fixture
def links(monkeypatch: pytest.MonkeyPatch) -> dict:
    data = {
        'downloads': {'android': 'https://example.com/app.apk', 'ios': None, 'macos': None, 'windows': None},
        'app_page': 'https://invoxy.example/app',
    }
    monkeypatch.setattr(svc, 'public_links', lambda: data)
    monkeypatch.setattr(svc, 'return_url', lambda: 'https://invoxy.example/app/return')
    return data


def test_app_page_offers_only_configured_downloads(links: dict) -> None:
    text, keyboard = app_login.build_app_connect_payload(SimpleNamespace())
    urls = [b.url for row in keyboard.inline_keyboard for b in row if b.url]
    assert urls == ['https://example.com/app.apk', 'https://invoxy.example/app']
    assert 'Войти через Telegram' in text
    links['downloads']['android'] = None
    text, keyboard = app_login.build_app_connect_payload(SimpleNamespace())
    assert 'появятся' in text


async def test_prompt_for_expired_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, 'get_pending_login', AsyncMock(return_value=None))
    message = _message()
    await app_login.show_app_login_prompt(message, 'x' * 32)
    assert 'устарел' in message.answer.await_args.args[0]


async def test_prompt_shows_device_and_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, 'get_pending_login', AsyncMock(return_value={'platform': 'android'}))
    message = _message()
    await app_login.show_app_login_prompt(message, 'r' * 32)
    text = message.answer.await_args.args[0]
    keyboard = message.answer.await_args.kwargs['reply_markup']
    assert 'Android' in text
    assert [b.callback_data for b in keyboard.inline_keyboard[0]] == [
        f'applogin:ok:{"r" * 32}',
        f'applogin:no:{"r" * 32}',
    ]


@pytest.mark.parametrize(('decision', 'approve'), [('ok', True), ('no', False)])
async def test_decision_binds_request_without_secrets_in_return_url(
    monkeypatch: pytest.MonkeyPatch, links: dict, decision: str, approve: bool
) -> None:
    decide = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, 'decide_login', decide)
    message = _message()
    callback = SimpleNamespace(data=f'applogin:{decision}:{"r" * 32}', message=message, answer=AsyncMock())
    monkeypatch.setattr(app_login.types, 'Message', type(message))
    await app_login.process_app_login_decision(callback, SimpleNamespace(id=7, status='active'))
    decide.assert_awaited_once_with('r' * 32, user_id=7, approve=approve)
    if approve:
        button = message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0]
        assert button.url == 'https://invoxy.example/app/return'
    else:
        assert message.edit_text.await_args.args[0] == 'Вход отменён.'


async def test_inactive_user_cannot_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    decide = AsyncMock()
    monkeypatch.setattr(svc, 'decide_login', decide)
    message = _message()
    monkeypatch.setattr(app_login.types, 'Message', type(message))
    callback = SimpleNamespace(data=f'applogin:ok:{"r" * 32}', message=message, answer=AsyncMock())
    await app_login.process_app_login_decision(callback, SimpleNamespace(id=7, status='blocked'))
    decide.assert_not_awaited()
