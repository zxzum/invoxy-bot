from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.database.models import Subscription, Tariff, User
from app.webapi.dependencies import get_db_session
from app.webapi.routes import support_context


@pytest.fixture
def context_app(monkeypatch):
    monkeypatch.setenv('SUPPORT_API_KEY', 'support-secret')
    monkeypatch.setattr(support_context.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))
    app = FastAPI()
    app.include_router(support_context.router)
    db = object()

    async def session():
        yield db

    app.dependency_overrides[get_db_session] = session
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(support_context, 'get_user_by_telegram_id', lookup)
    return app, db, lookup


HEADERS = {'Authorization': 'Bearer support-secret', 'X-Support-Request-Id': 'test-request'}


@pytest.mark.asyncio
@pytest.mark.parametrize('authorization', ['', 'Bearer wrong', 'Basic support-secret'])
async def test_context_rejects_unauthorized_before_lookup(context_app, authorization):
    app, _db, lookup = context_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get(
            '/internal/support/client-context?telegramId=5548521968',
            headers={**HEADERS, 'Authorization': authorization},
        )
    assert response.status_code == 401
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_context_rejects_unconfigured_key(context_app, monkeypatch):
    app, _db, lookup = context_app
    monkeypatch.delenv('SUPPORT_API_KEY')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/internal/support/client-context?telegramId=123', headers=HEADERS)
    assert response.status_code == 401
    lookup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('telegram_id', ['0', '-1', '9223372036854775808', 'bad'])
async def test_context_validates_telegram_id(context_app, telegram_id):
    app, _db, lookup = context_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/internal/support/client-context', params={'telegramId': telegram_id}, headers=HEADERS)
    assert response.status_code == 422
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_context_returns_only_operator_data_and_all_subscriptions(context_app):
    app, db, lookup = context_app
    now = datetime(2026, 10, 9, tzinfo=UTC)
    user = User(
        id=42, telegram_id=5548521968, username='user', first_name='Имя', email='user@example.com',
        balance_kopeks=12345, status='active', language='ru', created_at=now, password_hash='secret-hash',
    )
    user.subscriptions = [
        Subscription(
            id=7, end_date=now, status='active', is_trial=False, tariff=Tariff(name='Premium'),
            traffic_limit_gb=100, traffic_used_gb=12.5, device_limit=3,
            subscription_url='https://secret-subscription', whitelist_traffic_limit_gb=50,
            whitelist_traffic_used_bytes=2 * 1024**3,
        ),
        Subscription(id=8, end_date=now, status='expired', is_trial=True),
    ]
    lookup.return_value = user
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/internal/support/client-context?telegramId=5548521968', headers=HEADERS)
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'private, no-store'
    payload = response.json()
    assert payload['client']['id'] == '42'
    assert payload['client']['email'] == 'user@example.com'
    assert payload['client']['balanceKopeks'] == 12345
    assert len(payload['subscriptions']) == 2
    assert payload['subscriptions'][0]['trafficUsedBytes'] == int(12.5 * 1024**3)
    assert payload['subscriptions'][0]['deviceLimit'] == 3
    assert 'secret-hash' not in response.text
    assert 'secret-subscription' not in response.text
    lookup.assert_awaited_once_with(db, 5548521968)


@pytest.mark.asyncio
async def test_context_not_found_is_explicit(context_app):
    app, _db, _lookup = context_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/internal/support/client-context?telegramId=123', headers=HEADERS)
    assert response.json() == {'found': False, 'client': None, 'subscriptions': []}


@pytest.mark.asyncio
async def test_context_rate_limit_stops_lookup(context_app, monkeypatch):
    app, _db, lookup = context_app
    monkeypatch.setattr(support_context.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=True))
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/internal/support/client-context?telegramId=123', headers=HEADERS)
    assert response.status_code == 429
    lookup.assert_not_awaited()
