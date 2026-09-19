"""Cabinet purchase mirror sends the paid_successful banner (INVOXY).

`notify_telegram_user_about_cabinet_purchase` must prefer a photo send with
the per-screen banner and fall back to the shared text delivery when the
banner is unavailable or the photo send fails.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.services.cabinet_purchase_notification_service as m


def _user() -> MagicMock:
    user = MagicMock()
    user.id = 1
    user.telegram_id = 111
    return user


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.send_photo = AsyncMock(return_value=SimpleNamespace(photo=[SimpleNamespace(file_id='fid')]))
    bot.session = MagicMock()
    bot.session.close = AsyncMock()
    return bot


def _settings() -> SimpleNamespace:
    return SimpleNamespace(is_notifications_enabled=lambda: True)


async def test_purchase_mirror_prefers_banner_photo(monkeypatch) -> None:
    monkeypatch.setattr(m, 'settings', _settings())
    monkeypatch.setattr(m, 'get_screen_banner', lambda kind: 'BANNER')
    monkeypatch.setattr(m, 'cache_screen_banner_file_id', MagicMock())
    send = AsyncMock()
    monkeypatch.setattr(m.notification_delivery_service, 'send_notification', send)
    bot = _bot()
    monkeypatch.setattr('app.bot_factory.create_bot', lambda: bot)

    await m.notify_telegram_user_about_cabinet_purchase(_user(), '✅ куплен')

    bot.send_photo.assert_awaited_once()
    kwargs = bot.send_photo.await_args.kwargs
    assert kwargs['chat_id'] == 111
    assert kwargs['caption'] == '✅ куплен'
    assert kwargs['photo'] == 'BANNER'
    send.assert_not_awaited()


async def test_purchase_mirror_falls_back_to_text_without_banner(monkeypatch) -> None:
    monkeypatch.setattr(m, 'settings', _settings())
    monkeypatch.setattr(m, 'get_screen_banner', lambda kind: None)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(m.notification_delivery_service, 'send_notification', send)
    bot = _bot()
    monkeypatch.setattr('app.bot_factory.create_bot', lambda: bot)

    await m.notify_telegram_user_about_cabinet_purchase(_user(), '✅ куплен')

    bot.send_photo.assert_not_awaited()
    send.assert_awaited_once()


async def test_purchase_mirror_falls_back_to_text_when_photo_fails(monkeypatch) -> None:
    monkeypatch.setattr(m, 'settings', _settings())
    monkeypatch.setattr(m, 'get_screen_banner', lambda kind: 'BANNER')
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(m.notification_delivery_service, 'send_notification', send)
    bot = _bot()
    bot.send_photo = AsyncMock(side_effect=RuntimeError('nope'))
    monkeypatch.setattr('app.bot_factory.create_bot', lambda: bot)

    await m.notify_telegram_user_about_cabinet_purchase(_user(), '✅ куплен')

    send.assert_awaited_once()
    bot.session.close.assert_awaited_once()
