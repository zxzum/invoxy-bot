from __future__ import annotations

import pytest

from app.config import settings
from app.keyboards.inline import get_main_menu_keyboard


def test_main_menu_has_single_cabinet_webapp_button(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'default')
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://invoxy.my')
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://invoxy.my')

    kb = get_main_menu_keyboard(
        language='ru',
        has_had_paid_subscription=True,
        has_active_subscription=True,
        subscription_is_active=True,
        balance_kopeks=8700,
        is_admin=True,
    )

    all_buttons = [btn for row in kb.inline_keyboard for btn in row]
    webapp_buttons = [btn for btn in all_buttons if btn.web_app is not None]

    cabinet_webapps = [btn for btn in webapp_buttons if btn.web_app.url == 'https://invoxy.my']
    assert len(cabinet_webapps) == 1
    assert 'Личный кабинет' in cabinet_webapps[0].text

    callbacks = {btn.callback_data for btn in all_buttons if btn.callback_data}
    assert 'menu_balance' in callbacks
    assert 'menu_subscription' in callbacks
    assert 'menu_promocode' in callbacks
    assert 'menu_referrals' in callbacks
    assert 'menu_support' in callbacks
    assert 'menu_info' in callbacks
    assert 'menu_language' in callbacks
    assert 'admin_panel' in callbacks


def test_main_menu_without_cabinet_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'default')
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', '')
    monkeypatch.setattr(settings, 'CABINET_URL', '')

    kb = get_main_menu_keyboard(
        language='ru',
        has_had_paid_subscription=False,
        has_active_subscription=False,
        subscription_is_active=False,
        balance_kopeks=0,
        is_admin=False,
    )

    all_buttons = [btn for row in kb.inline_keyboard for btn in row]
    webapp_buttons = [btn for btn in all_buttons if btn.web_app is not None]
    assert len(webapp_buttons) == 0
