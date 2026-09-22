"""Invoice-эндпоинт прямой оплаты тарифа (кабинет).

Паттерн — как в tests/test_miniapp_payments.py: route-функция зовётся
напрямую с monkeypatch-зависимостями, без реальной БД.
"""

import types
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes.subscription_modules import purchase as purchase_module
from app.cabinet.schemas.subscription import TariffInvoiceRequest
from app.config import settings


def _make_user(balance_kopeks: int = 0, restriction: bool = False) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=10,
        telegram_id=10,
        username='tester',
        email=None,
        email_verified=False,
        language='ru',
        balance_kopeks=balance_kopeks,
        restriction_subscription=restriction,
        restriction_topup=False,
    )


def _make_tariff() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=5,
        name='Базовый',
        is_active=True,
        is_free=False,
        is_daily=False,
        daily_price_kopeks=0,
        device_limit=3,
        traffic_limit_gb=100,
        allowed_squads=['squad-1'],
        period_prices={'30': 9900},
        custom_days_enabled=False,
        price_per_day_kopeks=None,
        custom_traffic_enabled=False,
        traffic_price_per_gb_kopeks=None,
    )


def _pricing_result(price_kopeks: int) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        final_total=price_kopeks,
        original_total=price_kopeks,
        promo_offer_discount=0,
        breakdown={'group_discount_pct': {'period': 0}, 'offer_discount_pct': 0},
    )


@pytest.mark.anyio('asyncio')
async def test_invoice_creates_cart_and_payment_link(monkeypatch):
    """Хватает баланса на часть цены → инвойс на НЕДОСТАЮЩУЮ сумму, корзина сохранена."""
    user = _make_user(balance_kopeks=4000)
    tariff = _make_tariff()  # цена 9900 → нехватка 5900

    captured: dict[str, Any] = {}

    async def fake_save_cart(uid, cart_data):
        captured['cart'] = cart_data

    async def fake_link(db, user_, **kwargs):
        captured['amount_kopeks'] = kwargs['amount_kopeks']
        captured['description'] = kwargs['description']
        return ('https://pay.example/x', 'local-1')

    # Резолвер цены/валидаций мокается целиком — его внутренности покрыты
    # собственными тестами; здесь проверяется контракт invoice-эндпоинта.
    async def fake_context(db, user_, **kwargs):
        return purchase_module.TariffPurchaseContext(
            tariff=tariff,
            user=user_,
            is_daily=False,
            period_days=30,
            traffic_limit_gb=100,
            custom_traffic_gb=None,
            existing_subscription=None,
            effective_device_limit=3,
            device_limit=None,
            price_kopeks=9900,
            original_price=9900,
            discount_percent=0,
            promo_offer_discount_value=0,
            promo_offer_discount_percent=0,
            price_before_promo_offer=9900,
            promo_group=None,
            squads=['squad-1'],
        )

    async def fake_methods(user, db):
        return [
            types.SimpleNamespace(
                id='yookassa', name='ЮKassa', is_available=True,
                min_amount_kopeks=1000, max_amount_kopeks=1000000,
            )
        ]

    monkeypatch.setattr(purchase_module, '_resolve_tariff_purchase_context', fake_context)
    monkeypatch.setattr(purchase_module, 'get_payment_methods', fake_methods)
    monkeypatch.setattr(purchase_module, 'get_active_invoice_record', AsyncMock(return_value=None))
    monkeypatch.setattr(purchase_module, 'record_cabinet_notification', AsyncMock())
    monkeypatch.setattr(purchase_module, 'send_invoice_created_telegram_message', AsyncMock())
    monkeypatch.setattr(purchase_module.user_cart_service, 'save_user_cart', fake_save_cart)
    monkeypatch.setattr(purchase_module, '_create_payment_link', fake_link)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs', raising=False)
    monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda self: True, raising=False)

    fake_db = types.SimpleNamespace(add=lambda *args: None, commit=AsyncMock())
    request = TariffInvoiceRequest(tariff_id=5, period_days=30, payment_method='yookassa')
    response = await purchase_module.create_tariff_invoice(request, user=user, db=fake_db)

    assert response.payment_url == 'https://pay.example/x'
    assert response.amount_kopeks == 5900  # 9900 − 4000
    assert response.price_kopeks == 9900
    cart = captured['cart']
    assert cart['cart_mode'] == 'tariff_purchase'
    assert cart['total_price'] == 9900
    assert cart['missing_amount'] == 5900
    assert cart['source'] == 'cabinet'
    assert captured['amount_kopeks'] == 5900
    assert 'Базовый' in captured['description']


@pytest.mark.anyio('asyncio')
async def test_invoice_rejects_when_balance_covers_price(monkeypatch):
    """Баланс покрывает цену → 400 balance_sufficient (клиент зовёт обычную покупку)."""
    user = _make_user(balance_kopeks=99999)
    tariff = _make_tariff()

    async def fake_lock(db, uid):
        return user

    async def fake_get_tariff(db, tariff_id):
        return tariff

    def fake_available(promo_group_id):
        return True

    async def fake_price(self, *args, **kwargs):
        return _pricing_result(9900)

    async def fake_no_subscription(db, *args, **kwargs):
        return None

    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', fake_lock)
    monkeypatch.setattr(purchase_module, 'get_tariff_by_id', fake_get_tariff)
    monkeypatch.setattr(purchase_module, 'get_subscription_by_user_id', fake_no_subscription)
    monkeypatch.setattr(purchase_module, 'get_subscription_by_id_for_user', fake_no_subscription)
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_and_tariff', fake_no_subscription
    )
    tariff.is_available_for_promo_group = fake_available
    monkeypatch.setattr(purchase_module.pricing_engine, 'calculate_tariff_purchase_price', fake_price)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs', raising=False)
    monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda self: True, raising=False)

    from fastapi import HTTPException

    request = TariffInvoiceRequest(tariff_id=5, period_days=30, payment_method='yookassa')
    with pytest.raises(HTTPException) as exc_info:
        await purchase_module.create_tariff_invoice(request, user=user, db=types.SimpleNamespace())
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail['code'] == 'balance_sufficient'


@pytest.mark.anyio('asyncio')
async def test_invoice_forbidden_for_restricted_user(monkeypatch):
    from fastapi import HTTPException

    user = _make_user(restriction=True)
    request = TariffInvoiceRequest(tariff_id=5, period_days=30, payment_method='yookassa')
    with pytest.raises(HTTPException) as exc_info:
        await purchase_module.create_tariff_invoice(request, user=user, db=types.SimpleNamespace())
    assert exc_info.value.status_code == 403


@pytest.mark.anyio('asyncio')
async def test_invoice_rejects_unknown_method(monkeypatch):
    """Выключенный/неизвестный метод → 400, ссылка не создаётся, корзина не пишется."""
    user = _make_user(balance_kopeks=0)
    tariff = _make_tariff()

    async def fake_lock(db, uid):
        return user

    async def fake_get_tariff(db, tariff_id):
        return tariff

    def fake_available(promo_group_id):
        return True

    async def fake_price(self, *args, **kwargs):
        return _pricing_result(9900)

    async def fake_methods(user, db):
        return [
            types.SimpleNamespace(
                id='yookassa', name='ЮKassa', is_available=True,
                min_amount_kopeks=10000, max_amount_kopeks=1000000,
            )
        ]

    saved: dict[str, Any] = {}

    async def fake_save_cart(uid, cart_data):
        saved['called'] = True

    async def fake_no_subscription(db, *args, **kwargs):
        return None

    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', fake_lock)
    monkeypatch.setattr(purchase_module, 'get_tariff_by_id', fake_get_tariff)
    monkeypatch.setattr(purchase_module, 'get_subscription_by_user_id', fake_no_subscription)
    monkeypatch.setattr(purchase_module, 'get_subscription_by_id_for_user', fake_no_subscription)
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_and_tariff', fake_no_subscription
    )
    tariff.is_available_for_promo_group = fake_available
    monkeypatch.setattr(purchase_module.pricing_engine, 'calculate_tariff_purchase_price', fake_price)
    monkeypatch.setattr(purchase_module, 'get_payment_methods', fake_methods)
    monkeypatch.setattr(purchase_module.user_cart_service, 'save_user_cart', fake_save_cart)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs', raising=False)
    monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda self: True, raising=False)

    from fastapi import HTTPException

    request = TariffInvoiceRequest(tariff_id=5, period_days=30, payment_method='cryptobot')
    with pytest.raises(HTTPException) as exc_info:
        await purchase_module.create_tariff_invoice(request, user=user, db=types.SimpleNamespace())
    assert exc_info.value.status_code == 400
    assert 'called' not in saved
