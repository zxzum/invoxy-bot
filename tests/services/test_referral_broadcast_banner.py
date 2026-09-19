from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import FSInputFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User, UserStatus
from app.services.referral_broadcast_service import (
    build_referral_broadcast_message,
    send_user_referral_broadcast,
)


@pytest.mark.asyncio
async def test_build_referral_broadcast_message_legacy_conditions(monkeypatch):
    """Verifies that referral broadcast builds text with dynamic DB conditions and personal links."""
    monkeypatch.setattr(type(settings), 'is_referral_levels_scheme', lambda self: False)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 5000)
    monkeypatch.setattr(settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 10000)
    monkeypatch.setattr(settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 5000)
    monkeypatch.setattr(settings, 'REFERRAL_COMMISSION_PERCENT', 25)
    monkeypatch.setattr(settings, 'REFERRAL_MAX_COMMISSION_PAYMENTS', 0)
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'InvoxyBot')
    monkeypatch.setattr(type(settings), 'get_cabinet_referral_link', lambda self, code: f'https://invoxy.test/?ref={code}')

    user = User(
        id=42,
        telegram_id=12345678,
        username='tester',
        referral_code='REF42',
        language='ru',
        status=UserStatus.ACTIVE.value,
    )

    db = AsyncMock(spec=AsyncSession)
    message, markup = await build_referral_broadcast_message(user, db, bot_username='InvoxyBot')

    # Personal links present
    assert 'https://t.me/InvoxyBot?start=REF42' in message
    assert 'https://invoxy.test/?ref=REF42' in message
    assert 'REF42' in message

    # Dynamic conditions present (100/50/50/25)
    assert '50' in message
    assert '100' in message
    assert '25%' in message

    # Inline button to open referral menu
    assert markup.inline_keyboard[0][0].callback_data == 'menu_referrals'


@pytest.mark.asyncio
async def test_send_user_referral_broadcast_with_banner(monkeypatch):
    """Verifies that telegram user receives referral broadcast as a photo with ref.png banner."""
    user = User(
        id=42,
        telegram_id=12345678,
        username='tester',
        referral_code='REF42',
        language='ru',
        status=UserStatus.ACTIVE.value,
    )

    bot = AsyncMock()
    photo_mock = MagicMock()
    photo_mock.file_id = 'test_ref_file_id'
    sent_msg = MagicMock()
    sent_msg.photo = [photo_mock]
    bot.send_photo.return_value = sent_msg

    markup = MagicMock()
    fake_banner = FSInputFile('assets/banners/ref.png')
    monkeypatch.setattr('app.services.referral_broadcast_service.get_screen_banner', lambda kind: fake_banner if kind == 'referral' else None)

    cached_id = {}
    monkeypatch.setattr('app.services.referral_broadcast_service.cache_screen_banner_file_id', lambda kind, fid: cached_id.update({kind: fid}))

    success = await send_user_referral_broadcast(bot, user, 'Test Message', markup)

    assert success is True
    bot.send_photo.assert_awaited_once_with(
        chat_id=12345678,
        photo=fake_banner,
        caption='Test Message',
        reply_markup=markup,
        parse_mode='HTML',
    )
    assert cached_id.get('referral') == 'test_ref_file_id'
