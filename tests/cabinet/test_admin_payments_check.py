"""Tests for admin_payments and balance status mapping and manual check eligibility."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from app.cabinet.routes.admin_payments import (
    _get_status_info as admin_get_status_info,
    _is_checkable as admin_is_checkable,
)
from app.cabinet.routes.balance import (
    _get_status_info as balance_get_status_info,
    _is_checkable as balance_is_checkable,
)
from app.database.models import PaymentMethod
from app.services.payment_verification_service import PendingPayment


def _create_pending_payment(
    method: PaymentMethod,
    status: str,
    *,
    is_paid: bool = False,
    age_hours: float = 1.0,
) -> PendingPayment:
    return PendingPayment(
        method=method,
        local_id=101,
        identifier='rp_test_123',
        amount_kopeks=20000,
        status=status,
        is_paid=is_paid,
        created_at=datetime.now(UTC) - timedelta(hours=age_hours),
        user=MagicMock(id=1, telegram_id=None, email='test@example.com'),
        payment=MagicMock(payment_url='https://pay.rollypay.io/123'),
    )


def test_rollypay_status_info():
    """Verify ROLLYPAY status emoji and description."""
    record_pending = _create_pending_payment(PaymentMethod.ROLLYPAY, 'pending')
    emoji, text = admin_get_status_info(record_pending)
    assert emoji == '⏳'
    assert text == 'Ожидает оплаты'

    record_processing = _create_pending_payment(PaymentMethod.ROLLYPAY, 'processing')
    emoji, text = admin_get_status_info(record_processing)
    assert emoji == '⌛'
    assert text == 'Обрабатывается'

    record_paid = _create_pending_payment(PaymentMethod.ROLLYPAY, 'paid', is_paid=True)
    emoji, text = admin_get_status_info(record_paid)
    assert emoji == '✅'
    assert text == 'Оплачено'

    record_canceled = _create_pending_payment(PaymentMethod.ROLLYPAY, 'canceled')
    emoji, text = admin_get_status_info(record_canceled)
    assert emoji == '❌'
    assert text == 'Отменено'


def test_rollypay_is_checkable_admin():
    """Verify ROLLYPAY is_checkable in admin_payments."""
    # Recent pending payment must be checkable
    record_pending = _create_pending_payment(PaymentMethod.ROLLYPAY, 'pending', age_hours=0.5)
    assert admin_is_checkable(record_pending) is True

    record_created = _create_pending_payment(PaymentMethod.ROLLYPAY, 'created', age_hours=0.5)
    assert admin_is_checkable(record_created) is True

    record_processing = _create_pending_payment(PaymentMethod.ROLLYPAY, 'processing', age_hours=0.5)
    assert admin_is_checkable(record_processing) is True

    # Already paid or canceled must not be checkable
    record_paid = _create_pending_payment(PaymentMethod.ROLLYPAY, 'paid', is_paid=True)
    assert admin_is_checkable(record_paid) is False

    record_canceled = _create_pending_payment(PaymentMethod.ROLLYPAY, 'canceled')
    assert admin_is_checkable(record_canceled) is False

    # Older than 24h must not be checkable
    record_old = _create_pending_payment(PaymentMethod.ROLLYPAY, 'pending', age_hours=25.0)
    assert admin_is_checkable(record_old) is False


def test_rollypay_status_info_and_checkable_balance():
    """Verify ROLLYPAY is properly handled in cabinet/routes/balance.py."""
    record_pending = _create_pending_payment(PaymentMethod.ROLLYPAY, 'pending', age_hours=0.2)
    emoji, text = balance_get_status_info(record_pending)
    assert emoji == '⏳'
    assert text == 'Ожидает оплаты'
    assert balance_is_checkable(record_pending) is True

    record_paid = _create_pending_payment(PaymentMethod.ROLLYPAY, 'success', is_paid=True)
    emoji, text = balance_get_status_info(record_paid)
    assert emoji == '✅'
    assert text == 'Оплачено'
    assert balance_is_checkable(record_paid) is False
