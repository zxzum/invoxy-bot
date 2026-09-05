from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.subscription import my_subscriptions
from app.handlers.subscription.my_subscriptions import _format_subscription_line, show_subscription_detail
from app.utils.formatters import format_whitelist_traffic


def _subscription(**overrides):
    values = {
        'id': 7,
        'actual_status': 'active',
        'status_display': 'Активна',
        'tariff': SimpleNamespace(name='Стандарт'),
        'traffic_limit_gb': 100,
        'traffic_used_gb': 12.5,
        'device_limit': 3,
        'end_date': None,
        'subscription_url': None,
        'whitelist_traffic_limit_gb': 5,
        'whitelist_traffic_used_bytes': 2 * 1024**3,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_format_whitelist_traffic_uses_bytes_and_text_bar() -> None:
    assert format_whitelist_traffic(2 * 1024**3, 5) == '2.0 / 5 ГБ [████░░░░░░] 40%'
    assert format_whitelist_traffic(10 * 1024**3, 5) == '10.0 / 5 ГБ [██████████] 100%'
    assert format_whitelist_traffic(2 * 1024**3, 0) == ''


def test_multi_subscription_line_displays_whitelist_quota() -> None:
    text = _format_subscription_line(_subscription(), 1)

    assert '🔐 <b>Белый интернет: 2.0 / 5 ГБ [████░░░░░░] 40%</b>' in text


@pytest.mark.anyio('asyncio')
async def test_single_subscription_detail_displays_whitelist_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    subscription = _subscription()
    monkeypatch.setattr(my_subscriptions, 'get_subscription_by_id_for_user', AsyncMock(return_value=subscription))

    callback = SimpleNamespace(
        data='sm:7',
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )
    state = SimpleNamespace(update_data=AsyncMock())
    db_user = SimpleNamespace(id=1, language='ru')

    await show_subscription_detail(callback, db_user, SimpleNamespace(), state)

    text = callback.message.edit_text.await_args.args[0]
    assert '🔐 <b>Белый интернет: 2.0 / 5 ГБ [████░░░░░░] 40%</b>' in text
