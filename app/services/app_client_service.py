"""Invoxy VPN client: device-bound login, account state and device telemetry.

Login flow (no secret ever travels in a URL):
1. The app creates a request with ``code_challenge = base64url(sha256(verifier))``.
2. The user confirms ``request_id`` in the bot or in the signed-in cabinet.
3. The app exchanges ``request_id`` + ``verifier`` + ``device_id`` for a session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.system_setting import get_setting_value, upsert_system_setting
from app.database.models import AppDevice, AppDeviceEvent, CabinetNotification, Subscription, User
from app.utils.cache import cache, cache_key


logger = structlog.get_logger(__name__)

LOGIN_TTL_SECONDS = 300
LOGIN_PREFIX = 'app_login'
LOGIN_DECISION_PREFIX = 'app_login_decision'
EVENT_RETENTION_DAYS = 30
MAX_EVENTS_PER_REPORT = 20
APP_SCHEME = 'invoxyvpn'
ROUTING_SETTING_KEY = 'INVOXY_APP_ROUTING_JSON'

PLATFORMS = frozenset({'android', 'ios', 'macos', 'windows'})
REQUEST_ID_RE = re.compile(r'^[A-Za-z0-9_-]{32,64}$')
DEVICE_ID_RE = re.compile(r'^[A-Za-z0-9_-]{16,128}$')
CHALLENGE_RE = re.compile(r'^[A-Za-z0-9_-]{43}$')
DOMAIN_RE = re.compile(r'^(?=.{1,253}$)([a-z0-9а-яё-]{1,63}\.)+[a-z0-9а-яё-]{2,63}$')
PACKAGE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$')

# Direct-route defaults. Normal network: Russian apps bypass VPN entirely (Android).
# Restricted network: candidates only; the client applies a direct route after it
# verifies that the resource answers without VPN on the current network.
DEFAULT_ROUTING: dict[str, dict[str, list[str]]] = {
    'normal': {
        'android_packages': [
            'com.avito.android',
            'com.idamob.tinkoff.android',
            'com.vkontakte.android',
            'com.wildberries.ru',
            'ru.alfabank.mobile.android',
            'ru.dublgis.dgismobile',
            'ru.gazprombank.android.mobilebank.app',
            'ru.mail.mailapp',
            'ru.ok.android',
            'ru.oneme.app',
            'ru.ozon.app.android',
            'ru.raiffeisennews',
            'ru.rostel',
            'ru.rutube.app',
            'ru.sberbankmobile',
            'ru.vtb24.mobilebanking.android',
            'ru.yandex.searchplugin',
            'ru.yandex.taxi',
            'ru.yandex.yandexmaps',
        ],
    },
    'restricted': {
        'domains': [
            'esia.gosuslugi.ru',
            'gosuslugi.ru',
            'ozon.ru',
            'ozone.ru',
            'ozonusercontent.com',
            'userapi.com',
            'vk.com',
            'vk.ru',
            'vkuser.net',
        ],
        'android_packages': ['com.vkontakte.android', 'ru.ozon.app.android', 'ru.rostel'],
    },
}


class AppLoginError(Exception):
    """Login request is invalid for the caller; ``code`` is safe to return."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _login_key(request_id: str) -> str:
    return cache_key(LOGIN_PREFIX, request_id)


def _decision_key(request_id: str) -> str:
    return cache_key(LOGIN_DECISION_PREFIX, request_id)


def hash_device_id(device_id: str) -> str:
    """Pseudonymous, stable device key; the raw device id is never stored."""
    secret = settings.get_cabinet_jwt_secret().encode()
    return hmac.new(secret, f'invoxy-app-device:{device_id}'.encode(), hashlib.sha256).hexdigest()


def _challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip('=')


async def start_login(*, device_id: str, platform: str, app_version: str, code_challenge: str) -> str:
    if not DEVICE_ID_RE.fullmatch(device_id) or platform not in PLATFORMS or not CHALLENGE_RE.fullmatch(code_challenge):
        raise AppLoginError('invalid_request')

    request_id = secrets.token_urlsafe(24)
    stored = await cache.set(
        _login_key(request_id),
        {
            'challenge': code_challenge,
            'device': hash_device_id(device_id),
            'platform': platform,
            'app_version': app_version[:32],
            'created_at': datetime.now(UTC).isoformat(),
        },
        expire=LOGIN_TTL_SECONDS,
    )
    if not stored:
        raise RuntimeError('Failed to store app login request')
    return request_id


async def get_pending_login(request_id: str) -> dict[str, Any] | None:
    """Return the public part of a request that still awaits a decision."""
    if not REQUEST_ID_RE.fullmatch(request_id):
        return None
    data = await cache.get(_login_key(request_id))
    if not isinstance(data, dict) or await cache.exists(_decision_key(request_id)):
        return None
    return {
        'platform': data.get('platform'),
        'app_version': data.get('app_version'),
        'created_at': data.get('created_at'),
    }


async def decide_login(request_id: str, *, user_id: int, approve: bool) -> bool:
    """Bind a pending request to a user (or deny it). First decision wins."""
    if await get_pending_login(request_id) is None:
        return False
    decision = {'status': 'linked' if approve else 'denied', 'user_id': user_id}
    return await cache.setnx(_decision_key(request_id), decision, expire=LOGIN_TTL_SECONDS)


async def exchange_login(*, request_id: str, code_verifier: str, device_id: str) -> int | None:
    """Return the confirmed user id, ``None`` while pending; raise when unusable."""
    if not REQUEST_ID_RE.fullmatch(request_id) or not 43 <= len(code_verifier) <= 128:
        raise AppLoginError('expired')
    data = await cache.get(_login_key(request_id))
    if not isinstance(data, dict):
        raise AppLoginError('expired')
    if not hmac.compare_digest(str(data.get('challenge')), _challenge_for(code_verifier)) or not hmac.compare_digest(
        str(data.get('device')), hash_device_id(device_id)
    ):
        raise AppLoginError('device_mismatch')

    decision = await cache.get(_decision_key(request_id))
    if not isinstance(decision, dict):
        return None
    if decision.get('status') != 'linked':
        await cache.delete(_decision_key(request_id))
        await cache.delete(_login_key(request_id))
        raise AppLoginError('denied')

    consumed = await cache.getdel(_decision_key(request_id))
    await cache.delete(_login_key(request_id))
    if not isinstance(consumed, dict) or not consumed.get('user_id'):
        raise AppLoginError('expired')
    return int(consumed['user_id'])


def login_urls(request_id: str) -> dict[str, str | None]:
    bot_username = settings.get_bot_username()
    return {
        'bot_url': f'https://t.me/{bot_username}?start=applogin_{request_id}' if bot_username else None,
        'web_url': f'{cabinet_base_url()}/app/login?request={request_id}',
    }


def cabinet_base_url() -> str:
    return (settings.MINIAPP_CUSTOM_URL or settings.CABINET_URL or '').rstrip('/')


def return_url() -> str:
    """Public page that reopens the app; carries no parameters."""
    return f'{cabinet_base_url()}/app/return'


# --- Configuration -----------------------------------------------------------


def _clean_list(values: Any, pattern: re.Pattern[str], *, lower: bool, limit: int = 500) -> list[str]:
    if not isinstance(values, list):
        return []
    cleaned = {v.strip().rstrip('.') for v in values if isinstance(v, str)}
    if lower:
        cleaned = {v.lower() for v in cleaned}
    return sorted(v for v in cleaned if pattern.fullmatch(v))[:limit]


def normalize_routing(raw: Any) -> dict[str, dict[str, list[str]]]:
    raw = raw if isinstance(raw, dict) else {}
    normal = raw.get('normal') if isinstance(raw.get('normal'), dict) else {}
    restricted = raw.get('restricted') if isinstance(raw.get('restricted'), dict) else {}
    return {
        'normal': {'android_packages': _clean_list(normal.get('android_packages'), PACKAGE_RE, lower=False)},
        'restricted': {
            'domains': _clean_list(restricted.get('domains'), DOMAIN_RE, lower=True),
            'android_packages': _clean_list(restricted.get('android_packages'), PACKAGE_RE, lower=False),
        },
    }


def routing_version(data: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]


async def get_routing(db: AsyncSession) -> dict[str, Any]:
    raw = await get_setting_value(db, ROUTING_SETTING_KEY)
    data = DEFAULT_ROUTING
    if raw:
        try:
            data = normalize_routing(json.loads(raw))
        except (TypeError, ValueError):
            logger.warning('Invalid app routing setting; using defaults')
    return {'version': routing_version(data), **data}


async def save_routing(db: AsyncSession, raw: Any) -> dict[str, Any]:
    data = normalize_routing(raw)
    await upsert_system_setting(
        db,
        ROUTING_SETTING_KEY,
        json.dumps(data, ensure_ascii=False),
        description='Invoxy VPN app: direct routes for normal and restricted networks',
    )
    await db.commit()
    return {'version': routing_version(data), **data}


def _url_or_none(value: str | None) -> str | None:
    value = (value or '').strip()
    return value if value.startswith('https://') else None


def public_links() -> dict[str, Any]:
    base = cabinet_base_url()
    bot_username = settings.get_bot_username()
    return {
        'cabinet': f'{base}/dashboard' if base else None,
        'renew': f'{base}/subscriptions' if base else None,
        'app_page': f'{base}/app' if base else None,
        'bot': f'https://t.me/{bot_username}' if bot_username else None,
        'support': settings.get_support_contact_url(),
        'promo': _url_or_none(settings.INVOXY_APP_PROMO_URL),
        'downloads': {
            'android': _url_or_none(settings.INVOXY_APP_ANDROID_URL),
            'ios': _url_or_none(settings.INVOXY_APP_IOS_URL),
            'macos': _url_or_none(settings.INVOXY_APP_MACOS_URL),
            'windows': _url_or_none(settings.INVOXY_APP_WINDOWS_URL),
        },
    }


# --- Account state -----------------------------------------------------------

GB = 1024**3


def subscription_state(sub: Subscription) -> dict[str, Any]:
    base = cabinet_base_url()
    restricted_limit = int(getattr(sub, 'whitelist_traffic_limit_gb', 0) or 0)
    synced_at = sub.last_webhook_update_at or sub.updated_at
    return {
        'id': sub.id,
        'name': sub.tariff.name if sub.tariff else None,
        'status': sub.actual_status,
        'is_trial': bool(sub.is_trial),
        'expires_at': sub.end_date.isoformat() if sub.end_date else None,
        'traffic': {
            'used_bytes': int((sub.traffic_used_gb or 0) * GB),
            'limit_bytes': int((sub.traffic_limit_gb or 0) * GB),
        },
        'restricted_traffic': (
            {
                'used_bytes': int(sub.whitelist_traffic_used_bytes or 0),
                'limit_bytes': restricted_limit * GB,
            }
            if restricted_limit > 0
            else None
        ),
        'device_limit': sub.device_limit or 1,
        'subscription_url': settings.normalize_subscription_url(sub.subscription_url),
        'renew_url': f'{base}/subscriptions/{sub.id}/renew' if base else None,
        'synced_at': synced_at.isoformat() if synced_at else None,
    }


async def build_state(db: AsyncSession, user: User) -> dict[str, Any]:
    from app.database.crud.subscription import get_all_subscriptions_by_user_id

    subscriptions = await get_all_subscriptions_by_user_id(db, user.id)
    notifications = (
        (
            await db.execute(
                select(CabinetNotification)
                .where(CabinetNotification.user_id == user.id)
                .order_by(CabinetNotification.id.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    referral = None
    if user.referral_code:
        referral = {
            'link': settings.get_cabinet_referral_link(user.referral_code)
            or settings.get_bot_referral_link(user.referral_code),
            'bot_link': settings.get_bot_referral_link(user.referral_code),
        }
    name = ' '.join(filter(None, [user.first_name, user.last_name])).strip() or user.username or None
    return {
        'account': {'id': user.id, 'name': name, 'telegram_linked': bool(user.telegram_id)},
        'subscriptions': [subscription_state(sub) for sub in subscriptions],
        'referral': referral,
        'links': public_links(),
        'notifications': [
            {
                'id': n.id,
                'type': n.type,
                'title': n.title,
                'body': n.body,
                'category': (n.payload_json or {}).get('category') if isinstance(n.payload_json, dict) else None,
                'url': _url_or_none((n.payload_json or {}).get('url') if isinstance(n.payload_json, dict) else None),
                'created_at': n.created_at.isoformat() if n.created_at else None,
            }
            for n in notifications
        ],
        'generated_at': datetime.now(UTC).isoformat(),
    }


# --- Devices and diagnostics -------------------------------------------------

EVENT_KINDS = frozenset({'connect', 'disconnect', 'error'})
CODE_RE = re.compile(r'^[a-z0-9_.-]{1,40}$')
NETWORKS = frozenset({'wifi', 'cellular', 'ethernet', 'vpn', 'none', 'other'})


def _short(value: Any, limit: int) -> str | None:
    return str(value)[:limit] if value not in (None, '') else None


def _code(value: Any) -> str | None:
    value = str(value or '').lower()
    return value if CODE_RE.fullmatch(value) else None


async def report_device(db: AsyncSession, payload: dict[str, Any], user: User | None) -> AppDevice:
    device_id = str(payload.get('device_id') or '')
    platform = str(payload.get('platform') or '')
    if not DEVICE_ID_RE.fullmatch(device_id) or platform not in PLATFORMS:
        raise ValueError('invalid device')

    now = datetime.now(UTC)
    device_hash = hash_device_id(device_id)
    device = (await db.execute(select(AppDevice).where(AppDevice.device_hash == device_hash))).scalar_one_or_none()
    if device is None:
        device = AppDevice(device_hash=device_hash, platform=platform, first_seen_at=now)
        db.add(device)
    device.platform = platform
    device.os_version = _short(payload.get('os_version'), 64)
    device.app_version = _short(payload.get('app_version'), 32)
    device.last_seen_at = now
    if user is not None:
        device.user_id = user.id
    prefs = payload.get('notifications')
    if isinstance(prefs, dict):
        device.notification_prefs = {
            k: bool(v) for k, v in prefs.items() if k in {'renewal', 'traffic', 'service', 'promo'}
        }

    await db.flush()
    events = payload.get('events') if isinstance(payload.get('events'), list) else []
    for raw in events[:MAX_EVENTS_PER_REPORT]:
        if not isinstance(raw, dict) or raw.get('kind') not in EVENT_KINDS:
            continue
        ok = bool(raw.get('ok'))
        network = raw.get('network') if raw.get('network') in NETWORKS else None
        db.add(
            AppDeviceEvent(
                device_id=device.id,
                kind=raw['kind'],
                ok=ok,
                error_code=None if ok else _code(raw.get('error_code')),
                protocol=_code(raw.get('protocol')),
                network=network,
                restricted=bool(raw.get('restricted')),
                created_at=now,
            )
        )
        if raw['kind'] == 'connect':
            device.last_connect_ok = ok
            device.last_error_code = None if ok else _code(raw.get('error_code'))

    await db.execute(
        delete(AppDeviceEvent).where(AppDeviceEvent.created_at < now - timedelta(days=EVENT_RETENTION_DAYS))
    )
    await db.commit()
    return device


async def device_stats(db: AsyncSession, *, days: int = 7) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    active = await db.scalar(select(func.count(AppDevice.id)).where(AppDevice.last_seen_at >= since)) or 0
    platforms = (
        await db.execute(
            select(AppDevice.platform, AppDevice.app_version, func.count(AppDevice.id))
            .where(AppDevice.last_seen_at >= since)
            .group_by(AppDevice.platform, AppDevice.app_version)
        )
    ).all()
    outcomes = (
        await db.execute(
            select(AppDeviceEvent.ok, func.count(AppDeviceEvent.id))
            .where(AppDeviceEvent.kind == 'connect', AppDeviceEvent.created_at >= since)
            .group_by(AppDeviceEvent.ok)
        )
    ).all()
    errors = (
        await db.execute(
            select(AppDeviceEvent.error_code, func.count(AppDeviceEvent.id))
            .where(AppDeviceEvent.ok.is_(False), AppDeviceEvent.created_at >= since)
            .group_by(AppDeviceEvent.error_code)
            .order_by(func.count(AppDeviceEvent.id).desc())
            .limit(10)
        )
    ).all()
    counts = {bool(ok): count for ok, count in outcomes}
    return {
        'days': days,
        'active_devices': active,
        'platforms': [{'platform': p, 'app_version': v, 'devices': c} for p, v, c in platforms],
        'connect_ok': counts.get(True, 0),
        'connect_failed': counts.get(False, 0),
        'errors': [{'code': code or 'unknown', 'count': count} for code, count in errors],
    }
