"""INVOXY: Referral broadcast service for periodic and manual broadcasts."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

import structlog
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Tariff, User
from app.localization.texts import get_texts
from app.services.referral_reward_service import (
    ReferralRewardLevelService,
    describe_active_levels,
    describe_referee_bonus,
)
from app.utils.screen_banners import cache_screen_banner_file_id, get_screen_banner
from app.utils.user_utils import get_effective_referral_commission_percent


if TYPE_CHECKING:
    from aiogram import Bot

logger = structlog.get_logger(__name__)


async def get_reward_tariff_names(db: AsyncSession) -> dict[int, str]:
    """Retrieve tariff names referenced by referral reward levels."""
    configs = await ReferralRewardLevelService.get_all(db)
    ids = {cfg.referrer_tariff_id for cfg in configs.values() if cfg.referrer_tariff_id}
    ids |= {cfg.referee_tariff_id for cfg in configs.values() if cfg.referee_tariff_id}
    if not ids:
        return {}
    result = await db.execute(select(Tariff.id, Tariff.name).where(Tariff.id.in_(ids)))
    return {row.id: row.name for row in result.all()}


async def build_referral_broadcast_message(
    user: User,
    db: AsyncSession,
    bot_username: str | None = None,
    tariff_names: dict[int, str] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build dynamic referral broadcast message with DB conditions and personal links."""
    lang = getattr(user, 'language', 'ru') or 'ru'
    is_en = lang.lower().startswith('en')
    texts = get_texts(lang)
    username = bot_username or settings.get_bot_username()

    bot_link = settings.get_bot_referral_link(user.referral_code, username)
    cabinet_link = settings.get_cabinet_referral_link(user.referral_code)
    if not cabinet_link and user.referral_code:
        base_url = (settings.MINIAPP_CUSTOM_URL or settings.WEBHOOK_URL or '').strip().rstrip('/')
        if base_url and not base_url.startswith(('http://example.com', 'https://example.com')):
            safe_code = settings._encode_referral_code(user.referral_code)
            sep = '&' if '?' in base_url else '?'
            cabinet_link = f'{base_url}{sep}ref={safe_code}'
    code = user.referral_code or ''

    # Build condition lines from DB
    condition_lines: list[str] = []
    if settings.is_referral_levels_scheme():
        if tariff_names is None:
            tariff_names = await get_reward_tariff_names(db)
        levels_desc = await describe_active_levels(db, tariff_names=tariff_names, language=lang, viewer=user)
        for line in levels_desc:
            condition_lines.append(f'• {line}')
        referee_bonus = await describe_referee_bonus(db, tariff_names=tariff_names, language=lang, referrer=user)
        if referee_bonus:
            prefix = '• New user receives:' if is_en else '• Новый пользователь получает:'
            condition_lines.append(f'{prefix} <b>{referee_bonus}</b>')
    else:
        # Legacy scheme from SystemSetting / settings
        if settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS > 0:
            bonus = texts.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS)
            minimum = texts.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS)
            if is_en:
                condition_lines.append(
                    f'• New user receives: <b>{bonus}</b> on first top-up from <b>{minimum}</b>'
                )
            else:
                condition_lines.append(
                    f'• Новый пользователь получает: <b>{bonus}</b> при первом пополнении от <b>{minimum}</b>'
                )
        if settings.REFERRAL_INVITER_BONUS_KOPEKS > 0:
            bonus = texts.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS)
            if is_en:
                condition_lines.append(f"• You receive for each friend's first top-up: <b>{bonus}</b>")
            else:
                condition_lines.append(f'• Вы получаете за первое пополнение друга: <b>{bonus}</b>')

        pct = get_effective_referral_commission_percent(user)
        if settings.REFERRAL_MAX_COMMISSION_PAYMENTS > 0:
            if is_en:
                condition_lines.append(
                    f'• Commission from first {settings.REFERRAL_MAX_COMMISSION_PAYMENTS} top-ups: <b>{pct}%</b>'
                )
            else:
                condition_lines.append(
                    f'• Комиссия с первых {settings.REFERRAL_MAX_COMMISSION_PAYMENTS} пополнений: <b>{pct}%</b>'
                )
        elif is_en:
            condition_lines.append(f'• Commission from all top-ups: <b>{pct}%</b>')
        else:
            condition_lines.append(f'• Пожизненная комиссия со всех пополнений: <b>{pct}%</b>')

    conditions_block = '\n'.join(condition_lines) if condition_lines else ''

    if is_en:
        parts = [
            '🎁 <b>Invoxy VPN Referral Program</b>\n',
            'Invite friends and earn recurring rewards and bonuses!\n',
        ]
        if conditions_block:
            parts.extend(['<b>Program conditions:</b>', conditions_block, ''])
        parts.append('👇 <b>Your personal invite links:</b>')
        parts.append(f'🤖 <b>In Telegram:</b> <code>{html.escape(bot_link)}</code>')
        if cabinet_link:
            parts.append(f'🌐 <b>Web cabinet:</b> <code>{html.escape(cabinet_link)}</code>')
        parts.append(f'🆔 <b>Your code:</b> <code>{html.escape(code)}</code>')
    else:
        parts = [
            '🎁 <b>Партнёрская программа Invoxy VPN</b>\n',
            'Приглашайте друзей и получайте бонусы и пассивный доход от каждого пополнения!\n',
        ]
        if conditions_block:
            parts.extend(['<b>Условия программы:</b>', conditions_block, ''])
        parts.append('👇 <b>Ваши персональные ссылки для приглашения:</b>')
        parts.append(f'🤖 <b>В Telegram:</b> <code>{html.escape(bot_link)}</code>')
        if cabinet_link:
            parts.append(f'🌐 <b>На сайте:</b> <code>{html.escape(cabinet_link)}</code>')
        parts.append(f'🆔 <b>Ваш код:</b> <code>{html.escape(code)}</code>')

    message = '\n'.join(parts)
    from app.utils.miniapp_buttons import build_miniapp_or_callback_button

    button_text = '👥 Open Referral Program' if is_en else '👥 Реферальная программа'
    button = build_miniapp_or_callback_button(
        button_text,
        callback_data='menu_referrals',
        cabinet_path='/referral',
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[[button]])
    return message, markup


async def send_user_referral_broadcast(
    bot: Bot,
    user: User,
    message: str,
    markup: InlineKeyboardMarkup,
) -> bool:
    """Send referral broadcast with ref.png banner if Telegram, fallback to notification delivery."""
    from app.services.notification_delivery_service import notification_delivery_service
    from app.services.notification_types import NotificationType

    if user.telegram_id:
        banner = get_screen_banner('referral')
        if banner is not None:
            try:
                sent = await bot.send_photo(
                    chat_id=user.telegram_id,
                    photo=banner,
                    caption=message,
                    reply_markup=markup,
                    parse_mode='HTML',
                )
                photo = getattr(sent, 'photo', None)
                if photo:
                    cache_screen_banner_file_id('referral', photo[-1].file_id)
                return True
            except Exception as error:
                logger.warning(
                    'Failed to send photo referral broadcast, falling back to message',
                    user_id=user.id,
                    error=str(error),
                )

    return await notification_delivery_service.send_notification(
        user=user,
        notification_type=NotificationType.PROMO_OFFER,
        context={
            'message_html': message.replace('\n', '<br>'),
            'valid_hours': 0,
            'discount_percent': 0,
        },
        bot=bot,
        telegram_message=message,
        telegram_markup=markup,
    )
