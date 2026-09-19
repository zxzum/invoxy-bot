"""Per-screen banner resolver (INVOXY).

Centralizes banner filesystem paths so core handlers only name a screen
("main", "subscription", "referral", "paid_successful"). Text/presentation
logic stays in the handlers — this module only resolves media.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import structlog
from aiogram.types import FSInputFile

from app.config import settings
from app.utils.message_patch import _prepare_logo_for_send


logger = structlog.get_logger(__name__)

ScreenBannerKind = Literal['main', 'subscription', 'referral', 'paid_successful']

SCREEN_BANNERS: dict[str, str] = {
    'main': 'main.png',
    'subscription': 'sub-banner.png',
    'referral': 'ref.png',
    'paid_successful': 'paid_successful.png',
}

_BANNERS_DIR = Path(__file__).resolve().parents[2] / 'assets' / 'banners'

_file_id_cache: dict[str, str] = {}


def get_screen_banner(kind: str):
    """Return cached file_id or FSInputFile for a screen banner, else None."""
    filename = SCREEN_BANNERS.get(kind)
    if filename is None:
        return None
    if not settings.ENABLE_LOGO_MODE:
        return None
    if kind in _file_id_cache:
        return _file_id_cache[kind]
    path = _BANNERS_DIR / filename
    if not path.is_file():
        logger.warning('Screen banner missing — falling back to default media', kind=kind, path=str(path))
        return None
    return FSInputFile(_prepare_logo_for_send(path))


def cache_screen_banner_file_id(kind: str, file_id: str | None) -> None:
    """Remember Telegram file_id after a successful photo send."""
    if file_id:
        _file_id_cache[kind] = file_id
