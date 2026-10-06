import base64
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.cabinet.routes import app_client as route
from app.services import app_client_service as svc


DEVICE = 'device-0123456789abcdef'
VERIFIER = 'v' * 64


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')


class FakeCache:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def set(self, key: str, value: Any, expire: int | None = None) -> bool:
        self.data[key] = value
        return True

    async def setnx(self, key: str, value: Any, expire: int | None = None) -> bool:
        if key in self.data:
            return False
        self.data[key] = value
        return True

    async def get(self, key: str) -> Any:
        return self.data.get(key)

    async def getdel(self, key: str) -> Any:
        return self.data.pop(key, None)

    async def delete(self, key: str) -> bool:
        return self.data.pop(key, None) is not None

    async def exists(self, key: str) -> bool:
        return key in self.data


@pytest.fixture
def fake_cache(monkeypatch: pytest.MonkeyPatch) -> FakeCache:
    fake = FakeCache()
    monkeypatch.setattr(svc, 'cache', fake)
    monkeypatch.setattr(type(svc.settings), 'get_cabinet_jwt_secret', lambda _self: 'test-secret')
    monkeypatch.setattr(type(svc.settings), 'get_bot_username', lambda _self: 'invoxy_bot')
    monkeypatch.setattr(svc.settings, 'MINIAPP_CUSTOM_URL', 'https://invoxy.example')
    return fake


async def _start() -> str:
    return await svc.start_login(
        device_id=DEVICE, platform='android', app_version='1.0.0', code_challenge=_challenge(VERIFIER)
    )


async def test_login_happy_path_is_single_use(fake_cache: FakeCache) -> None:
    request_id = await _start()
    urls = svc.login_urls(request_id)
    assert urls['bot_url'] == f'https://t.me/invoxy_bot?start=applogin_{request_id}'
    assert urls['web_url'] == f'https://invoxy.example/app/login?request={request_id}'

    assert await svc.exchange_login(request_id=request_id, code_verifier=VERIFIER, device_id=DEVICE) is None
    assert (await svc.get_pending_login(request_id))['platform'] == 'android'
    assert await svc.decide_login(request_id, user_id=7, approve=True)
    assert await svc.get_pending_login(request_id) is None
    assert not await svc.decide_login(request_id, user_id=8, approve=True)

    assert await svc.exchange_login(request_id=request_id, code_verifier=VERIFIER, device_id=DEVICE) == 7
    # A parallel poll of the same device (app resume + return link) waits instead of failing the sign-in.
    assert await svc.exchange_login(request_id=request_id, code_verifier=VERIFIER, device_id=DEVICE) is None
    assert await svc.get_pending_login(request_id) is None
    assert not await svc.decide_login(request_id, user_id=8, approve=True)
    with pytest.raises(svc.AppLoginError) as stranger:
        await svc.exchange_login(request_id=request_id, code_verifier='w' * 64, device_id=DEVICE)
    assert stranger.value.code == 'device_mismatch'

    fake_cache.data.pop(svc._login_key(request_id))  # grace period over
    with pytest.raises(svc.AppLoginError) as replay:
        await svc.exchange_login(request_id=request_id, code_verifier=VERIFIER, device_id=DEVICE)
    assert replay.value.code == 'expired'


async def test_login_is_bound_to_initiating_device(fake_cache: FakeCache) -> None:
    request_id = await _start()
    await svc.decide_login(request_id, user_id=7, approve=True)

    for verifier, device in (('w' * 64, DEVICE), (VERIFIER, 'other-device-0123456789')):
        with pytest.raises(svc.AppLoginError) as error:
            await svc.exchange_login(request_id=request_id, code_verifier=verifier, device_id=device)
        assert error.value.code == 'device_mismatch'

    assert await svc.exchange_login(request_id=request_id, code_verifier=VERIFIER, device_id=DEVICE) == 7


async def test_denied_and_expired_requests(fake_cache: FakeCache) -> None:
    request_id = await _start()
    await svc.decide_login(request_id, user_id=7, approve=False)
    with pytest.raises(svc.AppLoginError) as denied:
        await svc.exchange_login(request_id=request_id, code_verifier=VERIFIER, device_id=DEVICE)
    assert denied.value.code == 'denied'

    with pytest.raises(svc.AppLoginError) as expired:
        await svc.exchange_login(request_id='x' * 32, code_verifier=VERIFIER, device_id=DEVICE)
    assert expired.value.code == 'expired'


async def test_start_rejects_bad_input(fake_cache: FakeCache) -> None:
    with pytest.raises(svc.AppLoginError):
        await svc.start_login(device_id=DEVICE, platform='linux', app_version='1', code_challenge=_challenge(VERIFIER))
    with pytest.raises(svc.AppLoginError):
        await svc.start_login(device_id='short', platform='ios', app_version='1', code_challenge=_challenge(VERIFIER))


def test_routing_is_normalized_and_versioned() -> None:
    data = svc.normalize_routing(
        {
            'normal': {'android_packages': ['ru.sberbankmobile', 'bad package']},
            'restricted': {'domains': ['GosUslugi.ru.', 'bad domain', 'vk.com', 'vk.com'], 'android_packages': ['x']},
        }
    )
    assert data == {
        'normal': {'android_packages': ['ru.sberbankmobile']},
        'restricted': {'domains': ['gosuslugi.ru', 'vk.com'], 'android_packages': []},
    }
    assert svc.routing_version(data) == svc.routing_version(svc.normalize_routing(data))
    assert svc.routing_version(data) != svc.routing_version(svc.DEFAULT_ROUTING)
    assert svc.normalize_routing(svc.DEFAULT_ROUTING) == svc.DEFAULT_ROUTING


def test_subscription_domains_are_cleaned_and_never_empty() -> None:
    assert svc.normalize_domains(
        ['https://Sub.Lazeika.XYZ/path?x=1', 'invoxy.xyz:8443', '*.example.org', 'bad domain', 'invoxy.xyz']
    ) == [
        'sub.lazeika.xyz',
        'invoxy.xyz',
        'example.org',
    ]
    assert svc.normalize_domains('a.example.org, b.example.org;  c.example.org') == [
        'a.example.org',
        'b.example.org',
        'c.example.org',
    ]
    # Nothing usable (or not a list at all): the defaults, never an empty allow-list.
    assert svc.normalize_domains([]) == svc.DEFAULT_SUBSCRIPTION_DOMAINS
    assert svc.normalize_domains(['localhost', 'x', '']) == svc.DEFAULT_SUBSCRIPTION_DOMAINS
    assert svc.normalize_domains(None) == svc.DEFAULT_SUBSCRIPTION_DOMAINS
    assert svc.normalize_domains({'a': 1}) == svc.DEFAULT_SUBSCRIPTION_DOMAINS
    assert svc.DEFAULT_SUBSCRIPTION_DOMAINS == ['invoxy.my', 'lazeika.xyz', 'invoxy.xyz']


async def test_subscription_domains_are_stored_and_read(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict[str, str] = {}

    async def fake_get(db: Any, key: str) -> str | None:
        return store.get(key)

    async def fake_upsert(db: Any, key: str, value: str, description: str | None = None) -> None:
        store[key] = value

    class _Db:
        async def commit(self) -> None:
            return None

    monkeypatch.setattr(svc, 'get_setting_value', fake_get)
    monkeypatch.setattr(svc, 'upsert_system_setting', fake_upsert)
    db = _Db()
    assert await svc.get_subscription_domains(db) == svc.DEFAULT_SUBSCRIPTION_DOMAINS
    assert await svc.save_subscription_domains(db, ['Sub.Example.org', 'junk junk']) == ['sub.example.org']
    assert await svc.get_subscription_domains(db) == ['sub.example.org']
    store[svc.SUBSCRIPTION_DOMAINS_KEY] = 'not json'
    assert await svc.get_subscription_domains(db) == svc.DEFAULT_SUBSCRIPTION_DOMAINS


def _subscription(**overrides: Any) -> SimpleNamespace:
    now = datetime.now(UTC)
    values = {
        'id': 5,
        'tariff': SimpleNamespace(name='Стандарт'),
        'actual_status': 'active',
        'is_trial': False,
        'end_date': now + timedelta(days=10),
        'traffic_used_gb': 1.5,
        'traffic_limit_gb': 100,
        'whitelist_traffic_limit_gb': 0,
        'whitelist_traffic_used_bytes': 0,
        'device_limit': 3,
        'subscription_url': 'https://sub.example/abc',
        'last_webhook_update_at': None,
        'updated_at': now,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_subscription_state_separates_quotas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc.settings, 'MINIAPP_CUSTOM_URL', 'https://invoxy.example')
    plain = svc.subscription_state(_subscription())
    assert plain['restricted_traffic'] is None
    assert plain['traffic'] == {'used_bytes': int(1.5 * 1024**3), 'limit_bytes': 100 * 1024**3}
    assert plain['renew_url'] == 'https://invoxy.example/subscriptions/5/renew'

    limited = svc.subscription_state(
        _subscription(actual_status='limited', whitelist_traffic_limit_gb=20, whitelist_traffic_used_bytes=123)
    )
    assert limited['status'] == 'limited'
    assert limited['restricted_traffic'] == {'used_bytes': 123, 'limit_bytes': 20 * 1024**3}


def _app(user: Any = None) -> FastAPI:
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[get_cabinet_db] = lambda: None
    if user is not None:
        app.dependency_overrides[get_current_cabinet_user] = lambda: user
    return app


async def test_http_login_flow(fake_cache: FakeCache, monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_limit(*args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(route.RateLimitCache, 'is_ip_rate_limited', no_limit)
    monkeypatch.setattr(route, 'get_client_ip', lambda _request: '203.0.113.5')
    user = SimpleNamespace(id=7, status='active')

    async def fake_get_user(_db: Any, user_id: int) -> Any:
        return user if user_id == 7 else None

    async def fake_auth(_user: Any, _db: Any) -> Any:
        return route.AuthResponse.model_construct(
            access_token='a', refresh_token='r', token_type='bearer', expires_in=60, user=None
        )

    class _Result:
        def scalar_one_or_none(self) -> None:
            return None

    class Db:
        async def commit(self) -> None:
            return None

        async def execute(self, *args: Any, **kwargs: Any) -> _Result:
            return _Result()

    async def fake_store(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(route, 'get_user_by_id', fake_get_user)
    monkeypatch.setattr(route, '_create_auth_response', fake_auth)
    monkeypatch.setattr(route, '_store_refresh_token', fake_store)
    app = _app(user)
    app.dependency_overrides[get_cabinet_db] = lambda: Db()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        start = await client.post(
            '/app/login',
            json={'device_id': DEVICE, 'platform': 'ios', 'app_version': '1', 'code_challenge': _challenge(VERIFIER)},
        )
        assert start.status_code == status.HTTP_200_OK
        request_id = start.json()['request_id']
        body = {'request_id': request_id, 'code_verifier': VERIFIER, 'device_id': DEVICE}

        assert (await client.post('/app/login/exchange', json=body)).status_code == status.HTTP_202_ACCEPTED
        assert (await client.get(f'/app/login/{request_id}')).json()['platform'] == 'ios'
        confirm = await client.post(f'/app/login/{request_id}/confirm')
        assert confirm.json() == {'status': 'ok', 'return_url': 'invoxyvpn://login'}
        assert (await client.post(f'/app/login/{request_id}/confirm')).status_code == status.HTTP_410_GONE

        wrong = await client.post('/app/login/exchange', json={**body, 'code_verifier': 'w' * 64})
        assert wrong.status_code == status.HTTP_403_FORBIDDEN
        done = await client.post('/app/login/exchange', json=body)
        assert done.status_code == status.HTTP_200_OK
        assert done.json()['access_token'] == 'a'
        # The second token pair is never issued; a late parallel poll keeps waiting.
        assert (await client.post('/app/login/exchange', json=body)).status_code == status.HTTP_202_ACCEPTED
        assert (await client.get(f'/app/login/{request_id}')).status_code == status.HTTP_410_GONE


# --- Device management for the app (current device is protected) ---------------


def test_device_removal_rules() -> None:
    svc.check_device_removal(hwid='other-device-0001', current_device_id='device-0123456789abcdef')
    for hwid in ('DEVICE-0123456789ABCDEF', '  device-0123456789abcdef  ', 'Device-0123456789abcdef'):
        with pytest.raises(svc.DeviceRemovalError) as error:
            svc.check_device_removal(hwid=hwid, current_device_id=' device-0123456789abcdef ')
        assert error.value.code == 'current_device'
    with pytest.raises(svc.DeviceRemovalError) as missing:
        svc.check_device_removal(hwid='other-device-0001', current_device_id=None)
    assert missing.value.code == 'device_header_required'
    with pytest.raises(svc.DeviceRemovalError) as blank:
        svc.check_device_removal(hwid='   ', current_device_id=DEVICE)
    assert blank.value.code == 'hwid_required'


def test_device_list_marks_the_current_one() -> None:
    raw = {
        'devices': [
            {
                'hwid': 'AAA-111',
                'platform': 'Android',
                'device_model': 'Pixel 7',
                'created_at': '2026-10-01T10:00:00Z',
                'local_name': None,
            },
            {
                'hwid': 'bbb-222',
                'platform': 'macOS',
                'device_model': 'MacBook',
                'created_at': None,
                'local_name': 'Ноутбук',
            },
        ],
        'total': 2,
        'device_limit': 3,
    }
    out = svc.format_app_devices(raw, current_device_id=' aaa-111 ')
    assert out['device_limit'] == 3
    assert [d['hwid'] for d in out['devices']] == ['AAA-111', 'bbb-222']
    assert [d['is_current'] for d in out['devices']] == [True, False]
    assert out['devices'][0]['name'] == 'Pixel 7'
    assert out['devices'][1]['name'] == 'Ноутбук'
    assert out['devices'][1]['last_seen'] is None


def test_device_list_without_header_marks_nothing() -> None:
    raw = {
        'devices': [{'hwid': 'AAA-111', 'platform': 'Android', 'device_model': 'Pixel'}],
        'total': 1,
        'device_limit': 1,
    }
    assert svc.format_app_devices(raw, current_device_id=None)['devices'][0]['is_current'] is False


class _Panel:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.owned = True

    async def list(self, **kwargs: Any) -> dict[str, Any]:
        return {
            'devices': [{'hwid': 'mine-0123456789abcdef', 'platform': 'Android', 'device_model': 'Pixel'}],
            'total': 1,
            'device_limit': 2,
        }

    async def delete(self, hwid: str, **kwargs: Any) -> dict[str, Any]:
        self.deleted.append(hwid)
        return {'success': True}

    async def belongs(self, user: Any, hwid: str) -> bool:
        return self.owned


@pytest.fixture
def panel(monkeypatch: pytest.MonkeyPatch, fake_cache: FakeCache) -> _Panel:
    async def no_limit(*args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(route.RateLimitCache, 'is_ip_rate_limited', no_limit)
    monkeypatch.setattr(route, 'get_client_ip', lambda _request: '203.0.113.5')
    fake = _Panel()
    monkeypatch.setattr(route, '_panel_get_devices', fake.list)
    monkeypatch.setattr(route, '_panel_delete_device', fake.delete)
    monkeypatch.setattr(route, 'verify_hwid_belongs_to_user', fake.belongs)
    return fake


async def test_http_device_list(panel: _Panel) -> None:
    app = _app(SimpleNamespace(id=7, status='active'))
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/app/devices/list', headers={'X-Invoxy-Device': 'MINE-0123456789ABCDEF'})
    assert response.status_code == status.HTTP_200_OK
    assert response.json()['devices'][0]['is_current'] is True


async def test_http_cannot_delete_the_current_device(panel: _Panel) -> None:
    app = _app(SimpleNamespace(id=7, status='active'))
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.delete(
            '/app/devices/Mine-0123456789abcdef', headers={'X-Invoxy-Device': 'mine-0123456789abcdef'}
        )
    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()['detail'] == 'current_device'
    assert panel.deleted == []


async def test_http_delete_other_device_needs_the_header_and_ownership(panel: _Panel) -> None:
    app = _app(SimpleNamespace(id=7, status='active'))
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        no_header = await client.delete('/app/devices/other-0123456789abcdef')
        assert no_header.status_code == status.HTTP_400_BAD_REQUEST
        panel.owned = False
        foreign = await client.delete(
            '/app/devices/other-0123456789abcdef', headers={'X-Invoxy-Device': 'mine-0123456789abcdef'}
        )
        assert foreign.status_code == status.HTTP_404_NOT_FOUND
        panel.owned = True
        ok = await client.delete(
            '/app/devices/other-0123456789abcdef', headers={'X-Invoxy-Device': 'mine-0123456789abcdef'}
        )
    assert ok.status_code == status.HTTP_204_NO_CONTENT
    assert panel.deleted == ['other-0123456789abcdef']


async def test_http_delete_is_audited(panel: _Panel, caplog: pytest.LogCaptureFixture) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch_logger = route.logger

    class Capture:
        def info(self, event: str, **kwargs: Any) -> None:
            events.append((event, kwargs))

        def warning(self, event: str, **kwargs: Any) -> None:
            events.append((event, kwargs))

    route.logger = Capture()  # type: ignore[assignment]
    try:
        app = _app(SimpleNamespace(id=7, status='active'))
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            await client.delete(
                '/app/devices/other-0123456789abcdef', headers={'X-Invoxy-Device': 'mine-0123456789abcdef'}
            )
            await client.delete(
                '/app/devices/mine-0123456789abcdef', headers={'X-Invoxy-Device': 'mine-0123456789abcdef'}
            )
    finally:
        route.logger = monkeypatch_logger
    names = [name for name, _ in events]
    assert 'app_device_removed' in names
    assert 'app_device_removal_blocked' in names
    removed = dict(events)['app_device_removed']
    assert removed['user_id'] == 7
    assert 'hwid' not in removed, 'the raw device id is never logged'


async def test_http_a_device_that_is_no_longer_on_the_account_cannot_remove_others(panel: _Panel) -> None:
    app = _app(SimpleNamespace(id=7, status='active'))
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.delete(
            '/app/devices/other-0123456789abcdef', headers={'X-Invoxy-Device': 'removed-0123456789abcdef'}
        )
    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()['detail'] == 'current_device_not_registered'
    assert panel.deleted == []


async def test_http_a_blocked_device_may_free_a_slot_only_when_the_account_is_at_its_limit(
    panel: _Panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(SimpleNamespace(id=7, status='active'))
    headers = {'X-Invoxy-Device': 'new-device-0123456789abcdef'}
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        # Limit 2, one device registered: there is room, so the blocked device has no business here.
        not_full = await client.delete('/app/devices/mine-0123456789abcdef', headers=headers)
        assert not_full.status_code == status.HTTP_409_CONFLICT

        async def full(**kwargs: Any) -> dict[str, Any]:
            return {
                'devices': [{'hwid': 'mine-0123456789abcdef'}, {'hwid': 'second-0123456789abcdef'}],
                'total': 2,
                'device_limit': 2,
            }

        monkeypatch.setattr(route, '_panel_get_devices', full)
        freed = await client.delete('/app/devices/mine-0123456789abcdef', headers=headers)
    assert freed.status_code == status.HTTP_204_NO_CONTENT
    assert panel.deleted == ['mine-0123456789abcdef']


async def test_http_removals_of_one_account_are_serialized(panel: _Panel, fake_cache: FakeCache) -> None:
    app = _app(SimpleNamespace(id=7, status='active'))
    fake_cache.data[f'{svc.DEVICE_LOCK_PREFIX}:7'] = '1'
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        busy = await client.delete(
            '/app/devices/other-0123456789abcdef', headers={'X-Invoxy-Device': 'mine-0123456789abcdef'}
        )
    assert busy.status_code == status.HTTP_409_CONFLICT
    assert busy.json()['detail'] == 'busy'
    assert panel.deleted == []
    fake_cache.data.clear()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        await client.delete('/app/devices/other-0123456789abcdef', headers={'X-Invoxy-Device': 'mine-0123456789abcdef'})
    assert not fake_cache.data, 'the lock is released'


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> '_Rows':
        return self

    def all(self) -> list[Any]:
        return self._rows


class _TokenDb:
    def __init__(self, tokens: list[Any]) -> None:
        self.tokens = tokens
        self.committed = False

    async def execute(self, *args: Any, **kwargs: Any) -> _Rows:
        return _Rows(self.tokens)

    async def commit(self) -> None:
        self.committed = True


def _token(info: str) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(device_info=info, revoked_at=None)


@pytest.mark.anyio
async def test_revoking_an_installation_signs_out_only_its_own_session() -> None:
    from types import SimpleNamespace

    from app.services import app_client_service as svc

    device = SimpleNamespace(device_hash='abc', user_id=7, revoked_at=None)
    mine, other, legacy = _token('invoxy_app:abc'), _token('invoxy_app:zzz'), _token('invoxy_app')
    db = _TokenDb([mine, other, legacy])
    assert await svc.revoke_device(db, device) == 1
    assert mine.revoked_at is not None and device.revoked_at is not None
    assert other.revoked_at is None, 'another installation of the same person stays signed in'
    assert legacy.revoked_at is None, 'an old untagged session is touched only when no tagged one matches'
    assert db.committed


@pytest.mark.anyio
async def test_revoking_falls_back_to_untagged_sessions_of_the_account() -> None:
    from types import SimpleNamespace

    from app.services import app_client_service as svc

    device = SimpleNamespace(device_hash='abc', user_id=7, revoked_at=None)
    legacy = _token('invoxy_app')
    assert await svc.revoke_device(_TokenDb([legacy]), device) == 1
    assert legacy.revoked_at is not None


def test_session_tag_binds_the_token_to_the_installation() -> None:
    from app.services import app_client_service as svc

    assert svc.app_session_tag('device-0123456789abcdef') == f"invoxy_app:{svc.hash_device_id('device-0123456789abcdef')}"
