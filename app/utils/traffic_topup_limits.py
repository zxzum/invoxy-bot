"""Helpers for the tariff traffic top-up ceiling."""

from __future__ import annotations

from datetime import UTC, datetime

from app.utils.timezone import get_local_timezone


TRAFFIC_TOPUP_MONTHLY_LIMIT_CODE = 'traffic_topup_monthly_limit'
TRAFFIC_TOPUP_MONTHLY_LIMIT_MESSAGE = (
    'Лимит докупки трафика на этот календарный месяц исчерпан'
)


class TrafficTopupMonthlyLimitExceeded(ValueError):
    """A subscription already has reached the traffic top-up limit in this calendar month."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or TRAFFIC_TOPUP_MONTHLY_LIMIT_MESSAGE)


def is_current_calendar_month(value: datetime | None, *, now: datetime | None = None) -> bool:
    """Return whether ``value`` is in the configured local calendar month."""
    if value is None:
        return False

    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    timezone = get_local_timezone()
    current_local = now.astimezone(timezone)
    value_local = value.astimezone(timezone)
    return (value_local.year, value_local.month) == (current_local.year, current_local.month)


def get_local_month_period_key(now: datetime | None = None) -> str:
    """Return local calendar month period key, e.g. '2026-09'."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local_dt = now.astimezone(get_local_timezone())
    return f'{local_dt.year:04d}-{local_dt.month:02d}'


def get_local_month_start_utc(now: datetime | None = None) -> datetime:
    """Return the UTC datetime of the start of the current local calendar month."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local_dt = now.astimezone(get_local_timezone())
    start_local = datetime(local_dt.year, local_dt.month, 1, 0, 0, 0, tzinfo=get_local_timezone())
    return start_local.astimezone(UTC)


def get_next_local_month_start_utc(now: datetime | None = None) -> datetime:
    """Return the UTC datetime of the start of the next local calendar month."""
    current = (now or datetime.now(UTC)).astimezone(get_local_timezone())
    year, month = (current.year + 1, 1) if current.month == 12 else (current.year, current.month + 1)
    return datetime(year, month, 1, 0, 0, 0, tzinfo=get_local_timezone()).astimezone(UTC)


get_next_local_month_start = get_next_local_month_start_utc


def next_traffic_topup_at(value: datetime | None = None, *, now: datetime | None = None) -> datetime | None:
    """Return the next local calendar-month boundary when a top-up is blocked."""
    if value is not None and not is_current_calendar_month(value, now=now):
        return None
    return get_next_local_month_start_utc(now=now)


async def count_monthly_traffic_purchases(
    db: object,
    subscription_id: int,
    *,
    now: datetime | None = None,
) -> int:
    """Count regular TrafficPurchase rows created in the current local calendar month."""
    if not hasattr(db, 'execute'):
        return 0

    from sqlalchemy import func, select

    from app.database.models import TrafficPurchase

    month_start = get_local_month_start_utc(now)
    query = (
        select(func.count(TrafficPurchase.id))
        .where(
            TrafficPurchase.subscription_id == subscription_id,
            TrafficPurchase.created_at >= month_start,
        )
    )
    result = await db.execute(query)
    return int(result.scalar() or 0)


def available_traffic_topup_gb(tariff: object, current_traffic_gb: int | None) -> int | None:
    """Return remaining tariff top-up capacity; ``None`` means unlimited."""
    maximum = int(getattr(tariff, 'max_topup_traffic_gb', 0) or 0)
    if maximum <= 0:
        return None
    return max(0, maximum - max(0, int(current_traffic_gb or 0)))
