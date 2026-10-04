from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.cabinet.dependencies import get_cabinet_db
from app.webapi.routes import public_subscription


class _Result:
    def __init__(self, subscription):
        self.subscription = subscription

    def scalars(self):
        return self

    def first(self):
        return self.subscription


class _Db:
    def __init__(self, subscription):
        self.subscription = subscription
        self.execute = AsyncMock(return_value=_Result(subscription))


def _app(db: _Db) -> FastAPI:
    app = FastAPI()
    app.include_router(public_subscription.router)

    async def _get_db():
        yield db

    app.dependency_overrides[get_cabinet_db] = _get_db
    return app


@pytest.mark.asyncio
async def test_public_quota_returns_only_scoped_quota(monkeypatch):
    subscription = SimpleNamespace(whitelist_traffic_limit_gb=100, whitelist_traffic_used_bytes=25 * 1024**3)
    db = _Db(subscription)
    monkeypatch.setattr(public_subscription, 'get_client_ip', lambda _request: '203.0.113.10')
    monkeypatch.setattr(public_subscription.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))
    monkeypatch.setattr(public_subscription.RateLimitCache, 'is_subject_rate_limited', AsyncMock(return_value=False))

    async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url='http://test') as client:
        response = await client.post(
            '/v1/whitelist/quota',
            headers={'Authorization': 'Bearer token_123'},
        )

    assert response.status_code == 200
    assert response.json()['status'] == 'ready'
    assert response.json()['usedBytes'] == 25 * 1024**3
    assert response.json()['limitBytes'] == 100 * 1024**3
    assert response.json()['remainingBytes'] == 75 * 1024**3
    assert response.json()['percent'] == 25.0
    assert 'username' not in response.json()
    assert 'token_123' not in str(db.execute.call_args)


@pytest.mark.asyncio
async def test_public_quota_rejects_invalid_token_before_database(monkeypatch):
    db = _Db(None)
    limiter = AsyncMock(return_value=False)
    monkeypatch.setattr(public_subscription.RateLimitCache, 'is_ip_rate_limited', limiter)

    async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url='http://test') as client:
        response = await client.post(
            '/v1/whitelist/quota',
            headers={'Authorization': 'Bearer ../../other-user'},
        )

    assert response.status_code == 401
    assert response.json() == {'status': 'unauthorized'}
    db.execute.assert_not_awaited()
    limiter.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_quota_returns_not_found_without_lte_limit(monkeypatch):
    db = _Db(SimpleNamespace(whitelist_traffic_limit_gb=0, whitelist_traffic_used_bytes=0))
    monkeypatch.setattr(public_subscription, 'get_client_ip', lambda _request: '203.0.113.10')
    monkeypatch.setattr(public_subscription.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))
    monkeypatch.setattr(public_subscription.RateLimitCache, 'is_subject_rate_limited', AsyncMock(return_value=False))

    async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url='http://test') as client:
        response = await client.post(
            '/v1/whitelist/quota',
            headers={'Authorization': 'Bearer token_123'},
        )

    assert response.status_code == 200
    assert response.json() == {'status': 'not_found'}


@pytest.mark.asyncio
async def test_public_quota_fails_closed_when_ip_limited(monkeypatch):
    db = _Db(SimpleNamespace(whitelist_traffic_limit_gb=100, whitelist_traffic_used_bytes=0))
    monkeypatch.setattr(public_subscription, 'get_client_ip', lambda _request: '203.0.113.10')
    monkeypatch.setattr(public_subscription.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=True))

    async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url='http://test') as client:
        response = await client.post(
            '/v1/whitelist/quota',
            headers={'Authorization': 'Bearer token_123'},
        )

    assert response.status_code == 429
    assert response.json() == {'status': 'rate_limited'}
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_quota_falls_back_to_remnawave_lookup(monkeypatch):
    subscription = SimpleNamespace(whitelist_traffic_limit_gb=50, whitelist_traffic_used_bytes=10 * 1024**3)
    # First execute returns None (not found by token directly), second returns subscription by panel id
    results = [_Result(None), _Result(subscription)]
    db = _Db(None)
    db.execute = AsyncMock(side_effect=results)

    monkeypatch.setattr(public_subscription, 'get_client_ip', lambda _request: '203.0.113.10')
    monkeypatch.setattr(public_subscription.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))
    monkeypatch.setattr(public_subscription.RateLimitCache, 'is_subject_rate_limited', AsyncMock(return_value=False))

    mock_api = AsyncMock()
    mock_api.get_user_by_short_uuid = AsyncMock(return_value=SimpleNamespace(id=999))
    mock_api_ctx = AsyncMock()
    mock_api_ctx.__aenter__.return_value = mock_api
    mock_api_ctx.__aexit__.return_value = None

    mock_rw_service = SimpleNamespace(_config_error=None, get_api_client=lambda: mock_api_ctx)

    monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', lambda: mock_rw_service)

    async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url='http://test') as client:
        response = await client.post(
            '/v1/whitelist/quota',
            headers={'Authorization': 'Bearer fallback_token'},
        )

    assert response.status_code == 200
    assert response.json()['status'] == 'ready'
    assert response.json()['limitBytes'] == 50 * 1024**3
    assert db.execute.await_count == 2

