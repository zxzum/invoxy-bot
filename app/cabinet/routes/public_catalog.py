"""Public price catalog for the marketing landing.

Unauthenticated on purpose: the homepage has to show the same purchasable
tariffs a guest would see in the cabinet. Squad UUIDs and server names stay
off this payload.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.dependencies import get_cabinet_db
from app.cabinet.routes.subscription_modules.purchase import _build_tariff_response
from app.config import settings
from app.database.crud.promo_group import get_default_promo_group
from app.database.crud.tariff import get_tariffs_for_user, get_trial_tariff


router = APIRouter(prefix='/public', tags=['Cabinet:Public'])

_HIDDEN_FIELDS = frozenset(
    {
        'servers',
        'servers_count',
        'is_current',
        'extra_devices_count',
        'promo_group_name',
        'traffic_topup_packages',
    }
)

_RESET_COPY = {
    'DAY': 'Трафик обновляется каждый день',
    'WEEK': 'Трафик обновляется каждую неделю',
    'MONTH': 'Трафик обновляется в начале месяца',
    'MONTH_ROLLING': 'Трафик обновляется через месяц после оплаты',
    'NO_RESET': 'Трафик сам не обновляется',
}


def _offers(packages: dict[Any, Any]) -> list[dict[str, Any]]:
    offers: list[dict[str, Any]] = []
    for gb, price in sorted(packages.items(), key=lambda item: int(item[0])):
        price_kopeks = int(price)
        offers.append(
            {
                'gb': int(gb),
                'price_kopeks': price_kopeks,
                'price_label': settings.format_price(price_kopeks),
            }
        )
    return offers


def _annotate_periods(periods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    copied = [dict(period) for period in periods]
    month = next((period for period in copied if int(period.get('days') or 0) == 30), None)
    if month is None:
        return copied
    month_price = int(month.get('price_kopeks') or 0)
    if month_price <= 0:
        return copied
    for period in copied:
        days = int(period.get('days') or 0)
        if days <= 30:
            continue
        months = days // 30
        baseline = month_price * months
        price = int(period.get('price_kopeks') or 0)
        if months > 0 and 0 < price < baseline:
            period['monthly_baseline_kopeks'] = baseline
            period['monthly_baseline_label'] = settings.format_price(baseline)
            period['saving_kopeks'] = baseline - price
            period['saving_label'] = settings.format_price(baseline - price)
    return copied


def present_public_tariff(payload: dict[str, Any], tariff: Any) -> dict[str, Any]:
    """Guest-safe tariff card. Drops infrastructure fields and labels every price."""
    public = {key: value for key, value in payload.items() if key not in _HIDDEN_FIELDS}
    public['periods'] = _annotate_periods(list(payload.get('periods') or []))
    device_price = int(public.get('device_price_kopeks') or 0)
    public['device_price_label'] = settings.format_price(device_price)
    reset_price = int(public.get('whitelist_reset_price_kopeks') or 0)
    public['whitelist_reset_price_label'] = settings.format_price(reset_price)
    daily_price = int(public.get('daily_price_kopeks') or 0)
    public['daily_price_label'] = settings.format_price(daily_price)
    per_day = int(public.get('price_per_day_kopeks') or 0)
    public['price_per_day_label'] = settings.format_price(per_day)
    reset_mode = str(public.get('traffic_reset_mode') or '')
    public['traffic_reset_label'] = _RESET_COPY.get(reset_mode)

    topup_enabled = bool(public.get('traffic_topup_enabled'))
    raw_topup = payload.get('traffic_topup_packages') or {}
    public['traffic_topup_offers'] = _offers(raw_topup) if topup_enabled else []

    whitelist_packages: dict[Any, Any] = {}
    if hasattr(tariff, 'get_whitelist_traffic_topup_packages'):
        whitelist_packages = tariff.get_whitelist_traffic_topup_packages() or {}
    whitelist_enabled = bool(getattr(tariff, 'whitelist_traffic_topup_enabled', False)) and bool(
        whitelist_packages
    )
    public['whitelist_topup_enabled'] = whitelist_enabled
    public['whitelist_topup_offers'] = _offers(whitelist_packages) if whitelist_enabled else []
    return public


def present_public_trial(trial_tariff: Any | None) -> dict[str, Any] | None:
    duration_days = int(settings.TRIAL_DURATION_DAYS or 0)
    if duration_days <= 0:
        return None

    traffic_limit_gb = int(settings.TRIAL_TRAFFIC_LIMIT_GB or 0)
    device_limit = int(settings.TRIAL_DEVICE_LIMIT or 0)
    whitelist_traffic_limit_gb = 0
    if trial_tariff is not None:
        traffic_limit_gb = int(getattr(trial_tariff, 'traffic_limit_gb', traffic_limit_gb) or 0)
        device_limit = int(getattr(trial_tariff, 'device_limit', device_limit) or 0)
        whitelist_traffic_limit_gb = int(getattr(trial_tariff, 'whitelist_traffic_limit_gb', 0) or 0)
        tariff_days = getattr(trial_tariff, 'trial_duration_days', None)
        if tariff_days:
            duration_days = int(tariff_days)

    requires_payment = bool(settings.TRIAL_PAYMENT_ENABLED)
    price_kopeks = int(settings.TRIAL_ACTIVATION_PRICE or 0) if requires_payment else 0
    return {
        'duration_days': duration_days,
        'traffic_limit_gb': traffic_limit_gb,
        'whitelist_traffic_limit_gb': whitelist_traffic_limit_gb,
        'device_limit': device_limit,
        'requires_payment': requires_payment,
        'price_kopeks': price_kopeks,
        'price_label': settings.format_price(price_kopeks),
    }


async def build_public_catalog(db: AsyncSession) -> dict[str, Any]:
    sales_mode = 'tariffs' if settings.is_tariffs_mode() else settings.get_sales_mode()
    tariffs: list[dict[str, Any]] = []
    if sales_mode == 'tariffs':
        # Guests have no promo group; show what a new account would see, i.e. the default group's tariffs.
        default_group = await get_default_promo_group(db)
        rows = await get_tariffs_for_user(db, default_group.id if default_group else None)
        for tariff in rows:
            payload = await _build_tariff_response(db, tariff, None, 'ru', None, None)
            tariffs.append(present_public_tariff(payload, tariff))

    bot_username = settings.get_bot_username()
    return {
        'sales_mode': sales_mode,
        'bot_url': f'https://t.me/{bot_username}' if bot_username else None,
        'support_url': settings.get_support_contact_url(),
        'trial': present_public_trial(await get_trial_tariff(db)),
        'tariffs': tariffs,
    }


@router.get('/catalog')
async def get_public_catalog(db: AsyncSession = Depends(get_cabinet_db)) -> dict[str, Any]:
    """Prices, limits and add-ons for the public landing."""
    return await build_public_catalog(db)
