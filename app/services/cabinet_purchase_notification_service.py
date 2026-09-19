import structlog

from app.config import settings
from app.database.models import User
from app.services.notification_delivery_service import notification_delivery_service
from app.services.notification_types import NotificationType
from app.utils.screen_banners import cache_screen_banner_file_id, get_screen_banner


logger = structlog.get_logger(__name__)


async def notify_telegram_user_about_cabinet_purchase(user: User, message: str) -> None:
    """Mirror a successful cabinet purchase to the user's Telegram bot."""
    if not user.telegram_id or not settings.is_notifications_enabled():
        return

    from app.bot_factory import create_bot

    bot = create_bot()
    try:
        # INVOXY: успешная покупка из кабинета зеркалится фото с баннером;
        # при любом сбое — обычный текстовый путь ниже.
        banner = get_screen_banner('paid_successful')
        if banner is not None:
            try:
                sent = await bot.send_photo(
                    chat_id=user.telegram_id,
                    photo=banner,
                    caption=message,
                    parse_mode='HTML',
                )
                photo = getattr(sent, 'photo', None)
                if photo:
                    cache_screen_banner_file_id('paid_successful', photo[-1].file_id)
                return
            except Exception as error:
                logger.warning(
                    'Banner send failed, falling back to text notification',
                    user_id=user.id,
                    error=error,
                )
        await notification_delivery_service.send_notification(
            user=user,
            notification_type=NotificationType.PAYMENT_RECEIVED,
            context={},
            bot=bot,
            telegram_message=message,
            use_websocket=False,
        )
    except Exception as error:
        logger.warning(
            'Failed to notify Telegram user about cabinet purchase',
            user_id=user.id,
            error=error,
        )
    finally:
        await bot.session.close()
