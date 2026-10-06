"""Источник-проверки подключения Invoxy reply-меню (setup_bot слишком тяжёл для запуска в тесте)."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _lines(path: str, predicate) -> list[int]:
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    return sorted(node.lineno for node in ast.walk(tree) if predicate(node))


def _call_text(node: ast.AST) -> str:
    return ast.unparse(node) if isinstance(node, ast.Call) else ''


def test_reply_menu_handlers_registered_before_start_handlers() -> None:
    reply = _lines('app/bot.py', lambda n: _call_text(n).startswith('invoxy_reply_menu.register_handlers('))
    start = _lines('app/bot.py', lambda n: _call_text(n).startswith('start.register_handlers('))
    assert reply and start
    assert reply[0] < start[0]


def test_keyboard_middleware_is_innermost_on_message_and_callback_query() -> None:
    inner = _lines('app/bot.py', lambda n: _call_text(n) == 'SubscriptionStatusMiddleware()')
    mine = _lines('app/bot.py', lambda n: _call_text(n) == 'InvoxyReplyKeyboardMiddleware()')
    assert len(mine) == 2
    assert min(mine) > min(inner)
    text = (ROOT / 'app/bot.py').read_text(encoding='utf-8')
    assert 'dp.message.middleware(InvoxyReplyKeyboardMiddleware())' in text
    assert 'dp.callback_query.middleware(InvoxyReplyKeyboardMiddleware())' in text


def test_bot_profile_is_configured_at_startup_after_the_menu_button() -> None:
    menu_button = _lines('main.py', lambda n: _call_text(n) == 'configure_chat_menu_button(bot)')
    profile = _lines('main.py', lambda n: _call_text(n) == 'configure_bot_profile(bot)')
    assert menu_button and profile
    assert profile[0] > menu_button[0]
