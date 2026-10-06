"""Service for notifying trial users who have active paid subscriptions in Lazeika VPN."""

import asyncio
from typing import Any

import structlog
from aiogram import Bot, types
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Subscription, SubscriptionStatus, User
from app.services.migration.migration_service import (
    MigrationCandidate,
    check_migration_eligibility,
)


logger = structlog.get_logger(__name__)


def get_migration_offer_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text='🚀 Перенести в Invoxy (+5 дней)',
                    callback_data='lazeika_migrate:execute',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text='Пропустить',
                    callback_data='lazeika_migrate:skip',
                )
            ],
        ]
    )


def build_migration_offer_text(candidate: MigrationCandidate, is_notification: bool = False) -> str:
    """Build user-facing HTML text describing the migration offer."""
    balance_line = ''
    if candidate.balance_rub > 0:
        balance_line = f'💰 Баланс: <b>+{candidate.balance_rub:.2f} ₽</b> (будет зачислен на счёт)\n'

    header = (
        '📢 <b>Вам доступен перенос подписки из Лазейка ВПН!</b>\n\n'
        'Мы обнаружили вашу оплаченную подписку в Лазейке:\n'
        if is_notification
        else '👋 <b>Найдена активная подписка в Лазейка ВПН!</b>\n\n'
    )

    return (
        f'{header}'
        f'📦 Тариф: <b>{candidate.lazeika_tariff_name}</b> ➔ <b>{candidate.target_tariff_name}</b>\n'
        f'⏳ Осталось: <b>{candidate.remaining_days}</b> дн. (+ <b>5 дней</b> в подарок 🎁)\n'
        f'📅 Срок действия: до <b>{candidate.new_end_date.strftime("%d.%m.%Y")}</b>\n'
        f'📱 Лимит устройств: <b>{candidate.device_limit}</b>\n'
        f'{balance_line}'
        '⚡️ Трафик будет обновлён и сброшен в качестве бонуса!\n\n'
        'Желаете осуществить перенос в <b>Invoxy VPN</b>?\n'
        '<i>Все ваши настройки подключения и ключи сохранятся — перенастраивать ничего не нужно.</i>'
    )


async def send_migration_notification(
    bot: Bot,
    db: AsyncSession,
    telegram_id: int,
    user: User | None = None,
    candidate: MigrationCandidate | None = None,
) -> bool:
    """Sends a targeted migration notification to a specific Telegram user."""
    try:
        if candidate is None:
            candidate = await check_migration_eligibility(db, telegram_id=telegram_id, user=user)
        if not candidate:
            logger.info('User not eligible for migration notification', telegram_id=telegram_id)
            return False

        text = build_migration_offer_text(candidate, is_notification=True)
        await bot.send_message(
            chat_id=telegram_id,
            text=text,
            reply_markup=get_migration_offer_keyboard(),
            parse_mode='HTML',
        )
        logger.info(
            'Sent migration notification to user',
            telegram_id=telegram_id,
            tariff=candidate.lazeika_tariff_name,
            remaining_days=candidate.remaining_days,
        )
        return True
    except Exception as e:
        logger.error('Failed to send migration notification', telegram_id=telegram_id, error=str(e))
        return False


async def notify_trial_user_about_migration(
    bot: Bot,
    db: AsyncSession,
    telegram_id: int,
) -> bool:
    """Checks eligibility and sends migration offer to a specific Telegram user."""
    return await send_migration_notification(bot, db, telegram_id=telegram_id)


async def notify_all_eligible_trial_users(
    bot: Bot,
    db: AsyncSession,
    delay_between_messages_sec: float = 0.5,
) -> dict[str, Any]:
    """Finds all users currently on trial and notifies those eligible for Lazeika migration.

    Returns stats dict: {total_checked, eligible_found, notified_success, errors}.
    """
    stats = {
        'total_checked': 0,
        'eligible_found': 0,
        'notified_success': 0,
        'errors': 0,
    }

    try:
        # Find users with active trial subscriptions and non-null telegram_id
        stmt = (
            select(User)
            .join(Subscription, Subscription.user_id == User.id)
            .where(
                User.telegram_id.isnot(None),
                Subscription.is_trial == True,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
            )
            .distinct()
        )
        result = await db.execute(stmt)
        trial_users = result.scalars().all()

        for user in trial_users:
            if not user.telegram_id:
                continue

            stats['total_checked'] += 1
            try:
                candidate = await check_migration_eligibility(db, telegram_id=user.telegram_id, user=user)
                if not candidate:
                    continue

                stats['eligible_found'] += 1
                sent = await send_migration_notification(
                    bot, db, user.telegram_id, user=user, candidate=candidate
                )
                if sent:
                    stats['notified_success'] += 1
                else:
                    stats['errors'] += 1

                if delay_between_messages_sec > 0:
                    await asyncio.sleep(delay_between_messages_sec)
            except Exception as user_err:
                stats['errors'] += 1
                logger.error(
                    'Error checking/notifying trial user for migration',
                    telegram_id=user.telegram_id,
                    error=str(user_err),
                )

    except Exception as exc:
        logger.error('Failed to run bulk trial migration notifier', error=str(exc))

    return stats
