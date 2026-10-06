"""Отметка «reply-клавиатура Invoxy уже установлена у пользователя на этом языке».

Telegram хранит reply-клавиатуру на стороне клиента, поэтому достаточно отправить её один раз
на язык. Без Redis отметку поставить нельзя — тогда клавиатуру не шлём (иначе она приходила бы
с каждым апдейтом).
"""

from __future__ import annotations

from app.keyboards.invoxy_reply import normalize_language
from app.utils.cache import cache


MARKER_TTL_SECONDS = 90 * 24 * 3600


def marker_key(telegram_id: int, language: str | None) -> str:
    return f'invoxy:rk:{telegram_id}:{normalize_language(language)}'


async def claim_marker(telegram_id: int, language: str | None) -> bool:
    """True, если отметки не было и мы её только что поставили (клавиатуру надо отправить)."""
    if not getattr(cache, '_connected', False):
        return False
    try:
        return bool(await cache.setnx(marker_key(telegram_id, language), 1, expire=MARKER_TTL_SECONDS))
    except Exception:
        return False


async def set_marker(telegram_id: int, language: str | None) -> None:
    if not getattr(cache, '_connected', False):
        return
    try:
        await cache.set(marker_key(telegram_id, language), 1, expire=MARKER_TTL_SECONDS)
    except Exception:
        return
