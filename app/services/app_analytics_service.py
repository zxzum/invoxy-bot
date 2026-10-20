"""Invoxy VPN client analytics: anonymous usage events and the client's own error reports.

What arrives (``POST /cabinet/app/events``) is deliberately small and flat: an event is a name and a few scalar props
(screen, connect result and time, server switches, update steps); an error is a message, a short stack and a
fingerprint. The client never sends keys, subscription links, addresses or visited sites, and this module cleans what
arrives once more (``redact``) before anything is stored.

Errors are folded by fingerprint: one row per kind of error and app version, with a counter, so a crash that hits a
thousand phones is one line with ``count=1000`` and one message in the admin chat, not a thousand messages.
"""

from __future__ import annotations

import html
import math
import re
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AppDevice, AppError, AppEvent
from app.services import app_client_service as clients


logger = structlog.get_logger(__name__)

EVENT_RETENTION_DAYS = 90
ERROR_RETENTION_DAYS = 180
MAX_EVENTS = 50
MAX_ERRORS = 10
MAX_PROPS = 14
MAX_BODY_BYTES = 64 * 1024

NAME_RE = re.compile(r'^[a-z][a-z0-9_.]{0,39}$')
KEY_RE = re.compile(r'^[a-z][a-z0-9_]{0,31}$')
FINGERPRINT_RE = re.compile(r'^[a-f0-9]{8,32}$')
SESSION_RE = re.compile(r'^[A-Za-z0-9_-]{6,24}$')
ERROR_KINDS = frozenset({'flutter', 'async', 'platform', 'handled', 'native', 'update'})

# An alert for a fingerprint that is already known repeats only when its counter crosses one of these, and never
# within ``ALERT_GAP`` of the last one.
ALERT_STEPS = (10, 100, 1000, 10000)
ALERT_GAP = timedelta(minutes=30)

_URL = re.compile(r'https?://[^\s"\'<>)]+', re.IGNORECASE)
_EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
_IPV4 = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
_UUID = re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.IGNORECASE)
_LONG_TOKEN = re.compile(r'\b[A-Za-z0-9_-]{32,}\b')


def redact(text: str, limit: int) -> str:
    """Removes what must never be stored from a message or a stack: links, addresses, ids and long tokens."""
    text = _URL.sub('<url>', text)
    text = _EMAIL.sub('<email>', text)
    text = _UUID.sub('<id>', text)
    text = _IPV4.sub('<ip>', text)
    text = _LONG_TOKEN.sub('<token>', text)
    return text[:limit]


def clean_props(raw: Any) -> dict[str, Any] | None:
    """Scalar props only: short strings (redacted), numbers, booleans. Anything nested or odd is dropped."""
    if not isinstance(raw, dict):
        return None
    props: dict[str, Any] = {}
    for key, value in raw.items():
        if len(props) >= MAX_PROPS:
            break
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            continue
        if isinstance(value, bool) or value is None:
            if value is not None:
                props[key] = value
        elif isinstance(value, int | float):
            if math.isfinite(value) and abs(value) < 1e12:  # not NaN or infinity, not absurd
                props[key] = value
        elif isinstance(value, str):
            props[key] = redact(value, 80)
    return props or None


def event_time(value: Any, now: datetime) -> datetime:
    """The client's clock is not trusted: a time outside a week back and five minutes ahead becomes «now»."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        try:
            moment = datetime.fromtimestamp(float(value), UTC)
        except (OverflowError, OSError, ValueError):
            return now
        if now - timedelta(days=7) <= moment <= now + timedelta(minutes=5):
            return moment
    return now


def clean_event(raw: Any, now: datetime) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = raw.get('name')
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        return None
    return {'name': name, 'props': clean_props(raw.get('props')), 'at': event_time(raw.get('ts'), now)}


def clean_error(raw: Any, now: datetime) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    fingerprint = str(raw.get('fingerprint') or '').lower()
    kind = raw.get('kind')
    if not FINGERPRINT_RE.fullmatch(fingerprint) or kind not in ERROR_KINDS:
        return None
    message = redact(str(raw.get('message') or ''), 500)
    stack = raw.get('stack')
    return {
        'fingerprint': fingerprint,
        'kind': kind,
        'message': message,
        'stack': redact(stack, 4000) if isinstance(stack, str) and stack else None,
        'context': clean_props(raw.get('props')),
        'at': event_time(raw.get('ts'), now),
    }


@dataclass
class IngestResult:
    events: int = 0
    errors: int = 0
    alerts: list[AppError] = field(default_factory=list)


def should_alert(*, is_new: bool, before: int, after: int, last_alert_at: datetime | None, now: datetime) -> bool:
    """A new kind of error is told at once; a known one only when it spikes (crosses a step), at most every 30 min."""
    if is_new:
        return True
    if last_alert_at is not None and now - last_alert_at < ALERT_GAP:
        return False
    return any(before < step <= after for step in ALERT_STEPS)


async def _device_for(db: AsyncSession, payload: dict[str, Any], platform: str, now: datetime) -> AppDevice:
    device_hash = clients.hash_device_id(str(payload['device_id']))
    device = (await db.execute(select(AppDevice).where(AppDevice.device_hash == device_hash))).scalar_one_or_none()
    if device is None:
        device = AppDevice(device_hash=device_hash, platform=platform, first_seen_at=now)
        db.add(device)
    device.platform = platform
    version = clients._short(payload.get('app_version'), 32)
    if version:
        device.app_version = version
    device.last_seen_at = now
    await db.flush()
    return device


async def ingest(db: AsyncSession, payload: dict[str, Any]) -> IngestResult:
    """Stores a batch. Raises ``ValueError`` for a request that is not from a client (bad id or platform)."""
    device_id = str(payload.get('device_id') or '')
    platform = str(payload.get('platform') or '')
    if not clients.DEVICE_ID_RE.fullmatch(device_id) or platform not in clients.PLATFORMS:
        raise ValueError('invalid device')

    now = datetime.now(UTC)
    device = await _device_for(db, payload, platform, now)
    version = clients._short(payload.get('app_version'), 32) or ''
    session = str(payload.get('session') or '')
    session = session if SESSION_RE.fullmatch(session) else None
    result = IngestResult()

    events = payload.get('events') if isinstance(payload.get('events'), list) else []
    for raw in events[:MAX_EVENTS]:
        event = clean_event(raw, now)
        if event is None:
            continue
        db.add(
            AppEvent(
                device_id=device.id,
                session=session,
                name=event['name'],
                props=event['props'],
                app_version=version or None,
                created_at=event['at'],
            )
        )
        result.events += 1

    errors = payload.get('errors') if isinstance(payload.get('errors'), list) else []
    context_base = {k: v for k, v in {'os': clients._short(payload.get('os_version'), 40)}.items() if v}
    for raw in errors[:MAX_ERRORS]:
        error = clean_error(raw, now)
        if error is None:
            continue
        row = (
            await db.execute(
                select(AppError).where(
                    AppError.fingerprint == error['fingerprint'],
                    AppError.app_version == version,
                    AppError.platform == platform,
                )
            )
        ).scalar_one_or_none()
        is_new = row is None
        before = 0 if row is None else row.count
        if row is None:
            row = AppError(
                fingerprint=error['fingerprint'],
                app_version=version,
                platform=platform,
                kind=error['kind'],
                message=error['message'],
                stack=error['stack'],
                context={**context_base, **(error['context'] or {})} or None,
                count=0,
                devices=1,
                first_seen_at=error['at'],
            )
            db.add(row)
        elif row.last_device_id != device.id:
            row.devices = (row.devices or 1) + 1
        row.count = before + 1
        row.last_seen_at = now
        row.last_device_id = device.id
        if should_alert(is_new=is_new, before=before, after=row.count, last_alert_at=row.last_alert_at, now=now):
            row.last_alert_at = now
            result.alerts.append(row)
        result.errors += 1

    await db.commit()
    await _purge(db, now)
    return result


_last_purge = 0.0


async def _purge(db: AsyncSession, now: datetime) -> None:
    """Old rows go, at most once an hour per process (the batch path stays cheap)."""
    global _last_purge
    if time.monotonic() - _last_purge < 3600:
        return
    _last_purge = time.monotonic()
    await db.execute(delete(AppEvent).where(AppEvent.created_at < now - timedelta(days=EVENT_RETENTION_DAYS)))
    await db.execute(delete(AppError).where(AppError.last_seen_at < now - timedelta(days=ERROR_RETENTION_DAYS)))
    await db.commit()


# --- the admin chat ----------------------------------------------------------------------------------------------

_recent_alerts: deque[float] = deque(maxlen=10)


def _flood() -> bool:
    """No more than ten alerts per ten minutes from this process, whatever the clients send."""
    cutoff = time.monotonic() - 600
    while _recent_alerts and _recent_alerts[0] < cutoff:
        _recent_alerts.popleft()
    if len(_recent_alerts) >= _recent_alerts.maxlen:
        return True
    _recent_alerts.append(time.monotonic())
    return False


def alert_text(row: AppError) -> str:
    stack = '\n'.join((row.stack or '').splitlines()[:6])
    spike = f' · <b>{row.count} раз</b> на {row.devices} устройствах' if row.count > 1 else ''
    return (
        '⚠️ <b>Ошибка в приложении Invoxy VPN</b>\n'
        f'{html.escape(row.platform)} · v{html.escape(row.app_version)} · {html.escape(row.kind)}{spike}\n'
        f'<code>{html.escape((row.message or "")[:300])}</code>'
        + (f'\n<pre>{html.escape(stack[:700])}</pre>' if stack else '')
    )


async def notify_admins(rows: list[AppError]) -> None:
    """Tells the admin chat (the errors topic) at once. Best effort: a failure here never fails the client's request."""
    if not rows:
        return
    try:
        from app.bot_factory import create_bot
        from app.config import settings
        from app.services.admin_notification_service import AdminNotificationService, NotificationCategory

        if not getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False):
            return
        bot = create_bot()
        try:
            service = AdminNotificationService(bot)
            for row in rows:
                if _flood():
                    logger.warning('app_error_alert_flood', fingerprint=row.fingerprint)
                    break
                await service.send_admin_notification(alert_text(row), category=NotificationCategory.ERRORS)
        finally:
            await bot.session.close()
    except Exception as error:
        logger.warning('app_error_alert_failed', error=str(error)[:200])


# --- what the admin looks at -------------------------------------------------------------------------------------

_SUMMARY_NAMES = (
    'connect',
    'disconnect',
    'screen',
    'switch',
    'ping',
    'login',
    'update',
    'setting',
    'service',
    'app_open',
)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return float(ordered[mid]) if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def aggregate_events(rows: list[tuple[str, dict[str, Any] | None, int]]) -> dict[str, Any]:
    """From ``(name, props, device)`` rows: how the app is used. Pure, so it is tested without a database."""
    connect_ok: Counter[str] = Counter()
    connect_failed: Counter[str] = Counter()
    by_network: dict[str, list[int]] = {}
    errors: Counter[str] = Counter()
    modes: Counter[str] = Counter()
    connect_ms: list[float] = []
    seconds: list[float] = []
    screens: Counter[str] = Counter()
    switches: Counter[str] = Counter()
    servers: Counter[str] = Counter()
    updates: Counter[str] = Counter()
    settings_: Counter[str] = Counter()
    logins: Counter[str] = Counter()
    pings: list[float] = []
    maintenance = 0
    opens = 0
    traffic_mb = 0.0
    for name, props, _device in rows:
        p = props or {}
        if name == 'connect':
            protocol = str(p.get('protocol') or 'unknown')
            network = str(p.get('network') or 'unknown')
            ok = bool(p.get('ok'))
            (connect_ok if ok else connect_failed)[protocol] += 1
            by_network.setdefault(network, [0, 0])[0 if ok else 1] += 1
            modes[str(p.get('mode') or 'unknown')] += 1
            if ok:
                if (ms := _num(p.get('ms'))) is not None:
                    connect_ms.append(ms)
                if p.get('server'):
                    servers[str(p['server'])] += 1
            else:
                errors[str(p.get('error') or 'unknown')] += 1
        elif name == 'disconnect':
            if (s := _num(p.get('seconds'))) is not None:
                seconds.append(s)
            traffic_mb += (_num(p.get('rx_mb')) or 0) + (_num(p.get('tx_mb')) or 0)
        elif name == 'screen':
            screens[str(p.get('name') or 'unknown')] += 1
        elif name == 'switch':
            switches[str(p.get('kind') or 'unknown')] += 1
        elif name == 'ping':
            if (ms := _num(p.get('median_ms'))) is not None:
                pings.append(ms)
        elif name == 'login':
            logins[f'{p.get("via") or "unknown"}:{"ok" if p.get("ok") else "fail"}'] += 1
        elif name == 'update':
            updates[str(p.get('stage') or 'unknown')] += 1
        elif name == 'setting':
            settings_[f'{p.get("key")}={p.get("value")}'] += 1
        elif name == 'service':
            maintenance += 1
        elif name == 'app_open':
            opens += 1
    ok_total, failed_total = sum(connect_ok.values()), sum(connect_failed.values())
    protocols = sorted(set(connect_ok) | set(connect_failed))
    return {
        'app_opens': opens,
        'connect': {
            'ok': ok_total,
            'failed': failed_total,
            'success_rate': round(ok_total / (ok_total + failed_total), 3) if ok_total + failed_total else None,
            'median_ms': _median(connect_ms),
            'modes': dict(modes),
            'by_protocol': [{'protocol': k, 'ok': connect_ok[k], 'failed': connect_failed[k]} for k in protocols],
            'by_network': [{'network': k, 'ok': v[0], 'failed': v[1]} for k, v in sorted(by_network.items())],
            'errors': [{'code': k, 'count': v} for k, v in errors.most_common(10)],
        },
        'sessions': {
            'count': len(seconds),
            'median_minutes': round(m / 60, 1) if (m := _median(seconds)) is not None else None,
            'total_hours': round(sum(seconds) / 3600, 1),
            'traffic_gb': round(traffic_mb / 1024, 2),
        },
        'screens': [{'name': k, 'count': v} for k, v in screens.most_common(12)],
        'switches': dict(switches),
        'servers': [{'server': k, 'connects': v} for k, v in servers.most_common(10)],
        'ping_median_ms': _median(pings),
        'updates': dict(updates),
        'logins': dict(logins),
        'settings': [{'setting': k, 'devices': v} for k, v in settings_.most_common(15)],
        'maintenance_notices': maintenance,
    }


async def summary(db: AsyncSession, *, days: int = 7) -> dict[str, Any]:
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    daily = (
        await db.execute(
            select(
                func.date(AppEvent.created_at), func.count(func.distinct(AppEvent.device_id)), func.count(AppEvent.id)
            )
            .where(AppEvent.created_at >= since)
            .group_by(func.date(AppEvent.created_at))
            .order_by(func.date(AppEvent.created_at))
        )
    ).all()
    rows = (
        await db.execute(
            select(AppEvent.name, AppEvent.props, AppEvent.device_id)
            .where(AppEvent.created_at >= since, AppEvent.name.in_(_SUMMARY_NAMES))
            .order_by(AppEvent.created_at.desc())
            .limit(100_000)
        )
    ).all()
    top_errors = (
        (
            await db.execute(
                select(AppError).where(AppError.last_seen_at >= since).order_by(AppError.count.desc()).limit(20)
            )
        )
        .scalars()
        .all()
    )
    new_devices = await db.scalar(select(func.count(AppDevice.id)).where(AppDevice.first_seen_at >= since)) or 0
    devices = await clients.device_stats(db, days=days)
    return {
        'days': days,
        'devices': {'active': devices['active_devices'], 'new': new_devices, 'versions': devices['platforms']},
        'daily': [{'day': str(day), 'devices': d, 'events': e} for day, d, e in daily],
        **aggregate_events([(name, props, device) for name, props, device in rows]),
        'errors': [
            {
                'fingerprint': e.fingerprint,
                'kind': e.kind,
                'app_version': e.app_version,
                'platform': e.platform,
                'message': e.message,
                'count': e.count,
                'devices': e.devices,
                'first_seen_at': e.first_seen_at.isoformat() if e.first_seen_at else None,
                'last_seen_at': e.last_seen_at.isoformat() if e.last_seen_at else None,
            }
            for e in top_errors
        ],
    }
