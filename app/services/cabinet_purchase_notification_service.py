import structlog

from app.config import settings
from app.database.models import User
from app.services.notification_delivery_service import notification_delivery_service
from app.services.notification_types import NotificationType


logger = structlog.get_logger(__name__)


async def notify_telegram_user_about_cabinet_purchase(user: User, message: str) -> None:
    """Mirror a successful cabinet purchase to the user's Telegram bot."""
    if not user.telegram_id or not settings.is_notifications_enabled():
        return

    from app.bot_factory import create_bot

    bot = create_bot()
    try:
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
