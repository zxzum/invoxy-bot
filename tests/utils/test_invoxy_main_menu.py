from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import types
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText

from app.config import Settings, settings
from app.handlers import invoxy_reply_menu, menu, start
from app.keyboards import inline
from app.localization.texts import get_texts
from app.utils import invoxy_main_menu as compact, message_patch, photo_message, rich_menu, screen_banners


@pytest.fixture(autouse=True)
def compact_settings(monkeypatch):
    monkeypatch.setattr(screen_banners, '_file_id_cache', {})
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'compact')
    monkeypatch.setattr(settings, 'MENU_LAYOUT_ENABLED', False)
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_ENABLED', False)
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_INLINE_BUTTONS', True)
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', True)
    monkeypatch.setattr(settings, 'SERVICE_WEBSITE_URL', 'https://invoxy.example')
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://invoxy.example')
    monkeypatch.setattr(settings, 'MINIAPP_SERVICE_NAME_RU', 'Invoxy VPN')
    monkeypatch.setattr(settings, 'MINIAPP_SERVICE_NAME_EN', 'Invoxy VPN')
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 3)
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'none')
    monkeypatch.setattr(inline, 'get_display_subscription_link', lambda sub: 'https://sub.example/key')
    monkeypatch.setattr(compact, 'get_tariff_by_id', AsyncMock(return_value=SimpleNamespace(name='Стандарт LTE')))


def _message():
    message = AsyncMock(spec=types.Message)
    message.chat = SimpleNamespace(id=42)
    message.text = 'Previous screen'
    message.photo = []
    for method in ('answer', 'answer_photo', 'delete', 'edit_text', 'edit_media'):
        setattr(message, method, AsyncMock())
    return message


def _callback():
    callback = AsyncMock(spec=types.CallbackQuery)
    callback.answer = AsyncMock()
    callback.bot = AsyncMock()
    return callback


def subscription(status='active', trial=False):
    return SimpleNamespace(
        id=7,
        tariff_id=3,
        is_trial=trial,
        actual_status=status,
        is_pending_trial=False,
        is_active=status in {'active', 'trial'},
        traffic_used_gb=0,
        traffic_limit_gb=600,
        whitelist_traffic_used_bytes=2 * 1024**3,
        whitelist_traffic_limit_gb=50,
        device_limit=5,
        end_date=datetime.now(UTC) + timedelta(days=45),
    )


def user(sub=None):
    return SimpleNamespace(
        id=1,
        telegram_id=42,
        language='ru',
        subscription=sub,
        subscriptions=[sub] if sub else [],
        has_had_paid_subscription=not bool(sub and sub.is_trial),
        balance_kopeks=0,
    )


def keyboard(sub=None, show_all=False, balance_kopeks=0):
    active = bool(sub and sub.actual_status in {'active', 'trial', 'limited'})
    return inline.get_main_menu_keyboard(
        has_active_subscription=active,
        subscription_is_active=active,
        has_had_paid_subscription=bool(sub),
        subscription=sub,
        balance_kopeks=balance_kopeks,
        show_all=show_all,
    )


def flattened(markup):
    return [button for row in markup.inline_keyboard for button in row]


def identity(button):
    return (button.callback_data, button.url, button.web_app.url if button.web_app else None)


def test_active_menu_has_large_grouped_buttons_and_neutral_website():
    markup = keyboard(subscription())
    assert [len(row) for row in markup.inline_keyboard] == [1, 1, 2, 1, 2, 1]
    assert markup.inline_keyboard[0][0].style == 'success'
    assert [button.callback_data for button in markup.inline_keyboard[1]] == ['menu_subscription']
    assert markup.inline_keyboard[1][0].text == 'Управление подпиской'
    assert [button.callback_data for button in markup.inline_keyboard[2]] == ['menu_balance', 'buy_traffic']
    assert markup.inline_keyboard[3][0].web_app
    assert [button.text for button in markup.inline_keyboard[4]] == ['Поддержка', 'Ещё']
    assert markup.inline_keyboard[-1][0].text == 'Сайт'
    assert markup.inline_keyboard[-1][0].style is None


def test_all_secondary_actions_remain_reachable_and_source_unchanged():
    full = keyboard(subscription(), show_all=True)
    original = full.model_dump_json()
    main = compact.compact_keyboard(full, get_texts('ru'))
    more = compact.more_keyboard(full, get_texts('ru'))
    assert {identity(b) for b in flattened(full)} <= {identity(b) for b in flattened(main) + flattened(more)}
    assert full.model_dump_json() == original
    callbacks = {button.callback_data for button in flattened(more)}
    assert {'menu_promocode', 'menu_referrals', 'menu_info', 'back_to_menu'} <= callbacks
    assert all(len(row) <= 2 for row in more.inline_keyboard)


def test_active_trial_can_buy_without_leaving_home():
    markup = keyboard(subscription(trial=True))
    assert any(button.callback_data == 'menu_buy' for button in flattened(markup))
    assert len(markup.inline_keyboard) <= 7


def test_new_user_has_full_width_trial_and_purchase():
    markup = inline.get_main_menu_keyboard()
    assert {b.callback_data for row in markup.inline_keyboard[:2] for b in row} == {'menu_trial', 'menu_buy'}
    assert all(len(row) == 1 for row in markup.inline_keyboard[:2])
    assert len(markup.inline_keyboard) <= 7


@pytest.mark.parametrize('status', ['active', 'trial', 'limited', 'expired', 'disabled', 'pending'])
async def test_status_card_keeps_all_metrics_in_a_readable_quote(monkeypatch, status):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    result = await compact.compact_menu_text(user(subscription(status)), get_texts('ru'), AsyncMock())
    assert len(result.splitlines()) <= 11
    assert '<img' not in result and '<blockquote>' in result
    assert 'Баланс' not in result and 'Устройства: 5' in result
    assert 'LTE-трафик: 2.0 из 50 ГБ' in result
    assert 'Стандарт LTE' in result
    if status in {'active', 'trial', 'limited'}:
        assert 'Трафик: 0 из 600 ГБ' in result
    else:
        assert 'вернуться к VPN' in result


async def test_multi_subscription_summary_stays_bounded(monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(
        compact, 'get_all_subscriptions_by_user_id', AsyncMock(return_value=[subscription() for _ in range(12)])
    )
    result = await compact.compact_menu_text(user(subscription()), get_texts('ru'), AsyncMock())
    assert len(result.splitlines()) == 7
    assert 'Ещё подписок: 11' in result


async def test_pending_trial_is_not_presented_as_paid_access(monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    sub = subscription('pending', trial=True)
    sub.is_pending_trial = True
    result = await compact.compact_menu_text(user(sub), get_texts('ru'), AsyncMock())
    assert 'пробного периода' in result and 'Стандарт LTE' not in result


async def test_missing_catalog_name_keeps_actions_usable(monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(compact, 'get_all_subscriptions_by_user_id', AsyncMock(side_effect=RuntimeError('db')))
    monkeypatch.setattr(compact, 'get_tariff_by_id', AsyncMock(side_effect=RuntimeError('db')))
    result = await compact.compact_menu_text(user(subscription()), get_texts('ru'), AsyncMock())
    assert '<b>VPN</b>' in result and 'Трафик' in result


async def test_name_is_escaped_and_unlimited_is_clear(monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(compact, 'get_tariff_by_id', AsyncMock(return_value=SimpleNamespace(name='<script>&' * 30)))
    sub = subscription()
    sub.traffic_limit_gb = 0
    result = await compact.compact_menu_text(user(sub), get_texts('en'), AsyncMock())
    assert '<script>' not in result and '&lt;script&gt;' in result
    assert 'Unlimited' in result
    assert len(result) < 600


async def test_initial_menu_sends_banner_with_full_card(monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    bot = AsyncMock()
    assert await rich_menu.try_send_rich_main_menu(
        bot, 42, user(subscription()), get_texts('ru'), AsyncMock(), keyboard(subscription())
    )
    bot.send_message.assert_not_awaited()
    bot.send_rich_message.assert_not_awaited()
    bot.send_photo.assert_awaited_once()
    sent = bot.send_photo.await_args.kwargs
    assert sent['reply_markup'].inline_keyboard and '<blockquote>' in sent['caption']


async def test_start_fallback_retains_banner():
    bot = AsyncMock()
    await start.send_menu_with_media(bot, 42, 'Compact menu', keyboard(subscription()), AsyncMock())
    bot.send_message.assert_not_awaited()
    bot.send_video.assert_not_awaited()
    bot.send_photo.assert_awaited_once()


async def test_message_patch_fallback_keeps_main_banner(monkeypatch):
    original = AsyncMock()
    monkeypatch.setattr(message_patch, '_original_answer', original)
    message = _message()
    await message_patch._answer_with_photo(message, 'Compact menu', reply_markup=keyboard(subscription()))
    original.assert_not_awaited()
    message.answer_photo.assert_awaited_once()


async def test_return_from_photo_edits_banner_and_keeps_buttons(monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    callback = _callback()
    callback.bot = AsyncMock()
    callback.message = _message()
    callback.message.photo = [SimpleNamespace(file_id='old-banner')]
    callback.message.chat = SimpleNamespace(id=42)
    assert await rich_menu.try_edit_rich_main_menu(
        callback, user(subscription()), get_texts('ru'), AsyncMock(), keyboard(subscription())
    )
    callback.message.delete.assert_not_awaited()
    callback.bot.send_message.assert_not_awaited()
    callback.message.edit_media.assert_awaited_once()
    sent = callback.message.edit_media.await_args
    assert '<blockquote>' in sent.args[0].caption
    assert len(sent.kwargs['reply_markup'].inline_keyboard) == 6
    callback.message.answer.assert_not_awaited()


async def test_repeated_text_menu_does_not_recreate_message():
    callback = _callback()
    callback.message = _message()
    callback.message.photo = []
    callback.message.edit_text.side_effect = TelegramBadRequest(EditMessageText(text='test'), 'message is not modified')
    await photo_message.edit_or_answer_photo(callback, 'test', keyboard(subscription()), force_text=True)
    callback.message.delete.assert_not_awaited()


async def test_more_callback_edits_in_place_and_acknowledges(monkeypatch):
    sub = subscription()
    monkeypatch.setattr(invoxy_reply_menu.MainMenuButtonService, 'get_buttons_for_user', AsyncMock(return_value=[]))
    monkeypatch.setattr(invoxy_reply_menu, 'has_subscription_checkout_draft', AsyncMock(return_value=False))
    monkeypatch.setattr(invoxy_reply_menu.user_cart_service, 'has_user_cart', AsyncMock(return_value=False))
    edit = AsyncMock()
    monkeypatch.setattr(invoxy_reply_menu, 'edit_or_answer_photo', edit)
    callback = _callback()
    callback.message = _message()
    await invoxy_reply_menu.handle_more_menu(callback, AsyncMock(), user(sub))
    assert edit.await_args.kwargs['media'] is not None
    assert edit.await_args.kwargs['media_kind'] == 'main'
    assert 'menu_promocode' in {b.callback_data for b in flattened(edit.await_args.args[2])}
    callback.answer.assert_awaited_once()


def test_compact_mode_is_valid_and_retains_bot_callbacks():
    assert Settings.normalize_main_menu_mode(' compact ') == 'compact'
    assert not settings.is_cabinet_mode()
    assert not settings.is_text_main_menu_mode()


async def test_classic_and_rich_entry_points_share_compact_text(monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    account = user(subscription())
    assert await menu.get_main_menu_text(
        account, get_texts('ru'), AsyncMock()
    ) == await rich_menu.build_main_menu_rich_html(account, get_texts('ru'), AsyncMock())


@pytest.mark.parametrize('lte_limit', [0, 50])
async def test_lte_and_unlimited_devices_are_not_misrepresented(monkeypatch, lte_limit):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    sub = subscription()
    sub.whitelist_traffic_limit_gb = lte_limit
    sub.whitelist_traffic_used_bytes = None
    sub.device_limit = 0
    result = await compact.compact_menu_text(user(sub), get_texts('ru'), AsyncMock())
    assert ('LTE-трафик: 0.0 из 50 ГБ' in result) == bool(lte_limit)
    assert 'Устройства: ∞' in result


async def test_main_banner_is_mandatory_even_when_generic_logos_disabled(monkeypatch):
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', False)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    bot = AsyncMock()
    bot.send_photo.return_value = SimpleNamespace(photo=[SimpleNamespace(file_id='main-banner')])
    await rich_menu.try_send_rich_main_menu(
        bot, 42, user(subscription()), get_texts('ru'), AsyncMock(), keyboard(subscription())
    )
    bot.send_photo.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    callback = _callback()
    callback.message = _message()
    await rich_menu.try_edit_rich_main_menu(
        callback, user(subscription()), get_texts('ru'), AsyncMock(), keyboard(subscription())
    )
    callback.message.edit_media.assert_awaited_once()
    callback.message.edit_text.assert_not_awaited()


async def test_repeated_banner_menu_does_not_recreate_message():
    callback = _callback()
    callback.message = _message()
    callback.message.edit_media.side_effect = TelegramBadRequest(
        EditMessageText(text='test'), 'message is not modified'
    )
    await photo_message.edit_or_answer_photo(
        callback, 'test', keyboard(subscription()), media='main-banner', media_kind='main'
    )
    callback.message.delete.assert_not_awaited()
    callback.message.answer_photo.assert_not_awaited()


async def test_daily_tariff_omits_misleading_expiry_date(monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(
        compact, 'get_tariff_by_id', AsyncMock(return_value=SimpleNamespace(name='Daily', is_daily=True))
    )
    result = await compact.compact_menu_text(user(subscription()), get_texts('ru'), AsyncMock())
    assert 'До ' not in result
    assert 'LTE-трафик' in result and 'Устройства' in result


async def test_return_from_text_restores_photo_if_edit_is_not_possible(monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    callback = _callback()
    callback.message = _message()
    callback.message.edit_media.side_effect = TelegramBadRequest(EditMessageText(text='test'), 'no media to edit')
    callback.message.answer_photo.return_value = SimpleNamespace(photo=[SimpleNamespace(file_id='banner-file')])
    assert await rich_menu.try_edit_rich_main_menu(
        callback, user(subscription()), get_texts('ru'), AsyncMock(), keyboard(subscription())
    )
    callback.message.answer_photo.assert_awaited_once()
    callback.message.answer.assert_not_awaited()


@pytest.mark.parametrize('balance_kopeks', [0, 12500])
async def test_balance_appears_only_on_its_button(monkeypatch, balance_kopeks):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    account = user(subscription())
    account.balance_kopeks = balance_kopeks
    result = await compact.compact_menu_text(account, get_texts('ru'), AsyncMock())
    assert 'Баланс' not in result
    markup = keyboard(account.subscription, balance_kopeks=balance_kopeks)
    button = next(b for b in flattened(markup) if b.callback_data == 'menu_balance')
    assert button.text == f'💰 Баланс: {get_texts("ru").format_price(balance_kopeks)}'


async def test_no_subscription_card_has_no_balance(monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    result = await compact.compact_menu_text(user(), get_texts('ru'), AsyncMock())
    assert 'Баланс' not in result
    assert '<blockquote>' in result
