"""App banners management service.

Provides in-app banners for the Invoxy mobile and desktop application.
Managed by administrators via cabinet admin panel.
"""

import json
import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.system_setting import (
    get_setting_value,
    upsert_system_setting,
)


logger = structlog.get_logger(__name__)

APP_BANNERS_SETTING_KEY = 'APP_BANNERS_JSON'


async def get_app_banners(db: AsyncSession, only_active: bool = False) -> list[dict[str, Any]]:
    """Retrieve list of app banners ordered by sort_order."""
    val = await get_setting_value(db, APP_BANNERS_SETTING_KEY)
    if not val:
        return []

    try:
        banners = json.loads(val)
        if not isinstance(banners, list):
            return []
    except Exception as e:
        logger.warning('Failed to parse APP_BANNERS_JSON', error=str(e))
        return []

    if only_active:
        banners = [b for b in banners if b.get('is_active', True)]

    # Sort by sort_order ascending
    banners.sort(key=lambda b: (b.get('sort_order', 0), b.get('title', '')))
    return banners


async def save_app_banners(db: AsyncSession, banners: list[dict[str, Any]]) -> None:
    """Save raw list of banners to system settings."""
    payload = json.dumps(banners, ensure_ascii=False)
    await upsert_system_setting(db, APP_BANNERS_SETTING_KEY, payload)


async def create_app_banner(db: AsyncSession, data: dict[str, Any]) -> dict[str, Any]:
    """Create a new app banner and persist."""
    banners = await get_app_banners(db, only_active=False)
    banner_id = data.get('id') or str(uuid.uuid4())[:8]
    new_banner = {
        'id': banner_id,
        'title': data['title'],
        'text': data.get('text', ''),
        'type': data.get('type', 'info'),  # 'info', 'promo', 'warning', 'mint'
        'action_url': data.get('action_url'),
        'icon': data.get('icon'),
        'is_active': bool(data.get('is_active', True)),
        'sort_order': int(data.get('sort_order', len(banners))),
    }
    # Avoid duplicate id
    banners = [b for b in banners if b.get('id') != banner_id]
    banners.append(new_banner)
    await save_app_banners(db, banners)
    logger.info('App banner created', banner_id=banner_id, title=new_banner['title'])
    return new_banner


async def update_app_banner(db: AsyncSession, banner_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """Update an existing app banner."""
    banners = await get_app_banners(db, only_active=False)
    found = False
    updated_banner = None
    new_banners = []
    for b in banners:
        if b.get('id') == banner_id:
            found = True
            b['title'] = data.get('title', b.get('title'))
            b['text'] = data.get('text', b.get('text'))
            b['type'] = data.get('type', b.get('type', 'info'))
            b['action_url'] = data.get('action_url', b.get('action_url'))
            b['icon'] = data.get('icon', b.get('icon'))
            if 'is_active' in data:
                b['is_active'] = bool(data['is_active'])
            if 'sort_order' in data:
                b['sort_order'] = int(data['sort_order'])
            updated_banner = b
        new_banners.append(b)

    if not found:
        return None

    await save_app_banners(db, new_banners)
    logger.info('App banner updated', banner_id=banner_id)
    return updated_banner


async def delete_app_banner(db: AsyncSession, banner_id: str) -> bool:
    """Delete an app banner by id."""
    banners = await get_app_banners(db, only_active=False)
    filtered = [b for b in banners if b.get('id') != banner_id]
    if len(filtered) == len(banners):
        return False

    await save_app_banners(db, filtered)
    logger.info('App banner deleted', banner_id=banner_id)
    return True
