"""Pair code authentication service.

Allows cabinet users to generate a short 6-character code (5 min TTL)
to easily authenticate the Invoxy VPN mobile/desktop app without links or QR codes.
"""

import secrets
from datetime import UTC, datetime
from typing import Any

import structlog

from app.utils.cache import cache, cache_key


logger = structlog.get_logger(__name__)

PAIR_CODE_TTL = 300  # 5 minutes
PAIR_CODE_PREFIX = 'pair_code'
PAIR_CODE_ALPHABET = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'  # 32 unambiguous chars
PAIR_CODE_LENGTH = 6


def _generate_code(length: int = PAIR_CODE_LENGTH) -> str:
    return ''.join(secrets.choice(PAIR_CODE_ALPHABET) for _ in range(length))


async def create_pair_code(user_id: int) -> tuple[str, int]:
    """Generate a 6-character pair code and store it in Redis for user_id.

    Returns (code, ttl_seconds).
    """
    for _ in range(3):
        code = _generate_code()
        key = cache_key(PAIR_CODE_PREFIX, code)
        exists = await cache.get(key)
        if exists is None:
            value: dict[str, Any] = {
                'user_id': user_id,
                'created_at': datetime.now(UTC).isoformat(),
            }
            stored = await cache.set(key, value, expire=PAIR_CODE_TTL)
            if not stored:
                logger.error('Failed to store pair code in Redis', user_id=user_id)
                raise RuntimeError('Failed to store pair code')
            logger.info('Pair code generated for user', user_id=user_id, code_prefix=code[:2])
            return code, PAIR_CODE_TTL

    raise RuntimeError('Could not generate unique pair code')


async def consume_pair_code(code: str) -> dict[str, Any] | None:
    """Atomically consume (get and delete) a pair code.

    Returns the payload containing user_id or None if invalid/expired.
    """
    cleaned = code.strip().upper()
    if len(cleaned) != PAIR_CODE_LENGTH:
        return None

    key = cache_key(PAIR_CODE_PREFIX, cleaned)
    data = await cache.getdel(key)
    if not data or not isinstance(data, dict):
        logger.warning('Pair code not found or expired', code_prefix=cleaned[:2])
        return None

    user_id = data.get('user_id')
    if not user_id:
        logger.warning('Pair code has no user_id', code_prefix=cleaned[:2])
        return None

    logger.info('Pair code consumed successfully', user_id=user_id, code_prefix=cleaned[:2])
    return data
