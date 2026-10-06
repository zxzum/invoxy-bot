import pytest

from app.config import settings
from app.keyboards import invoxy_reply
from app.utils import invoxy_links


@pytest.fixture(autouse=True)
def _urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'SERVICE_WEBSITE_URL', 'https://site.example')
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://invoxy.example')


def _labels(markup) -> list[str]:
    return [button.text for row in markup.keyboard for button in row]


@pytest.mark.parametrize(
    ('language', 'expected'),
    [
        ('ru', ['Мой тариф', 'Продлить', 'Кабинет', 'Сайт', 'Поддержка', 'Язык']),
        ('en', ['My tariff', 'Renew', 'Cabinet', 'Website', 'Support', 'Language']),
        (None, ['Мой тариф', 'Продлить', 'Кабинет', 'Сайт', 'Поддержка', 'Язык']),
        ('ua', ['Мой тариф', 'Продлить', 'Кабинет', 'Сайт', 'Поддержка', 'Язык']),
        ('en-US', ['My tariff', 'Renew', 'Cabinet', 'Website', 'Support', 'Language']),
    ],
)
def test_keyboard_labels_per_language(language, expected) -> None:
    assert _labels(invoxy_reply.build_reply_keyboard(language)) == expected


def test_keyboard_is_persistent_two_columns() -> None:
    markup = invoxy_reply.build_reply_keyboard('ru')
    assert markup.is_persistent is True
    assert markup.resize_keyboard is True
    assert [len(row) for row in markup.keyboard] == [2, 2, 2]
    assert all(button.web_app is None for row in markup.keyboard for button in row)


@pytest.mark.parametrize('value', [None, '', '   ', 'ftp://site.example', 'site.example'])
def test_site_button_omitted_without_valid_website_url(monkeypatch, value) -> None:
    monkeypatch.setattr(settings, 'SERVICE_WEBSITE_URL', value)
    labels = _labels(invoxy_reply.build_reply_keyboard('ru'))
    assert 'Сайт' not in labels
    assert len(labels) == 5


def test_site_is_never_aliased_to_cabinet_url(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'SERVICE_WEBSITE_URL', None)
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://invoxy.example')
    assert invoxy_links.website_url() is None


@pytest.mark.parametrize(
    ('text', 'action'),
    [
        ('Продлить', 'renew'),
        ('Renew', 'renew'),
        ('Мой тариф', 'tariff'),
        ('My tariff', 'tariff'),
        ('Кабинет', 'cabinet'),
        ('Cabinet', 'cabinet'),
        ('Сайт', 'site'),
        ('Website', 'site'),
        ('Поддержка', 'support'),
        ('Support', 'support'),
        ('Язык', 'language'),
        ('Language', 'language'),
        ('привет', None),
        ('', None),
        (None, None),
    ],
)
def test_resolve_reply_action_matches_every_language(text, action) -> None:
    assert invoxy_reply.resolve_reply_action(text) == action


def test_normalize_language() -> None:
    assert invoxy_reply.normalize_language('EN') == 'en'
    assert invoxy_reply.normalize_language('ru-RU') == 'ru'
    assert invoxy_reply.normalize_language('fa') == 'ru'
    assert invoxy_reply.normalize_language(None) == 'ru'


def test_cabinet_urls() -> None:
    assert invoxy_links.renew_url(7) == 'https://invoxy.example/subscriptions/7/renew'
    assert invoxy_links.purchase_url() == 'https://invoxy.example/subscription/purchase'
    assert invoxy_links.cabinet_root_url() == 'https://invoxy.example'


@pytest.mark.parametrize('base', ['http://invoxy.example', '', None])
def test_cabinet_urls_require_https(monkeypatch, base) -> None:
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', base)
    assert invoxy_links.renew_url(7) is None
    assert invoxy_links.purchase_url() is None
    assert invoxy_links.cabinet_root_url() is None
