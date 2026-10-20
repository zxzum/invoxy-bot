"""Contract for the Invoxy VPN client (see docs/INVOXY_APP_INTEGRATION.md)."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.utils.device_ownership import verify_hwid_belongs_to_user
from app.database.crud.user import get_user_by_id
from app.database.models import AppDevice, User, UserStatus
from app.services import app_analytics_service as analytics, app_client_service as svc
from app.utils.cache import RateLimitCache

from ..dependencies import get_cabinet_db, get_current_cabinet_user, get_optional_cabinet_user, require_permission
from ..ip_utils import get_client_ip
from ..schemas.auth import AuthResponse
from .auth import _create_auth_response, _store_refresh_token
from .subscription_modules.devices import delete_device as _panel_delete_device, get_devices as _panel_get_devices


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/app', tags=['Invoxy App'])
admin_router = APIRouter(prefix='/admin/app', tags=['Admin Invoxy App'])


class LoginStartRequest(BaseModel):
    device_id: str = Field(..., min_length=16, max_length=128)
    platform: str = Field(..., max_length=16)
    app_version: str = Field('', max_length=32)
    code_challenge: str = Field(..., min_length=43, max_length=43)


class LoginStartResponse(BaseModel):
    request_id: str
    bot_url: str | None
    web_url: str
    expires_in: int


class LoginExchangeRequest(BaseModel):
    request_id: str = Field(..., max_length=64)
    code_verifier: str = Field(..., min_length=43, max_length=128)
    device_id: str = Field(..., min_length=16, max_length=128)


class PackageList(BaseModel):
    android_packages: list[str] = Field(default_factory=list, max_length=500)


class RestrictedRouting(PackageList):
    domains: list[str] = Field(default_factory=list, max_length=500)


class SubscriptionDomains(BaseModel):
    domains: list[str] = Field(default_factory=list, max_length=50)


class RoutingUpdate(BaseModel):
    normal: PackageList = Field(default_factory=PackageList)
    restricted: RestrictedRouting = Field(default_factory=RestrictedRouting)


async def _limit(request: Request, action: str, limit: int) -> None:
    if await RateLimitCache.is_ip_rate_limited(get_client_ip(request), action, limit, 60, fail_closed=True):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, 'Too many requests', headers={'Retry-After': '60'})


@router.get('/config')
async def get_app_config(request: Request, db: AsyncSession = Depends(get_cabinet_db)) -> dict[str, Any]:
    await _limit(request, 'app_config', 30)
    return {
        'links': svc.public_links(),
        'routing': await svc.get_routing(db),
        'subscription_domains': await svc.get_subscription_domains(db),
        'service': svc.service_health(),
    }


def _panel_unavailable() -> HTTPException:
    """The panel behind the service did not answer: the app shows its maintenance notice, the VPN itself is unaffected."""
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE, {'code': svc.PANEL_UNAVAILABLE_CODE, 'message': svc.SERVICE_NOTE}
    )


@router.post('/login', response_model=LoginStartResponse)
async def start_app_login(body: LoginStartRequest, request: Request) -> LoginStartResponse:
    await _limit(request, 'app_login_start', 10)
    try:
        request_id = await svc.start_login(
            device_id=body.device_id,
            platform=body.platform,
            app_version=body.app_version,
            code_challenge=body.code_challenge,
        )
    except svc.AppLoginError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, error.code) from error
    except RuntimeError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'Service temporarily unavailable') from error
    return LoginStartResponse(request_id=request_id, expires_in=svc.LOGIN_TTL_SECONDS, **svc.login_urls(request_id))


@router.post('/login/exchange', response_model=AuthResponse)
async def exchange_app_login(
    body: LoginExchangeRequest,
    request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    await _limit(request, 'app_login_exchange', 60)
    try:
        user_id = await svc.exchange_login(
            request_id=body.request_id, code_verifier=body.code_verifier, device_id=body.device_id
        )
    except svc.AppLoginError as error:
        code = status.HTTP_403_FORBIDDEN if error.code == 'device_mismatch' else status.HTTP_410_GONE
        raise HTTPException(code, error.code) from error
    if user_id is None:
        return Response(status_code=status.HTTP_202_ACCEPTED)

    user = await get_user_by_id(db, user_id)
    if not user or user.status != UserStatus.ACTIVE.value:
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'account_inactive')
    user.cabinet_last_login = datetime.now(UTC)
    await db.commit()
    auth = await _create_auth_response(user, db)
    await _store_refresh_token(db, user.id, auth.refresh_token, device_info=svc.app_session_tag(body.device_id))
    await svc.clear_revocation(db, body.device_id)
    logger.info('Invoxy app login completed', user_id=user.id)
    return auth


@router.get('/login/{request_id}')
async def get_app_login(request_id: str, user: User = Depends(get_current_cabinet_user)) -> dict[str, Any]:
    pending = await svc.get_pending_login(request_id)
    if pending is None:
        raise HTTPException(status.HTTP_410_GONE, 'expired')
    return pending


@router.post('/login/{request_id}/{decision}')
async def decide_app_login(
    request_id: str, decision: str, user: User = Depends(get_current_cabinet_user)
) -> dict[str, Any]:
    if decision not in {'confirm', 'deny'}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Not found')
    if not await svc.decide_login(request_id, user_id=user.id, approve=decision == 'confirm'):
        raise HTTPException(status.HTTP_410_GONE, 'expired')
    return {'status': 'ok', 'return_url': f'{svc.APP_SCHEME}://login'}


@router.get('/state')
async def get_app_state(
    user: User = Depends(get_current_cabinet_user), db: AsyncSession = Depends(get_cabinet_db)
) -> dict[str, Any]:
    return await svc.build_state(db, user)


@router.post('/devices', status_code=status.HTTP_204_NO_CONTENT)
async def report_app_device(
    payload: dict[str, Any],
    request: Request,
    user: User | None = Depends(get_optional_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> Response:
    await _limit(request, 'app_device_report', 20)
    try:
        ip = None
        try:
            ip = get_client_ip(request)
        except HTTPException:
            ip = None
        device = await svc.report_device(db, payload, user, ip=ip)
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'invalid_device') from error
    if user is not None and device.revoked_at is not None:
        # An administrator signed this installation out: the app learns it from this answer and goes to sign-in.
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'device_revoked')
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post('/events', status_code=status.HTTP_204_NO_CONTENT)
async def report_app_events(
    request: Request, background: BackgroundTasks, db: AsyncSession = Depends(get_cabinet_db)
) -> Response:
    """Anonymous usage events and the client's own error reports (see ``app_analytics_service``).

    Open to any installation (no account needed: a device that is not signed in has errors too), so it is rate limited by
    address and the body is small. An error that is new, or spikes, reaches the admin chat at once.
    """
    await _limit(request, 'app_events', 40)
    declared = request.headers.get('content-length')
    if declared and declared.isdigit() and int(declared) > analytics.MAX_BODY_BYTES:
        raise HTTPException(413, 'too_large')
    raw = await request.body()
    if len(raw) > analytics.MAX_BODY_BYTES:
        raise HTTPException(413, 'too_large')
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'invalid_events') from error
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'invalid_events')
    try:
        result = await analytics.ingest(db, payload)
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'invalid_device') from error
    if result.alerts:
        background.add_task(analytics.notify_admins, result.alerts)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get('/devices/list')
async def list_my_devices(
    request: Request,
    subscription_id: int | None = Query(None),
    device_id: str | None = Header(None, alias='X-Invoxy-Device', max_length=128),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Devices of the subscription; the one that asks is marked ``is_current``."""
    await _limit(request, 'app_device_list', 60)
    raw = await _panel_get_devices(subscription_id=subscription_id, user=user, db=db)
    if svc.panel_degraded(raw):
        raise _panel_unavailable()
    return svc.format_app_devices(raw, current_device_id=device_id)


@router.delete('/devices/{hwid}', status_code=status.HTTP_204_NO_CONTENT)
async def remove_my_device(
    hwid: str,
    request: Request,
    subscription_id: int | None = Query(None),
    device_id: str | None = Header(None, alias='X-Invoxy-Device', max_length=128),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> Response:
    """Frees a device slot. The server decides what is allowed, whatever the client sends:

    - the asking device cannot remove itself (the user would lose access);
    - the asking device must still be on the account, unless the account is at its limit and the
      device needs a slot (two devices cannot remove each other: removals are serialized);
    - the target must belong to the user.
    """
    await _limit(request, 'app_device_remove', 20)
    target = (hwid or '').strip()
    try:
        svc.check_device_removal(hwid=target, current_device_id=device_id)
    except svc.DeviceRemovalError as error:
        if error.code == 'current_device':
            logger.warning('app_device_removal_blocked', user_id=user.id, reason=error.code)
        raise HTTPException(
            status.HTTP_409_CONFLICT if error.code == 'current_device' else status.HTTP_400_BAD_REQUEST, error.code
        ) from error

    if not await svc.acquire_device_lock(user.id):
        raise HTTPException(status.HTTP_409_CONFLICT, 'busy')
    try:
        listed = await _panel_get_devices(subscription_id=subscription_id, user=user, db=db)
        if svc.panel_degraded(listed):
            raise _panel_unavailable()
        if not svc.device_may_free_a_slot(listed, device_id):
            logger.warning('app_device_removal_blocked', user_id=user.id, reason='current_device_not_registered')
            raise HTTPException(status.HTTP_409_CONFLICT, 'current_device_not_registered')
        if not await verify_hwid_belongs_to_user(user, target):
            raise HTTPException(status.HTTP_404_NOT_FOUND, 'device_not_found')
        await _panel_delete_device(hwid=target, subscription_id=subscription_id, user=user, db=db)
    finally:
        await svc.release_device_lock(user.id)
    logger.info('app_device_removed', user_id=user.id, device=svc.short_hash(target), by=svc.short_hash(device_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Admin -------------------------------------------------------------------


@admin_router.get('/stats')
async def get_app_stats(
    days: int = Query(7, ge=1, le=30),
    admin: User = Depends(require_permission('stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    return await svc.device_stats(db, days=days)


@admin_router.get('/analytics')
async def get_app_analytics(
    days: int = Query(7, ge=1, le=90),
    admin: User = Depends(require_permission('stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """How the app is used and what breaks: active devices per day, connect results, screens, updates, top errors."""
    return await analytics.summary(db, days=days)


@admin_router.get('/devices')
async def list_app_devices(
    search: str = Query('', max_length=64),
    platform: str = Query('', max_length=16),
    failing: bool = False,
    days: int = Query(30, ge=1, le=90),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    admin: User = Depends(require_permission('stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    query = (
        select(AppDevice, User)
        .outerjoin(User, AppDevice.user_id == User.id)
        .where(AppDevice.last_seen_at >= datetime.now(UTC) - timedelta(days=days))
    )
    if platform:
        query = query.where(AppDevice.platform == platform)
    if failing:
        query = query.where(AppDevice.last_connect_ok.is_(False))
    term = search.strip().lstrip('@')
    if term:
        conditions = [User.username.ilike(f'%{term}%'), User.email.ilike(f'%{term}%')]
        if term.isdigit():
            conditions += [User.id == int(term), User.telegram_id == int(term)]
        query = query.where(or_(*conditions))
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = (await db.execute(query.order_by(AppDevice.last_seen_at.desc()).limit(limit).offset(offset))).all()
    return {
        'total': total,
        'items': [
            {
                'id': device.id,
                'platform': device.platform,
                'os_version': device.os_version,
                'device_model': device.device_model,
                'app_version': device.app_version,
                'last_ip': device.last_ip,
                'first_seen_at': device.first_seen_at.isoformat() if device.first_seen_at else None,
                'revoked_at': device.revoked_at.isoformat() if device.revoked_at else None,
                'last_seen_at': device.last_seen_at.isoformat() if device.last_seen_at else None,
                'last_connect_ok': device.last_connect_ok,
                'last_error_code': device.last_error_code,
                'user': (
                    {'id': owner.id, 'username': owner.username, 'telegram_id': owner.telegram_id} if owner else None
                ),
            }
            for device, owner in rows
        ],
    }


@admin_router.post('/devices/{device_id}/revoke')
async def revoke_app_device(
    device_id: int,
    admin: User = Depends(require_permission('users:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Sign an installation out of the account it is linked to (its refresh tokens are revoked)."""
    device = await db.get(AppDevice, device_id)
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'device_not_found')
    revoked = await svc.revoke_device(db, device)
    logger.info('app_device_revoked', admin_id=admin.id, device_id=device.id, user_id=device.user_id, tokens=revoked)
    return {'revoked_tokens': revoked, 'revoked_at': device.revoked_at.isoformat() if device.revoked_at else None}


@admin_router.get('/routing')
async def get_app_routing(
    admin: User = Depends(require_permission('settings:read')), db: AsyncSession = Depends(get_cabinet_db)
) -> dict[str, Any]:
    return await svc.get_routing(db)


@admin_router.put('/routing')
async def update_app_routing(
    body: RoutingUpdate,
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    return await svc.save_routing(db, body.model_dump())


@admin_router.get('/subscription-domains')
async def get_subscription_domains(
    admin: User = Depends(require_permission('settings:read')), db: AsyncSession = Depends(get_cabinet_db)
) -> dict[str, Any]:
    return {'domains': await svc.get_subscription_domains(db), 'defaults': svc.DEFAULT_SUBSCRIPTION_DOMAINS}


@admin_router.put('/subscription-domains')
async def update_subscription_domains(
    body: SubscriptionDomains,
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    domains = await svc.save_subscription_domains(db, body.domains)
    logger.info('app_subscription_domains_changed', admin_id=admin.id, count=len(domains))
    return {'domains': domains, 'defaults': svc.DEFAULT_SUBSCRIPTION_DOMAINS}
