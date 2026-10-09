from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.handlers import invoxy_screens as screens


NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def _sub(
    sub_id: int = 5, *, status: str = 'active', days: int = 20, name: str = 'Стандарт', **extra
) -> SimpleNamespace:
    end = NOW + timedelta(days=days)
    data = {
        'id': sub_id,
        'tariff_id': 1,
        'tariff': SimpleNamespace(name=name, is_active=True, period_prices={'30': 19900, '90': 49900}),
        'actual_status': status,
        'is_active': status in ('active', 'trial'),
        'days_left': max(days, 0),
        'end_date': end,
        'traffic_limit_gb': 100,
        'traffic_used_gb': 12.34,
    }
    data.update(extra)
    return SimpleNamespace(**data)


def _user(*subs) -> SimpleNamespace:
    return SimpleNamespace(id=1, language='ru', subscriptions=list(subs))


def _buttons(screen):
    return [b for row in screen.markup.inline_keyboard for b in row]


def _web_app_urls(screen) -> list[str]:
    return [b.web_app.url for b in _buttons(screen) if b.web_app]


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://invoxy.example')
    monkeypatch.setattr(settings, 'SERVICE_WEBSITE_URL', 'https://site.example')
    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
    monkeypatch.setattr(settings, 'AVAILABLE_LANGUAGES', 'ru,en')
    pricing = SimpleNamespace(
        calculate_renewal_price=AsyncMock(return_value=SimpleNamespace(final_total=19900, original_total=19900))
    )
    monkeypatch.setattr(screens, 'pricing_engine', pricing)


# ---- renewable_subscriptions -------------------------------------------------


def test_renewable_excludes_disabled_pending_and_sorts_active_first() -> None:
    expired = _sub(1, status='expired', days=-3)
    active_late = _sub(2, days=40)
    active_soon = _sub(3, days=4)
    disabled = _sub(4, status='disabled')
    pending = _sub(6, status='pending')
    result = screens.renewable_subscriptions([expired, active_late, disabled, pending, active_soon])
    assert [s.id for s in result] == [3, 2, 1]


def test_renewable_excludes_classic_subs_in_tariffs_mode(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    classic = _sub(1, tariff_id=None, tariff=None)
    assert [s.id for s in screens.renewable_subscriptions([classic, _sub(2)])] == [2]


# ---- renew screen ------------------------------------------------------------


async def test_renew_one_active_opens_renew_page_directly_with_price() -> None:
    screen = await screens.build_renew_screen(None, _user(_sub(5)), 'ru')
    assert _web_app_urls(screen) == ['https://invoxy.example/subscriptions/5/renew']
    assert 'Стандарт' in screen.text
    assert '26.10.2026' in screen.text
    assert '199' in screen.text
    assert '1 месяц' in screen.text
    assert screen.banner == 'renew'
    assert _buttons(screen)[-1].callback_data == 'back_to_menu'


async def test_renew_expired_but_renewable_uses_expired_copy_and_banner() -> None:
    screen = await screens.build_renew_screen(None, _user(_sub(5, status='expired', days=-3)), 'ru')
    assert _web_app_urls(screen) == ['https://invoxy.example/subscriptions/5/renew']
    assert 'закончился 03.10.2026' in screen.text
    assert screen.banner == 'renew_expired'


async def test_renew_english() -> None:
    screen = await screens.build_renew_screen(None, _user(_sub(5)), 'en')
    assert 'Renewal' in screen.text
    assert 'From' in screen.text
    assert 'Main menu' in [b.text for b in _buttons(screen)]
    assert '1 month' in screen.text


async def test_renew_without_any_subscription_offers_purchase() -> None:
    screen = await screens.build_renew_screen(None, _user(), 'ru')
    assert 'Активного тарифа нет — давайте оформим.' in screen.text
    assert _web_app_urls(screen) == ['https://invoxy.example/subscription/purchase']
    assert 'menu_buy' in [b.callback_data for b in _buttons(screen)]
    assert screen.banner == 'subscription'
    en = await screens.build_renew_screen(None, _user(), 'en')
    assert 'let’s set one up' in en.text


async def test_renew_only_disabled_or_pending_is_inactive() -> None:
    screen = await screens.build_renew_screen(None, _user(_sub(5, status='disabled'), _sub(6, status='pending')), 'ru')
    assert screen.banner == 'subscription_inactive'
    assert 'отключён или ждёт оплаты' in screen.text
    assert _web_app_urls(screen) == []
    assert 'menu_support' in [b.callback_data for b in _buttons(screen)]


async def test_renew_price_failure_falls_back_to_text_without_price(monkeypatch) -> None:
    screens.pricing_engine.calculate_renewal_price = AsyncMock(side_effect=RuntimeError('boom'))
    screen = await screens.build_renew_screen(None, _user(_sub(5)), 'ru')
    assert 'От ' not in screen.text
    assert _web_app_urls(screen) == ['https://invoxy.example/subscriptions/5/renew']


async def test_renew_without_https_miniapp_has_no_web_app_button(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'http://invoxy.example')
    screen = await screens.build_renew_screen(None, _user(_sub(5)), 'ru')
    assert _web_app_urls(screen) == []
    assert _buttons(screen)[-1].callback_data == 'back_to_menu'


async def test_renew_many_is_paged_and_callbacks_fit_telegram_limit() -> None:
    subs = [_sub(i, days=i) for i in range(1, 8)]
    first = await screens.build_renew_screen(None, _user(*subs), 'ru', page=0)
    assert len(_web_app_urls(first)) == 5
    assert 'Какой продлить' in first.text
    nav = [b.callback_data for b in _buttons(first) if b.callback_data and b.callback_data.startswith('rnw:')]
    assert nav == ['rnw:1']
    second = await screens.build_renew_screen(None, _user(*subs), 'ru', page=1)
    assert len(_web_app_urls(second)) == 2
    nav = [b.callback_data for b in _buttons(second) if b.callback_data and b.callback_data.startswith('rnw:')]
    assert nav == ['rnw:0']
    for screen in (first, second):
        assert all(len((b.callback_data or '').encode()) < 64 for b in _buttons(screen))


async def test_renew_page_is_clamped() -> None:
    screen = await screens.build_renew_screen(None, _user(*[_sub(i, days=i) for i in range(1, 8)]), 'ru', page=99)
    assert len(_web_app_urls(screen)) == 2


# ---- tariff screen -----------------------------------------------------------


async def test_tariff_active_status_block() -> None:
    screen = await screens.build_tariff_screen(None, _user(_sub(5)), 'ru')
    assert 'Мой тариф' in screen.text
    assert 'Стандарт' in screen.text
    assert 'осталось 20 дн.' in screen.text
    assert '12.3 из 100 ГБ' in screen.text
    assert 'Всё в порядке.' in screen.text
    assert screen.banner == 'subscription'
    callbacks = [b.callback_data for b in _buttons(screen)]
    assert 'subscription_connect' in callbacks and 'back_to_menu' in callbacks
    assert _web_app_urls(screen) == ['https://invoxy.example/subscriptions/5/renew']


async def test_tariff_ending_soon_and_unlimited_traffic() -> None:
    screen = await screens.build_tariff_screen(None, _user(_sub(5, days=2, traffic_limit_gb=0)), 'en')
    assert 'Ending soon' in screen.text
    assert 'Unlimited' in screen.text


async def test_tariff_expired() -> None:
    screen = await screens.build_tariff_screen(None, _user(_sub(5, status='expired', days=-3)), 'ru')
    assert 'закончился 03.10.2026' in screen.text
    assert screen.banner == 'subscription'


async def test_tariff_none_and_inactive() -> None:
    none_screen = await screens.build_tariff_screen(None, _user(), 'ru')
    assert 'нет активного тарифа' in none_screen.text
    assert none_screen.banner == 'subscription'
    assert 'menu_buy' in [b.callback_data for b in _buttons(none_screen)]
    inactive = await screens.build_tariff_screen(None, _user(_sub(5, status='disabled')), 'ru')
    assert inactive.banner == 'subscription_inactive'


async def test_tariff_escapes_html_in_tariff_name() -> None:
    screen = await screens.build_tariff_screen(None, _user(_sub(5, name='<b>x</b>')), 'ru')
    assert '<b>x</b>' not in screen.text
    assert '&lt;b&gt;' in screen.text


async def test_tariff_with_several_subscriptions_links_to_all() -> None:
    screen = await screens.build_tariff_screen(None, _user(_sub(5), _sub(6, days=40)), 'ru')
    assert 'menu_subscription' in [b.callback_data for b in _buttons(screen)]


# ---- cabinet / site / language ----------------------------------------------


def test_cabinet_screen_is_text_only_banner_with_web_app_button() -> None:
    screen = screens.build_cabinet_screen('en')
    assert screen.banner is None
    assert _web_app_urls(screen) == ['https://invoxy.example']
    assert 'Cabinet' in screen.text


def test_cabinet_screen_unavailable_without_https(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', '')
    screen = screens.build_cabinet_screen('ru')
    assert _web_app_urls(screen) == []
    assert 'недоступен' in screen.text


def test_site_screen_uses_website_url_only(monkeypatch) -> None:
    screen = screens.build_site_screen('ru')
    assert [b.url for b in _buttons(screen) if b.url] == ['https://site.example']
    assert screen.banner is None
    monkeypatch.setattr(settings, 'SERVICE_WEBSITE_URL', None)
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://invoxy.example')
    empty = screens.build_site_screen('ru')
    assert [b.url for b in _buttons(empty) if b.url] == []


def test_language_picker_lists_only_available_languages(monkeypatch) -> None:
    screen = screens.build_language_picker('ru')
    assert [b.callback_data for b in _buttons(screen)][:2] == ['ilang:ru', 'ilang:en']
    assert screen.banner is None
    monkeypatch.setattr(settings, 'AVAILABLE_LANGUAGES', 'ru')
    only_ru = screens.build_language_picker('ru')
    assert [b.callback_data for b in _buttons(only_ru) if b.callback_data.startswith('ilang:')] == ['ilang:ru']


def test_site_screen_escapes_html_in_the_url(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'SERVICE_WEBSITE_URL', 'https://site.example/?a=1&b=<2>')
    screen = screens.build_site_screen('ru')
    assert 'a=1&amp;b=&lt;2&gt;' in screen.text
    assert '<2>' not in screen.text


async def test_slow_pricing_never_blocks_the_renew_button(monkeypatch) -> None:
    import asyncio

    async def _slow(*_args, **_kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(screens, 'PRICE_HINT_TIMEOUT_SECONDS', 0.05)
    monkeypatch.setattr(screens, 'pricing_engine', SimpleNamespace(calculate_renewal_price=_slow))
    screen = await screens.build_renew_screen(None, _user(_sub(5)), 'ru')
    assert 'От ' not in screen.text
    assert _web_app_urls(screen) == ['https://invoxy.example/subscriptions/5/renew']
