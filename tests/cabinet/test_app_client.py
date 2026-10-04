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

    class Db:
        async def commit(self) -> None:
            return None

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
        assert (await client.post('/app/login/exchange', json=body)).status_code == status.HTTP_410_GONE
