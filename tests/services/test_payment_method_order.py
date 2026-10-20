from __future__ import annotations

import pytest
from aiogram.types import InlineKeyboardButton

from app.config import settings
from app.keyboards.inline import _apply_payment_name_overrides, get_payment_methods_keyboard
from app.services import payment_method_config_service as pm_service
from app.services.payment_method_config_service import (
    DEFAULT_METHOD_ORDER,
    get_method_sort_order,
)


def test_default_method_order_priorities():
    assert DEFAULT_METHOD_ORDER[0] == 'rollypay'
    assert DEFAULT_METHOD_ORDER[1] == 'platega'
    assert DEFAULT_METHOD_ORDER[2] == 'telegram_stars'


def test_get_method_sort_order_fallback():
    assert get_method_sort_order('rollypay') == 0
    assert get_method_sort_order('platega') == 1
    assert get_method_sort_order('telegram_stars') == 2
    assert get_method_sort_order('unknown_method') == 9999


def test_payment_methods_keyboard_ordering(monkeypatch: pytest.MonkeyPatch):
    # Enable stars, platega, rollypay
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True, raising=False)
    monkeypatch.setattr(type(settings), 'is_platega_enabled', lambda self: True)
    monkeypatch.setattr(type(settings), 'get_platega_active_methods', lambda self: ['1'])
    monkeypatch.setattr(settings, 'PLATEGA_INLINE_METHODS', False, raising=False)
    monkeypatch.setattr(type(settings), 'is_rollypay_enabled', lambda self: True)

    keyboard = get_payment_methods_keyboard(amount_kopeks=1000)
    buttons = [row[0] for row in keyboard.inline_keyboard]
    callbacks = [btn.callback_data for btn in buttons]

    # rollypay should be first, platega second, stars third, and menu_balance at the end
    # RollyPay and Platega are offered as groups of sub-methods now (*_group).
    assert callbacks[0].startswith('topup_amount|rollypay_group|')
    assert callbacks[1].startswith('topup_amount|platega_group|')
    assert callbacks[2].startswith('topup_amount|stars|')
    assert callbacks[-1] == 'menu_balance'


def test_custom_sort_order_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        pm_service,
        '_method_sort_orders',
        {'telegram_stars': 0, 'platega': 5, 'rollypay': 10},
    )

    rows = [
        [InlineKeyboardButton(text='Rollypay', callback_data='topup_rollypay')],
        [InlineKeyboardButton(text='Platega', callback_data='topup_platega')],
        [InlineKeyboardButton(text='Stars', callback_data='topup_stars')],
        [InlineKeyboardButton(text='Back', callback_data='menu_balance')],
    ]

    _apply_payment_name_overrides(rows)
    callbacks = [r[0].callback_data for r in rows]

    assert callbacks == ['topup_stars', 'topup_platega', 'topup_rollypay', 'menu_balance']
