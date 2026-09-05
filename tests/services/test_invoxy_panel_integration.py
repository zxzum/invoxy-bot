from app.config import settings
from app.services.remnawave_service import _filter_owned_panel_users


def test_subscription_url_uses_public_host_without_sub_path(monkeypatch):
    monkeypatch.setattr(settings, 'REMNAWAVE_SUBSCRIPTION_PUBLIC_URL', 'https://sub.lazeika.xyz')

    assert (
        settings.normalize_subscription_url('https://panel.lazeika.xyz/9SDTQ_nArWWnEhyF')
        == 'https://sub.lazeika.xyz/9SDTQ_nArWWnEhyF'
    )
    assert (
        settings.normalize_subscription_url('https://panel.lazeika.xyz/sub/9SDTQ_nArWWnEhyF')
        == 'https://sub.lazeika.xyz/9SDTQ_nArWWnEhyF'
    )


def test_shared_panel_filter_keeps_linked_ids_and_owned_prefix(monkeypatch):
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_OWNER_PREFIX', 'invoxy_')
    panel_users = [
        {'id': 10, 'username': 'stealthnet_user'},
        {'id': 20, 'username': 'stealthnet_linked'},
        {'id': 30, 'username': 'invoxy_new_user'},
    ]

    owned = _filter_owned_panel_users(panel_users, {20})

    assert [user['id'] for user in owned] == [20, 30]
