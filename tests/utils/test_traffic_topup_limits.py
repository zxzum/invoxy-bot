from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.database.crud import subscription as subscription_crud
from app.utils import traffic_topup_limits
from app.utils.traffic_topup_limits import (
    TrafficTopupMonthlyLimitExceeded,
    is_current_calendar_month,
)


def test_calendar_month_limit_resets_on_the_first_day(monkeypatch) -> None:
    monkeypatch.setattr(
        traffic_topup_limits,
        'get_local_timezone',
        lambda: timezone(timedelta(hours=3)),
    )
    assert is_current_calendar_month(
        datetime(2026, 9, 30, 20, 59, tzinfo=UTC),
        now=datetime(2026, 9, 30, 21, 0, tzinfo=UTC),
    ) is False


@pytest.mark.asyncio
async def test_monthly_limit_is_checked_after_subscription_lock(monkeypatch) -> None:
    calls: list[str] = []
    subscription = SimpleNamespace(
        traffic_topup_last_purchased_at=datetime.now(UTC),
    )

    async def lock(_db, _subscription) -> None:
        calls.append('locked')

    monkeypatch.setattr(subscription_crud, '_lock_subscription_row', lock)

    with pytest.raises(TrafficTopupMonthlyLimitExceeded):
        await subscription_crud.ensure_traffic_topup_available(object(), subscription)

    assert calls == ['locked']
