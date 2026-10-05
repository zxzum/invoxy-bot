"""Сбой автопокупки сохранённой корзины после пополнения не должен теряться в логах:
деньги зачислены, а тариф не куплен — администраторам уходит оповещение."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.payment import common


@pytest.fixture
def user() -> SimpleNamespace:
    return SimpleNamespace(id=42, telegram_id=4242, username='demo', balance_kopeks=30000)


@pytest.fixture
def cart(monkeypatch) -> None:
    monkeypatch.setattr(
        common.user_cart_service,
        'get_user_cart',
        AsyncMock(return_value={'total_price': 20000, 'tariff_id': 1}),
    )


def _patch_auto_purchase(monkeypatch, side_effect) -> None:
    import app.services.subscription_auto_purchase_service as auto

    monkeypatch.setattr(auto, 'try_resume_disabled_daily_after_topup', AsyncMock(return_value=False))
    monkeypatch.setattr(auto, 'auto_purchase_saved_cart_after_topup', AsyncMock(side_effect=side_effect))


@pytest.mark.asyncio
async def test_alerts_admins_when_cart_auto_purchase_fails(monkeypatch, user, cart) -> None:
    _patch_auto_purchase(monkeypatch, RuntimeError('remnawave down'))
    alert = AsyncMock()
    monkeypatch.setattr(common, '_alert_admins_cart_purchase_failed', alert)

    await common.send_cart_notification_after_topup(user, 20000, AsyncMock(), bot=object(), notify_email=False)

    alert.assert_awaited_once()
    assert alert.await_args.args[1] is user


@pytest.mark.asyncio
async def test_no_alert_when_cart_auto_purchase_succeeds(monkeypatch, user, cart) -> None:
    _patch_auto_purchase(monkeypatch, None)
    alert = AsyncMock()
    monkeypatch.setattr(common, '_alert_admins_cart_purchase_failed', alert)

    await common.send_cart_notification_after_topup(user, 20000, AsyncMock(), bot=object(), notify_email=False)

    alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_alert_sends_admin_notification(monkeypatch, user) -> None:
    sent = AsyncMock(return_value=True)

    class FakeService:
        def __init__(self, bot) -> None:
            self.bot = bot

        send_admin_notification = sent

    import app.services.admin_notification_service as admin_module

    monkeypatch.setattr(admin_module, 'AdminNotificationService', FakeService)

    await common._alert_admins_cart_purchase_failed(object(), user, 20000, RuntimeError('boom'))

    sent.assert_awaited_once()
    text = sent.await_args.args[0]
    assert '42' in text
    assert 'boom' in text


@pytest.mark.asyncio
async def test_alert_is_skipped_without_bot(user) -> None:
    await common._alert_admins_cart_purchase_failed(None, user, 20000, RuntimeError('boom'))
