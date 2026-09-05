from types import SimpleNamespace

from app.services.whitelist_traffic_service import (
    _effective_whitelist_squads,
    _panel_squad_ids,
    _usage_by_user,
)


def test_panel_squad_ids_accepts_uuid_and_name_forms():
    user = SimpleNamespace(
        active_internal_squads=[
            {'uuid': '  WHITELIST-UUID '},
            {'name': 'WHITELIST'},
            'JUST_VPN',
        ]
    )

    assert _panel_squad_ids(user) == {'whitelist-uuid', 'whitelist', 'just_vpn'}


def test_usage_by_user_sums_only_valid_non_negative_values():
    payload = {
        'nodes': [
            {'users': [{'id': 7, 'totalBytes': 100}, {'id': '8', 'totalBytes': 50}]},
            {'users': [{'id': 7, 'totalBytes': 25}, {'id': 'bad', 'totalBytes': 999}]},
        ]
    }

    assert _usage_by_user(payload) == {7: 125, 8: 50}


def test_effective_whitelist_squads_removes_and_restores_access():
    subscription = SimpleNamespace(
        connected_squads=['regular', 'WHITE-LIST'],
        whitelist_traffic_limit_gb=5,
        whitelist_traffic_used_bytes=5 * 1024**3,
    )
    assert _effective_whitelist_squads(subscription, 'white-list') == ['regular']

    subscription.whitelist_traffic_limit_gb = 10
    assert _effective_whitelist_squads(subscription, 'white-list') == ['regular', 'WHITE-LIST']
