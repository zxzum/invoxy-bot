from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.cabinet.routes.subscription_modules.traffic import (
    TrafficResetSaveCartRequest,
    build_traffic_reset_status,
    get_traffic_packages,
    purchase_traffic,
    save_traffic_reset_cart,
)
from app.database.crud.subscription import (
    add_subscription_traffic,
    reset_whitelist_subscription_traffic,
)
from app.database.models import Subscription, Tariff, User
from app.services.subscription_auto_purchase_service import _auto_reset_traffic


def _make_lte_tariff(
    *,
    whitelist_limit_gb: int = 50,
    reset_chunk_gb: int = 50,
    reset_price_kopeks: int = 15000,
    reset_min_used_gb: int = 10,
    reset_max_per_month: int = 1,
) -> Tariff:
    return Tariff(
        id=1,
        name='Стандарт 🌐 LTE',
        traffic_limit_gb=350,
        whitelist_traffic_limit_gb=whitelist_limit_gb,
        traffic_topup_enabled=False,
        traffic_topup_packages={},
        whitelist_traffic_topup_packages={},
        whitelist_reset_enabled=True,
        whitelist_reset_chunk_gb=reset_chunk_gb,
        whitelist_reset_price_kopeks=reset_price_kopeks,
        whitelist_reset_min_used_gb=reset_min_used_gb,
        whitelist_reset_max_per_month=reset_max_per_month,
        allowed_squads=['vpn-uuid', 'whitelist-uuid'],
    )


def _make_standard_tariff() -> Tariff:
    return Tariff(
        id=2,
        name='Стандарт 🛡️',
        traffic_limit_gb=350,
        whitelist_traffic_limit_gb=0,
        traffic_topup_enabled=True,
        traffic_topup_packages={'100': 5000},
        traffic_topup_max_per_month=2,
        whitelist_reset_enabled=False,
        allowed_squads=['vpn-uuid'],
    )


def test_build_traffic_reset_status_calculations():
    tariff = _make_lte_tariff(
        whitelist_limit_gb=50,
        reset_chunk_gb=50,
        reset_price_kopeks=15000,
        reset_min_used_gb=10,
        reset_max_per_month=1,
    )
    sub = SimpleNamespace(
        id=10,
        whitelist_traffic_limit_gb=50,
        whitelist_traffic_used_bytes=int(12.5 * 1024**3),
    )

    # 1. Normal state: 12.5 GB used, 0 resets this month -> available
    status = build_traffic_reset_status(
        tariff=tariff,
        sub=sub,
        used_this_month=0,
    )
    assert status.enabled is True
    assert status.chunk_gb == 50
    assert status.price_kopeks == 15000
    assert status.price_rubles == 150
    assert status.used_gb == 12.5
    assert status.limit_gb == 50
    assert status.will_clear_gb == 12.5
    assert status.used_after_gb == 0.0
    assert status.remaining_this_month == 1
    assert status.unavailable_reason is None

    # 2. Threshold not reached: 8 GB used (< 10 GB) -> unavailable
    sub.whitelist_traffic_used_bytes = int(8.0 * 1024**3)
    status_below = build_traffic_reset_status(
        tariff=tariff,
        sub=sub,
        used_this_month=0,
    )
    assert status_below.unavailable_reason == 'below_min_used'

    # 3. Monthly limit reached: 1 reset already done -> unavailable
    sub.whitelist_traffic_used_bytes = int(20.0 * 1024**3)
    status_limit = build_traffic_reset_status(
        tariff=tariff,
        sub=sub,
        used_this_month=1,
    )
    assert status_limit.unavailable_reason == 'monthly_limit'
    assert status_limit.remaining_this_month == 0
    assert status_limit.next_available_at is not None

    # 4. Partial reduction on large usage: 80 GB used, 50 GB chunk -> 30 GB left
    sub.whitelist_traffic_used_bytes = int(80.0 * 1024**3)
    sub.whitelist_traffic_limit_gb = 150
    tariff.whitelist_traffic_limit_gb = 150
    status_large = build_traffic_reset_status(
        tariff=tariff,
        sub=sub,
        used_this_month=0,
    )
    assert status_large.will_clear_gb == 50.0
    assert status_large.used_after_gb == 30.0


@pytest.mark.asyncio
async def test_reset_whitelist_subscription_traffic_logic():
    session = AsyncMock()
    tariff = _make_lte_tariff(
        whitelist_limit_gb=50,
        reset_chunk_gb=50,
        reset_price_kopeks=15000,
        reset_min_used_gb=10,
        reset_max_per_month=1,
    )
    sub = Subscription(
        id=10,
        user_id=1,
        status='active',
        traffic_limit_gb=350,
        whitelist_traffic_limit_gb=50,
        whitelist_traffic_used_bytes=int(12.5 * 1024**3),
        tariff_id=1,
    )
    sub.tariff = tariff

    with patch('app.database.crud.subscription._lock_subscription_row', return_value=sub),          patch('app.database.crud.subscription.count_monthly_traffic_purchases', return_value=0),          patch('app.database.crud.subscription.restore_whitelist_squad_if_needed', AsyncMock()) as mock_restore:

        # Test successful reset
        resp = await reset_whitelist_subscription_traffic(
            session=session,
            subscription_id=10,
            user_id=1,
            user_timezone='Europe/Moscow',
        )

        assert resp['success'] is True
        assert resp['cleared_gb'] == 12.5
        assert resp['new_used_gb'] == 0.0
        # INVOXY invariant: limit remains unchanged!
        assert resp['limit_gb'] == 50
        assert sub.whitelist_traffic_limit_gb == 50
        assert sub.whitelist_traffic_used_bytes == 0

        # Restoration should be called when needed
        assert mock_restore.called


@pytest.mark.asyncio
async def test_reset_whitelist_subscription_traffic_rejects_below_min_used():
    session = AsyncMock()
    tariff = _make_lte_tariff(reset_min_used_gb=10)
    sub = Subscription(
        id=10,
        user_id=1,
        status='active',
        whitelist_traffic_limit_gb=50,
        whitelist_traffic_used_bytes=int(5.0 * 1024**3),
        tariff_id=1,
    )
    sub.tariff = tariff

    with patch('app.database.crud.subscription._lock_subscription_row', return_value=sub):
        with pytest.raises(ValueError, match='below_min_used'):
            await reset_whitelist_subscription_traffic(
                session=session,
                subscription_id=10,
                user_id=1,
            )


@pytest.mark.asyncio
async def test_reset_whitelist_subscription_traffic_rejects_monthly_limit():
    session = AsyncMock()
    tariff = _make_lte_tariff(reset_max_per_month=1)
    sub = Subscription(
        id=10,
        user_id=1,
        status='active',
        whitelist_traffic_limit_gb=50,
        whitelist_traffic_used_bytes=int(15.0 * 1024**3),
        tariff_id=1,
    )
    sub.tariff = tariff

    with patch('app.database.crud.subscription._lock_subscription_row', return_value=sub),          patch('app.database.crud.subscription.count_monthly_traffic_purchases', return_value=1):
        with pytest.raises(ValueError, match='monthly_limit_exceeded'):
            await reset_whitelist_subscription_traffic(
                session=session,
                subscription_id=10,
                user_id=1,
            )


@pytest.mark.asyncio
async def test_add_subscription_traffic_regular_monthly_limit():
    session = AsyncMock()
    tariff = _make_standard_tariff()
    sub = Subscription(
        id=10,
        user_id=1,
        status='active',
        traffic_limit_gb=350,
        tariff_id=2,
    )
    sub.tariff = tariff

    # 1. When user already has 2 top-ups this month and max is 2 -> raises error
    with patch('app.database.crud.subscription._lock_subscription_row', return_value=sub),          patch('app.database.crud.subscription.count_monthly_traffic_purchases', return_value=2):
        with pytest.raises(ValueError, match='monthly_limit_exceeded'):
            await add_subscription_traffic(
                session=session,
                subscription_id=10,
                traffic_gb=100,
                cost_kopeks=5000,
                user_id=1,
            )

    # 2. When user has 1 top-up -> success
    with patch('app.database.crud.subscription._lock_subscription_row', return_value=sub),          patch('app.database.crud.subscription.count_monthly_traffic_purchases', return_value=1):
        updated_sub, purchase = await add_subscription_traffic(
            session=session,
            subscription_id=10,
            traffic_gb=100,
            cost_kopeks=5000,
            user_id=1,
        )
        assert updated_sub.traffic_limit_gb == 450
        assert purchase.traffic_gb == 100


@pytest.mark.asyncio
async def test_cabinet_traffic_routes():
    user = User(id=1, balance_kopeks=50000)
    session = AsyncMock()
    lte_tariff = _make_lte_tariff()
    lte_sub = Subscription(
        id=10,
        user_id=1,
        status='active',
        is_trial=False,
        traffic_limit_gb=350,
        whitelist_traffic_limit_gb=50,
        whitelist_traffic_used_bytes=int(12.5 * 1024**3),
        tariff_id=1,
    )
    lte_sub.tariff = lte_tariff

    # 1. Whitelist scope query for packages returns 400
    with pytest.raises(HTTPException) as exc_info:
        await get_traffic_packages(scope='whitelist', user=user, session=session)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == 'whitelist_packages_removed'

    # 2. Whitelist purchase returns 400
    with pytest.raises(HTTPException) as exc_info:
        await purchase_traffic(
            scope='whitelist',
            payload=SimpleNamespace(traffic_gb=50),
            user=user,
            session=session,
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == 'whitelist_packages_removed'

    # 3. Regular topup on LTE tariff returns 400
    with patch('app.cabinet.routes.subscription_modules.traffic._get_single_active_subscription', return_value=lte_sub):
        with pytest.raises(HTTPException) as exc_info:
            await purchase_traffic(
                scope='regular',
                payload=SimpleNamespace(traffic_gb=100),
                user=user,
                session=session,
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == 'lte_tariff_topup_not_allowed'

    # 4. Save traffic reset cart
    with patch('app.cabinet.routes.subscription_modules.traffic._get_single_active_subscription', return_value=lte_sub),          patch('app.cabinet.routes.subscription_modules.traffic.save_pending_subscription_cart', AsyncMock()):
        cart_resp = await save_traffic_reset_cart(
            payload=TrafficResetSaveCartRequest(subscription_id=10),
            user=user,
            session=session,
        )
        assert cart_resp['status'] == 'ok'
        assert cart_resp['cart_mode'] == 'traffic_reset'
        assert cart_resp['price_kopeks'] == 15000


@pytest.mark.asyncio
async def test_auto_purchase_service_handles_traffic_reset():
    session = AsyncMock()
    user = User(id=1, balance_kopeks=50000, telegram_id=123)
    tariff = _make_lte_tariff()
    sub = Subscription(
        id=10,
        user_id=1,
        status='active',
        traffic_limit_gb=350,
        whitelist_traffic_limit_gb=50,
        whitelist_traffic_used_bytes=int(12.5 * 1024**3),
        tariff_id=1,
    )
    sub.tariff = tariff

    pending_cart = {
        'cart_mode': 'traffic_reset',
        'subscription_id': 10,
        'price_kopeks': 15000,
    }

    with patch('app.database.crud.subscription.get_subscription_by_id_for_user', return_value=sub), \
         patch('app.database.crud.user.lock_user_for_pricing', return_value=user), \
         patch('app.database.crud.user.subtract_user_balance', AsyncMock()) as mock_sub, \
         patch('app.database.crud.subscription.reset_whitelist_subscription_traffic', return_value={
             'success': True,
             'cleared_gb': 12.5,
             'new_used_gb': 0.0,
             'limit_gb': 50,
             'price_kopeks': 15000,
         }) as mock_reset, \
         patch('app.services.subscription_auto_purchase_service._delete_cart_for_subscription', AsyncMock()) as mock_delete, \
         patch('app.database.crud.transaction.create_transaction', AsyncMock()), \
         patch('app.services.cabinet_purchase_notification_service.notify_telegram_user_about_cabinet_purchase', AsyncMock()):

        executed = await _auto_reset_traffic(session, user, pending_cart)
        assert executed is True
        assert mock_reset.called
        assert mock_delete.called
        assert mock_sub.called


def test_traffic_routes_have_no_var_keyword_params():
    import inspect

    from app.cabinet.routes.subscription_modules.traffic import router

    for route in router.routes:
        endpoint = getattr(route, 'endpoint', None)
        if endpoint and callable(endpoint):
            sig = inspect.signature(endpoint)
            for param_name, param in sig.parameters.items():
                assert param.kind != inspect.Parameter.VAR_KEYWORD, (
                    f"Route {route.path} has VAR_KEYWORD param '{param_name}' which FastAPI treats as required query parameter"
                )

