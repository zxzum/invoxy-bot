"""Tests for cabinet notifications mark-as-read and read-all endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.auth import create_access_token
from app.cabinet.dependencies import get_cabinet_db
from app.cabinet.routes.notifications import router as notifications_router
from app.database.models import (
    AdminRole,
    CabinetNotification,
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
from app.utils.cache import RateLimitCache
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
    CabinetNotification.__table__,
)


@pytest.fixture(autouse=True)
def bypass_rate_limit(monkeypatch):
    """By default allow all requests through IP rate limiting."""
    monkeypatch.setattr(RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))


def _build_app(db: AsyncSession) -> FastAPI:
    app = FastAPI()
    app.include_router(notifications_router, prefix='/cabinet')

    async def _override_db() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_cabinet_db] = _override_db
    return app


@pytest.mark.asyncio
async def test_mark_single_notification_read(monkeypatch):
    """Mark single notification read sets read_at timestamp and returns success."""
    async with memory_session(monkeypatch, TABLES) as db:
        user = User(
            id=1,
            telegram_id=11111,
            username='user1',
            first_name='User One',
            status=UserStatus.ACTIVE.value,
        )
        db.add(user)
        await db.commit()

        notification = CabinetNotification(
            id=10,
            user_id=user.id,
            type='system',
            title='Test Title',
            body='Test Body',
            payload_json={'key': 'value'},
            created_at=datetime.now(UTC),
            read_at=None,
        )
        db.add(notification)
        await db.commit()

        token = create_access_token(user.id, user.telegram_id)
        app = _build_app(db)

        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            resp = await client.post(
                f'/cabinet/notifications/{notification.id}/read',
                headers={'Authorization': f'Bearer {token}'},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data['success'] is True
        assert data['id'] == notification.id
        assert data['read_at'] is not None

        # Verify database record updated
        query = select(CabinetNotification).where(CabinetNotification.id == notification.id)
        updated = (await db.execute(query)).scalar_one()
        assert updated.read_at is not None
        assert updated.read_at.isoformat() == data['read_at'] or data['read_at'].startswith(
            updated.read_at.isoformat()[:19]
        )


@pytest.mark.asyncio
async def test_mark_single_notification_read_idempotent(monkeypatch):
    """Marking an already read notification is idempotent and does not overwrite read_at."""
    async with memory_session(monkeypatch, TABLES) as db:
        user = User(
            id=1,
            telegram_id=11111,
            username='user1',
            first_name='User One',
            status=UserStatus.ACTIVE.value,
        )
        db.add(user)
        await db.commit()

        initial_read_at = datetime.now(UTC) - timedelta(hours=3)
        notification = CabinetNotification(
            id=20,
            user_id=user.id,
            type='system',
            title='Test Title',
            body='Test Body',
            payload_json=None,
            created_at=initial_read_at - timedelta(days=1),
            read_at=initial_read_at,
        )
        db.add(notification)
        await db.commit()

        token = create_access_token(user.id, user.telegram_id)
        app = _build_app(db)

        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            resp = await client.post(
                f'/cabinet/notifications/{notification.id}/read',
                headers={'Authorization': f'Bearer {token}'},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data['success'] is True
        assert data['id'] == notification.id

        # Verify read_at in DB remains untouched
        query = select(CabinetNotification).where(CabinetNotification.id == notification.id)
        current = (await db.execute(query)).scalar_one()
        assert current.read_at == initial_read_at


@pytest.mark.asyncio
async def test_mark_notification_read_404_nonexistent_or_other_user(monkeypatch):
    """Returns 404 when notification does not exist or belongs to another user."""
    async with memory_session(monkeypatch, TABLES) as db:
        user1 = User(
            id=1,
            telegram_id=11111,
            username='user1',
            first_name='User One',
            status=UserStatus.ACTIVE.value,
        )
        user2 = User(
            id=2,
            telegram_id=22222,
            username='user2',
            first_name='User Two',
            status=UserStatus.ACTIVE.value,
        )
        db.add_all([user1, user2])
        await db.commit()

        user2_notif = CabinetNotification(
            id=30,
            user_id=user2.id,
            type='promo',
            title='User 2 Promo',
            body='For User 2 only',
            created_at=datetime.now(UTC),
            read_at=None,
        )
        db.add(user2_notif)
        await db.commit()

        token1 = create_access_token(user1.id, user1.telegram_id)
        app = _build_app(db)

        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            # 1. Non-existent notification
            resp_nonexistent = await client.post(
                '/cabinet/notifications/99999/read',
                headers={'Authorization': f'Bearer {token1}'},
            )
            assert resp_nonexistent.status_code == 404

            # 2. Notification belonging to user 2 requested by user 1
            resp_other_user = await client.post(
                f'/cabinet/notifications/{user2_notif.id}/read',
                headers={'Authorization': f'Bearer {token1}'},
            )
            assert resp_other_user.status_code == 404

        # Ensure user 2's notification was NOT modified
        query = select(CabinetNotification).where(CabinetNotification.id == user2_notif.id)
        unmodified = (await db.execute(query)).scalar_one()
        assert unmodified.read_at is None


@pytest.mark.asyncio
async def test_mark_all_read(monkeypatch):
    """Mark-all-read updates all unread for user, leaves read ones and other users untouched."""
    async with memory_session(monkeypatch, TABLES) as db:
        user1 = User(
            id=1,
            telegram_id=11111,
            username='user1',
            first_name='User One',
            status=UserStatus.ACTIVE.value,
        )
        user2 = User(
            id=2,
            telegram_id=22222,
            username='user2',
            first_name='User Two',
            status=UserStatus.ACTIVE.value,
        )
        db.add_all([user1, user2])
        await db.commit()

        u1_initial_read = datetime.now(UTC) - timedelta(days=2)
        notif1 = CabinetNotification(
            id=101,
            user_id=user1.id,
            type='info',
            title='U1 Unread 1',
            body='Body 1',
            created_at=datetime.now(UTC),
            read_at=None,
        )
        notif2 = CabinetNotification(
            id=102,
            user_id=user1.id,
            type='info',
            title='U1 Unread 2',
            body='Body 2',
            created_at=datetime.now(UTC),
            read_at=None,
        )
        notif3 = CabinetNotification(
            id=103,
            user_id=user1.id,
            type='info',
            title='U1 Already Read',
            body='Body 3',
            created_at=datetime.now(UTC),
            read_at=u1_initial_read,
        )
        notif_other = CabinetNotification(
            id=104,
            user_id=user2.id,
            type='info',
            title='U2 Unread',
            body='Body 4',
            created_at=datetime.now(UTC),
            read_at=None,
        )
        db.add_all([notif1, notif2, notif3, notif_other])
        await db.commit()

        token1 = create_access_token(user1.id, user1.telegram_id)
        app = _build_app(db)

        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            resp = await client.post(
                '/cabinet/notifications/read-all',
                headers={'Authorization': f'Bearer {token1}'},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data['success'] is True
        assert data['updated_count'] == 2

        # Check DB state
        q1 = select(CabinetNotification).where(CabinetNotification.id == notif1.id)
        r1 = (await db.execute(q1)).scalar_one()
        assert r1.read_at is not None

        q2 = select(CabinetNotification).where(CabinetNotification.id == notif2.id)
        r2 = (await db.execute(q2)).scalar_one()
        assert r2.read_at is not None

        q3 = select(CabinetNotification).where(CabinetNotification.id == notif3.id)
        r3 = (await db.execute(q3)).scalar_one()
        assert r3.read_at == u1_initial_read

        q4 = select(CabinetNotification).where(CabinetNotification.id == notif_other.id)
        r4 = (await db.execute(q4)).scalar_one()
        assert r4.read_at is None

        # Calling read-all a second time should update 0 rows
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            resp_again = await client.post(
                '/cabinet/notifications/read-all',
                headers={'Authorization': f'Bearer {token1}'},
            )
        assert resp_again.status_code == 200
        assert resp_again.json()['updated_count'] == 0


@pytest.mark.asyncio
async def test_notifications_endpoints_unauthenticated(monkeypatch):
    """Unauthenticated requests to read and read-all endpoints return 401."""
    async with memory_session(monkeypatch, TABLES) as db:
        app = _build_app(db)

        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            resp_single = await client.post('/cabinet/notifications/1/read')
            assert resp_single.status_code == 401

            resp_all = await client.post('/cabinet/notifications/read-all')
            assert resp_all.status_code == 401
