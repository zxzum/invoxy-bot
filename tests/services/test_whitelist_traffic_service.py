import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import Tariff
from app.services.whitelist_traffic_service import (
    _effective_whitelist_squads,
    _panel_squad_ids,
    _usage_by_user,
)


BOOTSTRAP_PATH = Path(__file__).resolve().parents[3] / 'deploy' / 'bootstrap_invoxy.py'


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location('invoxy_bootstrap', BOOTSTRAP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError('Could not load Invoxy bootstrap')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _BootstrapDb:
    def __init__(self, tariffs):
        self.tariffs = tariffs

    async def execute(self, query):
        entity = query.column_descriptions[0]['entity']
        return _Result([] if entity.__name__ == 'PromoGroup' else self.tariffs)

    async def refresh(self, *_args):
        return None


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


@pytest.mark.asyncio
async def test_production_seed_uses_scoped_package_prices_and_exact_device_caps(monkeypatch):
    bootstrap = _load_bootstrap()
    monkeypatch.setattr(bootstrap, 'upsert_system_setting', AsyncMock())
    tariffs = [
        Tariff(id=1, name='Стандарт 🛡️'),
        Tariff(id=2, name='Стандарт 🌐 Белый интернет'),
        Tariff(id=3, name='Премиум 💎 Белый интернет'),
        Tariff(id=4, name='Пробный период'),
    ]
    db = _BootstrapDb(tariffs)

    await bootstrap._ensure_tariffs(db)
    await bootstrap._ensure_tariffs(db)

    by_name = {tariff.name: tariff for tariff in tariffs}
    basic = by_name['Стандарт 🛡️']
    standard_white = by_name['Стандарт 🌐 Белый интернет']
    premium_white = by_name['Премиум 💎 Белый интернет']
    trial = by_name['Пробный период']

    assert basic.get_traffic_topup_packages() == {100: 5000, 300: 15000}
    assert standard_white.get_traffic_topup_packages() == {100: 5000, 300: 15000}
    assert standard_white.get_whitelist_traffic_topup_packages() == {50: 15000, 100: 30000}
    assert premium_white.get_traffic_topup_packages() == {100: 5000, 300: 15000}
    assert premium_white.get_whitelist_traffic_topup_packages() == {50: 15000, 100: 30000}
    assert basic.allowed_squads == [bootstrap.JUST_VPN_SQUAD_UUID]
    assert standard_white.allowed_squads == [bootstrap.JUST_VPN_SQUAD_UUID, bootstrap.WHITELIST_SQUAD_UUID]
    assert premium_white.allowed_squads == [bootstrap.JUST_VPN_SQUAD_UUID, bootstrap.WHITELIST_SQUAD_UUID]
    assert (basic.device_limit, basic.device_price_kopeks, basic.max_device_limit) == (3, 3000, 10)
    assert (standard_white.device_limit, standard_white.device_price_kopeks, standard_white.max_device_limit) == (
        5,
        5000,
        10,
    )
    assert (premium_white.device_limit, premium_white.device_price_kopeks, premium_white.max_device_limit) == (
        10,
        5000,
        15,
    )
    assert (trial.traffic_limit_gb, trial.whitelist_traffic_limit_gb) == (10, 5)
