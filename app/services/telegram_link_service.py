"""Telegram link deep-link token service.

Allows cabinet frontend to link a Telegram account via bot deep link
when Telegram Login Widget or OIDC is blocked or unavailable in browser.

Flow:
1. Frontend requests token: POST /cabinet/auth/account/link/telegram/deeplink/request (JWT auth)
2. User clicks t.me/bot?start=linktg_TOKEN
3. Bot prompts user: "Confirm linking Telegram to account <email/ID>"
4. User clicks "Confirm" in bot:
   - If Telegram user is not linked to another account: links directly, sets status='linked'
   - If Telegram user already has an account: creates merge token, sets status='merge_required'
5. Frontend polls: POST /cabinet/auth/account/link/telegram/deeplink/poll -> gets status
"""

import secrets
from datetime import UTC, datetime
from typing import Any

import structlog

from app.utils.cache import cache, cache_key


logger = structlog.get_logger(__name__)

TG_LINK_TOKEN_TTL = 300  # 5 minutes
TG_LINK_COMPLETED_TTL = 120  # 2 minutes poll window after completion
TG_LINK_TOKEN_MIN_LENGTH = 16
TG_LINK_PREFIX = 'tg_link'


async def create_telegram_link_token(user_id: int) -> str:
    """Generate a Telegram linking token and store it in Redis (pending state).

    Returns the raw token string (URL-safe, 24 bytes of entropy).
    """
    token = secrets.token_urlsafe(24)
    key = cache_key(TG_LINK_PREFIX, token)
    value: dict[str, Any] = {
        'status': 'pending',
        'user_id': user_id,
        'created_at': datetime.now(UTC).isoformat(),
    }
    stored = await cache.set(key, value, expire=TG_LINK_TOKEN_TTL)
    if not stored:
        logger.error('Failed to store telegram link token in Redis')
        raise RuntimeError('Failed to create telegram link token')

    logger.debug('Telegram link token created', token_prefix=token[:8], user_id=user_id)
    return token


async def get_telegram_link_token(token: str) -> dict[str, Any] | None:
    """Get telegram link token data without mutating it."""
    key = cache_key(TG_LINK_PREFIX, token)
    data: Any = await cache.get(key)
    if not data or not isinstance(data, dict):
        return None
    return data


async def set_telegram_link_token_status(
    token: str,
    status: str,
    merge_token: str | None = None,
    telegram_id: int | None = None,
) -> bool:
    """Update status of telegram link token (e.g. 'linked' or 'merge_required')."""
    key = cache_key(TG_LINK_PREFIX, token)
    data: Any = await cache.get(key)
    if not data or not isinstance(data, dict):
        logger.warning('Telegram link token not found for status update', token_prefix=token[:8])
        return False

    data['status'] = status
    if merge_token:
        data['merge_token'] = merge_token
    if telegram_id:
        data['telegram_id'] = telegram_id
    data['updated_at'] = datetime.now(UTC).isoformat()

    await cache.set(key, data, expire=TG_LINK_COMPLETED_TTL)
    logger.info(
        'Telegram link token status updated',
        token_prefix=token[:8],
        status=status,
        has_merge=bool(merge_token),
    )
    return True
