"""Helpers for the tariff traffic top-up ceiling."""

from __future__ import annotations

from datetime import UTC, datetime

from app.utils.timezone import get_local_timezone


TRAFFIC_TOPUP_MONTHLY_LIMIT_CODE = 'traffic_topup_monthly_limit'
TRAFFIC_TOPUP_MONTHLY_LIMIT_MESSAGE = (
    'Можно купить только один пакет трафика на подписку в календарный месяц'
)


class TrafficTopupMonthlyLimitExceeded(ValueError):
    """A subscription already has a paid traffic top-up in this calendar month."""

    def __init__(self) -> None:
        super().__init__(TRAFFIC_TOPUP_MONTHLY_LIMIT_MESSAGE)


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


def available_traffic_topup_gb(tariff: object, current_traffic_gb: int | None) -> int | None:
    """Return remaining tariff top-up capacity; ``None`` means unlimited."""
    maximum = int(getattr(tariff, 'max_topup_traffic_gb', 0) or 0)
    if maximum <= 0:
        return None
    return max(0, maximum - max(0, int(current_traffic_gb or 0)))
