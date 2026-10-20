"""Per-screen banner resolver (INVOXY).

Centralizes banner filesystem paths so core handlers only name a screen
("main", "subscription", "referral", "paid_successful", "renew", ...). Text/presentation
logic stays in the handlers — this module only resolves media.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import structlog
from aiogram.types import FSInputFile

from app.config import settings
from app.services.notification_types import NotificationType
from app.utils.message_patch import _prepare_logo_for_send


logger = structlog.get_logger(__name__)

ScreenBannerKind = Literal[
    'main',
    'subscription',
    'referral',
    'paid_successful',
    'renew',
    'renew_expired',
    'payment_pending',
    'payment_failed',
    'subscription_inactive',
    'invoice_cancelled', 'account_security', 'balance', 'connect', 'devices',
    'traffic', 'bonus', 'support', 'trial', 'promo', 'gift', 'account_status', 'grace_access',
]

SCREEN_BANNERS: dict[str, str] = {
    'main': 'main.png',
    'subscription': 'sub-banner.png',
    'referral': 'ref.png',
    'paid_successful': 'paid_successful.png',
    'renew': 'renew.png',
    'renew_expired': 'renew_expired.png',
    'payment_pending': 'payment_pending.png',
    'payment_failed': 'payment_failed.png',
    'subscription_inactive': 'subscription_inactive.png',
    'invoice_cancelled': 'invoice_cancelled.jpg',
    'account_security': 'account_security.jpg',
    'balance': 'balance.jpg',
    'connect': 'connect.jpg',
    'devices': 'devices.jpg',
    'traffic': 'traffic.jpg',
    'bonus': 'bonus.jpg',
    'support': 'support.jpg',
    'trial': 'trial.jpg',
    'promo': 'promo.jpg',
    'gift': 'gift.jpg',
    'account_status': 'account_status.jpg',
    'grace_access': 'grace_access.jpg',
}

NOTIFICATION_BANNERS = {
    NotificationType.BALANCE_TOPUP: 'paid_successful',
    NotificationType.BALANCE_CHANGE: 'balance',
    NotificationType.BALANCE_LOW: 'balance',
    NotificationType.PAYMENT_INVOICE_CREATED: 'payment_pending',
    NotificationType.PAYMENT_INVOICE_CANCELLED: 'invoice_cancelled',
    NotificationType.SUBSCRIPTION_ACTIVATED: 'paid_successful',
    NotificationType.SUBSCRIPTION_EXPIRING: 'renew',
    NotificationType.SUBSCRIPTION_EXPIRED: 'renew_expired',
    NotificationType.SUBSCRIPTION_RENEWED: 'renew',
    NotificationType.WINBACK_EXPIRED_1D: 'promo',
    NotificationType.WINBACK_DISCOUNT: 'promo',
    NotificationType.WINBACK_TRIAL_ENDING: 'trial',
    NotificationType.AUTOPAY_SUCCESS: 'renew',
    NotificationType.AUTOPAY_FAILED: 'payment_failed',
    NotificationType.AUTOPAY_INSUFFICIENT_FUNDS: 'balance',
    NotificationType.DAILY_INSUFFICIENT_FUNDS: 'balance',
    NotificationType.TRAFFIC_RESET: 'traffic',
    NotificationType.BAN_NOTIFICATION: 'account_status',
    NotificationType.UNBAN_NOTIFICATION: 'account_status',
    NotificationType.WARNING_NOTIFICATION: 'account_status',
    NotificationType.REFERRAL_BONUS: 'bonus',
    NotificationType.REFERRAL_REGISTERED: 'bonus',
    NotificationType.REFERRAL_WELCOME: 'bonus',
    NotificationType.PARTNER_APPLICATION_APPROVED: 'referral',
    NotificationType.PARTNER_APPLICATION_REJECTED: 'referral',
    NotificationType.WITHDRAWAL_APPROVED: 'bonus',
    NotificationType.WITHDRAWAL_REJECTED: 'bonus',
    NotificationType.WEBHOOK_SUB_EXPIRED: 'renew_expired',
    NotificationType.WEBHOOK_SUB_DISABLED: 'subscription_inactive',
    NotificationType.WEBHOOK_SUB_ENABLED: 'subscription',
    NotificationType.WEBHOOK_SUB_LIMITED: 'traffic',
    NotificationType.WEBHOOK_SUB_TRAFFIC_RESET: 'traffic',
    NotificationType.WEBHOOK_SUB_DELETED: 'subscription_inactive',
    NotificationType.WEBHOOK_SUB_REVOKED: 'subscription_inactive',
    NotificationType.WEBHOOK_SUB_EXPIRING: 'renew',
    NotificationType.WEBHOOK_SUB_FIRST_CONNECTED: 'connect',
    NotificationType.WEBHOOK_SUB_BANDWIDTH_THRESHOLD: 'traffic',
    NotificationType.WEBHOOK_USER_NOT_CONNECTED: 'connect',
    NotificationType.WEBHOOK_DEVICE_ADDED: 'devices',
    NotificationType.WEBHOOK_DEVICE_DELETED: 'devices',
    NotificationType.WEBHOOK_TORRENT_DETECTED: 'account_status',
    NotificationType.GRACE_ACCESS_GRANTED: 'grace_access',
    NotificationType.GRACE_ACCESS_ENDED: 'subscription_inactive',
    NotificationType.TICKET_REPLY: 'support',
    NotificationType.PROMO_GROUP_AUTO_ASSIGNED: 'bonus',
    NotificationType.PAYMENT_RECEIVED: 'paid_successful',
    NotificationType.PROMO_OFFER: 'promo',
    NotificationType.GUEST_SUBSCRIPTION_DELIVERED: 'paid_successful',
    NotificationType.GUEST_ACTIVATION_REQUIRED: 'connect',
    NotificationType.GUEST_GIFT_RECEIVED: 'gift',
    NotificationType.GUEST_GIFT_LINK_BUYER: 'gift',
}


def notification_banner(notification_type: NotificationType, context: dict) -> str | None:
    if notification_type is NotificationType.USER_REMINDER:
        return 'account_security' if context.get('builtin_key') == 'link_auth_method' else None
    if notification_type is NotificationType.SUBSCRIPTION_ACTIVATED and context.get('is_trial'):
        return 'trial'
    return NOTIFICATION_BANNERS.get(notification_type)

_BANNERS_DIR = Path(__file__).resolve().parents[2] / 'assets' / 'banners'

_file_id_cache: dict[str, str] = {}


def get_screen_banner(kind: str):
    """Return cached file_id or FSInputFile for a screen banner, else None."""
    filename = SCREEN_BANNERS.get(kind)
    if filename is None:
        return None
    # INVOXY: the main card keeps its mandatory banner in compact mode.
    if not settings.ENABLE_LOGO_MODE and not (kind == 'main' and settings.get_main_menu_mode() == 'compact'):
        return None
    if kind in _file_id_cache:
        return _file_id_cache[kind]
    path = _BANNERS_DIR / filename
    if not path.is_file():
        logger.warning('Screen banner missing — falling back to default media', kind=kind, path=str(path))
        return None
    return FSInputFile(_prepare_logo_for_send(path))


def cache_screen_banner_file_id(kind: str, file_id: str | None) -> None:
    """Remember Telegram file_id after a successful photo send."""
    if file_id:
        _file_id_cache[kind] = file_id
