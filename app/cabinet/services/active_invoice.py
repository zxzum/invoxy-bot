"""Active invoice service for cabinet.

Implements single active invoice per user rule:
- User creates payment link once; it stays valid for 30 minutes.
- Same intent (method + amount + purpose) returns the existing invoice idempotently.
- Different intent while active invoice exists raises HTTP 409 active_invoice_exists.
- Allows user cancellation (status marked as canceled locally, notification sent).
- Webhooks still credit even if marked canceled locally.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_factory import create_bot
from app.config import settings
from app.database.models import (
    CabinetNotification,
    PaymentMethod,
    User,
)
from app.services.notification_types import NotificationType
from app.services.payment_verification_service import (
    PendingPayment,
    get_payment_record,
    list_recent_pending_payments,
    method_display_name,
)


if TYPE_CHECKING:
    from app.cabinet.schemas.balance import PendingPaymentResponse

logger = structlog.get_logger(__name__)

ACTIVE_INVOICE_TTL = timedelta(minutes=30)
CANCELLED_STATUSES = frozenset(
    {'canceled', 'cancelled', 'fail', 'failed', 'declined', 'expired'}
)


def is_status_cancelled(status_str: str | None) -> bool:
    """Check if payment status denotes cancellation or failure."""
    if not status_str:
        return False
    return status_str.strip().lower() in CANCELLED_STATUSES


def is_invoice_active(record: PendingPayment, now: datetime | None = None) -> bool:
    """Check if payment invoice is still active (unpaid, not cancelled, within 30 min)."""
    if record.is_paid:
        return False
    if is_status_cancelled(record.status):
        return False
    current_time = now or datetime.now(UTC)
    return (record.created_at + ACTIVE_INVOICE_TTL) > current_time


def get_invoice_expires_at(record: PendingPayment) -> datetime:
    """Return effective expiration datetime (created_at + 30m, capped by provider expires_at if set)."""
    our_expiry = record.created_at + ACTIVE_INVOICE_TTL
    if record.expires_at and record.expires_at < our_expiry:
        return record.expires_at
    return our_expiry


def resolve_purpose(record: PendingPayment) -> tuple[str, str]:
    """Return (purpose_text, purpose_code: 'topup' | 'tariff')."""
    payment = record.payment
    desc = (
        getattr(payment, 'description', None)
        or getattr(payment, 'payload', None)
        or ''
    )
    if 'Оплата тарифа' in desc or 'Активация суточного тарифа' in desc or 'Тариф' in desc:
        return desc or 'Оплата тарифа', 'tariff'
    if desc and 'Пополнение баланса' not in desc:
        return desc, 'topup'
    return 'Пополнение баланса', 'topup'


def get_payment_url(record: PendingPayment) -> str | None:
    """Extract payment URL from payment record."""
    payment = record.payment
    payment_url = getattr(payment, 'payment_url', None)

    if record.method == PaymentMethod.PAL24:
        payment_url = getattr(payment, 'link_url', None) or getattr(payment, 'link_page_url', None) or payment_url
    elif record.method == PaymentMethod.WATA:
        payment_url = getattr(payment, 'url', None) or payment_url
    elif record.method == PaymentMethod.YOOKASSA:
        payment_url = getattr(payment, 'confirmation_url', None) or payment_url
    elif record.method == PaymentMethod.CRYPTOBOT:
        payment_url = (
            getattr(payment, 'bot_invoice_url', None)
            or getattr(payment, 'mini_app_invoice_url', None)
            or getattr(payment, 'web_app_invoice_url', None)
            or payment_url
        )
    elif record.method == PaymentMethod.PLATEGA:
        payment_url = getattr(payment, 'redirect_url', None) or payment_url
    elif record.method in (
        PaymentMethod.CLOUDPAYMENTS,
        PaymentMethod.FREEKASSA,
        PaymentMethod.KASSA_AI,
        PaymentMethod.RIOPAY,
    ):
        payment_url = getattr(payment, 'payment_url', None) or payment_url

    return payment_url


def build_pending_payment_response(record: PendingPayment) -> PendingPaymentResponse:
    """Convert PendingPayment to full PendingPaymentResponse with active invoice fields."""
    from app.cabinet.routes.balance import _get_status_info, _is_checkable
    from app.cabinet.schemas.balance import PendingPaymentResponse

    status_emoji, status_text = _get_status_info(record)
    purpose, purpose_code = resolve_purpose(record)
    active = is_invoice_active(record)
    expires_at = get_invoice_expires_at(record)
    url = get_payment_url(record)

    return PendingPaymentResponse(
        id=record.local_id,
        method=record.method.value,
        method_display=method_display_name(record.method),
        identifier=record.identifier,
        amount_kopeks=record.amount_kopeks,
        amount_rubles=record.amount_kopeks / 100,
        status=record.status or '',
        status_emoji=status_emoji,
        status_text=status_text,
        is_paid=record.is_paid,
        is_checkable=_is_checkable(record),
        created_at=record.created_at,
        expires_at=expires_at,
        payment_url=url,
        user_id=record.user.id if record.user else None,
        user_telegram_id=record.user.telegram_id if record.user else None,
        user_username=record.user.username if record.user else None,
        purpose=purpose,
        purpose_code=purpose_code,
        is_active=active,
        can_cancel=active,
    )


async def get_active_invoice_record(
    db: AsyncSession,
    user_id: int,
) -> PendingPayment | None:
    """Find user's current active pending payment within the 30-minute window."""
    recent = await list_recent_pending_payments(db, max_age=ACTIVE_INVOICE_TTL, user_id=user_id)
    for record in recent:
        if is_invoice_active(record):
            return record
    return None


def same_topup_intent(
    record: PendingPayment,
    method: str,
    amount_kopeks: int,
) -> bool:
    """Check if existing invoice has same top-up intent."""
    _, purpose_code = resolve_purpose(record)
    return (
        purpose_code == 'topup'
        and record.method.value == method
        and record.amount_kopeks == amount_kopeks
    )


def same_tariff_intent(
    record: PendingPayment,
    method: str,
    missing_kopeks: int,
    tariff_id: int,
    period_days: int,
    tariff_name: str | None = None,
) -> bool:
    """Check if existing invoice has same tariff purchase intent."""
    _, purpose_code = resolve_purpose(record)
    if purpose_code != 'tariff':
        return False
    if record.method.value != method or record.amount_kopeks != missing_kopeks:
        return False

    payment = record.payment
    meta = getattr(payment, 'metadata', None) or getattr(payment, 'extra_metadata', None)
    if isinstance(meta, dict) and 'tariff_id' in meta:
        if str(meta['tariff_id']) != str(tariff_id):
            return False

    desc = getattr(payment, 'description', None) or getattr(payment, 'payload', None) or ''
    if tariff_name and tariff_name not in desc:
        return False
    if f'{period_days} дн' not in desc and not ('суточн' in desc and period_days == 1):
        if 'дн' in desc:
            return False

    return True


async def send_invoice_created_telegram_message(
    user: User,
    purpose: str,
    amount_kopeks: int,
    method_display: str,
    payment_url: str,
) -> None:
    """Send Telegram message with invoice details and Pay button."""
    if not getattr(user, 'telegram_id', None):
        return
    if not getattr(settings, 'BOT_TOKEN', None):
        return

    amount_rubles = amount_kopeks / 100
    text = (
        '💳 <b>Счёт на оплату</b>\n\n'
        f'Назначение: {html.escape(purpose)}\n'
        f'Сумма: {amount_rubles:.0f} ₽\n'
        f'Способ: {html.escape(method_display)}\n'
        'Действует 30 минут'
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='Оплатить', url=payment_url)]
        ]
    )
    try:
        bot = create_bot()
        try:
            await bot.send_message(
                chat_id=user.telegram_id,
                text=text,
                reply_markup=keyboard,
                parse_mode='HTML',
            )
        finally:
            if hasattr(bot, 'session') and hasattr(bot.session, 'close'):
                await bot.session.close()
    except Exception as e:
        logger.warning('Failed to send invoice created telegram notification', user_id=user.id, error=str(e))


async def send_invoice_cancelled_telegram_message(
    user: User,
    purpose: str,
    amount_kopeks: int,
) -> None:
    """Send Telegram message when invoice is cancelled."""
    if not getattr(user, 'telegram_id', None):
        return
    if not getattr(settings, 'BOT_TOKEN', None):
        return

    amount_rubles = amount_kopeks / 100
    text = (
        f'Счёт на {amount_rubles:.0f} ₽ ({html.escape(purpose)}) отменён.\n'
        'Можно создать новый в кабинете или в боте.'
    )
    try:
        bot = create_bot()
        try:
            await bot.send_message(
                chat_id=user.telegram_id,
                text=text,
                parse_mode='HTML',
            )
        finally:
            if hasattr(bot, 'session') and hasattr(bot.session, 'close'):
                await bot.session.close()
    except Exception as e:
        logger.warning('Failed to send invoice cancelled telegram notification', user_id=user.id, error=str(e))


async def record_cabinet_notification(
    db: AsyncSession,
    user_id: int,
    type_: str,
    title: str,
    body: str,
    payload_json: dict[str, Any] | None = None,
) -> CabinetNotification:
    """Record in-app notification in cabinet_notifications table."""
    notif = CabinetNotification(
        user_id=user_id,
        type=type_,
        title=title,
        body=body,
        payload_json=payload_json,
        created_at=datetime.now(UTC),
    )
    db.add(notif)
    return notif


async def cancel_pending_payment(
    db: AsyncSession,
    user: User,
    method: str,
    payment_id: int,
) -> PendingPayment:
    """Cancel a user's pending payment locally."""
    try:
        payment_method = PaymentMethod(method)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Invalid payment method: {method}',
        )

    record = await get_payment_record(db, payment_method, payment_id)
    if not record or not record.user or record.user.id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Payment not found',
        )

    if record.is_paid:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Cannot cancel an already paid payment',
        )

    # Idempotent: if already cancelled
    if is_status_cancelled(record.status):
        return record

    payment = record.payment
    if record.method in {PaymentMethod.JUPITER, PaymentMethod.DONUT}:
        payment.status = 'cancelled'
        record.status = 'cancelled'
    else:
        payment.status = 'canceled'
        record.status = 'canceled'

    if hasattr(payment, 'updated_at'):
        payment.updated_at = datetime.now(UTC)

    purpose, _ = resolve_purpose(record)
    amount_rubles = record.amount_kopeks / 100
    await record_cabinet_notification(
        db=db,
        user_id=user.id,
        type_=NotificationType.PAYMENT_INVOICE_CANCELLED.value,
        title='Счёт отменён',
        body=f'Счёт на {amount_rubles:.0f} ₽ ({purpose}) отменён.',
        payload_json={
            'method': record.method.value,
            'payment_id': record.local_id,
            'amount_kopeks': record.amount_kopeks,
        },
    )

    await db.commit()
    refreshed = await get_payment_record(db, payment_method, payment_id) or record

    await send_invoice_cancelled_telegram_message(
        user=user,
        purpose=purpose,
        amount_kopeks=record.amount_kopeks,
    )

    return refreshed
