"""Ссылки Invoxy для бота: публичный сайт и страницы кабинета (Mini App).

Mini App открывается только inline-кнопкой web_app по https — Telegram не отдаёт
initData кнопкам reply-клавиатуры, а кабинет входит именно по initData.
"""

from __future__ import annotations

from app.config import settings
from app.utils.miniapp_buttons import build_cabinet_url


def website_url() -> str | None:
    """Публичный сайт сервиса. Намеренно НЕ подменяется CABINET_URL."""
    value = (settings.SERVICE_WEBSITE_URL or '').strip()
    if value.lower().startswith(('https://', 'http://')):
        return value
    return None


def _cabinet(path: str) -> str | None:
    url = build_cabinet_url(path)
    return url if url.lower().startswith('https://') else None


def cabinet_root_url() -> str | None:
    return _cabinet('/')


def renew_url(subscription_id: int) -> str | None:
    return _cabinet(f'/subscriptions/{int(subscription_id)}/renew')


def purchase_url() -> str | None:
    return _cabinet('/subscription/purchase')
