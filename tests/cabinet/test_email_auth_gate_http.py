"""Сквозная проверка гейта email-входа через настоящее FastAPI-приложение.

Юнит-тесты зовут обработчики напрямую с болванками; здесь запрос проходит роутер,
зависимости и сессию к настоящей (in-memory) БД — как curl из багрепорта.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.auth import email_auth_gate as gate
from app.cabinet.dependencies import get_cabinet_db
from app.cabinet.routes.auth import router as auth_router
from app.database.models import PromoGroup, SystemSetting, User
from tests.fixtures.sqlite_memory import memory_session


TABLES = (SystemSetting.__table__, PromoGroup.__table__, User.__table__)

REGISTER_BODY = {
    'email': 'test@example.org',
    'password': 'Str0ng-Passw0rd!',
    'first_name': 'Test',
    'accepted_legal_documents': ['public_offer', 'privacy_policy'],
}
LOGIN_BODY = {'email': 'test@example.org', 'password': 'Str0ng-Passw0rd!'}


def _app(db: AsyncSession) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router, prefix='/cabinet')

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_cabinet_db] = _db
    return app


async def _set_flag(db: AsyncSession, value: str) -> None:
    db.add(SystemSetting(key=gate.EMAIL_AUTH_ENABLED_KEY, value=value))
    await db.commit()


async def _users_count(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(User))).scalar_one()


@pytest.mark.asyncio
async def test_disabled_in_admin_refuses_register_and_login_over_http(monkeypatch):
    """Админ выключил email-вход в кабинете (строка в БД), окружение говорит «включён»:
    оба curl из отчёта получают 403 с кодом, пользователь не создаётся."""
    async with memory_session(monkeypatch, TABLES) as db:
        monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', True)
        await _set_flag(db, 'false')

        async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url='http://cabinet') as client:
            register = await client.post('/cabinet/auth/email/register/standalone', json=REGISTER_BODY)
            login = await client.post('/cabinet/auth/email/login', json=LOGIN_BODY)

        assert register.status_code == 403, register.text
        assert register.json()['detail']['code'] == gate.EMAIL_AUTH_DISABLED_CODE
        assert login.status_code == 403, login.text
        assert login.json()['detail']['code'] == gate.EMAIL_AUTH_DISABLED_CODE
        assert await _users_count(db) == 0


@pytest.mark.asyncio
async def test_enabled_in_admin_lets_request_through_the_gate(monkeypatch):
    """Обратная сторона: строка 'true' в БД при выключенном окружении — запрос проходит гейт
    и упирается уже в обычную проверку пароля (401), а не в 403."""
    from app.cabinet.routes import auth

    async def not_limited(*_args, **_kwargs):
        return False

    async with memory_session(monkeypatch, TABLES) as db:
        monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', False)
        monkeypatch.setattr(auth.RateLimitCache, 'is_ip_rate_limited', not_limited)
        await _set_flag(db, 'true')

        async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url='http://cabinet') as client:
            login = await client.post('/cabinet/auth/email/login', json=LOGIN_BODY)

        assert login.status_code == 401, login.text
        assert login.json()['detail'] == 'Invalid email or password'


@pytest.mark.asyncio
async def test_configured_test_email_login_auto_creates_verified_user_over_http(monkeypatch):
    """The staging login seam creates a verified email user without SMTP."""
    from app.cabinet.routes import auth

    async def not_limited(*_args, **_kwargs):
        return False

    async def no_roles(*_args, **_kwargs):
        return [], [], 0

    async def no_bonus(*_args, **_kwargs):
        return None

    async def no_store(*_args, **_kwargs):
        return None

    monkeypatch.setattr(gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', True)
    monkeypatch.setattr(gate.settings, 'TEST_EMAIL', 'staging@example.org')
    monkeypatch.setattr(gate.settings, 'TEST_EMAIL_PASSWORD', 'Staging-Passw0rd!')
    monkeypatch.setattr(gate.settings, 'CABINET_JWT_SECRET', 'staging-jwt-secret-' + ('x' * 64))
    monkeypatch.setattr(auth.RateLimitCache, 'is_ip_rate_limited', not_limited)
    monkeypatch.setattr(auth, 'ensure_superadmin_role_on_login', no_roles)
    monkeypatch.setattr(auth.UserRoleCRUD, 'get_user_permissions', no_roles)
    monkeypatch.setattr(auth, '_process_campaign_bonus', no_bonus)
    monkeypatch.setattr(auth, '_store_refresh_token', no_store)

    async with memory_session(monkeypatch, TABLES) as db:
        await _set_flag(db, 'true')

        async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url='http://cabinet') as client:
            response = await client.post(
                '/cabinet/auth/email/login',
                json={'email': 'STAGING@example.org', 'password': 'Staging-Passw0rd!'},
            )

        assert response.status_code == 200, response.text
        assert response.json()['user']['email'].lower() == 'staging@example.org'
        assert response.json()['user']['email_verified'] is True
        assert response.json()['access_token']
        assert await _users_count(db) == 1
