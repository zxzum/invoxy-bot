"""Правила смены тарифа: остаток к оплате и обнуление трафика.

Отчёт из «Багов»: «если в подписке осталось 0 дней, можно прыгать между
тарифами бесплатно + трафик сбрасывается из-за этого».
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.services.tariff_switch_policy import remaining_days_for_switch, should_reset_used_traffic


NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ('end_date', 'expected'),
    [
        (NOW + timedelta(minutes=1), 1),
        (NOW + timedelta(hours=5), 1),
        (NOW + timedelta(hours=23, minutes=59), 1),
        (NOW + timedelta(days=1), 1),
        (NOW + timedelta(days=1, hours=12), 1),
        (NOW + timedelta(days=29, hours=23), 29),
        (NOW + timedelta(days=30), 30),
    ],
)
def test_live_subscription_costs_at_least_one_day(end_date, expected):
    """Остаток в часах — не ноль: иначе переключение бесплатное."""
    assert remaining_days_for_switch(end_date, NOW) == expected


@pytest.mark.parametrize(
    'end_date',
    [None, NOW, NOW - timedelta(seconds=1), NOW - timedelta(days=3)],
)
def test_expired_subscription_has_no_remaining_days(end_date):
    """Истёкшей подписке путь в покупку, а не в переключение."""
    assert remaining_days_for_switch(end_date, NOW) == 0


def test_naive_datetime_is_treated_as_utc():
    """SQLite отдаёт даты без таймзоны — сравнение не должно падать."""
    assert remaining_days_for_switch(datetime(2026, 9, 10, 12, 0), NOW) == 2


def test_defaults_to_current_time():
    assert remaining_days_for_switch(datetime.now(UTC) + timedelta(hours=2)) == 1
    assert remaining_days_for_switch(datetime.now(UTC) - timedelta(hours=2)) == 0


class TestTrafficReset:
    def test_paid_switch_resets(self, monkeypatch):
        monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_TARIFF_SWITCH', True)
        assert should_reset_used_traffic(1) is True
        assert should_reset_used_traffic(500_00) is True

    def test_free_switch_keeps_used_traffic(self, monkeypatch):
        """Иначе понизил тариф — счётчик с нуля, вернулся — ещё раз."""
        monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_TARIFF_SWITCH', True)
        assert should_reset_used_traffic(0) is False

    def test_global_switch_off_wins(self, monkeypatch):
        monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_TARIFF_SWITCH', False)
        assert should_reset_used_traffic(500_00) is False
        assert should_reset_used_traffic(0) is False


class TestConvertedDays:
    def test_switch_up_with_ten_percent_fee(self):
        from app.database.models import Tariff
        from app.services.pricing_engine import PricingEngine

        basic = Tariff(id=2, name='Базовый', period_prices={'30': 10000})
        standard = Tariff(id=3, name='Стандарт', period_prices={'30': 20000})

        # 30 days of Basic (100 RUB) to Standard (200 RUB):
        # Remaining value = 100 RUB.
        # With 10% fee = 90 RUB.
        # Standard daily rate = 200 / 30 = 6.666 RUB/day.
        # 90 / (200/30) = 13.5 -> 13 days
        days = PricingEngine.calculate_converted_days(basic, standard, 30, commission_pct=10)
        assert days == 13

    def test_switch_down_with_ten_percent_fee(self):
        from app.database.models import Tariff
        from app.services.pricing_engine import PricingEngine

        standard = Tariff(id=3, name='Стандарт', period_prices={'30': 20000})
        basic = Tariff(id=2, name='Базовый', period_prices={'30': 10000})

        # 30 days of Standard (200 RUB) to Basic (100 RUB):
        # Remaining value = 200 RUB.
        # With 10% fee = 180 RUB.
        # Basic daily rate = 100 / 30 = 3.333 RUB/day.
        # 180 / (100/30) = 54 days
        days = PricingEngine.calculate_converted_days(standard, basic, 30, commission_pct=10)
        assert days == 54

    def test_zero_or_negative_days(self):
        from app.database.models import Tariff
        from app.services.pricing_engine import PricingEngine

        basic = Tariff(id=2, name='Базовый', period_prices={'30': 10000})
        standard = Tariff(id=3, name='Стандарт', period_prices={'30': 20000})

        assert PricingEngine.calculate_converted_days(basic, standard, 0, commission_pct=10) == 0
        assert PricingEngine.calculate_converted_days(basic, standard, -5, commission_pct=10) == 0

    def test_minimum_one_day_guarantee(self):
        from app.database.models import Tariff
        from app.services.pricing_engine import PricingEngine

        basic = Tariff(id=2, name='Базовый', period_prices={'30': 10000})
        premium = Tariff(id=4, name='Премиум', period_prices={'30': 40000})

        # 1 day of Basic (3.33 RUB) to Premium (13.33 RUB/day) with 10% fee = 3.0 RUB
        # 3.0 / 13.33 = 0.22 -> guarantees at least 1 day
        days = PricingEngine.calculate_converted_days(basic, premium, 1, commission_pct=10)
        assert days == 1

