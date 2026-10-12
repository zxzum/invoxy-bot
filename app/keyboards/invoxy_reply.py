"""Языки и текстовые алиасы старой клавиатуры Invoxy."""

from __future__ import annotations

from app.localization.texts import get_texts


SUPPORTED_LANGUAGES: tuple[str, ...] = ('ru', 'en')

REPLY_ACTIONS: tuple[str, ...] = ('tariff', 'renew', 'cabinet', 'site', 'support', 'language')

_LABELS: dict[str, tuple[str, str]] = {
    'tariff': ('INVOXY_BTN_TARIFF', 'Мой тариф'),
    'renew': ('INVOXY_BTN_RENEW', 'Продлить'),
    'cabinet': ('INVOXY_BTN_CABINET', 'Кабинет'),
    'site': ('INVOXY_BTN_SITE', 'Сайт'),
    'support': ('INVOXY_BTN_SUPPORT', 'Поддержка'),
    'language': ('INVOXY_BTN_LANGUAGE', 'Язык'),
}


def normalize_language(code: str | None) -> str:
    """Язык интерфейса бота: 'ru' или 'en'; всё остальное — русский по умолчанию."""
    normalized = (code or '').strip().lower().split('-')[0]
    return normalized if normalized in SUPPORTED_LANGUAGES else 'ru'


def action_label(action: str, language: str | None) -> str:
    key, default = _LABELS[action]
    return get_texts(normalize_language(language)).t(key, default)


def resolve_reply_action(text: str | None) -> str | None:
    """Действие по тексту кнопки на любом языке (клавиатура могла остаться со старым языком)."""
    if not text:
        return None
    clean = text.strip()
    for action in REPLY_ACTIONS:
        if any(action_label(action, lang) == clean for lang in SUPPORTED_LANGUAGES):
            return action
    return None
