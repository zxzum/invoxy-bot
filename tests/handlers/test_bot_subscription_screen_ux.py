from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.database.crud import welcome_text
from app.handlers import migration
from app.handlers.subscription import my_subscriptions, purchase, tariff_purchase
from app.keyboards.inline import get_subscription_keyboard


def tariff(tariff_id=2, **changes):
    values = dict(
        id=tariff_id,
        name='Базовый',
        description='Для сайтов и общения.',
        is_daily=False,
        period_prices={'30': 10000},
        traffic_limit_gb=300,
        device_limit=5,
        whitelist_traffic_limit_gb=0,
        is_highlighted=False,
        trial_duration_days=2,
        allowed_squads=['vpn', 'white'],
    )
    values.update(changes)
    return SimpleNamespace(**values)


def callback(data):
    return SimpleNamespace(data=data, message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())


def user(**changes):
    values = dict(
        id=1,
        telegram_id=1,
        language='ru',
        promo_group_id=None,
        auth_type='telegram',
        get_primary_promo_group=lambda: None,
        is_trial_already_used=lambda: False,
    )
    values.update(changes)
    return SimpleNamespace(**values)


async def test_welcome_reads_changed_db_values_and_preserves_custom_copy(monkeypatch):
    import app.database.crud.tariff as crud

    tariffs = [tariff(name='<Базовый>')]
    monkeypatch.setattr(
        welcome_text, 'get_active_welcome_text', AsyncMock(return_value='Наш текст\n{tariffs}\n{trial}\n{user_name}')
    )
    lookup = AsyncMock(return_value=tariffs)
    monkeypatch.setattr(crud, 'get_tariffs_for_user', lookup)
    monkeypatch.setattr(
        crud,
        'get_trial_tariff',
        AsyncMock(return_value=tariff(5, traffic_limit_gb=10, device_limit=1, whitelist_traffic_limit_gb=5)),
    )
    monkeypatch.setattr(Settings, 'is_trial_disabled_for_user', lambda *a: False)
    monkeypatch.setattr(Settings, 'is_trial_paid_activation_enabled', lambda *a: False)
    text = await welcome_text.get_welcome_text_for_user(
        None, SimpleNamespace(first_name='<Иван>'), user(promo_group_id=42)
    )
    assert 'Наш текст' in text and '&lt;Базовый&gt;' in text and '&lt;Иван&gt;' in text
    assert '300 ГБ' in text and '5 устр.' in text and '100' in text
    assert '10 ГБ' in text and 'до 5 ГБ LTE' in text and '{tariffs}' not in text
    lookup.assert_awaited_with(None, 42)
    tariffs[0].traffic_limit_gb = 400
    tariffs[0].period_prices = {'30': 13000}
    updated = await welcome_text.get_welcome_text_for_user(None, 'Иван', user())
    assert '400 ГБ' in updated and '130' in updated and '300 ГБ' not in updated


async def test_custom_welcome_without_tokens_does_not_query_tariffs(monkeypatch):
    import app.database.crud.tariff as crud

    monkeypatch.setattr(welcome_text, 'get_active_welcome_text', AsyncMock(return_value='Привет, {user_name}!'))
    lookup = AsyncMock()
    monkeypatch.setattr(crud, 'get_tariffs_for_user', lookup)
    assert await welcome_text.get_welcome_text_for_user(None, 'Иван') == 'Привет, Иван!'
    lookup.assert_not_called()


async def test_preview_shows_only_selected_tariff_without_purchase(monkeypatch):
    tariffs = [tariff(), tariff(3, name='Стандарт LTE', whitelist_traffic_limit_gb=50)]
    monkeypatch.setattr(tariff_purchase, 'get_tariffs_for_user', AsyncMock(return_value=tariffs))
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda *a: False)
    proceed = AsyncMock()
    monkeypatch.setattr(tariff_purchase, '_proceed_with_selected_tariff', proceed)
    state = SimpleNamespace(clear=AsyncMock(), get_data=AsyncMock(return_value={}), update_data=AsyncMock())
    cb = callback('tariff_preview:3')
    await tariff_purchase.show_tariffs_list.__wrapped__(cb, user(), None, state)
    text = cb.message.edit_text.await_args.args[0]
    assert '<b>Стандарт LTE</b>' in text and '<b>Базовый</b>' not in text
    assert '<blockquote>Для сайтов и общения.</blockquote>' in text and '50 ГБ' in text
    buttons = [b for row in cb.message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard for b in row]
    assert next(b for b in buttons if b.callback_data == 'tariff_preview:3').style == 'success'
    assert any(b.callback_data == 'tariff_preview:2' for b in buttons)
    assert any(b.callback_data == 'tariff_select:3' for b in buttons)
    proceed.assert_not_awaited()


@pytest.mark.parametrize('data', ['tariff_preview:999', 'tariff_preview:wrong'])
async def test_preview_rejects_inaccessible_or_invalid_tariff(monkeypatch, data):
    monkeypatch.setattr(tariff_purchase, 'get_tariffs_for_user', AsyncMock(return_value=[tariff()]))
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda *a: False)
    cb = callback(data)
    state = SimpleNamespace(clear=AsyncMock(), get_data=AsyncMock(return_value={}), update_data=AsyncMock())
    await tariff_purchase.show_tariffs_list.__wrapped__(cb, user(), None, state)
    cb.message.edit_text.assert_not_called()
    assert cb.answer.await_args.kwargs['show_alert'] is True


async def test_trial_shows_actual_quota_without_random_server_claim(monkeypatch):
    import app.database.crud.tariff as crud

    monkeypatch.setattr(Settings, 'is_tariffs_mode', lambda *a: True)
    monkeypatch.setattr(Settings, 'is_trial_disabled_for_user', lambda *a: False)
    monkeypatch.setattr(Settings, 'is_trial_paid_activation_enabled', lambda *a: False)
    monkeypatch.setattr(Settings, 'is_devices_selection_enabled', lambda *a: True)
    monkeypatch.setattr(
        crud,
        'get_trial_tariff',
        AsyncMock(return_value=tariff(5, traffic_limit_gb=10, device_limit=1, whitelist_traffic_limit_gb=5)),
    )
    cb = callback('menu_trial')
    await purchase.show_trial_offer(cb, user(), None)
    text = cb.message.edit_text.await_args.args[0]
    assert '2 дня' in text and '10 ГБ' in text and '5 ГБ' in text
    assert 'Случайный' not in text and 'сквад' not in text and 'сервер' not in text


@pytest.mark.parametrize('eligible', [False, True])
async def test_migration_button_requires_confirmed_eligibility(monkeypatch, eligible):
    monkeypatch.setattr(
        migration, 'check_migration_eligibility', AsyncMock(return_value=object() if eligible else None)
    )
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda *a: False)
    can_migrate = await migration.can_offer_lazeika_migration(None, user())
    sub = SimpleNamespace(id=1, subscription_url=None, actual_status='active', is_trial=True)
    keyboard = get_subscription_keyboard('ru', True, True, sub, can_migrate=can_migrate)
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert ('lazeika_migrate:check' in callbacks) is eligible
    multi = my_subscriptions._build_subscription_detail_keyboard(1, sub, can_migrate=can_migrate, single_view=True)
    callbacks = [b.callback_data for row in multi.inline_keyboard for b in row]
    assert ('lazeika_migrate:check' in callbacks) is eligible
    assert callbacks[-1] == 'back_to_menu'


async def test_migration_outage_hides_offer(monkeypatch):
    monkeypatch.setattr(migration, 'check_migration_eligibility', AsyncMock(side_effect=TimeoutError))
    assert await migration.can_offer_lazeika_migration(None, user()) is False


async def test_one_subscription_goes_directly_to_card_multiple_keep_list(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda *a: True)
    import app.services.gift_purchase_service as gifts

    monkeypatch.setattr(gifts, 'is_gift_enabled', AsyncMock(return_value=False))
    lookup = AsyncMock(return_value=[SimpleNamespace(id=7)])
    monkeypatch.setattr(my_subscriptions, 'get_all_subscriptions_by_user_id', lookup)
    render = AsyncMock()
    monkeypatch.setattr(my_subscriptions, 'show_subscription_detail', render)
    cb = callback('my_subscriptions')
    state = SimpleNamespace(update_data=AsyncMock())
    await my_subscriptions.show_my_subscriptions(cb, user(), None, state)
    assert render.await_count == 1
    assert render.await_args.kwargs == {'sub_id': 7, 'single_view': True}
    assert render.await_args.args[3] is state
    render.reset_mock()
    lookup.return_value = [SimpleNamespace(id=7), SimpleNamespace(id=8)]
    monkeypatch.setattr(my_subscriptions, '_format_subscription_line', lambda sub, idx: f'Подписка {idx}')
    monkeypatch.setattr(my_subscriptions, '_build_subscriptions_keyboard', MagicMock(return_value=None))
    renderer = AsyncMock()
    monkeypatch.setattr(my_subscriptions, 'edit_or_answer_photo', renderer)
    await my_subscriptions.show_my_subscriptions(cb, user(), None, state)
    render.assert_not_called()
    assert 'Подписка 1' in renderer.await_args.args[1] and 'Подписка 2' in renderer.await_args.args[1]


async def test_single_overview_has_no_balance_or_duplicate_limits(monkeypatch):
    import app.services.gift_purchase_service as gifts
    from app.database.crud import subscription as crud
    from app.services.subscription_service import SubscriptionService

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda *a: False)
    monkeypatch.setattr(Settings, 'is_devices_selection_enabled', lambda *a: True)
    monkeypatch.setattr(Settings, 'is_tariffs_mode', lambda *a: True)
    sub = SimpleNamespace(
        id=1,
        tariff_id=5,
        is_trial=True,
        status='active',
        end_date=datetime.now(UTC) + timedelta(days=2),
        traffic_limit_gb=10,
        traffic_used_gb=0,
        device_limit=1,
        whitelist_traffic_limit_gb=5,
        whitelist_traffic_used_bytes=0,
    )
    monkeypatch.setattr(crud, 'check_and_update_subscription_status', AsyncMock(return_value=sub))
    monkeypatch.setattr(SubscriptionService, 'sync_subscription_usage', AsyncMock())
    monkeypatch.setattr(SubscriptionService, 'ensure_subscription_synced', AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(gifts, 'is_gift_enabled', AsyncMock(return_value=False))
    import app.database.crud.tariff as tariff_crud

    monkeypatch.setattr(
        tariff_crud,
        'get_tariff_by_id',
        AsyncMock(return_value=tariff(5, name='Пробный период', traffic_limit_gb=10, device_limit=1)),
    )
    monkeypatch.setattr(migration, 'can_offer_lazeika_migration', AsyncMock(return_value=False))
    renderer = AsyncMock()
    monkeypatch.setattr(purchase, 'edit_or_answer_photo', renderer)
    cb = callback('menu_subscription')
    await purchase.show_subscription_info(
        cb,
        user(subscription=sub, remnawave_id=None),
        SimpleNamespace(
            refresh=AsyncMock(),
            execute=AsyncMock(
                return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
            ),
        ),
    )
    text = renderer.await_args.args[1]
    assert text.count('Пробный период') == 1 and 'Триал' not in text and 'Тестовая' not in text
    assert 'Баланс' not in text and 'Информация о подписке' not in text and 'Тип:' not in text
    assert text.count('Устройства:') == 1 and text.count('Трафик:') == 1
    assert '0.0 / 10 ГБ' in text and '0.0 / 5 ГБ' in text
