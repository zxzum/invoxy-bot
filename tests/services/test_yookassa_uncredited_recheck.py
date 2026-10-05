"""YooKassa: платёж, отмеченный оплаченным, но не зачисленный (нет transaction_id),
должен попадать в автосверку, иначе деньги пользователя теряются до ручного вмешательства."""

from datetime import UTC, datetime
from types import SimpleNamespace

from app.database.models import PaymentMethod
from app.services.payment_verification_service import (
    PendingPayment,
    _is_yookassa_recheck_candidate,
    needs_auto_check,
)


def _payment(status: str, is_paid: bool, transaction_id: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(status=status, is_paid=is_paid, transaction_id=transaction_id)


def _record(method: PaymentMethod, *, is_paid: bool, status: str = 'pending') -> PendingPayment:
    return PendingPayment(
        method=method,
        local_id=1,
        identifier='id-1',
        amount_kopeks=20000,
        status=status,
        is_paid=is_paid,
        created_at=datetime.now(UTC),
        user=SimpleNamespace(),
        payment=SimpleNamespace(),
    )


def test_pending_yookassa_payment_is_rechecked() -> None:
    assert _is_yookassa_recheck_candidate(_payment('pending', False))
    assert _is_yookassa_recheck_candidate(_payment('waiting_for_capture', False))


def test_paid_but_uncredited_yookassa_payment_is_rechecked() -> None:
    assert _is_yookassa_recheck_candidate(_payment('succeeded', True))


def test_credited_or_cancelled_yookassa_payment_is_skipped() -> None:
    assert not _is_yookassa_recheck_candidate(_payment('succeeded', True, transaction_id=5))
    assert not _is_yookassa_recheck_candidate(_payment('canceled', False))


def test_auto_check_keeps_paid_uncredited_yookassa_records() -> None:
    assert needs_auto_check(_record(PaymentMethod.YOOKASSA, is_paid=True, status='succeeded'))
    assert needs_auto_check(_record(PaymentMethod.YOOKASSA, is_paid=False))


def test_auto_check_skips_paid_records_of_other_providers() -> None:
    assert not needs_auto_check(_record(PaymentMethod.PAL24, is_paid=True, status='paid'))
    assert needs_auto_check(_record(PaymentMethod.PAL24, is_paid=False))
