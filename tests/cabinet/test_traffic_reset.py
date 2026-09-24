from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.cabinet.routes.subscription_modules.traffic import (
    TrafficResetSaveCartRequest,
    build_traffic_reset_status,
    get_traffic_packages,
    perform_traffic_reset,
    purchase_traffic,
    save_traffic_reset_cart,
)
from app.database.crud.subscription import (
    ResetMainTrafficResult,
    add_subscription_traffic,
    reset_main_subscription_traffic,
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
        period_prices={'30': 20000},
        whitelist_traffic_limit_gb=whitelist_limit_gb,
        traffic_topup_enabled=False,
        traffic_topup_packages={},
        whitelist_traffic_topup_packages={},
        whitelist_reset_enabled=True,
        whitelist_reset_chunk_gb=reset_chunk_gb,
        whitelist_reset_price_kopeks=reset_price_kopeks,
        whitelist_reset_min_used_gb=reset_min_used_gb,
        whitelist_reset_max_per_month=reset_max_per_month,
        main_traffic_reset_enabled=True,
        main_traffic_reset_price_kopeks=None,
        main_traffic_reset_min_used_gb=100,
        main_traffic_reset_max_per_month=0,
        allowed_squads=['vpn-uuid', 'whitelist-uuid'],
    )


def _make_standard_tariff(
    *,
    price_30_days: int = 10000,
    main_reset_enabled: bool = True,
    main_reset_price_kopeks: int | None = None,
    main_reset_min_used_gb: int = 100,
    main_reset_max_per_month: int = 0,
) -> Tariff:
    return Tariff(
        id=2,
        name='Стандарт 🛡️',
        traffic_limit_gb=350,
        period_prices={'30': price_30_days},
        whitelist_traffic_limit_gb=0,
        traffic_topup_enabled=True,
        traffic_topup_packages={'100': 5000},
        traffic_topup_max_per_month=2,
        whitelist_reset_enabled=False,
        main_traffic_reset_enabled=main_reset_enabled,
        main_traffic_reset_price_kopeks=main_reset_price_kopeks,
        main_traffic_reset_min_used_gb=main_reset_min_used_gb,
        main_traffic_reset_max_per_month=main_reset_max_per_month,
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


def test_build_traffic_reset_status_regular_scope():
    # 1. Default price (30-day tariff price: 10000 kopeks = 100 rubles)
    tariff = _make_standard_tariff(price_30_days=10000, main_reset_min_used_gb=100)
    sub = SimpleNamespace(
        id=10,
        traffic_limit_gb=350,
        traffic_used_gb=125.0,
    )

    status = build_traffic_reset_status(
        tariff=tariff,
        sub=sub,
        used_this_month=0,
        scope='regular',
    )
    assert status.enabled is True
    assert status.price_kopeks == 10000
    assert status.price_rubles == 100.0
    assert status.used_gb == 125.0
    assert status.limit_gb == 350
    assert status.will_clear_gb == 125.0
    assert status.used_after_gb == 0.0
    assert status.remaining_this_month == 999  # 0 max_per_month = unlimited
    assert status.unavailable_reason is None

    # 2. Admin override price (e.g. 25000 kopeks = 250 rubles)
    tariff_override = _make_standard_tariff(
        price_30_days=10000,
        main_reset_price_kopeks=25000,
        main_reset_min_used_gb=100,
    )
    status_override = build_traffic_reset_status(
        tariff=tariff_override,
        sub=sub,
        used_this_month=0,
        scope='regular',
    )
    assert status_override.price_kopeks == 25000
    assert status_override.price_rubles == 250.0

    # 3. Below min used GB (e.g. 85 GB < 100 GB)
    sub_low = SimpleNamespace(
        id=10,
        traffic_limit_gb=350,
        traffic_used_gb=85.0,
    )
    status_low = build_traffic_reset_status(
        tariff=tariff,
        sub=sub_low,
        used_this_month=0,
        scope='regular',
    )
    assert status_low.unavailable_reason == 'below_min_used'

    # 4. Feature disabled on tariff
    tariff_disabled = _make_standard_tariff(main_reset_enabled=False)
    status_disabled = build_traffic_reset_status(
        tariff=tariff_disabled,
        sub=sub,
        used_this_month=0,
        scope='regular',
    )
    assert status_disabled.enabled is False
    assert status_disabled.unavailable_reason == 'disabled'

    # 5. Monthly limit reached
    tariff_limited = _make_standard_tariff(main_reset_max_per_month=2)
    status_limited = build_traffic_reset_status(
        tariff=tariff_limited,
        sub=sub,
        used_this_month=2,
        scope='regular',
    )
    assert status_limited.unavailable_reason == 'monthly_limit'
    assert status_limited.remaining_this_month == 0


@pytest.mark.asyncio
async def test_reset_main_subscription_traffic_logic():
    session = AsyncMock()
    tariff = _make_standard_tariff(
        main_reset_min_used_gb=100,
        main_reset_max_per_month=2,
    )
    sub = Subscription(
        id=10,
        user_id=1,
        status='limited',
        traffic_limit_gb=350,
        traffic_used_gb=125.0,
        tariff_id=2,
        remnawave_id='uuid-test-123',
    )
    sub.tariff = tariff

    mock_panel = AsyncMock()
    mock_panel.reset_user_traffic = AsyncMock(return_value=True)
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_panel
    mock_cm.__aexit__.return_value = None

    with patch('app.database.crud.subscription._lock_subscription_row', return_value=sub), \
         patch('app.services.remnawave_service.RemnaWaveService') as mock_rw_cls:
        mock_rw = mock_rw_cls.return_value
        mock_rw.is_configured = True
        mock_rw.get_api_client.return_value = mock_cm

        resp = await reset_main_subscription_traffic(
            session=session,
            subscription=sub,
            commit=False,
        )

        assert resp['success'] is True
        assert resp['cleared_gb'] == 125.0
        assert resp['new_used_gb'] == 0.0

        # Tuple unpacking compatibility test
        cleared, new_used = resp
        assert cleared == 125.0
        assert new_used == 0.0

        # DB values updated
        assert sub.traffic_used_gb == 0.0
        # Limited reactivated to active!
        assert sub.status == 'active'
        assert sub.main_reset_count == 1
        assert sub.main_reset_period_key is not None

        # Panel called
        mock_panel.reset_user_traffic.assert_awaited_once_with('uuid-test-123')


@pytest.mark.asyncio
async def test_reset_main_subscription_traffic_rejects_below_min_used():
    session = AsyncMock()
    tariff = _make_standard_tariff(main_reset_min_used_gb=100)
    sub = Subscription(
        id=10,
        user_id=1,
        status='active',
        traffic_limit_gb=350,
        traffic_used_gb=75.0,
        tariff_id=2,
    )
    sub.tariff = tariff

    with patch('app.database.crud.subscription._lock_subscription_row', return_value=sub):
        with pytest.raises(ValueError, match='below_min_used'):
            await reset_main_subscription_traffic(
                session=session,
                subscription=sub,
                commit=False,
            )


@pytest.mark.asyncio
async def test_reset_main_subscription_traffic_rejects_monthly_limit():
    from app.utils.traffic_topup_limits import get_local_month_period_key

    session = AsyncMock()
    tariff = _make_standard_tariff(main_reset_max_per_month=1)
    now = datetime.now(UTC)
    current_period = get_local_month_period_key(now)
    sub = Subscription(
        id=10,
        user_id=1,
        status='active',
        traffic_limit_gb=350,
        traffic_used_gb=125.0,
        tariff_id=2,
        main_reset_period_key=current_period,
        main_reset_count=1,
    )
    sub.tariff = tariff

    with patch('app.database.crud.subscription._lock_subscription_row', return_value=sub):
        with pytest.raises(ValueError, match='monthly_limit_exceeded'):
            await reset_main_subscription_traffic(
                session=session,
                subscription=sub,
                commit=False,
                now=now,
            )


@pytest.mark.asyncio
async def test_cabinet_traffic_routes_regular_scope():
    user = User(id=1, balance_kopeks=50000)
    session = AsyncMock()
    tariff = _make_standard_tariff(price_30_days=10000, main_reset_min_used_gb=100)
    sub = Subscription(
        id=20,
        user_id=1,
        status='active',
        is_trial=False,
        traffic_limit_gb=350,
        traffic_used_gb=120.0,
        tariff_id=2,
    )
    sub.tariff = tariff

    # 1. Save cart with regular scope
    with patch('app.cabinet.routes.subscription_modules.traffic._get_single_active_subscription', return_value=sub), \
         patch('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=tariff)), \
         patch('app.cabinet.routes.subscription_modules.traffic.save_pending_subscription_cart', AsyncMock()):
        cart_resp = await save_traffic_reset_cart(
            payload=TrafficResetSaveCartRequest(subscription_id=20, scope='regular'),
            user=user,
            session=session,
        )
        assert cart_resp['status'] == 'ok'
        assert cart_resp['cart_mode'] == 'traffic_reset'
        assert cart_resp['price_kopeks'] == 10000

    # 2. Perform traffic reset with regular scope
    with patch('app.cabinet.routes.subscription_modules.traffic._get_single_active_subscription', return_value=sub), \
         patch('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=tariff)), \
         patch('app.database.crud.user.lock_user_for_pricing', return_value=user), \
         patch('app.cabinet.routes.subscription_modules.traffic.subtract_user_balance', AsyncMock(return_value=True)) as mock_subtract, \
         patch('app.cabinet.routes.subscription_modules.traffic.create_transaction', AsyncMock()) as mock_tx, \
         patch('app.database.crud.subscription.reset_main_subscription_traffic', AsyncMock(return_value=ResetMainTrafficResult(cleared_gb=120.0, new_used_gb=0.0))) as mock_reset, \
         patch('app.services.cabinet_purchase_notification_service.notify_telegram_user_about_cabinet_purchase', AsyncMock()):

        resp = await perform_traffic_reset(
            subscription_id=20,
            scope='regular',
            user=user,
            session=session,
        )
        assert resp.success is True
        assert resp.cleared_gb == 120.0
        assert resp.new_used_gb == 0.0
        assert mock_subtract.called
        assert mock_tx.called
        assert mock_reset.called

