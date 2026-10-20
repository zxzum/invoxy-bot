from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher, types

from app.config import settings
from app.handlers import invoxy_reply_menu as menu, invoxy_screens as screens
from app.states import RegistrationStates, SubscriptionStates


NOW = datetime.now(UTC)


def _sub(sub_id: int = 5, *, status: str = 'active', days: int = 20) -> SimpleNamespace:
    return SimpleNamespace(
        id=sub_id,
        tariff_id=1,
        tariff=SimpleNamespace(name='Стандарт', is_active=True, period_prices={'30': 19900}),
        actual_status=status,
        is_active=status == 'active',
        days_left=max(days, 0),
        end_date=NOW + timedelta(days=days),
        traffic_limit_gb=100,
        traffic_used_gb=1.0,
    )


def _user(*subs, language: str = 'ru') -> SimpleNamespace:
    return SimpleNamespace(id=1, telegram_id=42, language=language, subscriptions=list(subs))


def _message(text: str) -> AsyncMock:
    message = AsyncMock(spec=types.Message)
    message.text = text
    message.chat = SimpleNamespace(id=42)
    message.bot = AsyncMock()
    message.answer = AsyncMock()
    return message


def _state(current: str | None = None) -> AsyncMock:
    state = AsyncMock()
    state.get_state = AsyncMock(return_value=current)
    return state


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://invoxy.example')
    monkeypatch.setattr(settings, 'SERVICE_WEBSITE_URL', 'https://site.example')
    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
    monkeypatch.setattr(settings, 'AVAILABLE_LANGUAGES', 'ru,en')
    monkeypatch.setattr(settings, 'MENU_BUTTON_WEBAPP_ENABLED', True)
    monkeypatch.setattr(
        screens,
        'pricing_engine',
        SimpleNamespace(
            calculate_renewal_price=AsyncMock(return_value=SimpleNamespace(final_total=19900, original_total=19900))
        ),
    )
    monkeypatch.setattr(menu, 'get_screen_banner', lambda kind: f'banner:{kind}')
    fake_cache = SimpleNamespace(_connected=True, setnx=AsyncMock(return_value=True), set=AsyncMock(return_value=True))
    monkeypatch.setattr(menu, 'cache', fake_cache)
    monkeypatch.setattr(menu, 'apply_user_profile', AsyncMock())
    update = AsyncMock()
    monkeypatch.setattr(menu, 'update_user', update)
    return SimpleNamespace(cache=fake_cache, update_user=update)


# ---- reply buttons -----------------------------------------------------------


@pytest.mark.parametrize('text', ['Продлить', 'Renew'])
async def test_renew_button_sends_banner_screen_in_user_language(text) -> None:
    message = _message(text)
    await menu.handle_reply_button(
        message, _state(), AsyncMock(), _user(_sub(), language='en' if text == 'Renew' else 'ru')
    )
    kwargs = message.answer.await_args.kwargs
    assert kwargs['media'] == 'banner:renew'
    assert kwargs['media_kind'] == 'renew'
    assert kwargs['parse_mode'] == 'HTML'
    assert kwargs['reply_markup'].inline_keyboard[0][0].web_app.url == 'https://invoxy.example/subscriptions/5/renew'
    body = message.answer.await_args.args[0]
    assert ('Renewal' in body) == (text == 'Renew')


async def test_text_only_screens_bypass_the_logo_patch() -> None:
    for text in ('Кабинет', 'Язык', 'Сайт'):
        message = _message(text)
        await menu.handle_reply_button(message, _state(), AsyncMock(), _user(_sub()))
        message.answer.assert_not_awaited()
        sent = message.bot.send_message.await_args.kwargs
        assert sent['chat_id'] == 42
        assert sent['parse_mode'] == 'HTML'


async def test_site_without_website_url_says_so_instead_of_staying_silent(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'SERVICE_WEBSITE_URL', None)
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://invoxy.example')
    message = _message('Сайт')
    await menu.handle_reply_button(message, _state(), AsyncMock(), _user(_sub()))
    sent = message.bot.send_message.await_args.kwargs
    assert sent['text'] == 'Ссылка на сайт пока не настроена. Загляните позже.'
    assert 'invoxy.example' not in sent['text']
    assert all(b.url is None for row in sent['reply_markup'].inline_keyboard for b in row)


async def test_support_button_reuses_existing_support_screen(monkeypatch) -> None:
    monkeypatch.setattr(
        menu.SupportSettingsService, 'get_support_info_text', staticmethod(lambda lang: f'support-{lang}')
    )
    monkeypatch.setattr(menu, 'get_support_keyboard', lambda lang: f'kb-{lang}')
    message = _message('Support')
    await menu.handle_reply_button(message, _state(), AsyncMock(), _user(_sub(), language='en'))
    sent = message.answer.await_args
    assert sent.args[0] == 'support-en'
    assert sent.kwargs['media'] == 'banner:support'
    assert sent.kwargs['media_kind'] == 'support'
    assert sent.kwargs['reply_markup'] == 'kb-en'


async def test_unknown_text_and_unregistered_user_are_ignored() -> None:
    message = _message('привет')
    await menu.handle_reply_button(message, _state(), AsyncMock(), _user(_sub()))
    other = _message('Продлить')
    await menu.handle_reply_button(other, _state(), AsyncMock(), None)
    for m in (message, other):
        m.answer.assert_not_awaited()
        m.bot.send_message.assert_not_awaited()


async def test_active_fsm_state_is_cleared_before_the_action() -> None:
    state = _state(SubscriptionStates.selecting_period.state)
    await menu.handle_reply_button(_message('Мой тариф'), state, AsyncMock(), _user(_sub()))
    state.clear.assert_awaited_once()


async def test_double_tap_is_ignored(_env) -> None:
    _env.cache.setnx = AsyncMock(side_effect=[True, False])
    first, second = _message('Продлить'), _message('Продлить')
    await menu.handle_reply_button(first, _state(), AsyncMock(), _user(_sub()))
    await menu.handle_reply_button(second, _state(), AsyncMock(), _user(_sub()))
    first.answer.assert_awaited_once()
    second.answer.assert_not_awaited()


async def test_first_hit_never_blocks_when_redis_is_down(_env) -> None:
    _env.cache._connected = False
    assert await menu.first_hit(1, 'renew') is True
    assert await menu.first_hit(1, 'renew') is True
    _env.cache._connected = True
    _env.cache.setnx = AsyncMock(side_effect=RuntimeError('redis'))
    assert await menu.first_hit(1, 'renew') is True


async def test_failure_shows_generic_error_not_traceback(monkeypatch) -> None:
    monkeypatch.setattr(menu, 'build_renew_screen', AsyncMock(side_effect=RuntimeError('boom')))
    message = _message('Продлить')
    await menu.handle_reply_button(message, _state(), AsyncMock(), _user(_sub()))
    sent = message.bot.send_message.await_args.kwargs['text']
    assert 'Что-то пошло не так' in sent
    assert 'boom' not in sent


# ---- commands ----------------------------------------------------------------


@pytest.mark.parametrize(
    ('command', 'media_kind'), [('/renew', 'renew'), ('/renew@Invoxy_bot', 'renew'), ('/tariff', 'subscription')]
)
async def test_command_aliases_run_the_same_actions(command, media_kind) -> None:
    message = _message(command)
    await menu.handle_command(message, _state(), AsyncMock(), _user(_sub()))
    assert message.answer.await_args.kwargs['media_kind'] == media_kind


# ---- registration ------------------------------------------------------------


def _handler(dp: Dispatcher, callback):
    return next(h for h in dp.message.handlers if h.callback is callback)


async def test_reply_handler_is_not_active_during_registration() -> None:
    dp = Dispatcher()
    menu.register_handlers(dp)
    handler = _handler(dp, menu.handle_reply_button)
    ok, _ = await handler.check(_message('Продлить'), raw_state=RegistrationStates.waiting_for_referral_code.state)
    assert ok is False
    ok, _ = await handler.check(_message('Продлить'), raw_state=None)
    assert ok is True
    ok, _ = await handler.check(_message('Продлить'), raw_state=SubscriptionStates.selecting_period.state)
    assert ok is True
    ok, _ = await handler.check(_message('привет'), raw_state=None)
    assert ok is False


def test_registered_before_start_handlers() -> None:
    from app.handlers import start

    dp = Dispatcher()
    menu.register_handlers(dp)
    start.register_handlers(dp)
    callbacks = [h.callback for h in dp.message.handlers]
    assert callbacks.index(menu.handle_reply_button) < callbacks.index(start.cmd_start)


# ---- paging ------------------------------------------------------------------


def _callback(data: str, user_language: str = 'ru') -> AsyncMock:
    callback = AsyncMock(spec=types.CallbackQuery)
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = AsyncMock(spec=types.Message)
    callback.message.edit_text = AsyncMock()
    callback.message.chat = SimpleNamespace(id=42)
    callback.bot = AsyncMock()
    return callback


async def test_renew_page_edits_the_message_with_banner() -> None:
    subs = [_sub(i, days=i) for i in range(1, 8)]
    callback = _callback('rnw:1')
    await menu.handle_renew_page(callback, AsyncMock(), _user(*subs))
    kwargs = callback.message.edit_text.await_args.kwargs
    assert kwargs['media_kind'] == 'renew'
    assert len(kwargs['reply_markup'].inline_keyboard[0]) == 1
    callback.answer.assert_awaited()


@pytest.mark.parametrize('data', ['rnw:99', 'rnw:-1', 'rnw:abc', 'rnw:'])
async def test_renew_page_survives_bad_payloads(data) -> None:
    callback = _callback(data)
    await menu.handle_renew_page(callback, AsyncMock(), _user(*[_sub(i, days=i) for i in range(1, 8)]))
    callback.answer.assert_awaited()


async def test_renew_page_survives_edit_error() -> None:
    callback = _callback('rnw:1')
    callback.message.edit_text.side_effect = RuntimeError('message is not modified')
    await menu.handle_renew_page(callback, AsyncMock(), _user(*[_sub(i, days=i) for i in range(1, 8)]))
    callback.answer.assert_awaited()


# ---- language switch ---------------------------------------------------------


async def test_language_choice_persists_and_relabels_everything(_env) -> None:
    callback = _callback('ilang:en')
    db = AsyncMock()
    user = _user(_sub(), language='ru')
    await menu.handle_language_choice(callback, db, user)
    _env.update_user.assert_awaited_once_with(db, user, language='en')
    edited = callback.message.edit_text.await_args
    assert edited.args[0] == 'Language switched: English.'
    assert edited.kwargs['reply_markup'].inline_keyboard[0][0].text == 'Main menu'
    callback.bot.send_message.assert_not_awaited()
    menu.apply_user_profile.assert_awaited_once_with(callback.bot, 42, 'en')


@pytest.mark.parametrize('data', ['ilang:xx', 'ilang:ua', 'ilang:', 'ilang:ru' + 'x' * 40])
async def test_language_choice_rejects_unknown_codes(data, _env) -> None:
    callback = _callback(data)
    await menu.handle_language_choice(callback, AsyncMock(), _user(_sub(), language='ru'))
    _env.update_user.assert_not_awaited()
    assert callback.answer.await_args.kwargs.get('show_alert') is True


async def test_language_choice_same_language_is_a_noop(_env) -> None:
    callback = _callback('ilang:ru')
    await menu.handle_language_choice(callback, AsyncMock(), _user(_sub(), language='ru'))
    _env.update_user.assert_not_awaited()
    assert callback.answer.await_args.args[0] == 'Этот язык уже выбран.'


async def test_profile_update_failure_does_not_block_language_confirmation(_env) -> None:
    menu.apply_user_profile.side_effect = RuntimeError('telegram down')
    callback = _callback('ilang:en')
    await menu.handle_language_choice(callback, AsyncMock(), _user(_sub(), language='ru'))
    callback.answer.assert_awaited_once()
    callback.bot.send_message.assert_not_awaited()


async def test_language_choice_respects_available_languages(monkeypatch, _env) -> None:
    monkeypatch.setattr(settings, 'AVAILABLE_LANGUAGES', 'ru')
    callback = _callback('ilang:en')
    await menu.handle_language_choice(callback, AsyncMock(), _user(_sub(), language='ru'))
    _env.update_user.assert_not_awaited()


async def test_language_choice_double_tap_is_ignored(_env) -> None:
    _env.cache.setnx = AsyncMock(return_value=False)
    callback = _callback('ilang:en')
    await menu.handle_language_choice(callback, AsyncMock(), _user(_sub(), language='ru'))
    _env.update_user.assert_not_awaited()


async def test_language_choice_survives_send_keyboard_error(_env) -> None:
    callback = _callback('ilang:en')
    callback.bot.send_message.side_effect = RuntimeError('network error')
    await menu.handle_language_choice(callback, AsyncMock(), _user(_sub(), language='ru'))
    callback.answer.assert_awaited()


def test_command_handler_is_registered() -> None:
    dp = Dispatcher()
    menu.register_handlers(dp)
    assert any(h.callback is menu.handle_command for h in dp.message.handlers)
