"""Экраны reply-меню Invoxy: «Мой тариф», «Продлить», «Кабинет», «Сайт», «Язык».

Чистые построители без роутинга aiogram. Mini App открывается только inline-кнопкой
web_app (у кнопок reply-клавиатуры нет initData). Баннер — ключ из SCREEN_BANNERS либо None
(текстовые экраны).
"""

from __future__ import annotations

import asyncio
import html
from collections.abc import Sequence
from dataclasses import dataclass

import structlog
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from app.config import settings
from app.database.models import Subscription
from app.keyboards.invoxy_reply import SUPPORTED_LANGUAGES, normalize_language
from app.localization.texts import get_texts
from app.services.pricing_engine import pricing_engine
from app.utils.invoxy_links import cabinet_root_url, purchase_url, renew_url, website_url
from app.utils.pricing_utils import format_period_description


logger = structlog.get_logger(__name__)

RENEW_PAGE_SIZE = 5
ENDING_SOON_DAYS = 3
PRICE_HINT_TIMEOUT_SECONDS = 3.0
_NON_RENEWABLE_STATUSES = frozenset({'disabled', 'pending'})
_LANGUAGE_NAMES = {'ru': '🇷🇺 Русский', 'en': '🇬🇧 English'}


@dataclass(frozen=True)
class Screen:
    text: str
    markup: InlineKeyboardMarkup | None
    banner: str | None = None


def _is_active(sub: Subscription) -> bool:
    # 'limited' (трафик исчерпан) для UI считается активной — как в show_main_menu.
    return bool(sub.is_active or getattr(sub, 'actual_status', None) == 'limited')


def renewable_subscriptions(subs: Sequence[Subscription]) -> list[Subscription]:
    """Подписки, которые можно продлить: всё, кроме disabled/pending (как в cabinet renewal options).

    Сначала активные (ближайшее окончание первым), затем истёкшие (самые свежие первыми).
    """
    result = []
    for sub in subs or []:
        if getattr(sub, 'actual_status', None) in _NON_RENEWABLE_STATUSES:
            continue
        if settings.is_tariffs_mode() and not sub.tariff_id:
            continue
        result.append(sub)
    active = sorted((s for s in result if _is_active(s)), key=lambda s: s.end_date)
    expired = sorted((s for s in result if not _is_active(s)), key=lambda s: s.end_date, reverse=True)
    return active + expired


async def renewal_price_hint(db, sub: Subscription, user) -> tuple[int, int] | None:
    """(период в днях, цена в копейках) самого дешёвого периода продления; None, если цену не получить.

    Цена — приятное дополнение к сообщению: при ошибке или задержке дольше PRICE_HINT_TIMEOUT_SECONDS
    кнопка продления всё равно показывается, просто без цены.
    """

    async def _cheapest() -> tuple[int, int] | None:
        tariff = sub.tariff
        if tariff is not None and tariff.is_active and tariff.period_prices:
            periods = sorted(int(k) for k in tariff.period_prices)
        else:
            periods = list(settings.get_available_renewal_periods())
        best: tuple[int, int] | None = None
        for period in periods:
            pricing = await pricing_engine.calculate_renewal_price(db, sub, period, user=user)
            if pricing.final_total > 0 and (best is None or pricing.final_total < best[1]):
                best = (period, pricing.final_total)
        return best

    try:
        return await asyncio.wait_for(_cheapest(), timeout=PRICE_HINT_TIMEOUT_SECONDS)
    except Exception as error:  # включая TimeoutError
        logger.warning('Не удалось получить цену продления для сообщения бота', error=repr(error))
        return None


def _name(sub: Subscription) -> str:
    tariff = getattr(sub, 'tariff', None)
    return html.escape((getattr(tariff, 'name', None) or 'VPN').strip() or 'VPN')


def _date(sub: Subscription) -> str:
    return sub.end_date.strftime('%d.%m.%Y')


def _traffic(texts, sub: Subscription) -> str:
    limit = int(sub.traffic_limit_gb or 0)
    if limit == 0:
        return texts.t('INVOXY_TRAFFIC_UNLIMITED')
    used = round(float(sub.traffic_used_gb or 0), 1)
    return texts.t('INVOXY_TRAFFIC_VALUE').format(used=f'{used:g}', limit=limit)


def _main_menu_row(texts) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=texts.t('INVOXY_BTN_MAIN_MENU'), callback_data='back_to_menu')]


def _web_app_button(text: str, url: str | None) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))] if url else []


def _purchase_rows(texts) -> list[list[InlineKeyboardButton]]:
    rows = [[InlineKeyboardButton(text=texts.t('INVOXY_BTN_BUY'), callback_data='menu_buy')]]
    cabinet = _web_app_button(texts.t('INVOXY_BTN_BUY_CABINET'), purchase_url())
    if cabinet:
        rows.append(cabinet)
    return rows


def _support_row(texts) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=texts.t('INVOXY_BTN_SUPPORT'), callback_data='menu_support')]


async def build_tariff_screen(db, user, language: str | None) -> Screen:
    lang = normalize_language(language)
    texts = get_texts(lang)
    all_subs = list(getattr(user, 'subscriptions', None) or [])
    subs = renewable_subscriptions(all_subs)

    if not subs:
        if all_subs:  # есть тарифы, но все disabled/pending
            tariff = _name(all_subs[0])
            text = texts.t('INVOXY_TARIFF_INACTIVE').format(tariff=tariff)
            return Screen(
                text,
                InlineKeyboardMarkup(inline_keyboard=[_support_row(texts), _main_menu_row(texts)]),
                'subscription_inactive',
            )
        rows = [*_purchase_rows(texts), _main_menu_row(texts)]
        return Screen(texts.t('INVOXY_TARIFF_NONE'), InlineKeyboardMarkup(inline_keyboard=rows), 'subscription')

    sub = subs[0]
    if _is_active(sub):
        soon = sub.days_left <= ENDING_SOON_DAYS
        text = texts.t('INVOXY_TARIFF_STATUS').format(
            tariff=_name(sub),
            expires=_date(sub),
            days=sub.days_left,
            traffic=_traffic(texts, sub),
            next_action=texts.t('INVOXY_NEXT_SOON' if soon else 'INVOXY_NEXT_OK'),
        )
    else:
        text = texts.t('INVOXY_TARIFF_EXPIRED').format(tariff=_name(sub), expires=_date(sub))

    rows: list[list[InlineKeyboardButton]] = []
    renew = _web_app_button(texts.t('INVOXY_BTN_RENEW'), renew_url(sub.id))
    if renew:
        rows.append(renew)
    if _is_active(sub):
        rows.append([InlineKeyboardButton(text=texts.t('INVOXY_BTN_CONNECT'), callback_data='subscription_connect')])
    if len(subs) > 1:
        rows.append([InlineKeyboardButton(text=texts.t('INVOXY_BTN_ALL_TARIFFS'), callback_data='menu_subscription')])
    rows.append(_main_menu_row(texts))
    return Screen(text, InlineKeyboardMarkup(inline_keyboard=rows), 'subscription')


async def build_renew_screen(db, user, language: str | None, page: int = 0) -> Screen:
    lang = normalize_language(language)
    texts = get_texts(lang)
    all_subs = list(getattr(user, 'subscriptions', None) or [])
    subs = renewable_subscriptions(all_subs)

    if not subs:
        if all_subs:
            text = texts.t('INVOXY_TARIFF_INACTIVE').format(tariff=_name(all_subs[0]))
            rows = [_support_row(texts), _main_menu_row(texts)]
            return Screen(text, InlineKeyboardMarkup(inline_keyboard=rows), 'subscription_inactive')
        rows = [*_purchase_rows(texts), _main_menu_row(texts)]
        return Screen(texts.t('INVOXY_RENEW_NONE'), InlineKeyboardMarkup(inline_keyboard=rows), 'subscription')

    if len(subs) == 1:
        sub = subs[0]
        hint = await renewal_price_hint(db, sub, user)
        active = _is_active(sub)
        fields = {'tariff': _name(sub), 'expires': _date(sub)}
        if hint:
            fields['price'] = settings.format_price(hint[1])
            fields['period'] = format_period_description(hint[0], lang)
            key = 'INVOXY_RENEW_ONE' if active else 'INVOXY_RENEW_EXPIRED'
        else:
            key = 'INVOXY_RENEW_ONE_NOPRICE' if active else 'INVOXY_RENEW_EXPIRED_NOPRICE'
        rows = []
        button = _web_app_button(texts.t('INVOXY_BTN_OPEN_RENEW'), renew_url(sub.id))
        if button:
            rows.append(button)
        rows.append(_main_menu_row(texts))
        banner = 'renew' if active else 'renew_expired'
        return Screen(texts.t(key).format(**fields), InlineKeyboardMarkup(inline_keyboard=rows), banner)

    pages = (len(subs) + RENEW_PAGE_SIZE - 1) // RENEW_PAGE_SIZE
    page = min(max(int(page), 0), pages - 1)
    chunk = subs[page * RENEW_PAGE_SIZE : (page + 1) * RENEW_PAGE_SIZE]
    rows = []
    for sub in chunk:
        label = texts.t('INVOXY_BTN_RENEW_SUB').format(tariff=_name_plain(sub))
        button = _web_app_button(label, renew_url(sub.id))
        if button:
            rows.append(button)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=texts.t('INVOXY_BTN_PREV'), callback_data=f'rnw:{page - 1}'))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text=texts.t('INVOXY_BTN_NEXT'), callback_data=f'rnw:{page + 1}'))
    if nav:
        rows.append(nav)
    rows.append(_main_menu_row(texts))
    banner = 'renew' if _is_active(subs[0]) else 'renew_expired'
    return Screen(texts.t('INVOXY_RENEW_MANY'), InlineKeyboardMarkup(inline_keyboard=rows), banner)


def _name_plain(sub: Subscription) -> str:
    """Название тарифа для текста кнопки (без HTML-экранирования), не длиннее 30 символов."""
    tariff = getattr(sub, 'tariff', None)
    return ((getattr(tariff, 'name', None) or 'VPN').strip() or 'VPN')[:30]


def build_cabinet_screen(language: str | None) -> Screen:
    texts = get_texts(normalize_language(language))
    url = cabinet_root_url()
    if not url:
        return Screen(
            texts.t('INVOXY_CABINET_UNAVAILABLE'), InlineKeyboardMarkup(inline_keyboard=[_main_menu_row(texts)])
        )
    rows = [_web_app_button(texts.t('INVOXY_BTN_OPEN_CABINET'), url), _main_menu_row(texts)]
    return Screen(texts.t('INVOXY_CABINET_TEXT'), InlineKeyboardMarkup(inline_keyboard=rows))


def build_site_screen(language: str | None) -> Screen:
    texts = get_texts(normalize_language(language))
    url = website_url()
    if not url:
        return Screen(texts.t('INVOXY_SITE_UNAVAILABLE'), InlineKeyboardMarkup(inline_keyboard=[_main_menu_row(texts)]))
    rows = [[InlineKeyboardButton(text=texts.t('INVOXY_BTN_OPEN_SITE'), url=url)], _main_menu_row(texts)]
    return Screen(f'{texts.t("INVOXY_SITE_TEXT")} {html.escape(url)}', InlineKeyboardMarkup(inline_keyboard=rows))


def available_languages() -> list[str]:
    """Языки, доступные в переключателе: ru/en ∩ AVAILABLE_LANGUAGES (иначе только ru)."""
    configured = {str(code).strip().lower() for code in settings.get_available_languages()}
    result = [code for code in SUPPORTED_LANGUAGES if code in configured]
    return result or ['ru']


def build_language_picker(language: str | None) -> Screen:
    current = normalize_language(language)
    texts = get_texts(current)
    buttons = [
        InlineKeyboardButton(
            text=('✓ ' if code == current else '') + _LANGUAGE_NAMES[code], callback_data=f'ilang:{code}'
        )
        for code in available_languages()
    ]
    rows = [buttons, _main_menu_row(texts)]
    return Screen(texts.t('INVOXY_LANG_PROMPT'), InlineKeyboardMarkup(inline_keyboard=rows))
