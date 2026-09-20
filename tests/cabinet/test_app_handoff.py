"""Tests for backend app handoff token service and routes.

Covers:
- create_app_handoff_token service
- consume_app_handoff_token service (atomic consumption, single-use, audience verification)
- POST /cabinet/auth/app-link endpoint (authenticated, generates opaque token)
- POST /cabinet/auth/app-link/exchange endpoint (exchanges token for AuthResponse, device_info='app_link')
- Replay attempt (410)
- Expired/invalid token (410)
- Audience mismatch: web_auth token on app-link endpoint (410)
- Inactive user rejection (403)
- Rate limiting (429)
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.auth import create_access_token
from app.cabinet.dependencies import get_cabinet_db
from app.cabinet.routes.auth import router as auth_router
from app.cabinet.schemas.auth import (
    AppHandoffExchangeRequest,
    AppHandoffRequest,
    AppHandoffResponse,
)
from app.database.models import (
    AdminRole,
    CabinetRefreshToken,
    PromoGroup,
    Subscription,
    SystemSetting,
    Tariff,
    User,
    UserPromoGroup,
    UserRole,
    UserStatus,
    tariff_promo_groups,
)
from app.services.web_auth_service import (
    consume_app_handoff_token,
    create_app_handoff_token,
    create_web_auth_token,
    link_web_auth_token,
)
from app.utils.cache import RateLimitCache, cache_key
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    SystemSetting.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    tariff_promo_groups,
    UserPromoGroup.__table__,
    Subscription.__table__,
    User.__table__,
    CabinetRefreshToken.__table__,
    AdminRole.__table__,
    UserRole.__table__,
)


class InMemoryCache:
    """In-memory Redis cache mock with atomic GETDEL semantics."""

    def __init__(self):
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> Any | None:
        raw = self._store.get(key)
        if raw is not None:
            return json.loads(raw)
        return None

    async def set(self, key: str, value: Any, expire: int | None = None) -> bool:
        self._store[key] = json.dumps(value, default=str)
        return True

    async def getdel(self, key: str) -> Any | None:
        raw = self._store.pop(key, None)
        if raw is not None:
            return json.loads(raw)
        return None


@pytest.fixture(autouse=True)
def bypass_rate_limit(monkeypatch):
    """By default allow all requests through IP rate limiting."""
    monkeypatch.setattr(RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))


@pytest.fixture
def fake_cache(monkeypatch):
    """Provide an in-memory cache for web_auth and handoff tokens."""
    mem_cache = InMemoryCache()
    monkeypatch.setattr('app.services.web_auth_service.cache', mem_cache)
    return mem_cache


def _build_app(db: AsyncSession) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router, prefix='/cabinet')

    async def _override_db() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_cabinet_db] = _override_db
    return app


# ==================== Schema Unit Tests ====================


def test_app_handoff_schemas():
    """Verify AppHandoff schemas validate correctly."""
    req = AppHandoffExchangeRequest(token='a' * 32, device_id='phone-1')
    assert req.token == 'a' * 32
    assert req.device_id == 'phone-1'

    # AppHandoffRequest is an alias
    req_alias = AppHandoffRequest(token='b' * 32)
    assert req_alias.token == 'b' * 32
    assert req_alias.device_id is None

    resp = AppHandoffResponse(url='https://invoxy.my/app/connect?token=xyz', token_expires_in=120)
    assert resp.url == 'https://invoxy.my/app/connect?token=xyz'
    assert resp.token_expires_in == 120


# ==================== Service Unit Tests ====================


@pytest.mark.asyncio
async def test_create_app_handoff_token_service(fake_cache):
    """create_app_handoff_token generates token with purpose='app_login' and status='linked'."""
    token = await create_app_handoff_token(user_id=42)

    assert isinstance(token, str)
    assert len(token) >= 16

    key = cache_key('web_auth', token)
    stored = await fake_cache.get(key)
    assert stored is not None
    assert stored['user_id'] == 42
    assert stored['purpose'] == 'app_login'
    assert stored['status'] == 'linked'


@pytest.mark.asyncio
async def test_consume_app_handoff_token_success_and_single_use(fake_cache):
    """consume_app_handoff_token atomically consumes token and prevents reuse (replay)."""
    token = await create_app_handoff_token(user_id=42)

    # First consumption succeeds
    data = await consume_app_handoff_token(token)
    assert data is not None
    assert data['user_id'] == 42
    assert data['purpose'] == 'app_login'
    assert data['status'] == 'linked'

    # Second consumption returns None (single-use / replay protection)
    replay_data = await consume_app_handoff_token(token)
    assert replay_data is None


@pytest.mark.asyncio
async def test_consume_app_handoff_token_expired(fake_cache):
    """consume_app_handoff_token on unknown or expired token returns None."""
    data = await consume_app_handoff_token('non-existent-or-expired-token-123')
    assert data is None


@pytest.mark.asyncio
async def test_consume_app_handoff_token_audience_mismatch(fake_cache):
    """consume_app_handoff_token rejects token created with purpose='web_auth'."""
    web_token = await create_web_auth_token()

    # Attempt to consume web_auth token as app handoff token must fail
    data = await consume_app_handoff_token(web_token)
    assert data is None


# ==================== HTTP Route Tests ====================


@pytest.mark.asyncio
async def test_app_link_create_unauthenticated(monkeypatch):
    """POST /cabinet/auth/app-link without auth header returns 401."""
    async with memory_session(monkeypatch, TABLES) as db:
        app = _build_app(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            resp = await client.post('/cabinet/auth/app-link')
            assert resp.status_code == 401


@pytest.mark.asyncio
async def test_app_link_create_authenticated(monkeypatch, fake_cache):
    """POST /cabinet/auth/app-link with valid session returns connect URL and 120s expiry."""
    async with memory_session(monkeypatch, TABLES) as db:
        user = User(
            id=10,
            telegram_id=123456,
            username='app_tester',
            first_name='Tester',
            status=UserStatus.ACTIVE.value,
        )
        db.add(user)
        await db.commit()

        app = _build_app(db)
        token = create_access_token(user.id, user.telegram_id)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            resp = await client.post(
                '/cabinet/auth/app-link',
                headers={'Authorization': f'Bearer {token}'},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert 'url' in data
            assert data['url'].startswith('https://invoxy.my/app/connect?token=')
            assert data['token_expires_in'] == 120

            # Verify token payload stored in cache
            extracted_token = data['url'].split('token=')[1]
            cached_data = await fake_cache.get(cache_key('web_auth', extracted_token))
            assert cached_data is not None
            assert cached_data['user_id'] == 10
            assert cached_data['purpose'] == 'app_login'


@pytest.mark.asyncio
async def test_app_link_exchange_success(monkeypatch, fake_cache):
    """POST /cabinet/auth/app-link/exchange returns AuthResponse and stores device_info='app_link'."""
    async with memory_session(monkeypatch, TABLES) as db:
        user = User(
            id=15,
            telegram_id=654321,
            username='exchange_user',
            first_name='Exchange',
            status=UserStatus.ACTIVE.value,
        )
        db.add(user)
        await db.commit()

        token = await create_app_handoff_token(user_id=user.id)

        app = _build_app(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            resp = await client.post(
                '/cabinet/auth/app-link/exchange',
                json={'token': token, 'device_id': 'device-mobile-xyz'},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert 'access_token' in body
            assert 'refresh_token' in body
            assert body['token_type'] == 'bearer'
            assert body['user']['id'] == user.id

            # Verify refresh token in DB was stored with device_info='app_link'
            refresh_stmt = select(CabinetRefreshToken).where(CabinetRefreshToken.user_id == user.id)
            db_tokens = (await db.execute(refresh_stmt)).scalars().all()
            assert len(db_tokens) == 1
            assert db_tokens[0].device_info == 'app_link'


@pytest.mark.asyncio
async def test_app_link_exchange_replay_attempt_410(monkeypatch, fake_cache):
    """Exchanging the same token twice returns 410 on second attempt."""
    async with memory_session(monkeypatch, TABLES) as db:
        user = User(
            id=20,
            telegram_id=777,
            username='replay_user',
            first_name='Replay',
            status=UserStatus.ACTIVE.value,
        )
        db.add(user)
        await db.commit()

        token = await create_app_handoff_token(user_id=user.id)

        app = _build_app(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            # First exchange succeeds
            resp1 = await client.post(
                '/cabinet/auth/app-link/exchange',
                json={'token': token},
            )
            assert resp1.status_code == 200

            # Replay attempt fails with 410
            resp2 = await client.post(
                '/cabinet/auth/app-link/exchange',
                json={'token': token},
            )
            assert resp2.status_code == 410


@pytest.mark.asyncio
async def test_app_link_exchange_expired_or_invalid_token_410(monkeypatch, fake_cache):
    """Exchanging invalid/expired token returns 410."""
    async with memory_session(monkeypatch, TABLES) as db:
        app = _build_app(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            resp = await client.post(
                '/cabinet/auth/app-link/exchange',
                json={'token': 'non_existent_token_1234567890'},
            )
            assert resp.status_code == 410


@pytest.mark.asyncio
async def test_app_link_exchange_audience_mismatch_410(monkeypatch, fake_cache):
    """Exchanging a web_auth token on app-link/exchange returns 410."""
    async with memory_session(monkeypatch, TABLES) as db:
        user = User(
            id=25,
            telegram_id=888,
            username='audience_user',
            first_name='Audience',
            status=UserStatus.ACTIVE.value,
        )
        db.add(user)
        await db.commit()

        web_token = await create_web_auth_token()

        app = _build_app(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            resp = await client.post(
                '/cabinet/auth/app-link/exchange',
                json={'token': web_token},
            )
            assert resp.status_code == 410


@pytest.mark.asyncio
async def test_app_link_exchange_inactive_user_403(monkeypatch, fake_cache):
    """Exchanging handoff token for an inactive/blocked user returns 403."""
    async with memory_session(monkeypatch, TABLES) as db:
        user = User(
            id=30,
            telegram_id=999,
            username='blocked_user',
            first_name='Blocked',
            status=UserStatus.BLOCKED.value,
        )
        db.add(user)
        await db.commit()

        token = await create_app_handoff_token(user_id=user.id)

        app = _build_app(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            resp = await client.post(
                '/cabinet/auth/app-link/exchange',
                json={'token': token},
            )
            assert resp.status_code == 403
            assert resp.json()['detail'] == 'Account is deactivated'


@pytest.mark.asyncio
async def test_app_link_exchange_rate_limited_429(monkeypatch, fake_cache):
    """Rate limited client IP returns 429."""
    async with memory_session(monkeypatch, TABLES) as db:
        monkeypatch.setattr(RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=True))

        app = _build_app(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            resp = await client.post(
                '/cabinet/auth/app-link/exchange',
                json={'token': 'some_token_1234567890'},
            )
            assert resp.status_code == 429


@pytest.mark.asyncio
async def test_deeplink_request_and_poll_backward_compatibility(monkeypatch, fake_cache):
    """Existing /deeplink/request and /deeplink/poll flow remains intact."""
    async with memory_session(monkeypatch, TABLES) as db:
        user = User(
            id=50,
            telegram_id=55555,
            username='deeplink_user',
            first_name='Deeplink',
            status=UserStatus.ACTIVE.value,
        )
        db.add(user)
        await db.commit()

        monkeypatch.setattr('app.config.settings.BOT_USERNAME', 'invoxy_bot')

        app = _build_app(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            # 1. Request token
            req_resp = await client.post('/cabinet/auth/deeplink/request')
            assert req_resp.status_code == 200
            req_data = req_resp.json()
            token = req_data['token']
            assert req_data['bot_username'] == 'invoxy_bot'
            assert req_data['expires_in'] == 300

            # 2. Poll pending
            poll_resp1 = await client.post('/cabinet/auth/deeplink/poll', json={'token': token})
            assert poll_resp1.status_code == 202

            # 3. Simulate bot linking token
            linked = await link_web_auth_token(token=token, telegram_id=user.telegram_id, user_id=user.id)
            assert linked is True

            # 4. Poll completed
            poll_resp2 = await client.post('/cabinet/auth/deeplink/poll', json={'token': token})
            assert poll_resp2.status_code == 200
            assert poll_resp2.json()['user']['id'] == user.id

            # 5. Replay poll gives 410
            poll_resp3 = await client.post('/cabinet/auth/deeplink/poll', json={'token': token})
            assert poll_resp3.status_code == 410


@pytest.mark.asyncio
async def test_deeplink_poll_rejects_app_login_token(monkeypatch, fake_cache):
    """POST /cabinet/auth/deeplink/poll rejects tokens with purpose='app_login'."""
    async with memory_session(monkeypatch, TABLES) as db:
        app_token = await create_app_handoff_token(user_id=99)

        app = _build_app(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            resp = await client.post('/cabinet/auth/deeplink/poll', json={'token': app_token})
            assert resp.status_code == 410
