"""Tests for pair-code service, routes, and app banners."""

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
from app.cabinet.routes.app_banners import admin_router as admin_app_banners_router, router as app_banners_router
from app.cabinet.routes.auth import router as auth_router
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
from app.services.app_banner_service import (
    create_app_banner,
    delete_app_banner,
    update_app_banner,
)
from app.services.pair_code_service import (
    consume_pair_code,
    create_pair_code,
)
from app.utils.cache import RateLimitCache
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    SystemSetting.__table__,
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    tariff_promo_groups,
    UserPromoGroup.__table__,
    CabinetRefreshToken.__table__,
    UserRole.__table__,
    AdminRole.__table__,
)


class InMemoryCache:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> Any:
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

    async def delete(self, key: str) -> bool:
        return self._store.pop(key, None) is not None


@pytest.fixture(autouse=True)
def bypass_rate_limit(monkeypatch):
    monkeypatch.setattr(RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))


@pytest.fixture
def fake_cache(monkeypatch):
    mem = InMemoryCache()
    monkeypatch.setattr('app.services.pair_code_service.cache', mem)
    return mem


def _build_app(db: AsyncSession) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router, prefix='/cabinet')
    app.include_router(app_banners_router, prefix='/cabinet')
    app.include_router(admin_app_banners_router, prefix='/cabinet')

    async def _override_db() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_cabinet_db] = _override_db
    return app


@pytest.mark.asyncio
async def test_pair_code_service_lifecycle(fake_cache):
    """Test create_pair_code and consume_pair_code service functions."""
    code, ttl = await create_pair_code(user_id=42)
    assert len(code) == 6
    assert ttl == 300

    # Consume once
    data = await consume_pair_code(code)
    assert data is not None
    assert data['user_id'] == 42

    # Replay attempt fails
    replay = await consume_pair_code(code)
    assert replay is None


@pytest.mark.asyncio
async def test_pair_code_endpoints(monkeypatch, fake_cache):
    """Test POST /cabinet/auth/pair-code and POST /cabinet/auth/pair-code/exchange."""
    async with memory_session(monkeypatch, TABLES) as db:
        user = User(
            id=20,
            telegram_id=123456789,
            username='test_pair_user',
            first_name='PairUser',
            status=UserStatus.ACTIVE.value,
            balance_kopeks=50000,
        )
        db.add(user)
        await db.commit()

        app = _build_app(db)
        auth_token = create_access_token(user.id, user.telegram_id)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            # Generate pair code
            resp = await client.post(
                '/cabinet/auth/pair-code',
                headers={'Authorization': f'Bearer {auth_token}'},
            )
            assert resp.status_code == 200
            data = resp.json()
            code = data['code']
            assert len(code) == 6
            assert data['expires_in'] == 300

            # Exchange pair code
            exchange_resp = await client.post(
                '/cabinet/auth/pair-code/exchange',
                json={'code': code, 'device_id': 'flutter_device_1', 'device_name': 'Pixel 8'},
            )
            assert exchange_resp.status_code == 200
            auth_data = exchange_resp.json()
            assert 'access_token' in auth_data
            assert 'refresh_token' in auth_data
            assert auth_data['user']['id'] == user.id

            # Verify refresh token stored with device_info='pair_code'
            result = await db.execute(
                select(CabinetRefreshToken).where(CabinetRefreshToken.user_id == user.id)
            )
            token_record = result.scalar_one_or_none()
            assert token_record is not None
            assert token_record.device_info == 'pair_code'

            # Replay returns 410 Gone
            replay_resp = await client.post(
                '/cabinet/auth/pair-code/exchange',
                json={'code': code},
            )
            assert replay_resp.status_code == 410


@pytest.mark.asyncio
async def test_pair_code_exchange_invalid(monkeypatch, fake_cache):
    """Exchange non-existent or invalid code returns 410."""
    async with memory_session(monkeypatch, TABLES) as db:
        app = _build_app(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            resp = await client.post(
                '/cabinet/auth/pair-code/exchange',
                json={'code': 'ZZZZZZ'},
            )
            assert resp.status_code == 410


@pytest.mark.asyncio
async def test_app_banners_crud(monkeypatch):
    """Test app banners service and public list endpoint."""
    async with memory_session(monkeypatch, TABLES) as db:
        banner1 = await create_app_banner(
            db,
            {
                'title': 'Скидка на годовые тарифы',
                'text': 'Только до конца недели!',
                'type': 'promo',
                'action_url': 'https://invoxy.my/tariffs',
                'is_active': True,
                'sort_order': 1,
            },
        )
        await create_app_banner(
            db,
            {
                'title': 'Скрытый баннер',
                'text': 'Черновик',
                'type': 'info',
                'is_active': False,
                'sort_order': 2,
            },
        )

        app = _build_app(db)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            # Public endpoint returns only active
            resp = await client.get('/cabinet/app/banners')
            assert resp.status_code == 200
            items = resp.json()
            assert len(items) == 1
            assert items[0]['title'] == 'Скидка на годовые тарифы'

            # Update banner
            updated = await update_app_banner(db, banner1['id'], {'title': 'Новая скидка 30%'})
            assert updated is not None
            assert updated['title'] == 'Новая скидка 30%'

            # Delete banner
            deleted = await delete_app_banner(db, banner1['id'])
            assert deleted is True

            # Re-check public endpoint
            resp2 = await client.get('/cabinet/app/banners')
            assert resp2.status_code == 200
            assert len(resp2.json()) == 0
