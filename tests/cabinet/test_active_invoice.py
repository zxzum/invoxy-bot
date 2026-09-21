"""Tests for single active invoice per user and cabinet notifications.

Covers:
- Invoice active state checking and 30-minute expiration.
- Same vs different payment intent detection (topup and tariff).
- POST /cabinet/balance/topup idempotency and 409 active_invoice_exists.
- POST /cabinet/subscription/purchase-tariff/invoice idempotency and 409.
- POST /cabinet/balance/pending-payments/{method}/{id}/cancel behavior.
- GET /cabinet/notifications/history database-backed query.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import app_banners as app_banners_route
from app.cabinet.routes import balance as balance_route, notifications as notifications_route
from app.cabinet.routes.app_banners import AppBannerCreateRequest
from app.cabinet.routes.subscription_modules import purchase as purchase_route
from app.cabinet.schemas.balance import TopUpRequest
from app.cabinet.schemas.subscription import TariffInvoiceRequest
from app.cabinet.services.active_invoice import (
    cancel_pending_payment,
    is_invoice_active,
    is_status_cancelled,
    same_tariff_intent,
    same_topup_intent,
)
from app.database.models import CabinetNotification, PaymentMethod, User
from app.services.payment_verification_service import PendingPayment


def _make_dummy_user(id_: int = 1, telegram_id: int = 123456) -> User:
    u = User(id=id_, telegram_id=telegram_id, username='testuser')
    u.balance_kopeks = 0
    return u


def _make_pending_payment(
    *,
    user: User,
    local_id: int = 101,
    method: PaymentMethod = PaymentMethod.YOOKASSA,
    amount_kopeks: int = 50000,
    status: str = 'pending',
    is_paid: bool = False,
    minutes_ago: int = 5,
    description: str = 'Пополнение баланса',
) -> PendingPayment:
    created_at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    payment_obj = SimpleNamespace(
        id=local_id,
        user_id=user.id,
        amount_kopeks=amount_kopeks,
        status=status,
        is_paid=is_paid,
        created_at=created_at,
        description=description,
        confirmation_url='https://pay.example.com/checkout/101',
    )
    return PendingPayment(
        method=method,
        local_id=local_id,
        identifier=f'inv_{local_id}',
        amount_kopeks=amount_kopeks,
        status=status,
        is_paid=is_paid,
        created_at=created_at,
        expires_at=None,
        user=user,
        payment=payment_obj,
    )


# ============ Unit tests for helper logic ============


def test_is_invoice_active():
    user = _make_dummy_user()
    now = datetime.now(UTC)

    # Active fresh pending invoice
    fresh = _make_pending_payment(user=user, minutes_ago=5, status='pending', is_paid=False)
    assert is_invoice_active(fresh, now=now) is True

    # Expired (> 30 min)
    old = _make_pending_payment(user=user, minutes_ago=31, status='pending', is_paid=False)
    assert is_invoice_active(old, now=now) is False

    # Already paid
    paid = _make_pending_payment(user=user, minutes_ago=5, status='succeeded', is_paid=True)
    assert is_invoice_active(paid, now=now) is False

    # Cancelled statuses
    for st in ('canceled', 'cancelled', 'fail', 'failed', 'declined', 'expired'):
        assert is_status_cancelled(st) is True
        c = _make_pending_payment(user=user, minutes_ago=5, status=st, is_paid=False)
        assert is_invoice_active(c, now=now) is False


def test_same_intent_topup():
    user = _make_dummy_user()
    record = _make_pending_payment(
        user=user,
        method=PaymentMethod.YOOKASSA,
        amount_kopeks=50000,
        description='Пополнение баланса',
    )

    # Exact match
    assert same_topup_intent(record, method='yookassa', amount_kopeks=50000) is True

    # Different amount
    assert same_topup_intent(record, method='yookassa', amount_kopeks=30000) is False

    # Different method
    assert same_topup_intent(record, method='cryptobot', amount_kopeks=50000) is False


def test_same_intent_tariff():
    user = _make_dummy_user()
    record = _make_pending_payment(
        user=user,
        method=PaymentMethod.YOOKASSA,
        amount_kopeks=40000,
        description='Оплата тарифа «Премиум» (30 дн.)',
    )

    assert (
        same_tariff_intent(
            record,
            method='yookassa',
            missing_kopeks=40000,
            tariff_id=1,
            period_days=30,
            tariff_name='Премиум',
        )
        is True
    )

    # Different amount
    assert (
        same_tariff_intent(
            record,
            method='yookassa',
            missing_kopeks=30000,
            tariff_id=1,
            period_days=30,
            tariff_name='Премиум',
        )
        is False
    )

    # Topup payment passed to tariff check
    topup_record = _make_pending_payment(
        user=user,
        method=PaymentMethod.YOOKASSA,
        amount_kopeks=40000,
        description='Пополнение баланса',
    )
    assert (
        same_tariff_intent(
            topup_record,
            method='yookassa',
            missing_kopeks=40000,
            tariff_id=1,
            period_days=30,
        )
        is False
    )


# ============ Endpoint tests: create_topup ============


@pytest.mark.asyncio
async def test_create_topup_first_time(monkeypatch):
    user = _make_dummy_user()
    db = AsyncMock()

    # Mock payment methods
    fake_method = SimpleNamespace(
        id='yookassa',
        name='ЮKassa',
        is_available=True,
        min_amount_kopeks=1000,
        max_amount_kopeks=10000000,
    )
    monkeypatch.setattr(
        balance_route, 'get_payment_methods', AsyncMock(return_value=[fake_method])
    )
    # No active invoice
    monkeypatch.setattr(
        balance_route, 'get_active_invoice_record', AsyncMock(return_value=None)
    )
    # Mock link creation
    monkeypatch.setattr(
        balance_route,
        '_create_payment_link',
        AsyncMock(return_value=('https://pay.yookassa.ru/test', 'yk_123')),
    )
    mock_notif = AsyncMock()
    monkeypatch.setattr(balance_route, 'record_cabinet_notification', mock_notif)
    mock_tg = AsyncMock()
    monkeypatch.setattr(balance_route, 'send_invoice_created_telegram_message', mock_tg)

    req = TopUpRequest(payment_method='yookassa', amount_kopeks=50000)
    resp = await balance_route.create_topup(request=req, user=user, db=db)

    assert resp.payment_url == 'https://pay.yookassa.ru/test'
    assert resp.payment_id == 'yk_123'
    assert resp.amount_kopeks == 50000
    assert resp.expires_at is not None
    assert (resp.expires_at - datetime.now(UTC)).total_seconds() > 1700

    mock_notif.assert_awaited_once()
    mock_tg.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_topup_same_intent_idempotent(monkeypatch):
    user = _make_dummy_user()
    db = AsyncMock()

    fake_method = SimpleNamespace(
        id='yookassa',
        name='ЮKassa',
        is_available=True,
        min_amount_kopeks=1000,
        max_amount_kopeks=10000000,
    )
    monkeypatch.setattr(
        balance_route, 'get_payment_methods', AsyncMock(return_value=[fake_method])
    )

    existing = _make_pending_payment(
        user=user,
        method=PaymentMethod.YOOKASSA,
        amount_kopeks=50000,
        description='Пополнение баланса',
    )
    monkeypatch.setattr(
        balance_route, 'get_active_invoice_record', AsyncMock(return_value=existing)
    )
    create_link_mock = AsyncMock()
    monkeypatch.setattr(balance_route, '_create_payment_link', create_link_mock)

    req = TopUpRequest(payment_method='yookassa', amount_kopeks=50000)
    resp = await balance_route.create_topup(request=req, user=user, db=db)

    assert resp.payment_url == 'https://pay.example.com/checkout/101'
    assert resp.amount_kopeks == 50000
    create_link_mock.assert_not_called()


@pytest.mark.asyncio
async def test_create_topup_different_intent_raises_409(monkeypatch):
    user = _make_dummy_user()
    db = AsyncMock()

    fake_method = SimpleNamespace(
        id='yookassa',
        name='ЮKassa',
        is_available=True,
        min_amount_kopeks=1000,
        max_amount_kopeks=10000000,
    )
    monkeypatch.setattr(
        balance_route, 'get_payment_methods', AsyncMock(return_value=[fake_method])
    )

    existing = _make_pending_payment(
        user=user,
        method=PaymentMethod.YOOKASSA,
        amount_kopeks=50000,
        description='Пополнение баланса',
    )
    monkeypatch.setattr(
        balance_route, 'get_active_invoice_record', AsyncMock(return_value=existing)
    )

    req = TopUpRequest(payment_method='yookassa', amount_kopeks=80000)
    with pytest.raises(HTTPException) as exc_info:
        await balance_route.create_topup(request=req, user=user, db=db)

    assert exc_info.value.status_code == 409
    detail = exc_info.value.detail
    assert detail['code'] == 'active_invoice_exists'
    assert 'payment' in detail
    assert detail['payment']['amount_kopeks'] == 50000


# ============ Cancel tests ============


@pytest.mark.asyncio
async def test_cancel_pending_payment_success(monkeypatch):
    user = _make_dummy_user()
    db = AsyncMock()

    existing = _make_pending_payment(user=user, local_id=101, status='pending')
    monkeypatch.setattr(
        'app.cabinet.services.active_invoice.get_payment_record',
        AsyncMock(return_value=existing),
    )
    mock_notif = AsyncMock()
    monkeypatch.setattr(
        'app.cabinet.services.active_invoice.record_cabinet_notification',
        mock_notif,
    )
    mock_tg = AsyncMock()
    monkeypatch.setattr(
        'app.cabinet.services.active_invoice.send_invoice_cancelled_telegram_message',
        mock_tg,
    )

    cancelled = await cancel_pending_payment(
        db=db,
        user=user,
        method='yookassa',
        payment_id=101,
    )

    assert cancelled.status == 'canceled'
    assert existing.payment.status == 'canceled'
    mock_notif.assert_awaited_once()
    mock_tg.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_pending_payment_checks_provider_before_cancelling(monkeypatch):
    user = _make_dummy_user()
    db = AsyncMock()
    pending = _make_pending_payment(user=user, local_id=101, status='pending')
    paid = _make_pending_payment(user=user, local_id=101, status='succeeded', is_paid=True)

    monkeypatch.setattr(balance_route, 'get_payment_record', AsyncMock(return_value=pending))
    monkeypatch.setattr(balance_route, '_is_checkable', lambda record: True)
    monkeypatch.setattr(balance_route, 'create_bot', lambda: SimpleNamespace(session=SimpleNamespace(close=AsyncMock())))
    monkeypatch.setattr(balance_route, 'PaymentService', lambda bot: object())
    monkeypatch.setattr(balance_route, 'run_manual_check', AsyncMock(return_value=paid))
    cancel_mock = AsyncMock(side_effect=AssertionError('paid invoice must not be cancelled'))
    monkeypatch.setattr(balance_route, 'cancel_pending_payment', cancel_mock)

    response = await balance_route.cancel_user_pending_payment(
        method='yookassa', payment_id=101, user=user, db=db
    )

    assert response.is_paid is True
    assert response.status == 'succeeded'
    cancel_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_banner_create_commits(monkeypatch):
    db = AsyncMock()
    banner = {'id': 'banner-1', 'title': 'Test', 'text': ''}
    monkeypatch.setattr(app_banners_route, 'create_app_banner', AsyncMock(return_value=banner))

    response = await app_banners_route.create_new_app_banner(
        request=AppBannerCreateRequest(title='Test'),
        admin=_make_dummy_user(),
        db=db,
    )

    assert response == banner
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_other_user_payment_404(monkeypatch):
    user1 = _make_dummy_user(id_=1)
    user2 = _make_dummy_user(id_=2)
    db = AsyncMock()

    existing = _make_pending_payment(user=user2, local_id=101)
    monkeypatch.setattr(
        'app.cabinet.services.active_invoice.get_payment_record',
        AsyncMock(return_value=existing),
    )

    with pytest.raises(HTTPException) as exc_info:
        await cancel_pending_payment(
            db=db,
            user=user1,
            method='yookassa',
            payment_id=101,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_cancel_paid_payment_409(monkeypatch):
    user = _make_dummy_user()
    db = AsyncMock()

    existing = _make_pending_payment(user=user, local_id=101, is_paid=True)
    monkeypatch.setattr(
        'app.cabinet.services.active_invoice.get_payment_record',
        AsyncMock(return_value=existing),
    )

    with pytest.raises(HTTPException) as exc_info:
        await cancel_pending_payment(
            db=db,
            user=user,
            method='yookassa',
            payment_id=101,
        )

    assert exc_info.value.status_code == 409


# ============ Endpoint tests: create_tariff_invoice ============


@pytest.mark.asyncio
async def test_create_tariff_invoice_same_intent_and_409(monkeypatch):
    user = _make_dummy_user()
    db = AsyncMock()

    # Enable tariffs mode
    monkeypatch.setattr(purchase_route.settings.__class__, 'is_tariffs_mode', lambda self: True)

    # Mock tariff purchase context
    fake_tariff = SimpleNamespace(id=5, name='Premium', allowed_squads=[])
    fake_ctx = SimpleNamespace(
        tariff=fake_tariff,
        period_days=30,
        price_kopeks=60000,
        is_daily=False,
        traffic_limit_gb=0,
        effective_device_limit=3,
        discount_percent=0,
        promo_offer_discount_value=0,
        existing_subscription=None,
    )
    monkeypatch.setattr(
        purchase_route, '_resolve_tariff_purchase_context', AsyncMock(return_value=fake_ctx)
    )

    fake_method = SimpleNamespace(
        id='yookassa',
        name='ЮKassa',
        is_available=True,
        min_amount_kopeks=1000,
    )
    monkeypatch.setattr(
        purchase_route, 'get_payment_methods', AsyncMock(return_value=[fake_method])
    )

    existing = _make_pending_payment(
        user=user,
        local_id=202,
        method=PaymentMethod.YOOKASSA,
        amount_kopeks=60000,
        description='Оплата тарифа «Premium» (30 дн.)',
    )
    monkeypatch.setattr(
        purchase_route, 'get_active_invoice_record', AsyncMock(return_value=existing)
    )

    # 1. Same intent -> returns existing
    req_same = TariffInvoiceRequest(
        tariff_id=5,
        period_days=30,
        payment_method='yookassa',
    )
    resp = await purchase_route.create_tariff_invoice(request=req_same, user=user, db=db)
    assert resp.payment_id == '202'
    assert resp.amount_kopeks == 60000
    assert resp.expires_at is not None

    # 2. Different intent (e.g. different method) -> 409
    req_diff = TariffInvoiceRequest(
        tariff_id=5,
        period_days=30,
        payment_method='cryptobot',
    )
    fake_crypto = SimpleNamespace(
        id='cryptobot',
        name='CryptoBot',
        is_available=True,
        min_amount_kopeks=1000,
    )
    monkeypatch.setattr(
        purchase_route, 'get_payment_methods', AsyncMock(return_value=[fake_crypto])
    )

    with pytest.raises(HTTPException) as exc_info:
        await purchase_route.create_tariff_invoice(request=req_diff, user=user, db=db)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail['code'] == 'active_invoice_exists'


# ============ Endpoint tests: notifications/history ============


@pytest.mark.asyncio
async def test_get_notification_history(monkeypatch):
    user = _make_dummy_user()
    db = AsyncMock()

    now = datetime.now(UTC)
    notifs = [
        CabinetNotification(
            id=1,
            user_id=user.id,
            type='payment_invoice_created',
            title='Счёт на оплату',
            body='Пополнение баланса 500 ₽',
            payload_json={'method': 'yookassa'},
            created_at=now,
            read_at=None,
        )
    ]

    # Mock count query scalar_one()
    count_result = SimpleNamespace(scalar_one=lambda: 1)
    # Mock items query scalars().all()
    items_result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: notifs))

    db.execute = AsyncMock(side_effect=[count_result, items_result])

    resp = await notifications_route.get_notification_history(
        limit=20, offset=0, user=user, db=db
    )

    assert resp.total == 1
    assert len(resp.notifications) == 1
    assert resp.notifications[0].title == 'Счёт на оплату'
    assert resp.notifications[0].type == 'payment_invoice_created'
