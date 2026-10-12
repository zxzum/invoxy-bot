import json
from types import SimpleNamespace

from app.cabinet.routes.public_catalog import present_public_tariff, present_public_trial
from app.config import settings


def test_public_tariff_drops_servers_and_labels_every_price(monkeypatch):
    monkeypatch.setattr(settings, 'PRICE_ROUNDING_ENABLED', True)
    payload = {
        'id': 2,
        'name': 'Стандарт',
        'description': 'Описание из кассы',
        'servers': [{'uuid': 'secret-squad-uuid', 'name': 'Default-Squad'}],
        'servers_count': 2,
        'is_current': False,
        'device_price_kopeks': 3000,
        'daily_price_kopeks': 0,
        'price_per_day_kopeks': 0,
        'whitelist_reset_price_kopeks': 15000,
        'traffic_reset_mode': 'MONTH_ROLLING',
        'traffic_topup_enabled': True,
        'traffic_topup_packages': {'100': 7000},
        'periods': [
            {
                'days': 30,
                'months': 1,
                'price_kopeks': 20000,
                'price_label': '200 ₽',
                'price_per_month_kopeks': 20000,
                'price_per_month_label': '200 ₽',
            },
            {
                'days': 90,
                'months': 3,
                'price_kopeks': 57000,
                'price_label': '570 ₽',
                'price_per_month_kopeks': 19000,
                'price_per_month_label': '190 ₽',
            },
        ],
    }
    tariff = type(
        'Tariff',
        (),
        {
            'whitelist_traffic_topup_enabled': True,
            'get_whitelist_traffic_topup_packages': lambda self: {20: 5000},
        },
    )()

    public = present_public_tariff(payload, tariff)
    encoded = json.dumps(public, ensure_ascii=False)

    assert 'servers' not in public
    assert 'secret-squad-uuid' not in encoded
    assert 'Default-Squad' not in encoded
    assert public['device_price_label'] == '30 ₽'
    assert public['traffic_topup_offers'] == [{'gb': 100, 'price_kopeks': 7000, 'price_label': '70 ₽'}]
    assert public['whitelist_topup_offers'] == [{'gb': 20, 'price_kopeks': 5000, 'price_label': '50 ₽'}]
    assert public['traffic_reset_label'] == 'Трафик обновляется через месяц после оплаты'
    assert public['periods'][1]['saving_kopeks'] == 3000
    assert public['periods'][1]['saving_label'] == '30 ₽'
    assert public['description'] == 'Описание из кассы'


def test_public_trial_uses_tariff_limits(monkeypatch):
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 2)
    monkeypatch.setattr(settings, 'TRIAL_TRAFFIC_LIMIT_GB', 1)
    monkeypatch.setattr(settings, 'TRIAL_DEVICE_LIMIT', 1)
    monkeypatch.setattr(settings, 'TRIAL_PAYMENT_ENABLED', False)
    monkeypatch.setattr(settings, 'PRICE_ROUNDING_ENABLED', True)
    trial = type(
        'Trial',
        (),
        {
            'traffic_limit_gb': 10,
            'device_limit': 1,
            'whitelist_traffic_limit_gb': 5,
            'trial_duration_days': 2,
        },
    )()

    payload = present_public_trial(trial)

    assert payload is not None
    assert payload['traffic_limit_gb'] == 10
    assert payload['whitelist_traffic_limit_gb'] == 5
    assert payload['requires_payment'] is False
    assert payload['price_kopeks'] == 0


def test_public_trial_hidden_when_duration_is_zero(monkeypatch):
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 0)
    assert present_public_trial(None) is None


async def test_public_catalog_uses_default_promo_group(monkeypatch):
    from app.cabinet.routes import public_catalog

    seen: dict[str, object] = {}

    async def fake_default(db):
        return type('Group', (), {'id': 7})()

    async def fake_tariffs(db, promo_group_id):
        seen['group'] = promo_group_id
        return []

    async def fake_trial(db):
        return None

    monkeypatch.setattr(public_catalog, 'get_default_promo_group', fake_default)
    monkeypatch.setattr(public_catalog, 'get_tariffs_for_user', fake_tariffs)
    monkeypatch.setattr(public_catalog, 'get_trial_tariff', fake_trial)
    monkeypatch.setattr(
        public_catalog,
        'settings',
        SimpleNamespace(
            is_tariffs_mode=lambda: True,
            get_sales_mode=lambda: 'tariffs',
            get_bot_username=lambda: 'invoxy_bot',
            get_support_contact_url=lambda: None,
            TRIAL_DURATION_DAYS=0,
        ),
    )

    catalog = await public_catalog.build_public_catalog(db=None)

    assert seen['group'] == 7
    assert catalog['sales_mode'] == 'tariffs'
