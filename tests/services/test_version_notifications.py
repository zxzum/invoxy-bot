from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.services import version_service as module
from app.services.version_service import VersionInfo, VersionService


def release(tag='v4.8.0', body='New optional UI feature'):
    return VersionInfo(tag, '2026-10-01T00:00:00Z', tag, body)


@pytest.fixture
def service(monkeypatch):
    svc = VersionService()
    svc.repo = 'test/bot'
    svc._notification_service = AsyncMock()
    svc._notification_service.send_version_update_notification.return_value = True
    monkeypatch.setattr(module.cache, '_connected', True)
    store = {}

    async def get(key):
        return store.get(key)

    async def setnx(key, value, expire):
        assert expire == 48 * 3600
        if key in store:
            return False
        store[key] = value
        return True

    async def delete(key):
        store.pop(key, None)

    monkeypatch.setattr(module.cache, 'get', get)
    monkeypatch.setattr(module.cache, 'setnx', setnx)
    monkeypatch.setattr(module.cache, 'delete', delete)
    return svc, store


@pytest.mark.asyncio
async def test_routine_releases_share_persistent_48_hour_limit(service):
    svc, store = service
    await svc._send_update_notification([release()])
    svc._cache.clear()  # Simulate process restart/release cache refresh.
    await svc._send_update_notification([release('v4.9.0')])
    svc._notification_service.send_version_update_notification.assert_awaited_once()
    assert list(store) == ['version_notification:test/bot:routine']


@pytest.mark.asyncio
async def test_new_security_release_bypasses_routine_limit(service):
    svc, _ = service
    await svc._send_update_notification([release()])
    await svc._send_update_notification([release('v4.9.0', 'Закрыта уязвимость OIDC')])
    await svc._send_update_notification([release('v4.9.0', 'Закрыта уязвимость OIDC')])
    assert svc._notification_service.send_version_update_notification.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('raises', [False, True])
async def test_failed_delivery_does_not_consume_limit(service, raises):
    svc, store = service
    send = svc._notification_service.send_version_update_notification
    send.return_value = False
    if raises:
        send.side_effect = RuntimeError('Telegram unavailable')
    await svc._send_update_notification([release()])
    assert not store
    send.side_effect = None
    send.return_value = True
    await svc._send_update_notification([release()])
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_without_redis_uses_local_48_hour_limit(service, monkeypatch):
    svc, store = service
    monkeypatch.setattr(module.cache, '_connected', False)
    await svc._send_update_notification([release()])
    store.clear()
    await svc._send_update_notification([release()])
    svc._notification_service.send_version_update_notification.assert_awaited_once()
    svc._cache['version_notification:test/bot:routine'] = datetime.now(UTC) - timedelta(seconds=1)
    await svc._send_update_notification([release()])
    assert svc._notification_service.send_version_update_notification.await_count == 2


@pytest.mark.asyncio
async def test_update_comparison_uses_upstream_base_not_custom_version(service):
    svc, _ = service
    svc.current_version = '6.0.0'
    svc.upstream_version = '4.7.1'
    svc._fetch_releases = AsyncMock(return_value=[release()])
    updates, releases = await svc.check_for_updates(force=True)
    assert updates
    assert len(releases) == 1
    svc._notification_service.send_version_update_notification.assert_not_awaited()
