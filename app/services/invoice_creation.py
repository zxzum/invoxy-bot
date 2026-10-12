"""Shared invoice creation guard for bot and cabinet."""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import wraps
from types import SimpleNamespace
from uuid import uuid4

import structlog
from fastapi import HTTPException
from redis.exceptions import RedisError

from app.database.models import PaymentMethod
from app.utils.cache import cache


logger = structlog.get_logger(__name__)


@asynccontextmanager
async def invoice_creation_lock(user_id: int):
    # INVOXY: provider CRUD commits internally; a transaction row lock would
    # release before the invoice is ready. Redis serializes bot and cabinet.
    if cache.redis_client is None or not cache._connected:
        logger.warning('Invoice creation blocked: Redis unavailable', user_id=user_id)
        raise HTTPException(503, 'Создание счёта временно недоступно. Попробуйте позже.')
    try:
        async with cache.redis_client.lock(f'invoice:create:{user_id}', timeout=90, blocking_timeout=5):
            async with asyncio.timeout(60):
                yield
    except (RedisError, TimeoutError) as error:
        logger.warning('Invoice creation interrupted', user_id=user_id, error_type=type(error).__name__)
        raise HTTPException(503, 'Счёт ещё создаётся. Подождите немного и попробуйте снова.') from error


def serialized_invoice_creation(func):
    signature = inspect.signature(func, eval_str=True)

    @wraps(func)
    async def wrapped(*args, **kwargs):
        arguments = signature.bind(*args, **kwargs).arguments
        async with invoice_creation_lock(arguments['user'].id):
            return await func(*args, **kwargs)

    wrapped.__signature__ = signature
    return wrapped


async def get_stars_invoice(user, data=None):
    """Stars links have no provider payment row; retain the open link in Redis."""
    from app.services.payment_verification_service import PendingPayment

    if data is None:
        data = await cache.get(f'invoice:stars:{user.id}')
    if not data:
        return None
    created = datetime.fromisoformat(data['created_at'])
    return PendingPayment(
        method=PaymentMethod.TELEGRAM_STARS,
        local_id=data['id'],
        identifier=data['payload'],
        amount_kopeks=data['amount_kopeks'],
        status='pending',
        is_paid=False,
        created_at=created,
        expires_at=datetime.fromisoformat(data['expires_at']),
        user=user,
        payment=SimpleNamespace(payment_url=data['payment_url'], description='Пополнение баланса'),
    )


async def create_managed_stars_invoice(db, user, payment_service, amount_kopeks: int, stars_amount: int):
    """Caller holds invoice_creation_lock; reuse identical intent across clients."""
    from app.cabinet.services.active_invoice import (
        ACTIVE_INVOICE_TTL,
        build_pending_payment_response,
        get_active_invoice_record,
        get_payment_url,
        same_topup_intent,
    )

    active = await get_active_invoice_record(db, user.id)
    if active:
        if same_topup_intent(active, 'telegram_stars', amount_kopeks):
            return get_payment_url(active)
        raise HTTPException(
            409,
            {
                'code': 'active_invoice_exists',
                'message': 'Оплатите или отмените неоплаченный счёт перед созданием нового.',
                'payment': build_pending_payment_response(active).model_dump(mode='json'),
            },
        )
    now = datetime.now(UTC)
    invoice_id = -int(uuid4().int % (2**52)) - 1
    payload = f'balance_{user.id}_{amount_kopeks}_invoice{abs(invoice_id)}'
    url = await payment_service.create_stars_invoice(
        amount_kopeks=amount_kopeks,
        description=f'Пополнение баланса на {amount_kopeks / 100:.2f} ₽',
        payload=payload,
        stars_amount=stars_amount,
    )
    saved = await cache.set(
        f'invoice:stars:{user.id}',
        {
            'id': invoice_id,
            'payload': payload,
            'payment_url': url,
            'amount_kopeks': amount_kopeks,
            'stars_amount': stars_amount,
            'created_at': now.isoformat(),
            'expires_at': (now + ACTIVE_INVOICE_TTL).isoformat(),
        },
        expire=ACTIVE_INVOICE_TTL,
    )
    if not saved:
        raise HTTPException(503, 'Не удалось сохранить счёт. Попробуйте позже.')
    return url


async def clear_stars_invoice(user_id: int, payload: str):
    # Compare and delete atomically: a late payment must not erase a newer invoice.
    if cache.redis_client is not None:
        await cache.redis_client.eval(
            "local v=redis.call('GET',KEYS[1]); if v and cjson.decode(v).payload==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0",
            1,
            f'invoice:stars:{user_id}',
            payload,
        )
