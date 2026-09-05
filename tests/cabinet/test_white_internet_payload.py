from types import SimpleNamespace

from app.cabinet.routes.subscription_modules.multi_tariff import _subscription_to_list_item


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
