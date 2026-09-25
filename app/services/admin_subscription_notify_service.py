"""
Service for sending notifications to users when an admin modifies their subscription:
- Granting/issuing a new subscription (create)
- Extending an existing subscription (extend / set_end_date)
- Cancelling/annulling a subscription (cancel / reset / shorten)
"""

from __future__ import annotations

import asyncio
import html
from datetime import UTC, datetime

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Subscription, User
from app.utils.miniapp_buttons import build_miniapp_or_callback_button
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)


def _format_date(dt: datetime | None) -> str:
    if not dt:
        return '—'
    return format_local_datetime(dt, fmt='%d.%m.%Y')


def _format_traffic(limit_gb: float | None, lang: str = 'ru') -> str:
    if not limit_gb or limit_gb == 0:
        return 'Безлимитный' if lang == 'ru' else 'Unlimited'
    return f'{int(limit_gb) if limit_gb == int(limit_gb) else limit_gb} ГБ' if lang == 'ru' else f'{limit_gb} GB'


def _format_devices(limit: int | None, lang: str = 'ru') -> str:
    if not limit or limit == 0:
        return '1'
    if lang == 'ru':
        if limit == 1:
            return '1 устройство'
        if 2 <= limit <= 4:
            return f'{limit} устройства'
        return f'{limit} устройств'
    return f'{limit} device' if limit == 1 else f'{limit} devices'


class AdminSubscriptionNotifyService:
    """Unified service for notifying users about admin-driven subscription changes."""

    @staticmethod
    async def _send_tg_message(
        user: User,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        bot: Bot | None = None,
    ) -> bool:
        """Send Telegram notification safely."""
        if not user.telegram_id:
            return False

        should_close_bot = False
        if bot is None:
            if not settings.BOT_TOKEN:
                return False
            from app.bot_factory import create_bot

            bot = create_bot()
            should_close_bot = True

        try:
            from app.utils.rich_notify import try_send_rich_notification

            if await try_send_rich_notification(
                bot,
                user.telegram_id,
                text,
                keyboard=reply_markup,
                with_logo=getattr(settings, 'ENABLE_LOGO_MODE', False),
            ):
                return True

            await bot.send_message(
                chat_id=user.telegram_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode='HTML',
            )
            return True
        except TelegramForbiddenError:
            logger.info('User blocked bot, cannot send admin subscription notice', telegram_id=user.telegram_id)
            return False
        except TelegramBadRequest as exc:
            logger.warning(
                'Telegram bad request on admin subscription notice',
                telegram_id=user.telegram_id,
                error=str(exc),
            )
            return False
        except Exception as exc:
            logger.warning(
                'Failed to send Telegram admin subscription notice',
                telegram_id=user.telegram_id,
                error=str(exc),
            )
            return False
        finally:
            if should_close_bot and bot and bot.session:
                try:
                    await bot.session.close()
                except Exception:
                    pass

    @classmethod
    async def notify_subscription_granted(
        cls,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        days: int,
        tariff_name: str | None = None,
        bot: Bot | None = None,
    ) -> None:
        """Notify user that an admin granted/created a subscription."""
        lang = user.language or 'ru'
        end_date_str = _format_date(subscription.end_date)
        traffic_str = _format_traffic(subscription.traffic_limit_gb, lang)
        devices_str = _format_devices(subscription.device_limit, lang)

        resolved_tariff = tariff_name
        if not resolved_tariff and getattr(subscription, 'tariff', None):
            resolved_tariff = subscription.tariff.name

        # 1. Telegram
        if user.telegram_id:
            if lang == 'ru':
                tariff_line = f'📦 Тариф: <b>{html.escape(resolved_tariff)}</b>\n' if resolved_tariff else ''
                tg_text = (
                    f'🎁 <b>Вам выдана подписка!</b>\n\n'
                    f'Администратор оформил вам доступ к сервису Invoxy VPN.\n\n'
                    f'{tariff_line}'
                    f'📅 Срок: <b>{days} дн.</b>\n'
                    f'⏰ Действует до: <b>{end_date_str}</b>\n'
                    f'📊 Трафик: <b>{traffic_str}</b>\n'
                    f'📱 Устройств: <b>{devices_str}</b>\n\n'
                    f'Нажмите кнопку ниже для подключения:'
                )
                connect_btn_text = '🚀 Подключить'
            else:
                tariff_line = f'📦 Plan: <b>{html.escape(resolved_tariff)}</b>\n' if resolved_tariff else ''
                tg_text = (
                    f'🎁 <b>You have been granted a subscription!</b>\n\n'
                    f'An administrator has issued you access to Invoxy VPN.\n\n'
                    f'{tariff_line}'
                    f'📅 Duration: <b>{days} days</b>\n'
                    f'⏰ Valid until: <b>{end_date_str}</b>\n'
                    f'📊 Traffic: <b>{traffic_str}</b>\n'
                    f'📱 Devices: <b>{devices_str}</b>\n\n'
                    f'Tap the button below to connect:'
                )
                connect_btn_text = '🚀 Connect'

            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [build_miniapp_or_callback_button(text=connect_btn_text, callback_data='subscription_connect')],
                ]
            )
            asyncio.create_task(cls._send_tg_message(user, tg_text, kb, bot=bot))

        # 2. Email (for verified email users)
        if user.email and getattr(user, 'email_verified', False):
            try:
                from app.services.notification_delivery_service import NotificationDeliveryService
                from app.services.notification_types import NotificationType

                delivery = NotificationDeliveryService()
                asyncio.create_task(
                    delivery.send_notification(
                        user=user,
                        notification_type=NotificationType.SUBSCRIPTION_ACTIVATED,
                        context={
                            'expires_at': end_date_str,
                            'traffic_limit_gb': subscription.traffic_limit_gb,
                            'device_limit': subscription.device_limit,
                            'tariff_name': resolved_tariff or '',
                        },
                        bot=None,
                        use_websocket=False,
                    )
                )
            except Exception as e:
                logger.warning('Failed to queue email notification on grant', user_id=user.id, error=str(e))

        # 3. Cabinet WebSocket event
        try:
            from app.cabinet.routes.websocket import notify_user_subscription_activated

            asyncio.create_task(notify_user_subscription_activated(user.id, subscription.id))
        except Exception as e:
            logger.debug('Failed to emit websocket activation event', user_id=user.id, error=str(e))

    @classmethod
    async def notify_subscription_extended(
        cls,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        days: int | None = None,
        bot: Bot | None = None,
    ) -> None:
        """Notify user that their subscription was extended by an admin."""
        lang = user.language or 'ru'
        end_date_str = _format_date(subscription.end_date)

        tariff_name = ''
        if getattr(subscription, 'tariff', None):
            tariff_name = subscription.tariff.name

        # 1. Telegram
        if user.telegram_id:
            tariff_label = f' «{html.escape(tariff_name)}»' if tariff_name else ''
            days_line = f'📅 Продлено на: <b>{days} дн.</b>\n' if days and days > 0 else ''
            if lang == 'ru':
                tg_text = (
                    f'✨ <b>Ваша подписка{tariff_label} продлена!</b>\n\n'
                    f'Администратор продлил действие вашей подписки.\n\n'
                    f'{days_line}'
                    f'⏰ Действует до: <b>{end_date_str}</b>'
                )
                sub_btn_text = '📱 Моя подписка'
            else:
                days_line_en = f'📅 Extended by: <b>{days} days</b>\n' if days and days > 0 else ''
                tg_text = (
                    f'✨ <b>Your subscription{tariff_label} has been extended!</b>\n\n'
                    f'An administrator has extended your subscription.\n\n'
                    f'{days_line_en}'
                    f'⏰ Valid until: <b>{end_date_str}</b>'
                )
                sub_btn_text = '📱 My subscription'

            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [build_miniapp_or_callback_button(text=sub_btn_text, callback_data='menu_subscription')],
                ]
            )
            asyncio.create_task(cls._send_tg_message(user, tg_text, kb, bot=bot))

        # 2. Email
        if user.email and getattr(user, 'email_verified', False):
            try:
                from app.services.notification_delivery_service import NotificationDeliveryService
                from app.services.notification_types import NotificationType

                delivery = NotificationDeliveryService()
                asyncio.create_task(
                    delivery.send_notification(
                        user=user,
                        notification_type=NotificationType.SUBSCRIPTION_RENEWED,
                        context={
                            'new_expires_at': end_date_str,
                            'expires_at': end_date_str,
                            'traffic_limit_gb': subscription.traffic_limit_gb,
                            'device_limit': subscription.device_limit,
                            'tariff_name': tariff_name or '',
                        },
                        bot=None,
                        use_websocket=False,
                    )
                )
            except Exception as e:
                logger.warning('Failed to queue email notification on extend', user_id=user.id, error=str(e))

        # 3. Cabinet WebSocket event
        try:
            from app.cabinet.routes.websocket import notify_user_subscription_renewed

            asyncio.create_task(notify_user_subscription_renewed(user.id, subscription.id))
        except Exception as e:
            logger.debug('Failed to emit websocket renewed event', user_id=user.id, error=str(e))

    @classmethod
    async def notify_subscription_cancelled(
        cls,
        db: AsyncSession,
        user: User,
        subscription: Subscription | None = None,
        reason: str | None = None,
        bot: Bot | None = None,
    ) -> None:
        """Notify user that their subscription was cancelled/annulled by an admin."""
        lang = user.language or 'ru'
        tariff_name = ''
        if subscription and getattr(subscription, 'tariff', None):
            tariff_name = subscription.tariff.name

        # 1. Telegram
        if user.telegram_id:
            tariff_label = f' «{html.escape(tariff_name)}»' if tariff_name else ''
            reason_line = f'\nПричина: {html.escape(reason)}' if reason else ''
            if lang == 'ru':
                tg_text = (
                    f'⚠️ <b>Ваша подписка{tariff_label} аннулирована</b>\n\n'
                    f'Действие вашей подписки было прекращено администратором.{reason_line}\n\n'
                    f'Если у вас есть вопросы, вы можете обратиться в поддержку.'
                )
                support_btn_text = '💬 Поддержка'
            else:
                tg_text = (
                    f'⚠️ <b>Your subscription{tariff_label} has been cancelled</b>\n\n'
                    f'Your subscription was cancelled by an administrator.{reason_line}\n\n'
                    f'If you have any questions, please contact support.'
                )
                support_btn_text = '💬 Support'

            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [build_miniapp_or_callback_button(text=support_btn_text, callback_data='support')],
                ]
            )
            asyncio.create_task(cls._send_tg_message(user, tg_text, kb, bot=bot))

        # 2. Email
        if user.email and getattr(user, 'email_verified', False):
            try:
                from app.services.notification_delivery_service import NotificationDeliveryService
                from app.services.notification_types import NotificationType

                delivery = NotificationDeliveryService()
                asyncio.create_task(
                    delivery.send_notification(
                        user=user,
                        notification_type=NotificationType.SUBSCRIPTION_EXPIRED,
                        context={
                            'tariff_name': tariff_name or '',
                            'expired_at': _format_date(datetime.now(UTC)),
                        },
                        bot=None,
                        use_websocket=False,
                    )
                )
            except Exception as e:
                logger.warning('Failed to queue email notification on cancel', user_id=user.id, error=str(e))

        # 3. Cabinet WebSocket event
        try:
            from app.cabinet.routes.websocket import notify_user_subscription_expired

            asyncio.create_task(notify_user_subscription_expired(user.id))
        except Exception as e:
            logger.debug('Failed to emit websocket expired event', user_id=user.id, error=str(e))
