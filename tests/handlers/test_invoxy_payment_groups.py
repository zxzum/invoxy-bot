from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


pytestmark = pytest.mark.usefixtures('invoice_redis')

from app.config import settings
from app.handlers.balance import rollypay
from app.keyboards.inline import get_payment_methods_keyboard
from app.services import payment_method_config_service as configs
from app.utils.payment_utils import get_payment_methods_text


class State:
    def __init__(self, **data):
        self.data = data
        self.current = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **data):
        self.data.update(data)

    async def set_state(self, state):
        self.current = state

    async def clear(self):
        self.data.clear()
        self.current = None


@pytest.fixture
def providers(monkeypatch):
    monkeypatch.setattr(settings, 'ROLLYPAY_ENABLED', True)
    monkeypatch.setattr(settings, 'ROLLYPAY_API_KEY', 'test')
    monkeypatch.setattr(settings, 'ROLLYPAY_SIGNING_SECRET', 'test')
    monkeypatch.setattr(settings, 'PLATEGA_ENABLED', True)
    monkeypatch.setattr(settings, 'PLATEGA_MERCHANT_ID', 'test')
    monkeypatch.setattr(settings, 'PLATEGA_SECRET', 'test')
    monkeypatch.setattr(settings, 'PLATEGA_INLINE_METHODS', False)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)
    monkeypatch.setattr(type(settings), 'is_support_topup_enabled', lambda self: True)
    monkeypatch.setattr(
        configs,
        '_display_name_overrides',
        {
            'rollypay': 'Платежная система №1 (Рекомендуется)',
            'platega': 'Платежная система №2',
            'telegram_stars': 'Telegram Stars',
        },
    )
    monkeypatch.setattr(configs, '_method_sort_orders', {'rollypay': 0, 'platega': 1, 'telegram_stars': 2})


@pytest.mark.parametrize('amount', [0, 12345])
def test_parent_menu_contains_ordered_gateways_not_flat_options(providers, amount):
    keyboard = get_payment_methods_keyboard(amount, 'ru')
    buttons = [row[0] for row in keyboard.inline_keyboard]
    assert [button.text for button in buttons[:3]] == [
        'Платежная система №1 (Рекомендуется)',
        'Платежная система №2',
        'Telegram Stars',
    ]
    assert buttons[3].callback_data == 'topup_support'
    assert not any('platega_m' in (button.callback_data or '') for button in buttons)
    if amount:
        assert buttons[0].callback_data == f'topup_amount|rollypay_group|{amount}'
        assert buttons[1].callback_data == f'topup_amount|platega_group|{amount}'
    text = get_payment_methods_text('ru')
    assert 'Платежная система №1 (Рекомендуется)' in text
    assert 'Платежная система №2' in text
    assert text.index('Платежная система №1') < text.index('Платежная система №2') < text.index('Telegram Stars')


@pytest.fixture
def flow(providers, monkeypatch):
    user = SimpleNamespace(id=7, telegram_id=77, language='ru', restriction_topup=False, email=None)
    callback = SimpleNamespace(
        data='rollypay_method_sbp', message=SimpleNamespace(answer=AsyncMock()), answer=AsyncMock()
    )
    options = [{'id': 'sbp', 'name': '⚡ СБП'}, {'id': 'card', 'name': '💳 Карты РФ'}]
    monkeypatch.setattr(
        rollypay, 'get_enabled_methods_for_user', AsyncMock(return_value=[{'id': 'rollypay', 'options': options}])
    )
    monkeypatch.setattr('app.handlers.balance.invoices.list_recent_pending_payments', AsyncMock(return_value=[]))
    photo = AsyncMock()
    monkeypatch.setattr('app.utils.photo_message.edit_or_answer_photo', photo)
    monkeypatch.setattr(rollypay, 'get_topup_amount_keyboard', AsyncMock(return_value=None))
    create = AsyncMock(return_value={'payment_url': 'https://example.com/test-payment'})
    monkeypatch.setattr(rollypay, 'PaymentService', lambda: SimpleNamespace(create_rollypay_payment=create))
    return user, callback, photo, create


@pytest.mark.asyncio
async def test_gateway_opens_options_without_creating_invoice(flow):
    user, callback, photo, create = flow
    state = State()
    await rollypay.start_rollypay_topup(callback, user, MagicMock(), state)
    buttons = photo.await_args.args[2].inline_keyboard
    assert [row[0].callback_data for row in buttons] == ['rollypay_method_sbp', 'rollypay_method_card', 'balance_topup']
    create.assert_not_awaited()
    assert state.data['rollypay_method'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['sbp', 'card'])
async def test_selected_method_and_exact_amount_reach_provider(flow, method):
    user, callback, _, create = flow
    callback.data = f'rollypay_method_{method}'
    state = State(rollypay_pending_amount=12345)
    await rollypay.select_rollypay_method(callback, user, MagicMock(), state)
    assert create.await_args.kwargs['payment_method_type'] == method
    assert create.await_args.kwargs['amount_kopeks'] == 12345
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_or_forged_option_does_not_create_payment(flow):
    user, callback, _, create = flow
    callback.data = 'rollypay_method_unknown'
    await rollypay.select_rollypay_method(callback, user, MagicMock(), State(rollypay_pending_amount=12345))
    create.assert_not_awaited()
    assert callback.answer.await_args.kwargs['show_alert'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('amount', [0, 999, 10000001])
async def test_amount_limits_are_checked_before_provider(flow, amount):
    user, callback, _, create = flow
    await rollypay.process_rollypay_payment_amount(
        callback.message, user, MagicMock(), amount, State(rollypay_method='sbp')
    )
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_restricted_user_cannot_select_gateway_option(flow):
    user, callback, _, create = flow
    user.restriction_topup = True
    await rollypay.select_rollypay_method(callback, user, MagicMock(), State(rollypay_pending_amount=12345))
    create.assert_not_awaited()
    assert callback.answer.await_args.kwargs['show_alert'] is True
