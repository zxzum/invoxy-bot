"""Web auth deep-link token service.

Allows cabinet frontend to authenticate users via Telegram bot deep link
when oauth.telegram.org is blocked/unreachable.

Flow:
1. Frontend requests token: POST /cabinet/auth/deeplink/request
2. User clicks t.me/bot?start=webauth_TOKEN
3. Bot receives /start, links token to Telegram user
4. Frontend polls: POST /cabinet/auth/deeplink/poll -> gets JWT tokens
"""

import secrets
from datetime import UTC, datetime
from typing import Any

import structlog

from app.utils.cache import cache, cache_key


logger = structlog.get_logger(__name__)

WEB_AUTH_TOKEN_TTL = 300  # 5 minutes
WEB_AUTH_LINKED_TTL = 120  # seconds — poll window after token is linked
WEB_AUTH_TOKEN_MIN_LENGTH = 16
WEB_AUTH_PREFIX = 'web_auth'
CABINET_WS_TICKET_TTL = 45  # short-lived one-time ticket for browser WebSocket upgrades
CABINET_WS_TICKET_PREFIX = 'cabinet_ws_ticket'


async def create_web_auth_token() -> str:
    """Generate a web auth token and store it in Redis (pending state).

    Returns the raw token string (URL-safe, 24 bytes of entropy).
    """
    token = secrets.token_urlsafe(24)
    key = cache_key(WEB_AUTH_PREFIX, token)
    value: dict[str, Any] = {
        'status': 'pending',
        'created_at': datetime.now(UTC).isoformat(),
    }
    stored = await cache.set(key, value, expire=WEB_AUTH_TOKEN_TTL)
    if not stored:
        logger.error('Failed to store web auth token in Redis')
        raise RuntimeError('Failed to create web auth token')

    logger.debug('Web auth token created', token_prefix=token[:8])
    return token


async def link_web_auth_token(token: str, telegram_id: int, user_id: int) -> bool:
    """Link a web auth token to a Telegram user (called by bot on /start).

    Atomically takes the token (GETDEL) so only one caller can win the race.
    Returns True if token was found and linked, False if expired/invalid.
    """
    key = cache_key(WEB_AUTH_PREFIX, token)
    # Atomically take the token — only one concurrent caller can succeed
    data: Any = await cache.getdel(key)

    if not data or not isinstance(data, dict):
        logger.warning('Web auth token not found or expired', token_prefix=token[:8])
        return False

    if data.get('status') != 'pending':
        logger.warning('Web auth token already used', token_prefix=token[:8])
        return False

    # Update token with user info
    data['status'] = 'linked'
    data['telegram_id'] = telegram_id
    data['user_id'] = user_id
    data['linked_at'] = datetime.now(UTC).isoformat()

    # Re-store with reduced TTL (only needs to survive the poll window)
    await cache.set(key, data, expire=WEB_AUTH_LINKED_TTL)

    logger.info('Web auth token linked', token_prefix=token[:8], telegram_id=telegram_id)
    return True


async def poll_web_auth_token(token: str) -> dict[str, Any] | None:
    """Poll for web auth token status (non-destructive).

    Returns:
        - None if token doesn't exist or is expired
        - dict with status='pending' if not yet linked
        - dict with status='linked' and user_id/telegram_id if linked
    """
    key = cache_key(WEB_AUTH_PREFIX, token)
    data: Any = await cache.get(key)

    if not data or not isinstance(data, dict):
        return None

    return data


async def consume_web_auth_token(token: str) -> dict[str, Any] | None:
    """Atomically get and delete a web auth token.

    Used after successful poll to prevent token reuse.
    Returns the token data or None.
    """
    key = cache_key(WEB_AUTH_PREFIX, token)
    data = await cache.getdel(key)
    if not data or not isinstance(data, dict):
        return None
    return data


async def create_cabinet_ws_ticket(user_id: int) -> str:
    """Create a short-lived, one-time ticket for the cabinet WebSocket."""
    ticket = secrets.token_urlsafe(24)
    value: dict[str, Any] = {
        'purpose': 'cabinet_ws',
        'user_id': user_id,
        'created_at': datetime.now(UTC).isoformat(),
    }
    stored = await cache.set(
        cache_key(CABINET_WS_TICKET_PREFIX, ticket),
        value,
        expire=CABINET_WS_TICKET_TTL,
    )
    if not stored:
        logger.error('Failed to store cabinet WebSocket ticket in Redis', user_id=user_id)
        raise RuntimeError('Failed to create cabinet WebSocket ticket')

    logger.debug('Cabinet WebSocket ticket created', ticket_prefix=ticket[:8], user_id=user_id)
    return ticket


async def consume_cabinet_ws_ticket(ticket: str) -> dict[str, Any] | None:
    """Atomically consume a cabinet WebSocket ticket and validate its binding."""
    if not ticket:
        return None

    data = await cache.getdel(cache_key(CABINET_WS_TICKET_PREFIX, ticket))
    if not isinstance(data, dict) or data.get('purpose') != 'cabinet_ws':
        return None

    try:
        user_id = int(data.get('user_id'))
    except (TypeError, ValueError):
        return None
    if user_id <= 0:
        return None

    return data
