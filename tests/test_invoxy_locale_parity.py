"""INVOXY_* bot-UX copy ships in ru and en only (ru is the default).

ua/fa/zh deliberately do not carry these keys; ``test_locale_integrity`` exempts the
``INVOXY_`` prefix for them. These tests keep ru/en in lockstep.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest


APP_DIR = Path(__file__).resolve().parents[1] / 'app'
LOCALE_DIR = APP_DIR / 'localization' / 'locales'
PLACEHOLDER_RE = re.compile(r'\{[^}]*\}')
PREFIX = 'INVOXY_'


@pytest.fixture(scope='module')
def locales() -> dict[str, dict[str, str]]:
    return {lang: json.loads((LOCALE_DIR / f'{lang}.json').read_text(encoding='utf-8')) for lang in ('ru', 'en')}


def _invoxy_keys(locale: dict[str, str]) -> set[str]:
    return {key for key in locale if key.startswith(PREFIX)}


def test_invoxy_keys_exist_in_ru_and_en(locales) -> None:
    ru_keys = _invoxy_keys(locales['ru'])
    assert ru_keys, 'no INVOXY_* keys found'
    assert ru_keys == _invoxy_keys(locales['en'])


def test_invoxy_keys_are_not_added_to_other_languages() -> None:
    for lang in ('ua', 'fa', 'zh'):
        data = json.loads((LOCALE_DIR / f'{lang}.json').read_text(encoding='utf-8'))
        assert not _invoxy_keys(data), f'{lang}.json must not carry invented INVOXY_* copy'


def test_invoxy_placeholders_match(locales) -> None:
    problems = []
    for key in _invoxy_keys(locales['ru']):
        ru_ph = sorted(PLACEHOLDER_RE.findall(locales['ru'][key]))
        en_ph = sorted(PLACEHOLDER_RE.findall(locales['en'][key]))
        if ru_ph != en_ph:
            problems.append(f'{key}: ru={ru_ph} en={en_ph}')
    assert not problems, '\n'.join(problems)


def test_invoxy_values_are_non_empty_strings(locales) -> None:
    for lang, data in locales.items():
        for key in _invoxy_keys(data):
            assert isinstance(data[key], str) and data[key].strip(), f'{lang}:{key} is empty'


def test_keys_used_in_code_exist(locales) -> None:
    used: set[str] = set()
    for path in APP_DIR.rglob('*.py'):
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == 't'
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value.startswith(PREFIX)
            ):
                used.add(node.args[0].value)
    assert used - set(locales['ru']) == set()
    assert used - set(locales['en']) == set()


def test_ru_en_reply_button_labels_differ(locales) -> None:
    for key in (
        'INVOXY_BTN_TARIFF',
        'INVOXY_BTN_RENEW',
        'INVOXY_BTN_CABINET',
        'INVOXY_BTN_SITE',
        'INVOXY_BTN_SUPPORT',
        'INVOXY_BTN_LANGUAGE',
    ):
        assert locales['ru'][key] != locales['en'][key], key
