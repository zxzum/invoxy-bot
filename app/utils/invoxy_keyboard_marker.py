"""Отметка снятия старой reply-клавиатуры; отдельный ключ от прежней установки."""

from __future__ import annotations

from app.keyboards.invoxy_reply import normalize_language
from app.utils.cache import cache


MARKER_TTL_SECONDS = 90 * 24 * 3600


def marker_key(telegram_id: int, language: str | None) -> str:
    return f'invoxy:rk-removed:{telegram_id}:{normalize_language(language)}'


async def claim_marker(telegram_id: int, language: str | None) -> bool:
    """True, если этому пользователю ещё нужно отправить ReplyKeyboardRemove."""
    if not getattr(cache, '_connected', False):
        return False
    try:
        return bool(await cache.setnx(marker_key(telegram_id, language), 1, expire=MARKER_TTL_SECONDS))
    except Exception:
        return False


async def release_marker(telegram_id: int, language: str | None) -> None:
    if not getattr(cache, '_connected', False):
        return
    try:
        await cache.delete(marker_key(telegram_id, language))
    except Exception:
        return
