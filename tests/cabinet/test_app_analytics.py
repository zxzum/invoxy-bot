import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from app.cabinet.dependencies import get_cabinet_db
from app.cabinet.routes import app_client as route
from app.database.models import AppDevice, AppError, AppEvent
from app.services import app_analytics_service as analytics, app_client_service as svc


NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
DEVICE = 'device-0123456789abcdef'


# --- what is accepted ----------------------------------------------------------------------------------------------


def test_redact_removes_links_addresses_ids_and_tokens() -> None:
    text = (
        'failed https://invoxy.my/sub/AbCdEf0123456789AbCdEf0123456789 for user@example.com at 203.0.113.9 '
        'id 123e4567-e89b-12d3-a456-426614174000 token ABCDEFGHIJKLMNOPQRSTUVWXYZ012345'
    )
    cleaned = analytics.redact(text, 500)
    for secret in ('invoxy.my', 'user@example.com', '203.0.113.9', '123e4567', 'ABCDEFGHIJ'):
        assert secret not in cleaned
    assert (
        '<url>' in cleaned and '<email>' in cleaned and '<ip>' in cleaned and '<id>' in cleaned and '<token>' in cleaned
    )


def test_props_are_flat_scalars_only() -> None:
    props = analytics.clean_props(
        {
            'ok': True,
            'ms': 1830,
            'server': 'DE #3',
            'nested': {'a': 1},
            'list': [1, 2],
            'Bad Key': 1,
            'none': None,
            'nan': float('nan'),
            'link': 'see https://example.org/x?key=1',
        }
    )
    assert props == {'ok': True, 'ms': 1830, 'server': 'DE #3', 'link': 'see <url>'}
    assert analytics.clean_props('x') is None
    assert analytics.clean_props({}) is None


def test_props_are_capped() -> None:
    props = analytics.clean_props({f'k{i}': i for i in range(40)})
    assert props is not None and len(props) == analytics.MAX_PROPS


def test_event_time_is_not_trusted_beyond_a_week_back_or_five_minutes_ahead() -> None:
    good = (NOW - timedelta(hours=1)).timestamp()
    assert analytics.event_time(good, NOW) == datetime.fromtimestamp(good, UTC)
    assert analytics.event_time((NOW - timedelta(days=30)).timestamp(), NOW) == NOW
    assert analytics.event_time((NOW + timedelta(hours=2)).timestamp(), NOW) == NOW
    assert analytics.event_time('yesterday', NOW) == NOW
    assert analytics.event_time(True, NOW) == NOW
    assert analytics.event_time(1e30, NOW) == NOW


def test_events_need_a_clean_name() -> None:
    assert analytics.clean_event({'name': 'connect', 'props': {'ok': True}}, NOW)['name'] == 'connect'
    for name in ('Connect', 'a b', '', 'x' * 41, 7, None, '../etc'):
        assert analytics.clean_event({'name': name}, NOW) is None
    assert analytics.clean_event('connect', NOW) is None


def test_errors_need_a_fingerprint_and_a_known_kind() -> None:
    ok = analytics.clean_error(
        {'fingerprint': 'AB12CD34EF56', 'kind': 'flutter', 'message': 'boom at 10.0.0.1', 'stack': '#0 f (a.dart:1)'},
        NOW,
    )
    assert ok is not None
    assert ok['fingerprint'] == 'ab12cd34ef56'
    assert '10.0.0.1' not in ok['message']
    assert analytics.clean_error({'fingerprint': 'zz', 'kind': 'flutter'}, NOW) is None
    assert analytics.clean_error({'fingerprint': 'ab12cd34ef56', 'kind': 'weird'}, NOW) is None


# --- alerts ----------------------------------------------------------------------------------------------------------


def test_a_new_error_alerts_a_known_one_only_when_it_spikes() -> None:
    def alert(is_new: bool, before: int, after: int, last: datetime | None = None) -> bool:
        return analytics.should_alert(is_new=is_new, before=before, after=after, last_alert_at=last, now=NOW)

    assert alert(True, 0, 1)
    assert not alert(False, 1, 2)
    assert alert(False, 9, 10)
    assert not alert(False, 10, 11)
    assert alert(False, 99, 100)
    assert not alert(False, 9, 10, last=NOW - timedelta(minutes=5)), 'not again within half an hour'
    assert alert(False, 9, 10, last=NOW - timedelta(minutes=40))


def test_the_alert_text_is_escaped() -> None:
    row = AppError(
        fingerprint='ab12cd34ef56',
        app_version='1.3.20',
        platform='macos',
        kind='flutter',
        message='<script>alert(1)</script>',
        stack='#0 a\n#1 b',
        count=12,
        devices=3,
    )
    text = analytics.alert_text(row)
    assert '<script>' not in text and '&lt;script&gt;' in text
    assert '12 раз' in text and '3 устройствах' in text


# --- the numbers -------------------------------------------------------------------------------------------------------


def test_aggregate_events() -> None:
    rows: list[tuple[str, dict[str, Any] | None, int]] = [
        ('app_open', {'cold': True}, 1),
        ('screen', {'name': 'home'}, 1),
        ('screen', {'name': 'home'}, 2),
        ('screen', {'name': 'servers'}, 2),
        (
            'connect',
            {'ok': True, 'ms': 1000, 'protocol': 'vless-xhttp', 'network': 'wifi', 'server': 'DE #3', 'mode': 'auto'},
            1,
        ),
        (
            'connect',
            {
                'ok': True,
                'ms': 3000,
                'protocol': 'vless-reality',
                'network': 'cellular',
                'server': 'FI #3',
                'mode': 'manual',
            },
            2,
        ),
        ('connect', {'ok': False, 'error': 'core_failed', 'protocol': 'vless-xhttp', 'network': 'wifi'}, 2),
        ('disconnect', {'seconds': 600, 'rx_mb': 512, 'tx_mb': 512}, 1),
        ('disconnect', {'seconds': 1800, 'rx_mb': 0, 'tx_mb': 0}, 2),
        ('switch', {'kind': 'reroll'}, 1),
        ('switch', {'kind': 'auto_move'}, 2),
        ('update', {'stage': 'found'}, 1),
        ('update', {'stage': 'ready'}, 1),
        ('setting', {'key': 'auto', 'value': False}, 2),
        ('login', {'via': 'telegram', 'ok': True}, 1),
        ('service', {'state': 'maintenance'}, 1),
        ('ping', {'median_ms': 80}, 1),
        ('ping', {'median_ms': 120}, 2),
    ]
    out = analytics.aggregate_events(rows)
    assert out['app_opens'] == 1
    assert out['connect']['ok'] == 2 and out['connect']['failed'] == 1
    assert out['connect']['success_rate'] == pytest.approx(0.667, abs=0.001)
    assert out['connect']['median_ms'] == 2000
    assert out['connect']['errors'] == [{'code': 'core_failed', 'count': 1}]
    assert {'protocol': 'vless-xhttp', 'ok': 1, 'failed': 1} in out['connect']['by_protocol']
    assert {'network': 'cellular', 'ok': 1, 'failed': 0} in out['connect']['by_network']
    assert out['sessions'] == {'count': 2, 'median_minutes': 20.0, 'total_hours': 0.7, 'traffic_gb': 1.0}
    assert out['screens'][0] == {'name': 'home', 'count': 2}
    assert out['switches'] == {'reroll': 1, 'auto_move': 1}
    assert out['updates'] == {'found': 1, 'ready': 1}
    assert out['logins'] == {'telegram:ok': 1}
    assert out['ping_median_ms'] == 100
    assert out['maintenance_notices'] == 1
    assert {'server': 'DE #3', 'connects': 1} in out['servers']


def test_aggregate_of_nothing_is_empty_not_a_crash() -> None:
    out = analytics.aggregate_events([])
    assert out['connect']['success_rate'] is None
    assert out['sessions']['median_minutes'] is None


# --- storing -----------------------------------------------------------------------------------------------------------


class _Result:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _Db:
    """Enough of a session for ``ingest``: finds a device and an error by their keys, collects what is added."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.devices: list[AppDevice] = []
        self.errors: list[AppError] = []
        self.commits = 0
        self._next = 0

    def add(self, item: Any) -> None:
        self.added.append(item)
        if isinstance(item, AppDevice):
            self.devices.append(item)
        if isinstance(item, AppError):
            self.errors.append(item)

    async def flush(self) -> None:
        for item in self.added:
            if getattr(item, 'id', 1) is None:
                self._next += 1
                item.id = self._next

    async def commit(self) -> None:
        self.commits += 1

    async def execute(self, statement: Any) -> _Result:
        text = str(statement)
        if 'app_devices' in text and 'SELECT' in text:
            return _Result(self.devices[0] if self.devices else None)
        if 'app_errors' in text and 'SELECT' in text:
            return _Result(self.errors[0] if self.errors else None)
        return _Result(None)


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(svc.settings), 'get_cabinet_jwt_secret', lambda _self: 'test-secret')
    monkeypatch.setattr(analytics, '_last_purge', 10**12)  # no purge in these tests


def _payload(**extra: Any) -> dict[str, Any]:
    return {
        'device_id': DEVICE,
        'platform': 'macos',
        'app_version': '1.3.20',
        'os_version': 'macOS 14.6',
        'session': 'abc123def456',
        'events': [{'name': 'connect', 'ts': NOW.timestamp(), 'props': {'ok': True, 'ms': 900}}, {'name': 'BAD NAME'}],
        'errors': [],
        **extra,
    }


async def test_ingest_stores_the_clean_events_and_ignores_the_rest() -> None:
    db = _Db()
    result = await analytics.ingest(db, _payload())  # type: ignore[arg-type]
    assert result.events == 1 and result.errors == 0
    events = [a for a in db.added if isinstance(a, AppEvent)]
    assert len(events) == 1 and events[0].name == 'connect' and events[0].session == 'abc123def456'
    assert db.devices[0].platform == 'macos' and db.devices[0].app_version == '1.3.20'
    assert db.devices[0].device_hash != DEVICE, 'the raw device id is never stored'
    assert db.commits >= 1


async def test_ingest_refuses_what_is_not_a_client() -> None:
    for bad in ({'device_id': 'x', 'platform': 'macos'}, {'device_id': DEVICE, 'platform': 'beos'}, {}):
        with pytest.raises(ValueError):
            await analytics.ingest(_Db(), bad)  # type: ignore[arg-type]


async def test_ingest_caps_the_batch() -> None:
    db = _Db()
    many = [{'name': 'screen', 'props': {'name': 'home'}} for _ in range(200)]
    result = await analytics.ingest(db, _payload(events=many))  # type: ignore[arg-type]
    assert result.events == analytics.MAX_EVENTS


async def test_a_new_error_is_stored_and_alerts_and_the_same_one_again_only_counts() -> None:
    db = _Db()
    err = {'fingerprint': 'ab12cd34ef56', 'kind': 'flutter', 'message': 'boom', 'stack': '#0 f'}
    first = await analytics.ingest(db, _payload(events=[], errors=[err]))  # type: ignore[arg-type]
    assert first.errors == 1 and len(first.alerts) == 1
    row = db.errors[0]
    assert row.count == 1 and row.app_version == '1.3.20' and row.platform == 'macos'
    second = await analytics.ingest(db, _payload(events=[], errors=[err]))  # type: ignore[arg-type]
    assert second.alerts == [] and row.count == 2
    assert len(db.errors) == 1, 'one row per kind of error'


# --- the door ------------------------------------------------------------------------------------------------------------


@pytest.fixture
def door(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    async def no_limit(*args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(route.RateLimitCache, 'is_ip_rate_limited', no_limit)
    monkeypatch.setattr(route, 'get_client_ip', lambda _request: '203.0.113.5')
    seen: list[Any] = []

    async def fake_ingest(_db: Any, payload: dict[str, Any]) -> Any:
        seen.append(payload)
        if payload.get('device_id') == 'bad':
            raise ValueError('invalid device')
        return analytics.IngestResult(events=1, errors=0)

    monkeypatch.setattr(route.analytics, 'ingest', fake_ingest)
    return seen


def _client() -> AsyncClient:
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[get_cabinet_db] = lambda: None
    return AsyncClient(transport=ASGITransport(app=app), base_url='http://test')


async def test_the_events_door_accepts_a_batch_without_an_account(door: list[Any]) -> None:
    async with _client() as client:
        response = await client.post('/app/events', content=json.dumps(_payload()))
    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert door[0]['device_id'] == DEVICE


async def test_the_events_door_refuses_junk_and_big_bodies(door: list[Any]) -> None:
    async with _client() as client:
        assert (await client.post('/app/events', content='not json')).status_code == status.HTTP_400_BAD_REQUEST
        assert (await client.post('/app/events', content='[1,2]')).status_code == status.HTTP_400_BAD_REQUEST
        assert (await client.post('/app/events', content=json.dumps({'device_id': 'bad'}))).status_code == 400
        big = json.dumps({'device_id': DEVICE, 'pad': 'x' * (analytics.MAX_BODY_BYTES + 10)})
        assert (await client.post('/app/events', content=big)).status_code == 413
    assert len(door) == 1, 'only the request that was a client reached the store (the invalid one raised inside)'


async def test_the_events_door_alerts_in_the_background(door: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    told: list[Any] = []

    async def fake_ingest(_db: Any, payload: dict[str, Any]) -> Any:
        return analytics.IngestResult(errors=1, alerts=[SimpleNamespace(fingerprint='ab12cd34ef56')])  # type: ignore[arg-type]

    async def fake_notify(rows: list[Any]) -> None:
        told.extend(rows)

    monkeypatch.setattr(route.analytics, 'ingest', fake_ingest)
    monkeypatch.setattr(route.analytics, 'notify_admins', fake_notify)
    async with _client() as client:
        response = await client.post('/app/events', content=json.dumps(_payload()))
    assert response.status_code == 204
    assert [r.fingerprint for r in told] == ['ab12cd34ef56']
