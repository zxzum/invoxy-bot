"""Compact admin invoice lifecycle; source lives beside the existing payment."""

import html
from contextvars import ContextVar
from types import SimpleNamespace

from sqlalchemy import inspect, select

from app.config import settings
from app.database.models import Transaction
from app.utils.cache import cache


invoice_source = ContextVar('invoice_source', default='telegram_bot')
SOURCES = {
    'telegram_bot': 'Telegram-бот',
    'telegram_miniapp': 'Telegram Mini App',
    'cabinet': 'Кабинет',
    'app': 'VPN App',
}
SOURCE_KEY = 'invoxy_invoice_source'


def invoice_message(
    user,
    *,
    event,
    amount_kopeks,
    method,
    invoice_id,
    source,
    purpose='Пополнение баланса',
    method_title=None,
    old_balance=None,
    transaction_id=None,
) -> str:
    name = html.escape(getattr(user, 'full_name', None) or getattr(user, 'username', None) or f'#{user.id}')
    username = getattr(user, 'username', None)
    if username and getattr(user, 'full_name', None):
        name += f' (@{html.escape(username.lstrip("@"))})'
    lines = [
        f'<b>{event}</b>',
        f'🧾 Счёт: <code>{html.escape(str(method))}:{html.escape(str(invoice_id))}</code>',
        f'👤 {name} · User #{user.id}',
    ]
    contacts = []
    if telegram_id := getattr(user, 'telegram_id', None):
        contacts.append(f'Telegram ID: <code>{telegram_id}</code>')
    if email := getattr(user, 'email', None):
        contacts.append(html.escape(email))
    if contacts:
        lines.append(' · '.join(contacts))
    lines.extend(
        [
            f'💳 <b>{settings.format_price(amount_kopeks, round_kopeks=False)}</b> · {html.escape(method_title or str(method))}',
            f'📋 {html.escape(purpose)}',
            f'Источник: {SOURCES.get(source, "Неизвестен")}',
        ]
    )
    if (balance := getattr(user, 'balance_kopeks', None)) is not None:
        before = f'{settings.format_price(old_balance, round_kopeks=False)} → ' if old_balance is not None else ''
        lines.append(f'💰 Баланс: {before}{settings.format_price(balance, round_kopeks=False)}')
    if transaction_id is not None:
        lines.append(f'Транзакция: <code>#{transaction_id}</code>')
    return '\n'.join(lines)


def resolve_purpose(record) -> tuple[str, str]:
    """Return (purpose_text, purpose_code: 'topup' | 'tariff')."""
    payment = record.payment
    desc = getattr(payment, 'description', None) or getattr(payment, 'payload', None) or ''
    if 'Оплата тарифа' in desc or 'Активация суточного тарифа' in desc or 'Тариф' in desc:
        return desc or 'Оплата тарифа', 'tariff'
    if desc and 'Пополнение баланса' not in desc:
        return desc, 'topup'
    return 'Пополнение баланса', 'topup'


async def paid_invoice_details(db, transaction, *, stars_payload: str | None = None):
    if stars_payload:
        data = await cache.get(f'invoice:stars:source:{stars_payload}') or {}
        return data.get('id', transaction.id), data.get(SOURCE_KEY), 'Пополнение баланса'
    if db is not None:
        relation = inspect(Transaction).relationships.get(f'{transaction.payment_method}_payment')
        if relation is not None:
            model = relation.mapper.class_
            payment = await db.scalar(select(model).where(model.transaction_id == transaction.id))
            if payment:
                metadata = payment.metadata_json or {}
                purpose, _ = resolve_purpose(SimpleNamespace(payment=payment))
                return payment.id, metadata.get(SOURCE_KEY) or metadata.get('source'), purpose
    return transaction.id, None, 'Пополнение баланса'
