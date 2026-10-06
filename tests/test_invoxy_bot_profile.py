from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SetMyCommands
from aiogram.types import BotCommandScopeChat, MenuButtonWebApp

from app.config import settings
from app.utils import invoxy_bot_profile as profile


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'SERVICE_WEBSITE_URL', 'https://site.example')
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://invoxy.example')
    monkeypatch.setattr(settings, 'MENU_BUTTON_WEBAPP_ENABLED', True)
    monkeypatch.setattr(settings, 'MENU_BUTTON_WEBAPP_URL', '')


def _bot() -> AsyncMock:
    return AsyncMock(spec=Bot)


def test_commands_ru_and_en() -> None:
    ru = {c.command: c.description for c in profile.commands_for('ru')}
    en = {c.command: c.description for c in profile.commands_for('en')}
    assert list(ru) == ['start', 'tariff', 'renew', 'cabinet', 'site', 'support', 'language']
    assert ru['start'] == 'Главное меню'
    assert en['start'] == 'Main menu'
    assert en['language'] == 'Change language'


def test_site_command_and_link_omitted_without_website_url(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'SERVICE_WEBSITE_URL', None)
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://invoxy.example')
    assert 'site' not in [c.command for c in profile.commands_for('ru')]
    assert 'http' not in profile.description_for('ru')
    assert 'invoxy.example' not in profile.description_for('en')


def test_description_contains_site_link_and_respects_limits(monkeypatch) -> None:
    assert 'https://site.example' in profile.description_for('ru')
    assert len(profile.description_for('en')) <= 512
    assert len(profile.short_description_for('ru')) <= 120
    monkeypatch.setattr(settings, 'SERVICE_WEBSITE_URL', 'https://site.example/' + 'x' * 600)
    long = profile.description_for('ru')
    assert len(long) <= 512
    assert 'xxxx' not in long


async def test_configure_bot_profile_sets_default_and_english() -> None:
    bot = _bot()
    await profile.configure_bot_profile(bot)
    codes = [call.kwargs.get('language_code') for call in bot.set_my_commands.await_args_list]
    assert codes == [None, 'en']
    assert [c.kwargs.get('language_code') for c in bot.set_my_description.await_args_list] == [None, 'en']
    assert [c.kwargs.get('language_code') for c in bot.set_my_short_description.await_args_list] == [None, 'en']


async def test_apply_user_profile_sets_chat_scoped_commands_and_localized_menu_button() -> None:
    bot = _bot()
    await profile.apply_user_profile(bot, 42, 'en')
    scope = bot.set_my_commands.await_args.kwargs['scope']
    assert isinstance(scope, BotCommandScopeChat) and scope.chat_id == 42
    assert bot.set_my_commands.await_args.args[0][0].description == 'Main menu'
    kwargs = bot.set_chat_menu_button.await_args.kwargs
    assert kwargs['chat_id'] == 42
    button = kwargs['menu_button']
    assert isinstance(button, MenuButtonWebApp)
    assert button.text == 'Cabinet'
    assert button.web_app.url == 'https://invoxy.example'


async def test_menu_button_skipped_when_not_https(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'http://invoxy.example')
    bot = _bot()
    await profile.apply_user_profile(bot, 42, 'ru')
    bot.set_chat_menu_button.assert_not_awaited()
    bot.set_my_commands.assert_awaited()


async def test_telegram_errors_never_propagate() -> None:
    bot = _bot()
    error = TelegramBadRequest(method=SetMyCommands(commands=[]), message='nope')
    for method in ('set_my_commands', 'set_my_description', 'set_my_short_description', 'set_chat_menu_button'):
        getattr(bot, method).side_effect = error
    await profile.configure_bot_profile(bot)
    await profile.apply_user_profile(bot, 1, 'ru')
