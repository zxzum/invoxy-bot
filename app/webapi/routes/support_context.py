"""Read-only client cards for the Invoxy Telegram support relay."""

from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.ip_utils import get_client_ip
from app.database.crud.user import get_user_by_telegram_id
from app.utils.cache import RateLimitCache
from app.webapi.dependencies import get_db_session


router = APIRouter(prefix='/internal/support', tags=['support'])
security = HTTPBearer(auto_error=False)


async def require_support_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> None:
    expected = os.environ.get('SUPPORT_API_KEY', '')
    supplied = credentials.credentials if credentials else ''
    if not expected or not supplied or not secrets.compare_digest(expected.encode(), supplied.encode()):
        raise HTTPException(401, 'Unauthorized', headers={'WWW-Authenticate': 'Bearer'})
    if await RateLimitCache.is_ip_rate_limited(
        get_client_ip(request), 'support_client_context', 120, 60, fail_closed=True
    ):
        raise HTTPException(429, 'Too many requests')


@router.get('/client-context')
async def get_client_context(
    response: Response,
    telegram_id: int = Query(alias='telegramId', gt=0, le=9223372036854775807),
    request_id: str = Header(alias='X-Support-Request-Id', min_length=1, max_length=64),
    _authorized: None = Depends(require_support_key),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    response.headers['Cache-Control'] = 'private, no-store'
    user = await get_user_by_telegram_id(db, telegram_id)
    if user is None:
        return {'found': False, 'client': None, 'subscriptions': []}
    return {
        'found': True,
        'client': {
            'id': str(user.id),
            'telegramId': str(user.telegram_id),
            'username': user.username or '',
            'displayName': user.full_name,
            'email': user.email or '',
            'language': user.language,
            'status': user.status,
            'balanceKopeks': user.balance_kopeks or 0,
            'createdAt': user.created_at.isoformat() if user.created_at else '',
        },
        'subscriptions': [
            {
                'id': str(subscription.id),
                'tariffName': subscription.tariff.name if subscription.tariff else 'Без тарифа',
                'status': subscription.status,
                'isTrial': subscription.is_trial,
                'expiresAt': subscription.end_date.isoformat(),
                'trafficLimitBytes': (subscription.traffic_limit_gb or 0) * 1024**3,
                'trafficUsedBytes': int((subscription.traffic_used_gb or 0) * 1024**3),
                'deviceLimit': subscription.device_limit or 0,
                'whitelistTrafficLimitBytes': (subscription.whitelist_traffic_limit_gb or 0) * 1024**3,
                'whitelistTrafficUsedBytes': subscription.whitelist_traffic_used_bytes or 0,
            }
            for subscription in user.subscriptions[:20]
        ],
    }
