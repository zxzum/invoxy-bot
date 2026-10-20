from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import account_linking, auth
from app.cabinet.schemas.auth import EmailRegisterRequest


@pytest.mark.asyncio
@pytest.mark.parametrize('delivered', [True, False])
async def test_verification_helper_returns_actual_delivery_result(monkeypatch, delivered):
    user = SimpleNamespace(id=7, email='new@example.com')
    db = AsyncMock()
    monkeypatch.setattr(type(auth.settings), 'is_cabinet_email_verification_enabled', lambda self: True)
    monkeypatch.setattr(auth.email_service, 'is_configured', lambda: True)
    send = MagicMock(return_value=delivered)
    monkeypatch.setattr(auth.email_service, 'send_verification_email', send)
    monkeypatch.setattr(auth, 'get_rendered_override', AsyncMock(return_value=None))
    result = await auth._issue_verification_email(db, user, language='ru', username='Test')
    assert result is delivered
    assert user.email_verification_token
    db.commit.assert_awaited_once()
    send.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('delivered', [True, False])
async def test_email_link_keeps_telegram_and_reports_delivery_failure(monkeypatch, delivered):
    user = SimpleNamespace(id=7, telegram_id=700, email=None, email_verified=False, language='ru', first_name='Test')
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: None)
    monkeypatch.setattr(auth, 'require_email_auth_enabled', AsyncMock())
    monkeypatch.setattr(auth, 'get_client_ip', lambda _: '127.0.0.1')
    monkeypatch.setattr(auth.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))
    monkeypatch.setattr(auth.disposable_email_service, 'is_disposable', lambda _: False)
    monkeypatch.setattr(type(auth.settings), 'get_admin_emails', lambda self: [])
    monkeypatch.setattr(type(auth.settings), 'is_cabinet_email_verification_enabled', lambda self: True)
    monkeypatch.setattr(auth, 'hash_password', lambda _: 'hashed')
    monkeypatch.setattr(auth, '_issue_verification_email', AsyncMock(return_value=delivered))
    request = EmailRegisterRequest(email='new@example.com', password='valid-password-123')
    if delivered:
        result = await auth.register_email(request, object(), user, db)
        assert result['message'] == 'Verification email sent'
    else:
        with pytest.raises(HTTPException) as error:
            await auth.register_email(request, object(), user, db)
        assert error.value.status_code == 503
    assert user.telegram_id == 700
    assert user.email_verified is False


@pytest.mark.asyncio
@pytest.mark.parametrize('existing_owner', [None, 8])
async def test_telegram_link_preserves_email_and_requires_merge_for_existing_owner(monkeypatch, existing_owner):
    user = SimpleNamespace(id=7, telegram_id=None, email='login@example.com', password_hash='hash', username=None, first_name=None, last_name=None)
    db = AsyncMock()
    monkeypatch.setattr(account_linking, 'get_client_ip', lambda _: '127.0.0.1')
    monkeypatch.setattr(account_linking.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))
    monkeypatch.setattr(account_linking, 'validate_telegram_init_data', lambda *a, **kw: {'id': 700, 'first_name': 'Test'})
    monkeypatch.setattr(account_linking, 'get_user_by_telegram_id', AsyncMock(return_value=SimpleNamespace(id=existing_owner) if existing_owner else None))
    monkeypatch.setattr(account_linking, 'create_merge_token', AsyncMock(return_value='merge-test-token'))
    from app.services import remnawave_resync_service

    monkeypatch.setattr(remnawave_resync_service, 'resync_user_subscriptions_with_panel', AsyncMock(return_value={'synced': 0, 'failed': 0}))
    result = await account_linking.link_telegram(account_linking.LinkTelegramRequest(init_data='signed-test'), object(), user, db)
    assert user.email == 'login@example.com' and user.password_hash == 'hash'
    if existing_owner:
        assert result.merge_required and not result.success
        assert user.telegram_id is None
        db.commit.assert_not_awaited()
    else:
        assert result.success and user.telegram_id == 700
        db.commit.assert_awaited_once()
