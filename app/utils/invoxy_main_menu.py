"""Главная карточка Invoxy с полной сводкой и сгруппированными действиями."""

import html
from datetime import UTC, datetime

import structlog
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from app.config import settings
from app.database.crud.subscription import get_all_subscriptions_by_user_id
from app.database.crud.tariff import get_tariff_by_id
from app.utils.invoxy_links import cabinet_root_url, website_url
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)


def _partition(keyboard, texts):
    primary, core, extra = [], [], []
    connect_text = texts.t('CONNECT_BUTTON', '🔗 Подключиться')
    cabinet_url = cabinet_root_url()
    for row in keyboard.inline_keyboard:
        for button in row:
            callback = button.callback_data
            if (
                callback
                in {
                    'subscription_connect',
                    'open_subscription_link',
                    'menu_trial',
                    'menu_buy',
                    'return_to_saved_cart',
                    'subscription_resume_checkout',
                }
                or button.text == connect_text
            ):
                primary.append(button)
            elif callback in {'menu_subscription', 'menu_balance', 'buy_traffic', 'menu_support'}:
                core.append(button)
            elif button.web_app and button.web_app.url.rstrip('/') == cabinet_url:
                core.append(button.model_copy(update={'text': texts.t('INVOXY_BTN_CABINET')}))
            elif not (button.url and button.url == website_url()):
                extra.append(button)
    return primary[:2], core, primary[2:] + extra


def compact_keyboard(keyboard, texts):
    primary, core, _ = _partition(keyboard, texts)
    labels = {'menu_support': 'INVOXY_BTN_SUPPORT', 'menu_subscription': 'INVOXY_BTN_MANAGE_SUBSCRIPTION'}
    for index, button in enumerate(core):
        label = 'INVOXY_BTN_OPEN_CABINET' if button.web_app else labels.get(button.callback_data)
        if label:
            core[index] = button.model_copy(update={'text': texts.t(label)})
    if not any(button.web_app for button in core) and cabinet_root_url():
        core.append(
            InlineKeyboardButton(text=texts.t('INVOXY_BTN_OPEN_CABINET'), web_app=WebAppInfo(url=cabinet_root_url()))
        )
    primary = [
        button.model_copy(update={'text': texts.t('INVOXY_BTN_BUY')}) if button.callback_data == 'menu_buy' else button
        for button in primary
    ]
    rows = [[button] for button in primary]
    rows.extend([button] for button in core if button.callback_data == 'menu_subscription')
    billing = [button for button in core if button.callback_data in {'menu_balance', 'buy_traffic'}]
    if billing:
        rows.append(billing)
    rows.extend([button] for button in core if button.web_app)
    last = [button for button in core if button.callback_data == 'menu_support']
    last.append(InlineKeyboardButton(text=texts.t('INVOXY_BTN_MORE'), callback_data='invoxy_more'))
    rows.append(last)
    if website_url():
        rows.append([InlineKeyboardButton(text=texts.t('INVOXY_BTN_SITE'), url=website_url())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def more_keyboard(keyboard, texts):
    _, _, extra = _partition(keyboard, texts)
    extra = [
        button.model_copy(update={'text': texts.t('INVOXY_BTN_INVITE')})
        if button.callback_data == 'menu_referrals'
        else button
        for button in extra
    ]
    rows = [extra[i : i + 2] for i in range(0, len(extra), 2)]
    rows.append([InlineKeyboardButton(text=texts.t('INVOXY_BTN_MAIN_MENU'), callback_data='back_to_menu')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def compact_menu_text(user, texts, db):
    subscriptions = [getattr(user, 'subscription', None)]
    if settings.is_multi_tariff_enabled():
        try:
            subscriptions = await get_all_subscriptions_by_user_id(db, user.id)
        except Exception as error:
            logger.warning('Не удалось загрузить список подписок для компактного меню', error=str(error))
    subscriptions = [sub for sub in subscriptions if sub and not getattr(sub, 'is_pending_trial', False)]
    lines = []
    if not subscriptions:
        lines.append(texts.t('INVOXY_HOME_NO_SUB'))
        return f'<blockquote>{lines[0]}</blockquote>'
    sub = next((s for s in subscriptions if s.actual_status in {'active', 'trial', 'limited'}), subscriptions[0])
    tariff = None
    if getattr(sub, 'tariff_id', None):
        try:
            tariff = await get_tariff_by_id(db, sub.tariff_id)
        except Exception as error:
            logger.warning('Не удалось загрузить название тарифа для компактного меню', error=str(error))
    name = html.escape((getattr(tariff, 'name', None) or 'VPN')[:40])
    status = sub.actual_status
    label_key = (
        'MAIN_MENU_RICH_STATUS_TRIAL'
        if getattr(sub, 'is_trial', False) and status in {'active', 'trial'}
        else {
            'active': 'MAIN_MENU_RICH_STATUS_ACTIVE',
            'trial': 'MAIN_MENU_RICH_STATUS_TRIAL',
            'limited': 'MAIN_MENU_RICH_STATUS_LIMITED',
            'expired': 'MAIN_MENU_RICH_STATUS_EXPIRED',
            'disabled': 'MAIN_MENU_RICH_STATUS_DISABLED',
            'pending': 'MAIN_MENU_RICH_STATUS_PENDING',
        }.get(status, 'MAIN_MENU_RICH_STATUS_DISABLED')
    )
    lines.append(f'<b>{name}</b> · {texts.t(label_key)}')
    if getattr(sub, 'end_date', None) and not getattr(tariff, 'is_daily', False):
        days = max(0, (sub.end_date - datetime.now(UTC)).days)
        lines.append(
            texts.t('INVOXY_HOME_UNTIL').format(date=format_local_datetime(sub.end_date, '%d.%m.%Y'), days=days)
        )
    lines.append('')
    used = f'{float(getattr(sub, "traffic_used_gb", 0) or 0):g}'
    limit = getattr(sub, 'traffic_limit_gb', 0) or 0
    traffic = (
        texts.t('INVOXY_TRAFFIC_VALUE').format(used=used, limit=limit) if limit else texts.t('INVOXY_TRAFFIC_UNLIMITED')
    )
    lines.append('📊 ' + texts.t('INVOXY_HOME_TRAFFIC').format(traffic=traffic))
    lte_limit = getattr(sub, 'whitelist_traffic_limit_gb', 0) or 0
    if lte_limit > 0:
        lte_used = max(0, getattr(sub, 'whitelist_traffic_used_bytes', 0) or 0) / 1024**3
        lte_traffic = texts.t('INVOXY_TRAFFIC_VALUE').format(used=f'{lte_used:.1f}', limit=lte_limit)
        lines.append(texts.t('INVOXY_HOME_LTE').format(traffic=lte_traffic))
    devices = texts.format_device_limit(getattr(sub, 'device_limit', None))
    lines.append(texts.t('MAIN_MENU_RICH_DEVICES').format(devices=devices))
    if status not in {'active', 'trial', 'limited'}:
        lines.append('')
        lines.append(texts.t('INVOXY_HOME_INACTIVE'))
    if len(subscriptions) > 1:
        lines.append(texts.t('INVOXY_HOME_OTHER_SUBS').format(count=len(subscriptions) - 1))
    return '<blockquote>' + '\n'.join(lines) + '</blockquote>'
