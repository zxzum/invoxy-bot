from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.keyboards import inline
from app.services.menu_layout_service import MenuLayoutService


@pytest.fixture(autouse=True)
def menu_settings(monkeypatch):
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'default')
    monkeypatch.setattr(settings, 'MENU_LAYOUT_ENABLED', False)
    monkeypatch.setattr(settings, 'SERVICE_WEBSITE_URL', 'https://invoxy.example')
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://invoxy.example')
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 3)
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'none')
    monkeypatch.setattr(inline, 'get_display_subscription_link', lambda sub: 'https://sub.example/key')


def buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


@pytest.mark.parametrize('mode', ['callback', 'link', 'miniapp_custom', 'miniapp_subscription', 'happ_cryptolink'])
def test_active_connection_is_green_and_secondary_actions_neutral(monkeypatch, mode):
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', mode)
    markup = inline.get_main_menu_keyboard(
        has_active_subscription=True,
        subscription_is_active=True,
        subscription=SimpleNamespace(is_trial=False, traffic_limit_gb=100, tariff_id=None),
    )
    assert markup.inline_keyboard[0][0].style == 'success'
    by_callback = {button.callback_data: button for button in buttons(markup) if button.callback_data}
    assert by_callback['menu_balance'].style is None
    assert by_callback['menu_info'].style is None
    assert 'menu_buy' not in by_callback


@pytest.mark.parametrize(('language', 'label'), [('ru', 'Сайт'), ('en', 'Website')])
def test_purchase_trial_and_website_have_clear_actions(language, label):
    markup = inline.get_main_menu_keyboard(language=language)
    by_callback = {button.callback_data: button for button in buttons(markup) if button.callback_data}
    assert by_callback['menu_buy'].style == 'success'
    assert by_callback['menu_trial'].style == 'primary'
    website = markup.inline_keyboard[-1][0]
    assert website.text == label
    assert website.url == 'https://invoxy.example'
    assert website.web_app is None
    assert 'style' not in website.model_dump(exclude_none=True)


def test_existing_custom_site_link_is_not_duplicated():
    markup = inline.get_main_menu_keyboard(
        custom_buttons=[InlineKeyboardButton(text='Website', url='https://invoxy.example')]
    )
    assert sum(button.url == 'https://invoxy.example' for button in buttons(markup)) == 1


def test_cabinet_menu_keeps_website_distinct_from_miniapp(monkeypatch):
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'cabinet')
    markup = inline.get_main_menu_keyboard()
    assert markup.inline_keyboard[-1][0].url == 'https://invoxy.example'
    assert any(button.web_app is not None for button in buttons(markup))


async def test_layout_builder_also_has_website(monkeypatch):
    monkeypatch.setattr(settings, 'MENU_LAYOUT_ENABLED', True)
    monkeypatch.setattr(MenuLayoutService, 'build_keyboard', AsyncMock(return_value=InlineKeyboardMarkup(inline_keyboard=[])))
    markup = await inline.get_main_menu_keyboard_async(AsyncMock())
    assert markup.inline_keyboard[-1][0].url == 'https://invoxy.example'
