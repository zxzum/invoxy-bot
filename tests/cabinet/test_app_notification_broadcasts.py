from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes import admin_broadcasts
from app.cabinet.schemas.broadcasts import BroadcastPreviewRequest, CombinedBroadcastCreateRequest
from app.database.models import BroadcastHistory


def _db() -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = []
    db.execute.return_value = result
    db.add = MagicMock(side_effect=lambda item: setattr(item, 'id', 42) if isinstance(item, BroadcastHistory) else None)
    db.add_all = MagicMock()
    return db


async def test_app_broadcast_writes_filtered_safe_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    recipients = [
        SimpleNamespace(id=1, notification_settings={'news_enabled': True}),
        SimpleNamespace(id=2, notification_settings={'news_enabled': False}),
    ]
    fetch = AsyncMock(return_value=recipients)
    monkeypatch.setattr(admin_broadcasts, 'get_target_users', fetch)
    db = _db()

    response = await admin_broadcasts.create_combined_broadcast(
        request=CombinedBroadcastCreateRequest(
            channel='app', target='all', message_text='Новые тарифы уже доступны', category='news'
        ),
        admin=SimpleNamespace(id=10, username='admin'),
        db=db,
    )

    notifications = db.add_all.call_args.args[0]
    assert len(notifications) == 1
    assert notifications[0].user_id == 1
    assert notifications[0].type == 'broadcast'
    assert notifications[0].title == 'Сообщение от Invoxy VPN'
    assert notifications[0].body == 'Новые тарифы уже доступны'
    assert notifications[0].payload_json is None
    assert response.channel == 'app'
    assert response.status == 'completed'
    assert response.total_count == response.sent_count == 1
    fetch.assert_awaited_once_with(db, 'all')


async def test_app_broadcast_rejects_media(monkeypatch: pytest.MonkeyPatch) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(admin_broadcasts, 'get_target_users', fetch)

    with pytest.raises(HTTPException) as exc:
        await admin_broadcasts.create_combined_broadcast(
            request=CombinedBroadcastCreateRequest(
                channel='app',
                target='all',
                message_text='Сообщение',
                media={'type': 'photo', 'file_id': 'A' * 32},
            ),
            admin=SimpleNamespace(id=10, username='admin'),
            db=_db(),
        )

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert 'Media is not supported' in exc.value.detail
    fetch.assert_not_awaited()


async def test_app_preview_counts_after_category_preferences(monkeypatch: pytest.MonkeyPatch) -> None:
    recipients = [
        SimpleNamespace(id=1, notification_settings={'news_enabled': True}),
        SimpleNamespace(id=2, notification_settings={'news_enabled': False}),
    ]
    fetch = AsyncMock(return_value=recipients)
    monkeypatch.setattr(admin_broadcasts, 'get_target_users', fetch)

    response = await admin_broadcasts.preview_broadcast(
        request=BroadcastPreviewRequest(target='all', channel='app', category='news'),
        admin=SimpleNamespace(id=10),
        db=_db(),
    )

    assert response.model_dump() == {'target': 'all', 'count': 1}
    fetch.assert_awaited_once()


async def test_app_preview_resolves_custom_target(monkeypatch: pytest.MonkeyPatch) -> None:
    recipients = [
        SimpleNamespace(id=1, notification_settings={'promo_offers_enabled': True}),
        SimpleNamespace(id=2, notification_settings={'promo_offers_enabled': False}),
    ]
    fetch = AsyncMock(return_value=recipients)
    monkeypatch.setattr(admin_broadcasts, 'get_custom_users', fetch)
    db = _db()

    response = await admin_broadcasts.preview_broadcast(
        request=BroadcastPreviewRequest(target='custom_week', channel='app', category='promo'),
        admin=SimpleNamespace(id=10),
        db=db,
    )

    assert response.model_dump() == {'target': 'custom_week', 'count': 1}
    fetch.assert_awaited_once_with(db, 'week')


async def test_legacy_preview_without_channel_uses_existing_count(monkeypatch: pytest.MonkeyPatch) -> None:
    count = AsyncMock(return_value=12)
    recipients = AsyncMock()
    monkeypatch.setattr(admin_broadcasts, 'get_target_users_count', count)
    monkeypatch.setattr(admin_broadcasts, 'get_target_users', recipients)
    db = _db()

    response = await admin_broadcasts.preview_broadcast(
        request=BroadcastPreviewRequest(target='all'),
        admin=SimpleNamespace(id=10),
        db=db,
    )

    assert response.model_dump() == {'target': 'all', 'count': 12}
    count.assert_awaited_once_with(db, 'all')
    recipients.assert_not_awaited()
