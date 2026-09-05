from types import SimpleNamespace

import pytest

from app.cabinet.routes.subscription_modules.multi_tariff import _subscription_to_list_item
from app.cabinet.routes.subscription_modules.purchase import _build_tariff_response
from app.database.models import Tariff


def test_list_payload_exposes_independent_white_internet_usage():
    subscription = SimpleNamespace(
        id=1, tariff=None, tariff_id=None, actual_status='active', traffic_limit_gb=350,
        traffic_used_gb=12, device_limit=3, end_date=None, subscription_url=None,
        subscription_crypto_link=None, is_trial=False, autopay_enabled=False,
        connected_squads=[], whitelist_traffic_limit_gb=50,
        whitelist_traffic_used_bytes=int(2.5 * 1024**3),
    )
    payload = _subscription_to_list_item(subscription).model_dump()
    assert payload['traffic_used_gb'] == 12
    assert payload['whitelist_traffic_limit_gb'] == 50
    assert payload['whitelist_traffic_used_gb'] == 2.5

    subscription.whitelist_traffic_limit_gb = None
    subscription.whitelist_traffic_used_bytes = None
    payload = _subscription_to_list_item(subscription).model_dump()
    assert payload['whitelist_traffic_limit_gb'] == 0
    assert payload['whitelist_traffic_used_gb'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('device_limit', 'device_price_kopeks', 'max_device_limit'),
    [(3, 3000, 10), (5, 5000, 10), (10, 5000, 15)],
)
async def test_purchase_payload_exposes_exact_tariff_device_caps(
    device_limit: int,
    device_price_kopeks: int,
    max_device_limit: int,
):
    tariff = Tariff(
        id=1,
        name='Test tariff',
        traffic_limit_gb=100,
        device_limit=device_limit,
        device_price_kopeks=device_price_kopeks,
        max_device_limit=max_device_limit,
        period_prices={'30': 10000},
        daily_price_kopeks=0,
        price_per_day_kopeks=0,
    )

    payload = await _build_tariff_response(object(), tariff)

    assert payload['device_limit'] == device_limit
    assert payload['base_device_limit'] == device_limit
    assert payload['device_price_kopeks'] == device_price_kopeks
    assert payload['max_device_limit'] == max_device_limit
