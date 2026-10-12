"""Unit tests for RollyPay payment integration and subscription fulfillment."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.database.models import Tariff, User
from app.services.payment_service import PaymentService


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.mark.asyncio
@pytest.mark.parametrize(('method', 'api_method'), [('sbp', 'sbp'), ('foreign_card', 'intl_card')])
async def test_create_rollypay_payment_email_user_order_id(monkeypatch, method, api_method):
    """Verify that email user without telegram_id gets order_id with user.id, not 'rpNone_'."""
    monkeypatch.setattr(settings, 'ROLLYPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'ROLLYPAY_API_KEY', 'test_key', raising=False)
    monkeypatch.setattr(settings, 'ROLLYPAY_SIGNING_SECRET', 'test_secret', raising=False)
    monkeypatch.setattr(settings, 'ROLLYPAY_MIN_AMOUNT_KOPEKS', 1000, raising=False)
    monkeypatch.setattr(settings, 'ROLLYPAY_MAX_AMOUNT_KOPEKS', 10000000, raising=False)

    service = PaymentService()
    db = AsyncMock()

    # User with id 42 and telegram_id=None
    email_user = User(id=42, telegram_id=None, email='playxd2791@gmail.com')

    with (
        patch('app.services.payment_service.get_user_by_id', return_value=email_user),
        patch('app.services.rollypay_service.rollypay_service.create_payment', new_callable=AsyncMock) as mock_api,
        patch('app.database.crud.rollypay.create_rollypay_payment', new_callable=AsyncMock) as mock_crud,
    ):
        mock_api.return_value = {
            'pay_url': 'https://pay.rollypay.io/f0e5643f',
            'payment_id': 'pay_123456',
            'expires_at': datetime.now(UTC).isoformat(),
        }
        mock_crud.return_value = MagicMock(id=1, order_id='rp42_abc123')

        extra = {
            'purpose': 'tariff_purchase',
            'tariff_id': '10',
            'source': 'cabinet',
            'cart_data': {'tariff_id': 10, 'period_days': 30, 'total_price': 20000},
        }

        result = await service.create_rollypay_payment(
            db,
            user_id=42,
            amount_kopeks=20000,
            description='Оплата тарифа «Стандарт LTE» (30 дн.)',
            extra_metadata=extra,
            payment_method_type=method,
        )

        assert result is not None
        # Verify API call
        assert mock_api.called
        call_kwargs = mock_api.call_args.kwargs
        order_id = call_kwargs['order_id']
        assert order_id.startswith('rp42_')
        assert 'None' not in order_id
        assert call_kwargs['customer_id'] == '42'
        assert call_kwargs['payment_method'] == api_method

        # Verify CRUD call metadata_json persistence
        assert mock_crud.called
        crud_kwargs = mock_crud.call_args.kwargs
        saved_meta = crud_kwargs['metadata_json']
        assert saved_meta['purpose'] == 'tariff_purchase'
        assert saved_meta['cart_data'] == extra['cart_data']


@pytest.mark.asyncio
async def test_finalize_rollypay_payment_reconstructs_cart_fallback():
    """Verify fallback cart reconstruction from description when Redis cart is expired."""
    service = PaymentService()
    db = AsyncMock()

    user = User(id=42, telegram_id=None, email='playxd2791@gmail.com', balance_kopeks=0, has_made_first_topup=False)
    payment = MagicMock(
        id=77,
        user_id=42,
        order_id='rp42_d2f870',
        amount_kopeks=20000,
        is_paid=False,
        status='created',
        transaction_id=None,
        description='Оплата тарифа «Стандарт LTE» (30 дн.)',
        metadata_json={
            'user_id': 42,
            'amount_kopeks': 20000,
            'description': 'Оплата тарифа «Стандарт LTE» (30 дн.)',
        },
    )

    mock_tariff = Tariff(id=5, name='Стандарт LTE', is_active=True, is_daily=False)

    with (
        patch('app.services.payment_service.get_user_by_id', return_value=user),
        patch('app.services.payment_service.get_transaction_by_external_id', return_value=None),
        patch('app.services.payment_service.create_transaction', return_value=MagicMock(id=999)),
        patch('app.database.crud.rollypay.link_rollypay_payment_to_transaction', new_callable=AsyncMock),
        patch('app.database.crud.user.lock_user_for_update', return_value=user),
        patch('app.database.crud.transaction.emit_transaction_side_effects', new_callable=AsyncMock),
        patch('app.database.crud.tariff.get_all_active_tariffs', return_value=[mock_tariff]),
        patch('app.services.payment.common.send_cart_notification_after_topup', new_callable=AsyncMock) as mock_notify,
    ):
        success = await service._finalize_rollypay_payment(
            db,
            payment,
            rollypay_payment_id='pay_123',
            trigger='test',
        )

        assert success is True
        assert mock_notify.called
        call_kwargs = mock_notify.call_args.kwargs
        cart_fallback = call_kwargs.get('cart_data_fallback')
        assert cart_fallback is not None
        assert cart_fallback['tariff_id'] == 5
        assert cart_fallback['period_days'] == 30
        assert cart_fallback['cart_mode'] == 'tariff_purchase'


@pytest.mark.asyncio
async def test_auto_purchase_saved_cart_after_topup_with_fallback(monkeypatch):
    """Verify that auto_purchase_saved_cart_after_topup uses cart_data_fallback even when redis has no intent."""
    from app.services.subscription_auto_purchase_service import auto_purchase_saved_cart_after_topup

    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True, raising=False)

    db = AsyncMock()
    user = User(id=42, telegram_id=None, email='playxd2791@gmail.com', balance_kopeks=20000)

    cart_fallback = {
        'cart_mode': 'tariff_purchase',
        'tariff_id': 5,
        'period_days': 30,
        'total_price': 20000,
        'user_id': 42,
        'saved_cart': True,
        'return_to_cart': True,
    }

    with (
        patch(
            'app.services.user_cart_service.user_cart_service.get_user_cart', new_callable=AsyncMock, return_value=None
        ),
        patch(
            'app.services.user_cart_service.user_cart_service.get_all_subscription_carts',
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            'app.services.user_cart_service.user_cart_service.has_topup_intent',
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            'app.services.subscription_auto_purchase_service._process_single_cart',
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_process,
    ):
        result = await auto_purchase_saved_cart_after_topup(
            db,
            user,
            cart_data_fallback=cart_fallback,
        )

        assert result is True
        assert mock_process.called
        assert mock_process.call_args[0][2] == cart_fallback
