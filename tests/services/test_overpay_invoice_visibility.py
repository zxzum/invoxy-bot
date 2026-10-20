from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import PaymentMethod
from app.services.payment_verification_service import _fetch_overpay_payments, get_payment_record, run_manual_check


def payment(**values):
    return SimpleNamespace(
        id=5, user=SimpleNamespace(id=7), user_id=7, order_id='overpay-5', amount_kopeks=10000,
        created_at=datetime.now(UTC), expires_at=None, **{'status': 'pending', 'is_paid': False, **values},
    )


@pytest.mark.asyncio
async def test_overpay_pending_invoice_is_visible_and_owner_filtered():
    active = payment()
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [active, payment(is_paid=True), payment(status='cancelled')]))
    records = await _fetch_overpay_payments(db, datetime.now(UTC) - timedelta(days=1), user_id=7)
    assert len(records) == 1
    assert records[0].method == PaymentMethod.OVERPAY and records[0].local_id == 5
    assert 7 in db.execute.await_args.args[0].compile().params.values()


@pytest.mark.asyncio
async def test_overpay_record_can_be_checked_and_cancelled():
    db = AsyncMock()
    db.get.return_value = payment()
    record = await get_payment_record(db, PaymentMethod.OVERPAY, 5)
    assert record.user.id == 7 and record.identifier == 'overpay-5'
    service = SimpleNamespace(check_overpay_payment_status=AsyncMock(return_value={'payment': db.get.return_value}))
    checked = await run_manual_check(db, PaymentMethod.OVERPAY, 5, service)
    assert checked.method == PaymentMethod.OVERPAY
    service.check_overpay_payment_status.assert_awaited_once_with(db, 'overpay-5')
