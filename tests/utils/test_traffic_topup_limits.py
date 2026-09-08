from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.database.crud import subscription as subscription_crud
from app.utils import traffic_topup_limits
from app.utils.traffic_topup_limits import (
    TrafficTopupMonthlyLimitExceeded,
    is_current_calendar_month,
    next_traffic_topup_at,
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


def test_next_topup_is_first_day_of_next_local_month(monkeypatch) -> None:
    monkeypatch.setattr(
        traffic_topup_limits,
        'get_local_timezone',
        lambda: timezone(timedelta(hours=3)),
    )
    result = next_traffic_topup_at(
        datetime(2026, 9, 1, tzinfo=UTC),
        now=datetime(2026, 9, 9, 12, tzinfo=UTC),
    )
    assert result == datetime(2026, 9, 30, 21, tzinfo=UTC)


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


@pytest.mark.asyncio
async def test_regular_and_whitelist_monthly_limits_are_independent(monkeypatch) -> None:
    now = datetime.now(UTC)
    subscription = SimpleNamespace(
        traffic_topup_last_purchased_at=now,
        whitelist_traffic_topup_last_purchased_at=None,
    )

    async def lock(_db, _subscription) -> None:
        return None

    monkeypatch.setattr(subscription_crud, '_lock_subscription_row', lock)

    await subscription_crud.ensure_traffic_topup_available(
        object(),
        subscription,
        now=now,
        scope='whitelist',
    )

    subscription.traffic_topup_last_purchased_at = None
    subscription.whitelist_traffic_topup_last_purchased_at = now
    await subscription_crud.ensure_traffic_topup_available(
        object(),
        subscription,
        now=now,
        scope='regular',
    )
