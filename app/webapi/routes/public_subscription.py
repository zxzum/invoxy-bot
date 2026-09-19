"""Read-only public data needed by the subscription page."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.dependencies import get_cabinet_db
from app.cabinet.ip_utils import get_client_ip
from app.database.models import Subscription
from app.utils.cache import RateLimitCache


logger = structlog.get_logger(__name__)
router = APIRouter(tags=['public-subscription'])
security = HTTPBearer(auto_error=False)

TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
RATE_LIMIT_ACTION = 'public_whitelist_quota'
APP_CONFIG_RATE_LIMIT_ACTION = 'public_subscription_app_config'
RATE_LIMIT_WINDOW_SECONDS = 60
IP_RATE_LIMIT = 60
SUBJECT_RATE_LIMIT = 30
APP_CONFIG_IP_RATE_LIMIT = 30
BYTES_PER_GB = 1024**3


def _headers() -> dict[str, str]:
    return {
        'Cache-Control': 'private, no-store',
        'Vary': 'Origin, Authorization',
        'X-Content-Type-Options': 'nosniff',
    }


def _json(payload: dict, code: int = status.HTTP_200_OK) -> JSONResponse:
    return JSONResponse(payload, status_code=code, headers=_headers())


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {'status': 'unauthorized'},
        status_code=status.HTTP_401_UNAUTHORIZED,
        headers={**_headers(), 'WWW-Authenticate': 'Bearer'},
    )


def _token_subject(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


async def _is_rate_limited(request: Request, token: str, action: str) -> bool:
    if await RateLimitCache.is_ip_rate_limited(
        get_client_ip(request),
        action,
        IP_RATE_LIMIT if token else APP_CONFIG_IP_RATE_LIMIT,
        RATE_LIMIT_WINDOW_SECONDS,
        fail_closed=True,
    ):
        return True
    return bool(
        token
        and await RateLimitCache.is_subject_rate_limited(
            _token_subject(token),
            action,
            SUBJECT_RATE_LIMIT,
            RATE_LIMIT_WINDOW_SECONDS,
            fail_closed=True,
        )
    )


async def _load_public_app_config() -> dict[str, Any] | None:
    from app.cabinet.routes.subscription_modules.status import _load_app_config_async

    return await _load_app_config_async()


@router.post('/v1/whitelist/quota')
async def get_public_whitelist_quota(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_cabinet_db),
) -> JSONResponse:
    """Return only the LTE quota belonging to the subscription URL token."""
    token = credentials.credentials if credentials else ''
    if not TOKEN_RE.fullmatch(token):
        return _unauthorized()

    if await _is_rate_limited(request, token, RATE_LIMIT_ACTION):
        return _json({'status': 'rate_limited'}, status.HTTP_429_TOO_MANY_REQUESTS)

    try:
        result = await db.execute(
            select(Subscription)
            .where(Subscription.subscription_url.is_not(None))
            .where(Subscription.subscription_url.endswith(f'/{token}', autoescape=True))
            .order_by(Subscription.updated_at.desc())
            .limit(1)
        )
        subscription = result.scalars().first()
    except Exception as error:  # pragma: no cover - database/runtime failure
        logger.warning('Public whitelist quota lookup failed', error=type(error).__name__)
        return _json({'status': 'unavailable'}, status.HTTP_503_SERVICE_UNAVAILABLE)

    limit_gb = int(getattr(subscription, 'whitelist_traffic_limit_gb', 0) or 0) if subscription else 0
    if not subscription or limit_gb <= 0:
        return _json({'status': 'not_found'})

    limit_bytes = limit_gb * BYTES_PER_GB
    used_bytes = max(0, int(getattr(subscription, 'whitelist_traffic_used_bytes', 0) or 0))
    remaining_bytes = max(0, limit_bytes - used_bytes)
    percent = min(100.0, round((used_bytes / limit_bytes) * 100, 2))

    return _json(
        {
            'status': 'ready',
            'usedBytes': used_bytes,
            'limitBytes': limit_bytes,
            'remainingBytes': remaining_bytes,
            'percent': percent,
            'asOf': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        }
    )


@router.get('/v1/whitelist/app-config')
async def get_public_app_config(request: Request) -> JSONResponse:
    """Return the read-only app template configured in RemnaWave."""
    if await _is_rate_limited(request, '', APP_CONFIG_RATE_LIMIT_ACTION):
        return _json({'status': 'rate_limited'}, status.HTTP_429_TOO_MANY_REQUESTS)

    try:
        config = await _load_public_app_config()
    except Exception as error:  # pragma: no cover - panel/runtime failure
        logger.warning('Public app config lookup failed', error=type(error).__name__)
        return _json({'status': 'unavailable'}, status.HTTP_503_SERVICE_UNAVAILABLE)

    platforms = config.get('platforms') if isinstance(config, dict) else None
    if not isinstance(platforms, dict) or not platforms:
        return _json({'status': 'not_found'})

    return _json(
        {
            'status': 'ready',
            'platforms': platforms,
            'svgLibrary': config.get('svgLibrary', {}),
        }
    )
