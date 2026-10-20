import asyncio
import html
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog
from aiogram import Bot, types
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from sqlalchemy import func, inspect as sa_inspect, select
from sqlalchemy.exc import MissingGreenlet, NoInspectionAvailable
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.promo_group import get_promo_group_by_id
from app.database.crud.subscription_event import create_subscription_event
from app.database.crud.transaction import get_transaction_by_id
from app.database.crud.user import get_user_by_id
from app.database.models import (
    AdvertisingCampaign,
    AdvertisingCampaignRegistration,
    GuestPurchase,
    PromoCodeType,
    PromoGroup,
    Subscription,
    Transaction,
    User,
    WithdrawalRequest,
    WithdrawalRequestStatus,
)
from app.keyboards.group_callbacks import strip_group_unsafe_buttons
from app.utils.formatters import format_username_link
from app.utils.message_patch import caption_exceeds_telegram_limit
from app.utils.rich_admin import classic_admin_html_to_rich, try_send_rich_admin_message
from app.utils.timezone import format_local_datetime


# Стандартный формат Telegram bot token: `<numeric_id>:<random_35chars>`.
# Может появиться в str(e) от aiogram при сетевых ошибках, если транспорт
# (httpx/aiohttp) сериализует URL `https://api.telegram.org/bot<TOKEN>/...`.
# Не светим токен в логи (структурированные логи могут уехать в Sentry / ELK).
# Trailing — negative lookahead, а не `\b`: иначе токены, оканчивающиеся
# на `-` или `_`, теряли последний символ при редакции (1-char leak).
# Leading `(?<![\w-])` — намеренно НЕ матчит, если перед токеном стоит word/digit
# (например `foo123456789:AAH...`). Это trade-off против false-positive'ов
# на timestamp/UUID-подобных последовательностях. Aiogram и httpx всегда
# префиксят токен либо `bot`, либо URL-границей (`/`, пробел, кавычка),
# так что реальный corpus ошибок не страдает.
_BOT_TOKEN_RE: re.Pattern[str] = re.compile(
    r'(?<![\w-])(?:bot)?\d{6,}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])',
)


def _redact_telegram_secrets(text: str) -> str:
    """Replace Telegram bot tokens in an arbitrary string with a placeholder."""
    return _BOT_TOKEN_RE.sub('bot[REDACTED]', text)


def _format_waiting(waited_minutes: int) -> str:
    """Сколько заявка ждёт решения: «3 д 4 ч», «2 ч 5 мин», «15 мин»."""
    total = max(0, int(waited_minutes))
    days, rest = divmod(total, 24 * 60)
    hours, minutes = divmod(rest, 60)
    if days:
        return f'{days} д {hours} ч' if hours else f'{days} д'
    if hours:
        return f'{hours} ч {minutes} мин' if minutes else f'{hours} ч'
    return f'{minutes} мин'


class NotificationCategory(StrEnum):
    """Категории уведомлений для маршрутизации по топикам."""

    PURCHASES = 'purchases'  # Покупки подписок, покупки с лендинга
    RENEWALS = 'renewals'  # Продления
    TRIALS = 'trials'  # Триалы
    BALANCE = 'balance'  # Пополнение баланса
    ADDONS = 'addons'  # Докупка трафика/устройств/серверов
    INFRASTRUCTURE = 'infrastructure'  # Ноды, техработы, статус панели, вебхуки
    ERRORS = 'errors'  # Ошибки бота, краши
    PROMO = 'promo'  # Промокоды, кампании, промогруппы
    PARTNERS = 'partners'  # Партнёрки, выводы, админ-действия
    TICKETS = 'tickets'  # Тикеты (уже существует)
    NEW_USERS = 'new_users'  # Регистрации новых клиентов


logger = structlog.get_logger(__name__)


def _loaded_relationship(instance: object, name: str) -> Any:
    """Значение связи, только если она УЖЕ загружена; иначе None и никакого IO.

    ``getattr(obj, name, None)`` для этого не годится: у незагруженной связи
    async-сессия не отдаёт None, а лезет в базу — и падает MissingGreenlet, потому
    что default у getattr срабатывает лишь на AttributeError.

    Ровно на этом падало уведомление о регистрации по рекламной кампании:
    apply_campaign_bonus перечитывает пользователя через ``db.refresh(user)``
    (сам по себе — фикс прошлого MissingGreenlet), а refresh сбрасывает ранее
    загруженные связи, включая promo_group.
    """
    try:
        state = sa_inspect(instance)
    except NoInspectionAvailable:
        # Не ORM-объект (тестовые фейки, SimpleNamespace) — обычный доступ безопасен.
        return getattr(instance, name, None)

    if name in state.unloaded:
        return None
    return state.dict.get(name)


class AdminNotificationService:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.chat_id = getattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None)
        self.topic_id = getattr(settings, 'ADMIN_NOTIFICATIONS_TOPIC_ID', None)
        self.ticket_topic_id = getattr(settings, 'ADMIN_NOTIFICATIONS_TICKET_TOPIC_ID', None)
        self.enabled = getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)

        # Маппинг категорий на topic_id (None = fallback на self.topic_id)
        self.category_topics: dict[NotificationCategory, int | None] = {
            NotificationCategory.PURCHASES: getattr(settings, 'ADMIN_NOTIFICATIONS_PURCHASES_TOPIC_ID', None),
            NotificationCategory.RENEWALS: getattr(settings, 'ADMIN_NOTIFICATIONS_RENEWALS_TOPIC_ID', None),
            NotificationCategory.TRIALS: getattr(settings, 'ADMIN_NOTIFICATIONS_TRIALS_TOPIC_ID', None),
            NotificationCategory.BALANCE: getattr(settings, 'ADMIN_NOTIFICATIONS_BALANCE_TOPIC_ID', None),
            NotificationCategory.ADDONS: getattr(settings, 'ADMIN_NOTIFICATIONS_ADDONS_TOPIC_ID', None),
            NotificationCategory.INFRASTRUCTURE: getattr(settings, 'ADMIN_NOTIFICATIONS_INFRASTRUCTURE_TOPIC_ID', None),
            NotificationCategory.ERRORS: getattr(settings, 'ADMIN_NOTIFICATIONS_ERRORS_TOPIC_ID', None),
            NotificationCategory.PROMO: getattr(settings, 'ADMIN_NOTIFICATIONS_PROMO_TOPIC_ID', None),
            NotificationCategory.PARTNERS: getattr(settings, 'ADMIN_NOTIFICATIONS_PARTNERS_TOPIC_ID', None),
            NotificationCategory.TICKETS: self.ticket_topic_id,
            NotificationCategory.NEW_USERS: getattr(settings, 'ADMIN_NOTIFICATIONS_NEW_USERS_TOPIC_ID', None),
        }

        # Per-category enabled flags (default True — backwards compatible)
        self.category_enabled: dict[NotificationCategory, bool] = {}
        for cat in NotificationCategory:
            key = f'ADMIN_NOTIFICATIONS_{cat.value.upper()}_ENABLED'
            self.category_enabled[cat] = getattr(settings, key, True)

    async def _get_referrer_info(self, db: AsyncSession, referred_by_id: int | None) -> str:
        if not referred_by_id:
            return 'Нет'

        try:
            referrer = await get_user_by_id(db, referred_by_id)
            if not referrer:
                return f'ID {referred_by_id} (не найден)'

            if referrer.username:
                return f'{format_username_link(referrer.username)} (ID: {referred_by_id})'
            if referrer.telegram_id:
                return f'ID {referrer.telegram_id}'
            if referrer.email:
                return f'📧 {html.escape(referrer.email)}'
            return f'User#{referred_by_id}'

        except Exception as e:
            logger.error('Ошибка получения данных рефера', referred_by_id=referred_by_id, error=e)
            return f'ID {referred_by_id}'

    async def _get_user_promo_group(self, db: AsyncSession, user: User) -> PromoGroup | None:
        promo_group = _loaded_relationship(user, 'promo_group')
        if promo_group:
            return promo_group

        try:
            promo_group_id = user.promo_group_id
        except Exception:
            # Инстанс отвязан от сессии или протух — колонка тоже ушла бы в ленивую
            # подгрузку. Берём последнее известное значение из __dict__.
            promo_group_id = user.__dict__.get('promo_group_id')
        if not promo_group_id:
            return None

        try:
            await db.refresh(user, attribute_names=['promo_group'])
        except Exception:
            # relationship might not be available — fallback to direct fetch
            pass

        promo_group = _loaded_relationship(user, 'promo_group')
        if promo_group:
            return promo_group

        try:
            return await get_promo_group_by_id(db, promo_group_id)
        except Exception as e:
            logger.error(
                'Ошибка загрузки промогруппы пользователя',
                promo_group_id=promo_group_id,
                telegram_id=user.__dict__.get('telegram_id'),
                e=e,
            )
            return None

    def _get_user_display(self, user: User) -> str:
        first_name = getattr(user, 'first_name', '') or getattr(user, 'full_name', '') or ''
        if first_name:
            return html.escape(first_name)

        username = getattr(user, 'username', '') or ''
        if username:
            return html.escape(username)

        telegram_id = getattr(user, 'telegram_id', None)
        if telegram_id is None:
            email = getattr(user, 'email', None)
            if email:
                return html.escape(email)
            return f'User#{getattr(user, "id", "Unknown")}'
        return f'ID{telegram_id}'

    def _get_user_identifier_display(self, user: User) -> str:
        """Get user identifier for display in notifications (telegram_id or email)."""
        telegram_id = getattr(user, 'telegram_id', None)
        if telegram_id:
            return f'<code>{telegram_id}</code>'

        email = getattr(user, 'email', None)
        if email:
            return f'📧 {html.escape(email)}'

        return f'User#{getattr(user, "id", "Unknown")}'

    def _get_user_identifier_label(self, user: User) -> str:
        """Get label for user identifier (Telegram ID or Email)."""
        telegram_id = getattr(user, 'telegram_id', None)
        if telegram_id:
            return 'Telegram ID'
        email = getattr(user, 'email', None)
        if email:
            return 'Email'
        return 'ID'

    async def _record_subscription_event(
        self,
        db: AsyncSession,
        *,
        event_type: str,
        user: User,
        subscription: Subscription | None,
        transaction: Transaction | None = None,
        amount_kopeks: int | None = None,
        message: str | None = None,
        extra: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        """Persist subscription-related event for external dashboards."""

        try:
            await create_subscription_event(
                db,
                user_id=user.id,
                event_type=event_type,
                subscription_id=subscription.id if subscription else None,
                transaction_id=transaction.id if transaction else None,
                amount_kopeks=amount_kopeks,
                currency=None,
                message=message,
                occurred_at=occurred_at,
                extra=extra or None,
            )
        except Exception:
            logger.error(
                'Не удалось сохранить событие подписки для пользователя',
                event_type=event_type,
                getattr=getattr(user, 'id', 'unknown'),
                exc_info=True,
            )

            try:
                await db.rollback()
            except Exception:
                logger.error(
                    'Не удалось выполнить rollback после ошибки события подписки пользователя',
                    getattr=getattr(user, 'id', 'unknown'),
                    exc_info=True,
                )

    def _format_promo_group_discounts(self, promo_group: PromoGroup) -> list[str]:
        discount_lines: list[str] = []

        discount_map = {
            'servers': ('Серверы', promo_group.server_discount_percent),
            'traffic': ('Трафик', promo_group.traffic_discount_percent),
            'devices': ('Устройства', promo_group.device_discount_percent),
        }

        for title, percent in discount_map.values():
            if percent and percent > 0:
                discount_lines.append(f'• {title}: -{percent}%')

        period_discounts_raw = promo_group.period_discounts or {}
        period_items: list[tuple[int, int]] = []

        if isinstance(period_discounts_raw, dict):
            for raw_days, raw_percent in period_discounts_raw.items():
                try:
                    days = int(raw_days)
                    percent = int(raw_percent)
                except (TypeError, ValueError):
                    continue

                if percent > 0:
                    period_items.append((days, percent))

        period_items.sort(key=lambda item: item[0])

        if period_items:
            formatted_periods = ', '.join(f'{days} д. — -{percent}%' for days, percent in period_items)
            discount_lines.append(f'• Периоды: {formatted_periods}')

        if promo_group.apply_discounts_to_addons:
            discount_lines.append('• Доп. услуги: ✅ скидка действует')
        else:
            discount_lines.append('• Доп. услуги: ❌ без скидки')

        return discount_lines

    def _format_promo_group_block(
        self,
        promo_group: PromoGroup | None,
        *,
        title: str = 'Промогруппа',
        icon: str = '🏷️',
    ) -> str:
        if not promo_group:
            return f'{icon} <b>{title}:</b> —'

        lines = [f'{icon} <b>{title}:</b> {html.escape(promo_group.name)}']

        discount_lines = self._format_promo_group_discounts(promo_group)
        if discount_lines:
            lines.append('💸 <b>Скидки:</b>')
            lines.extend(discount_lines)
        else:
            lines.append('💸 <b>Скидки:</b> отсутствуют')

        return '\n'.join(lines)

    def _get_promocode_type_display(self, promo_type: str | None) -> str:
        mapping = {
            PromoCodeType.BALANCE.value: '💰 Бонус на баланс',
            PromoCodeType.SUBSCRIPTION_DAYS.value: '⏰ Доп. дни подписки',
            PromoCodeType.TRIAL_SUBSCRIPTION.value: '🎁 Триал подписка',
            PromoCodeType.PROMO_GROUP.value: '👥 Промогруппа',
            PromoCodeType.DISCOUNT.value: '💸 Скидка',
            PromoCodeType.BALANCE_AND_DAYS.value: '💰📅 Баланс + дни подписки',
        }

        if not promo_type:
            return 'ℹ️ Не указан'

        return mapping.get(promo_type, f'ℹ️ {promo_type}')

    def _format_campaign_bonus(self, campaign: AdvertisingCampaign, *, tariff_name: str | None = None) -> list[str]:
        if campaign.is_balance_bonus:
            return [
                f'💰 Баланс: {settings.format_price(campaign.balance_bonus_kopeks or 0)}',
            ]

        if campaign.is_subscription_bonus:
            default_devices = getattr(settings, 'DEFAULT_DEVICE_LIMIT', 1)
            details = [
                (
                    f'📅 {campaign.subscription_duration_days or 0} дн. '
                    f'• 📊 {campaign.subscription_traffic_gb or 0} ГБ '
                    f'• 📱 {campaign.subscription_device_limit or default_devices} устр.'
                ),
            ]
            if campaign.subscription_squads:
                details.append(f'🌐 Сквады: {len(campaign.subscription_squads)} шт.')
            return details

        if campaign.is_tariff_bonus:
            name = tariff_name or f'ID {campaign.tariff_id}'
            details = [f'📦 Тариф: <b>{name}</b>']
            if campaign.tariff_duration_days:
                details.append(f'📅 Период: {campaign.tariff_duration_days} дней')
            return details

        if campaign.is_none_bonus:
            return ['🔗 Только отслеживание']

        return ['ℹ️ Бонусы не предусмотрены']

    async def send_trial_activation_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        *,
        charged_amount_kopeks: int | None = None,
    ) -> bool:
        try:
            await self._record_subscription_event(
                db,
                event_type='activation',
                user=user,
                subscription=subscription,
                transaction=None,
                amount_kopeks=charged_amount_kopeks,
                message='Trial activation',
                occurred_at=datetime.now(UTC),
                extra={
                    'charged_amount_kopeks': charged_amount_kopeks,
                    'trial_duration_days': (
                        max(1, round((subscription.end_date - subscription.start_date).total_seconds() / 86400))
                        if subscription.end_date and subscription.start_date
                        else settings.TRIAL_DURATION_DAYS
                    ),
                    'traffic_limit_gb': (
                        subscription.traffic_limit_gb
                        if subscription.traffic_limit_gb is not None
                        else settings.TRIAL_TRAFFIC_LIMIT_GB
                    ),
                    'device_limit': subscription.device_limit,
                },
            )

            if not self._is_enabled():
                return False

            user_status = '🆕 Новый' if not user.has_had_paid_subscription else '🔄 Существующий'
            promo_group = await self._get_user_promo_group(db, user)
            user_display = self._get_user_display(user)

            trial_device_limit = subscription.device_limit
            if trial_device_limit is None:
                fallback_forced_limit = settings.get_disabled_mode_device_limit()
                if fallback_forced_limit is not None:
                    trial_device_limit = fallback_forced_limit
                else:
                    trial_device_limit = settings.TRIAL_DEVICE_LIMIT

            payment_block = ''
            if charged_amount_kopeks and charged_amount_kopeks > 0:
                payment_block = f'\n💳 <b>Оплата за активацию:</b> {settings.format_price(charged_amount_kopeks)}'

            user_id_label = self._get_user_identifier_label(user)
            user_id_display = self._get_user_identifier_display(user)

            # Получаем название тарифа (если режим тарифов)
            tariff_name = await self._get_tariff_name(db, subscription)

            message_lines = [
                '🎯 <b>АКТИВАЦИЯ ТРИАЛА</b>',
                '',
                f'👤 <b>Пользователь:</b> {user_display}',
                f'🆔 <b>{user_id_label}:</b> {user_id_display}',
                f'📱 <b>Username:</b> {format_username_link(getattr(user, "username", None), "отсутствует")}',
                f'👥 <b>Статус:</b> {user_status}',
                '',
            ]

            # Промогруппа — только название, без скидок
            if promo_group:
                message_lines.append(f'🏷️ <b>Промогруппа:</b> {html.escape(promo_group.name)}')
            else:
                message_lines.append('🏷️ <b>Промогруппа:</b> —')

            # Тариф триала (если есть)
            if tariff_name:
                message_lines.append(f'📦 <b>Тариф:</b> {tariff_name}')

            message_lines.append('')

            trial_duration_days = settings.TRIAL_DURATION_DAYS
            if subscription.end_date and subscription.start_date:
                trial_duration_days = max(
                    1, round((subscription.end_date - subscription.start_date).total_seconds() / 86400)
                )

            trial_traffic_gb = (
                subscription.traffic_limit_gb
                if subscription.traffic_limit_gb is not None
                else settings.TRIAL_TRAFFIC_LIMIT_GB
            )

            message_lines.extend(
                [
                    '⏰ <b>Параметры триала:</b>',
                    f'📅 Период: {trial_duration_days} дней',
                    f'📊 Трафик: {self._format_traffic(trial_traffic_gb)}',
                    f'📱 Устройства: {trial_device_limit}',
                    f'🌐 Сервер: {subscription.connected_squads[0] if subscription.connected_squads else "По умолчанию"}',
                ]
            )

            if payment_block:
                message_lines.append(payment_block)

            message_lines.append('')
            message_lines.append(
                f'📆 <b>Действует до:</b> {format_local_datetime(subscription.end_date, "%d.%m.%Y %H:%M")}'
            )

            # Реферер — только если есть
            if user.referred_by_id:
                referrer_info = await self._get_referrer_info(db, user.referred_by_id)
                if referrer_info != 'Нет':
                    message_lines.append(f'🔗 <b>Реферер:</b> {referrer_info}')

            message_lines.append('')
            message_lines.append(f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>')

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.TRIALS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о триале', error=e)
            return False

    async def _get_tariff_name(self, db: AsyncSession, subscription: Subscription) -> str | None:
        """Получает название тарифа подписки, если он есть."""
        if not subscription.tariff_id:
            return None

        try:
            from app.database.crud.tariff import get_tariff_by_id

            tariff = await get_tariff_by_id(db, subscription.tariff_id)
            if tariff:
                return html.escape(tariff.name)
        except Exception:
            pass
        return None

    async def _get_tariff_name_by_id(self, db: AsyncSession | None, tariff_id: int | None) -> str | None:
        """Получает название тарифа по tariff_id."""
        if not tariff_id or not db:
            return None
        try:
            from app.database.crud.tariff import get_tariff_by_id

            tariff = await get_tariff_by_id(db, tariff_id)
            if tariff and tariff.name:
                return html.escape(tariff.name)
        except Exception:
            pass
        return None

    def _build_subscription_diff_block_sync(
        self,
        subscription: Any,
        old_state: dict[str, Any] | None,
        period_days: int | None = None,
        old_tariff_name: str | None = None,
        new_tariff_name: str | None = None,
    ) -> list[str]:
        lines: list[str] = []
        if not old_state:
            lines.append('🔍 <b>ПАРАМЕТРЫ ПОДПИСКИ:</b>')
            limit_gb = getattr(subscription, 'traffic_limit_gb', None)
            lines.append(f'• 📊 Лимит трафика: <b>{self._format_traffic(limit_gb)}</b>')
            wl_limit = getattr(subscription, 'whitelist_traffic_limit_gb', 0) or 0
            if wl_limit > 0:
                lines.append(f'• 🛡️ Лимит LTE: <b>{wl_limit} ГБ</b>')
            end_date = getattr(subscription, 'end_date', None)
            if end_date:
                lines.append(
                    f'• 📅 Срок: до <b>{format_local_datetime(end_date, "%d.%m.%Y")}</b>'
                    + (f' ({period_days} дн.)' if period_days else '')
                )
            dev_limit = getattr(subscription, 'device_limit', None)
            if dev_limit:
                lines.append(f'• 📱 Устройства: <b>{dev_limit}</b>')
            status = getattr(subscription, 'status', None)
            if status:
                lines.append(f'• 🏷️ Статус: <b>{status}</b>')
            return lines

        lines.append('🔍 <b>ЧТО ИЗМЕНИЛОСЬ:</b>')

        # Тариф
        old_tid = old_state.get('tariff_id')
        new_tid = getattr(subscription, 'tariff_id', None)
        if old_tariff_name or new_tariff_name or (old_tid != new_tid):
            old_t_display = old_tariff_name or (f'ID {old_tid}' if old_tid else 'Без тарифа')
            new_t_display = new_tariff_name or (f'ID {new_tid}' if new_tid else 'Без тарифа')
            if old_t_display != new_t_display:
                lines.append(f'• 🏷️ Тариф: <b>{old_t_display}</b> → <b>{new_t_display}</b>')

        # Лимит трафика
        old_limit = old_state.get('traffic_limit_gb')
        new_limit = getattr(subscription, 'traffic_limit_gb', None)
        if old_limit != new_limit:
            lines.append(
                f'• 📊 Лимит трафика: <b>{self._format_traffic(old_limit)} → {self._format_traffic(new_limit)}</b>'
            )
        elif new_limit is not None:
            lines.append(f'• 📊 Лимит трафика: <b>{self._format_traffic(new_limit)}</b>')

        # Лимит LTE / Whitelist
        old_wl = old_state.get('whitelist_traffic_limit_gb', 0) or 0
        new_wl = getattr(subscription, 'whitelist_traffic_limit_gb', 0) or 0
        if old_wl != new_wl:
            lines.append(f'• 🛡️ Лимит LTE: <b>{old_wl} ГБ → {new_wl} ГБ</b>')
        elif new_wl > 0:
            lines.append(f'• 🛡️ Лимит LTE: <b>{new_wl} ГБ</b>')

        # Расход трафика
        old_used = round(float(old_state.get('traffic_used_gb', 0.0) or 0.0), 2)
        new_used = round(float(getattr(subscription, 'traffic_used_gb', 0.0) or 0.0), 2)
        if old_used > 0 and new_used == 0.0:
            lines.append(f'• 📉 Расход трафика: <b>{old_used} ГБ → 0.0 ГБ</b> (обнулён)')
        elif old_used != new_used:
            lines.append(f'• 📉 Расход трафика: <b>{old_used} ГБ → {new_used} ГБ</b>')

        # Расход LTE
        old_wl_bytes = old_state.get('whitelist_traffic_used_bytes', 0) or 0
        new_wl_bytes = getattr(subscription, 'whitelist_traffic_used_bytes', 0) or 0
        if old_wl_bytes > 0 and new_wl_bytes == 0:
            lines.append(f'• 📉 Расход LTE: <b>{round(old_wl_bytes / (1024**3), 2)} ГБ → 0.0 ГБ</b> (обнулён)')
        elif old_wl_bytes != new_wl_bytes and (old_wl_bytes > 0 or new_wl_bytes > 0):
            lines.append(
                f'• 📉 Расход LTE: <b>{round(old_wl_bytes / (1024**3), 2)} ГБ → {round(new_wl_bytes / (1024**3), 2)} ГБ</b>'
            )

        # Срок действия
        old_end = old_state.get('end_date')
        new_end = getattr(subscription, 'end_date', None)
        old_end_str = format_local_datetime(old_end, '%d.%m.%Y') if old_end else '—'
        new_end_str = format_local_datetime(new_end, '%d.%m.%Y') if new_end else '—'
        if old_end and new_end and old_end != new_end:
            days_suffix = f' (+{period_days} дн.)' if period_days else ''
            lines.append(f'• 📅 Срок: <b>до {old_end_str} → до {new_end_str}</b>{days_suffix}')
        elif new_end:
            days_suffix = f' ({period_days} дн.)' if period_days else ''
            lines.append(f'• 📅 Срок: <b>до {new_end_str}</b>{days_suffix}')

        # Статус
        old_status = old_state.get('status')
        new_status = getattr(subscription, 'status', None)
        if old_status != new_status:
            lines.append(f'• 🏷️ Статус: <code>{old_status}</code> → <b>{new_status}</b>')
        elif new_status:
            lines.append(f'• 🏷️ Статус: <b>{new_status}</b>')

        # Триал
        old_trial = old_state.get('is_trial', False)
        new_trial = getattr(subscription, 'is_trial', False)
        if old_trial and not new_trial:
            lines.append('• 🎯 Триал: <b>Да → Нет (конвертирован)</b>')

        # Устройства
        old_dev = old_state.get('device_limit')
        new_dev = getattr(subscription, 'device_limit', None)
        if old_dev and new_dev and old_dev != new_dev:
            lines.append(f'• 📱 Устройства: <b>{old_dev} → {new_dev}</b>')

        return lines

    async def _build_subscription_diff_block(
        self,
        db: AsyncSession | None,
        subscription: Any,
        old_state: dict[str, Any] | None,
        period_days: int | None = None,
        current_tariff_name: str | None = None,
    ) -> list[str]:
        old_tariff_name = None
        new_tariff_name = current_tariff_name
        if old_state and old_state.get('tariff_id'):
            old_tariff_name = await self._get_tariff_name_by_id(db, old_state.get('tariff_id'))
        if not new_tariff_name and getattr(subscription, 'tariff_id', None):
            new_tariff_name = await self._get_tariff_name_by_id(db, getattr(subscription, 'tariff_id', None))

        return self._build_subscription_diff_block_sync(
            subscription=subscription,
            old_state=old_state,
            period_days=period_days,
            old_tariff_name=old_tariff_name,
            new_tariff_name=new_tariff_name,
        )

    async def _verify_remnawave_state(self, subscription: Any) -> dict[str, Any] | None:
        """Fetch current user state from RemnaWave panel with a strict timeout."""
        try:
            from app.services.remnawave_service import RemnaWaveService

            remnawave = RemnaWaveService()
            if not getattr(remnawave, 'is_configured', False):
                return None
            panel_id = getattr(subscription, 'remnawave_id', None)
            short_uuid = getattr(subscription, 'remnawave_short_uuid', None)
            if not panel_id and not short_uuid:
                return None

            async with asyncio.timeout(3.5):
                async with remnawave.get_api_client() as api:
                    panel_user = None
                    if panel_id:
                        try:
                            panel_user = await api.get_user_by_id(panel_id)
                        except Exception:
                            pass
                    if not panel_user and short_uuid:
                        try:
                            panel_user = await api.get_user_by_short_uuid(short_uuid)
                        except Exception:
                            pass
                    if panel_user:
                        return {
                            'id': getattr(panel_user, 'id', None),
                            'status': getattr(panel_user, 'status', None),
                            'traffic_limit_bytes': getattr(panel_user, 'traffic_limit_bytes', None),
                            'used_traffic_bytes': getattr(panel_user, 'used_traffic_bytes', None),
                            'email': getattr(panel_user, 'email', None),
                        }
        except Exception as e:
            logger.debug('Auditing RemnaWave user skipped/failed', error=str(e))
        return None

    def _format_remnawave_verification(
        self,
        subscription: Any,
        rw_info: dict[str, Any] | None,
    ) -> list[str]:
        lines: list[str] = []
        lines.append('📡 <b>СИНХРОНИЗАЦИЯ REMNAWAVE:</b>')
        panel_id = getattr(subscription, 'remnawave_id', None)
        short_uuid = getattr(subscription, 'remnawave_short_uuid', None)
        id_display = f'ID {panel_id}' if panel_id else 'ID —'
        if short_uuid:
            id_display += f' ({short_uuid})'
        lines.append(f'🔑 Панель: <code>{id_display}</code>')

        if not rw_info:
            lines.append('ℹ️ Статус панели: данные не получены (офлайн/таймаут)')
            return lines

        panel_status = str(rw_info.get('status') or 'UNKNOWN').upper()
        panel_bytes = int(rw_info.get('traffic_limit_bytes') or 0)
        panel_limit_gb = round(panel_bytes / (1024**3), 1)

        expected_gb = getattr(subscription, 'traffic_limit_gb', 0) or 0
        expected_bytes = expected_gb * 1024**3

        # Сверка лимита: если ожидается лимит > 0 и он не совпадает с панелью
        if expected_gb > 0 and panel_bytes != expected_bytes:
            lines.append(
                f'⚠️ <b>ВНИМАНИЕ: РАССИНХРОН ЛИМИТА!</b>\n'
                f'  • В БД: <b>{expected_gb} ГБ</b>\n'
                f'  • В панели: <b>{panel_limit_gb} ГБ</b>\n'
                f'  • Статус панели: <b>{panel_status}</b>'
            )
        else:
            lines.append(
                f'✅ Лимит в панели: <b>{panel_limit_gb} ГБ</b> (совпадает с БД)\n'
                f'✅ Статус в панели: <b>{panel_status}</b>'
            )
        return lines

    def _build_admin_audit_keyboard(self, user: Any, subscription: Any) -> types.InlineKeyboardMarkup | None:
        buttons: list[types.InlineKeyboardButton] = []
        admin_base = (settings.CABINET_URL or '').rstrip('/')
        user_id = getattr(user, 'id', None)
        if admin_base and user_id:
            buttons.append(
                types.InlineKeyboardButton(
                    text='🔗 Открыть в админке',
                    url=f'{admin_base}/admin/users/{user_id}',
                )
            )
        panel_base = (settings.REMNAWAVE_API_URL or '').rstrip('/').removesuffix('/api')
        panel_id = getattr(subscription, 'remnawave_id', None)
        if panel_base and panel_id:
            buttons.append(
                types.InlineKeyboardButton(
                    text='⚙️ Панель RemnaWave',
                    url=f'{panel_base}/users/{panel_id}',
                )
            )
        if buttons:
            return types.InlineKeyboardMarkup(inline_keyboard=[[btn] for btn in buttons])
        return None

    async def send_traffic_reset_notification(
        self,
        db: AsyncSession,
        user: Any,
        subscription: Any,
        *,
        reset_scope: str,  # 'regular', 'whitelist' / 'lte', 'admin', 'topup'
        cleared_gb: float,
        price_kopeks: int = 0,
        payment_method: str = 'Баланс',
        transaction: Any = None,
        old_state: dict[str, Any] | None = None,
        admin_user: Any = None,
    ) -> bool:
        if not self._is_enabled():
            return False

        try:
            if reset_scope == 'regular':
                title = '🔄 СБРОС ОСНОВНОГО ТРАФИКА'
            elif reset_scope in ('whitelist', 'lte'):
                title = '🔄 СБРОС ТРАФИКА LTE (Белый интернет)'
            elif reset_scope == 'topup':
                title = '📈 ДОКУПКА ТРАФИКА'
            elif reset_scope == 'admin':
                admin_name = (
                    getattr(admin_user, 'full_name', '')
                    or getattr(admin_user, 'username', '')
                    or f'Admin#{getattr(admin_user, "id", "")}'
                )
                title = f'⚙️ АДМИН-СБРОС ТРАФИКА ({admin_name})'
            else:
                title = '🔄 СБРОС ТРАФИКА'

            tariff_name = await self._get_tariff_name(db, subscription)
            user_display = self._get_user_display(user)
            user_id_display = self._get_user_identifier_display(user)

            lines = [
                f'<b>{title}</b>',
                '',
                f'👤 {user_display} ({user_id_display})',
            ]

            username = getattr(user, 'username', None)
            if username:
                lines.append(f'📱 {format_username_link(username)}')

            email = getattr(user, 'email', None)
            if email:
                lines.append(f'📧 {html.escape(email)}')

            sub_id = getattr(subscription, 'id', '—')
            lines.append(f'🆔 User: #{getattr(user, "id", "—")} | Подписка: #{sub_id}')

            if tariff_name:
                lines.append(f'🏷️ Тариф: <b>{tariff_name}</b> (ID {getattr(subscription, "tariff_id", "—")})')

            lines.append('')
            lines.append(f'📉 Сброшено расхода: <b>{cleared_gb:.2f} ГБ</b>')

            if reset_scope in ('whitelist', 'lte'):
                wl_limit = getattr(subscription, 'whitelist_traffic_limit_gb', 0)
                lines.append(f'📊 Лимит LTE: <b>{self._format_traffic(wl_limit)}</b>')
            else:
                main_limit = getattr(subscription, 'traffic_limit_gb', 0)
                lines.append(f'📊 Лимит тарифа: <b>{self._format_traffic(main_limit)}</b>')

            lines.append('')
            # RemnaWave verification
            rw_info = await self._verify_remnawave_state(subscription)
            lines.extend(self._format_remnawave_verification(subscription, rw_info))

            lines.append('')
            if price_kopeks > 0:
                lines.append(f'💵 <b>{settings.format_price(price_kopeks)}</b> • {payment_method}')
            else:
                lines.append('💸 Бесплатно')

            balance_kopeks = getattr(user, 'balance_kopeks', None)
            if balance_kopeks is not None:
                lines.append(f'💰 Баланс: {settings.format_price(balance_kopeks)}')

            if transaction:
                lines.append(f'🆔 #{getattr(transaction, "id", "")}')

            lines.extend(
                [
                    '',
                    f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M")}</i>',
                ]
            )

            keyboard = self._build_admin_audit_keyboard(user, subscription)
            return await self._send_message(
                '\n'.join(lines),
                reply_markup=keyboard,
                category=NotificationCategory.ADDONS,
            )

        except Exception as e:
            logger.error('Ошибка отправки уведомления о сбросе трафика', error=e)
            return False

    async def send_subscription_purchase_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        transaction: Transaction | None,
        period_days: int,
        was_trial_conversion: bool = False,
        amount_kopeks: int | None = None,
        purchase_type: str | None = None,  # 'first_purchase', 'renewal', 'tariff_switch', None (auto-detect)
        old_state: dict[str, Any] | None = None,
    ) -> bool:
        try:
            total_amount = (
                amount_kopeks if amount_kopeks is not None else (abs(transaction.amount_kopeks) if transaction else 0)
            )

            await self._record_subscription_event(
                db,
                event_type='purchase',
                user=user,
                subscription=subscription,
                transaction=transaction,
                amount_kopeks=total_amount,
                message='Subscription purchase',
                occurred_at=(transaction.completed_at or transaction.created_at) if transaction else datetime.now(UTC),
                extra={
                    'period_days': period_days,
                    'was_trial_conversion': was_trial_conversion,
                    'payment_method': self._get_payment_method_display(transaction.payment_method)
                    if transaction
                    else 'Баланс',
                },
            )

            if not self._is_enabled():
                return False

            # Определяем тип операции и заголовок
            if purchase_type == 'tariff_switch':
                event_title = '🔄 СМЕНА ТАРИФА'
                user_status = 'Смена тарифа'
            elif was_trial_conversion:
                event_title = '🔄 КОНВЕРСИЯ ИЗ ТРИАЛА'
                user_status = 'Конверсия'
            elif purchase_type == 'first_purchase':
                event_title = '💎 ПОКУПКА ПОДПИСКИ'
                user_status = 'Первая покупка'
            elif purchase_type == 'renewal' or (purchase_type is None and user.has_had_paid_subscription):
                event_title = '💎 ПРОДЛЕНИЕ ПОДПИСКИ'
                user_status = 'Продление'
            else:
                event_title = '💎 ПОКУПКА ПОДПИСКИ'
                user_status = 'Первая покупка'

            # Получаем название тарифа
            tariff_name = await self._get_tariff_name(db, subscription)

            servers_info = await self._get_servers_info(subscription.connected_squads)
            payment_method = self._get_payment_method_display(transaction.payment_method) if transaction else 'Баланс'
            user_display = self._get_user_display(user)
            user_id_display = self._get_user_identifier_display(user)

            # Формируем компактное сообщение
            message_lines = [
                f'<b>{event_title}</b>',
                '',
                f'👤 {user_display} ({user_id_display})',
            ]

            # Добавляем username только если есть
            username = getattr(user, 'username', None)
            if username:
                message_lines.append(f'📱 {format_username_link(username)}')

            email = getattr(user, 'email', None)
            if email:
                message_lines.append(f'📧 {html.escape(email)}')

            sub_id = getattr(subscription, 'id', '—')
            user_pk = getattr(user, 'id', '—')
            message_lines.append(f'🆔 User: #{user_pk} | Подписка: #{sub_id}')
            message_lines.append(f'📋 {user_status}')

            # Тариф (если есть)
            if tariff_name:
                message_lines.append(f'🏷️ Тариф: <b>{tariff_name}</b> (ID {getattr(subscription, "tariff_id", "—")})')

            # Блок ЧТО ИЗМЕНИЛОСЬ (DIFF)
            resolved_old_state = (
                old_state if old_state is not None else getattr(subscription, '_previous_state', None)
            )
            diff_lines = await self._build_subscription_diff_block(
                db, subscription, resolved_old_state, period_days=period_days, current_tariff_name=tariff_name
            )
            if diff_lines:
                message_lines.append('')
                message_lines.extend(diff_lines)

            # Блок REMNAWAVE СИНХРОНИЗАЦИЯ
            rw_info = await self._verify_remnawave_state(subscription)
            message_lines.append('')
            message_lines.extend(self._format_remnawave_verification(subscription, rw_info))

            message_lines.extend(
                [
                    '',
                    f'🌐 Серверы: {servers_info}',
                    f'💵 <b>{settings.format_price(total_amount)}</b> • {payment_method}',
                ]
            )

            # Баланс после покупки
            message_lines.append(f'💰 Баланс: {settings.format_price(user.balance_kopeks)}')

            # Реферер (только если есть)
            if user.referred_by_id:
                referrer_info = await self._get_referrer_info(db, user.referred_by_id)
                if referrer_info != 'Нет':
                    message_lines.append(f'🔗 Реф: {referrer_info}')

            # ID транзакции (только если есть)
            if transaction:
                message_lines.append(f'🆔 #{transaction.id}')

            message_lines.extend(
                [
                    '',
                    f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M")}</i>',
                ]
            )

            # Маршрутизация по категориям (зеркалит логику заголовков выше)
            if purchase_type == 'renewal' or (
                not was_trial_conversion and purchase_type is None and user.has_had_paid_subscription
            ):
                cat = NotificationCategory.RENEWALS
            else:
                cat = NotificationCategory.PURCHASES

            keyboard = self._build_admin_audit_keyboard(user, subscription)
            return await self._send_message('\n'.join(message_lines), reply_markup=keyboard, category=cat)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о покупке', error=e)
            return False

    async def send_version_update_notification(self, current_version: str, latest_version, total_updates: int) -> bool:
        """Отправляет уведомление о новых обновлениях."""
        if not self._is_enabled():
            return False

        try:
            from app.utils.markdown_to_telegram import github_markdown_to_telegram_html, truncate_for_blockquote

            repo = getattr(settings, 'VERSION_CHECK_REPO', 'fr1ngg/remnawave-bedolaga-telegram-bot')
            release_url = f'https://github.com/{repo}/releases/tag/{latest_version.tag_name}'
            repo_url = f'https://github.com/{repo}'
            timestamp = format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')

            if latest_version.prerelease:
                header = '🧪 <b>Pre-release</b>'
            elif latest_version.is_dev:
                header = '🔧 <b>Dev build</b>'
            else:
                header = '🆕 <b>Доступно обновление</b>'

            # -- message prefix (everything before blockquote) --
            prefix_lines = [
                header,
                '',
                f'<code>{current_version}</code>  →  <b><a href="{release_url}">{latest_version.tag_name}</a></b>',
                f'📅 {latest_version.formatted_date}',
                '',
            ]
            message_prefix = '\n'.join(prefix_lines)

            # -- message suffix (everything after blockquote) --
            suffix_lines = ['']
            if total_updates > 1:
                suffix_lines.append(f'Доступно обновлений: <b>{total_updates}</b>')
            suffix_lines.extend(
                [
                    f'<a href="{repo_url}">Репозиторий</a>',
                    '',
                    f'<i>{timestamp}</i>',
                ]
            )
            message_suffix = '\n'.join(suffix_lines)

            # -- description in blockquote --
            raw_description = getattr(latest_version, 'full_description', '') or latest_version.short_description
            description_html = github_markdown_to_telegram_html(raw_description)

            if description_html:
                description_html = truncate_for_blockquote(
                    description_html,
                    message_prefix=message_prefix,
                    message_suffix=message_suffix,
                )
                message = f'{message_prefix}<blockquote expandable>{description_html}</blockquote>{message_suffix}'
            else:
                message = f'{message_prefix}{message_suffix}'

            return await self._send_message(message, category=NotificationCategory.INFRASTRUCTURE)

        except Exception as e:
            logger.error('Ошибка отправки уведомления об обновлении', error=e)
            return False

    async def send_version_check_error_notification(self, error_message: str, current_version: str) -> bool:
        if not self._is_enabled():
            return False

        try:
            message = f"""⚠️ <b>ОШИБКА ПРОВЕРКИ ОБНОВЛЕНИЙ</b>

    📦 <b>Текущая версия:</b> <code>{current_version}</code>
    ❌ <b>Ошибка:</b> {error_message}

    🔄 Следующая попытка через час.
    ⚙️ Проверьте доступность GitHub API и настройки сети.

    ⚙️ <i>Система автоматических обновлений • {format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')}</i>"""

            return await self._send_message(message, category=NotificationCategory.ERRORS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления об ошибке проверки версий', error=e)
            return False

    def _build_balance_topup_message(
        self,
        user: User,
        transaction: Transaction,
        old_balance: int,
        *,
        topup_status: str,
        referrer_info: str,
        subscription: Subscription | None,
        promo_group: PromoGroup | None,
    ) -> str:
        payment_method = self._get_payment_method_display(transaction.payment_method)
        balance_change = user.balance_kopeks - old_balance
        subscription_status = self._get_subscription_status(subscription)
        timestamp = format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')
        user_display = self._get_user_display(user)
        user_id_display = self._get_user_identifier_display(user)

        # --- Основной блок ---
        message_lines: list[str] = [
            '💰 <b>ПОПОЛНЕНИЕ БАЛАНСА</b>',
            '',
            f'👤 {user_display} ({user_id_display})',
        ]

        username = getattr(user, 'username', None)
        if username:
            message_lines.append(f'📱 {format_username_link(username)}')

        message_lines.append(f'💳 {topup_status}')

        # Промогруппа -- только название
        if promo_group:
            message_lines.append(f'🏷️ Промогруппа: {html.escape(promo_group.name)}')

        message_lines.append('')

        # --- Детали пополнения ---
        message_lines.extend(
            [
                f'💵 <b>{settings.format_price(transaction.amount_kopeks)}</b> | {payment_method}',
                '',
                (
                    f'📉 {settings.format_price(old_balance)} → 📈 {settings.format_price(user.balance_kopeks)}'
                    f' (<b>+{settings.format_price(balance_change)}</b>)'
                ),
            ]
        )

        # --- Подписка ---
        message_lines.append(f'📱 Подписка: {subscription_status}')

        # --- Реферер (только если есть) ---
        if referrer_info and referrer_info != 'Нет':
            message_lines.append(f'🔗 Реферер: {referrer_info}')

        # --- Expandable blockquote с техническими деталями ---
        detail_lines: list[str] = [
            f'ID транзакции: {transaction.id}',
            f'Способ оплаты: {transaction.payment_method or "balance"}',
        ]

        if transaction.external_id:
            detail_lines.append(f'Внешний ID: {transaction.external_id}')

        if transaction.description:
            desc = transaction.description
            if len(desc) > 120:
                desc = desc[:117] + '...'
            detail_lines.append(f'Описание: {html.escape(desc)}')

        if transaction.created_at:
            detail_lines.append(f'Создана: {format_local_datetime(transaction.created_at, "%d.%m.%Y %H:%M:%S")}')

        if transaction.completed_at:
            detail_lines.append(f'Завершена: {format_local_datetime(transaction.completed_at, "%d.%m.%Y %H:%M:%S")}')

        if transaction.receipt_uuid:
            detail_lines.append(f'Чек UUID: {transaction.receipt_uuid}')

        blockquote_body = '\n'.join(detail_lines)
        message_lines.extend(
            [
                '',
                f'<blockquote expandable>{blockquote_body}</blockquote>',
            ]
        )

        message_lines.append(f'<i>{timestamp}</i>')

        return '\n'.join(message_lines)

    async def _reload_topup_notification_entities(
        self,
        db: AsyncSession,
        user: User,
        transaction: Transaction,
    ) -> tuple[User, Transaction, Subscription | None, PromoGroup | None]:
        refreshed_user = await get_user_by_id(db, user.id)
        if not refreshed_user:
            raise ValueError(f'Не удалось повторно загрузить пользователя {user.id} для уведомления о пополнении')

        refreshed_transaction = await get_transaction_by_id(db, transaction.id)
        if not refreshed_transaction:
            raise ValueError(f'Не удалось повторно загрузить транзакцию {transaction.id} для уведомления о пополнении')

        subscription = getattr(refreshed_user, 'subscription', None)
        promo_group = await self._get_user_promo_group(db, refreshed_user)

        return refreshed_user, refreshed_transaction, subscription, promo_group

    def _is_lazy_loading_error(self, error: Exception) -> bool:
        message = str(error).lower()
        return (
            isinstance(error, MissingGreenlet)
            or 'greenlet_spawn' in message
            or 'await_only' in message
            or 'missinggreenlet' in message
        )

    async def send_balance_topup_notification(
        self,
        user: User,
        transaction: Transaction,
        old_balance: int,
        *,
        topup_status: str,
        referrer_info: str,
        subscription: Subscription | None,
        promo_group: PromoGroup | None,
        db: AsyncSession | None = None,
        stars_payload: str | None = None,
    ) -> bool:
        logger.info('Начинаем отправку уведомления о пополнении баланса')

        if db:
            try:
                await self._record_subscription_event(
                    db,
                    event_type='balance_topup',
                    user=user,
                    subscription=subscription,
                    transaction=transaction,
                    amount_kopeks=transaction.amount_kopeks,
                    message='Balance top-up',
                    occurred_at=transaction.completed_at or transaction.created_at,
                    extra={
                        'status': topup_status,
                        'balance_before': old_balance,
                        'balance_after': user.balance_kopeks,
                        'referrer_info': referrer_info,
                        'promo_group_id': getattr(promo_group, 'id', None),
                        'promo_group_name': getattr(promo_group, 'name', None),
                    },
                )
            except Exception:
                logger.error(
                    'Не удалось сохранить событие пополнения баланса пользователя',
                    getattr=getattr(user, 'id', 'unknown'),
                    exc_info=True,
                )

        if not self._is_enabled():
            return False

        if transaction.payment_method not in {'manual', 'balance', None}:
            from app.services.invoice_notifications import invoice_message, paid_invoice_details

            try:
                invoice_id, source, purpose = await paid_invoice_details(db, transaction, stars_payload=stars_payload)
                message = invoice_message(
                    user,
                    event='✅ Счёт оплачен',
                    amount_kopeks=transaction.amount_kopeks,
                    method=transaction.payment_method,
                    invoice_id=invoice_id,
                    source=source,
                    purpose=purpose,
                    method_title=self._get_payment_method_display(transaction.payment_method),
                    old_balance=old_balance,
                    transaction_id=transaction.id,
                )
                return await self._send_message(
                    message, reply_markup=self._build_admin_audit_keyboard(user, subscription),
                    category=NotificationCategory.BALANCE,
                )
            except Exception:
                logger.warning('Compact payment notification failed; using existing message', exc_info=True)

        try:
            logger.info('Пытаемся создать сообщение уведомления')
            message = self._build_balance_topup_message(
                user,
                transaction,
                old_balance,
                topup_status=topup_status,
                referrer_info=referrer_info,
                subscription=subscription,
                promo_group=promo_group,
            )
            logger.info('Сообщение уведомления создано успешно')
        except Exception as error:
            logger.info(
                'Перехвачена ошибка при создании сообщения уведомления', __name__=type(error).__name__, error=error
            )
            if not self._is_lazy_loading_error(error):
                logger.error('Ошибка подготовки уведомления о пополнении', error=error, exc_info=True)
                return False

            if db is None:
                logger.error(
                    'Недостаточно данных для уведомления о пополнении и отсутствует доступ к БД',
                    error=error,
                    exc_info=True,
                )
                return False

            logger.warning(
                'Повторная загрузка данных для уведомления о пополнении после ошибки ленивой загрузки', error=error
            )

            try:
                logger.info('Пытаемся перезагрузить данные для уведомления')
                (
                    user,
                    transaction,
                    subscription,
                    promo_group,
                ) = await self._reload_topup_notification_entities(db, user, transaction)
                logger.info('Данные успешно перезагружены')
            except Exception as reload_error:
                logger.error(
                    'Ошибка повторной загрузки данных для уведомления о пополнении',
                    reload_error=reload_error,
                    exc_info=True,
                )
                return False

            try:
                logger.info('Пытаемся создать сообщение после перезагрузки данных')
                message = self._build_balance_topup_message(
                    user,
                    transaction,
                    old_balance,
                    topup_status=topup_status,
                    referrer_info=referrer_info,
                    subscription=subscription,
                    promo_group=promo_group,
                )
                logger.info('Сообщение успешно создано после перезагрузки данных')
            except Exception as rebuild_error:
                logger.error(
                    'Ошибка повторной подготовки уведомления о пополнении после повторной загрузки',
                    rebuild_error=rebuild_error,
                    exc_info=True,
                )
                return False

        try:
            return await self._send_message(message, category=NotificationCategory.BALANCE)
        except Exception as e:
            logger.error('Ошибка отправки уведомления о пополнении', error=e, exc_info=True)
            return False

    async def send_subscription_extension_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        transaction: Transaction,
        extended_days: int,
        old_end_date: datetime,
        *,
        new_end_date: datetime | None = None,
        balance_after: int | None = None,
    ) -> bool:
        try:
            current_end_date = new_end_date or subscription.end_date
            current_balance = balance_after if balance_after is not None else user.balance_kopeks

            await self._record_subscription_event(
                db,
                event_type='renewal',
                user=user,
                subscription=subscription,
                transaction=transaction,
                amount_kopeks=abs(transaction.amount_kopeks),
                message='Subscription renewed',
                occurred_at=transaction.completed_at or transaction.created_at,
                extra={
                    'extended_days': extended_days,
                    'previous_end_date': old_end_date.isoformat(),
                    'new_end_date': current_end_date.isoformat(),
                    'payment_method': transaction.payment_method,
                    'balance_after': current_balance,
                },
            )

            if not self._is_enabled():
                return False

            payment_method = self._get_payment_method_display(transaction.payment_method)
            servers_info = await self._get_servers_info(subscription.connected_squads)
            promo_group = await self._get_user_promo_group(db, user)
            promo_block = self._format_promo_group_block(promo_group)
            user_display = self._get_user_display(user)
            user_id_label = self._get_user_identifier_label(user)
            user_id_display = self._get_user_identifier_display(user)

            message = f"""⏰ <b>ПРОДЛЕНИЕ ПОДПИСКИ</b>

👤 <b>Пользователь:</b> {user_display}
🆔 <b>{user_id_label}:</b> {user_id_display}
📱 <b>Username:</b> {format_username_link(getattr(user, 'username', None), 'отсутствует')}

{promo_block}

💰 <b>Платеж:</b>
💵 Сумма: {settings.format_price(abs(transaction.amount_kopeks))}
💳 Способ: {payment_method}
🆔 ID транзакции: {transaction.id}

📅 <b>Продление:</b>
➕ Добавлено дней: {extended_days}
📆 Было до: {format_local_datetime(old_end_date, '%d.%m.%Y %H:%M')}
📆 Стало до: {format_local_datetime(current_end_date, '%d.%m.%Y %H:%M')}

📱 <b>Текущие параметры:</b>
📊 Трафик: {self._format_traffic(subscription.traffic_limit_gb)}
📱 Устройства: {subscription.device_limit}
🌐 Серверы: {servers_info}

💰 <b>Баланс после операции:</b> {settings.format_price(current_balance)}

⏰ <i>{format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')}</i>"""

            return await self._send_message(message, category=NotificationCategory.RENEWALS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о продлении', error=e)
            return False

    async def send_promocode_activation_notification(
        self,
        db: AsyncSession,
        user: User,
        promocode_data: dict[str, Any],
        effect_description: str,
        balance_before_kopeks: int | None = None,
        balance_after_kopeks: int | None = None,
    ) -> bool:
        try:
            await self._record_subscription_event(
                db,
                event_type='promocode_activation',
                user=user,
                subscription=None,
                transaction=None,
                amount_kopeks=promocode_data.get('balance_bonus_kopeks'),
                message='Promocode activation',
                occurred_at=datetime.now(UTC),
                extra={
                    'code': promocode_data.get('code'),
                    'type': promocode_data.get('type'),
                    'subscription_days': promocode_data.get('subscription_days'),
                    'balance_bonus_kopeks': promocode_data.get('balance_bonus_kopeks'),
                    'description': effect_description,
                    'valid_until': (
                        promocode_data.get('valid_until').isoformat()
                        if isinstance(promocode_data.get('valid_until'), datetime)
                        else promocode_data.get('valid_until')
                    ),
                    'balance_before_kopeks': balance_before_kopeks,
                    'balance_after_kopeks': balance_after_kopeks,
                },
            )
        except Exception:
            logger.error(
                'Не удалось сохранить событие активации промокода пользователя',
                getattr=getattr(user, 'id', 'unknown'),
                exc_info=True,
            )

        if not self._is_enabled():
            return False

        try:
            promo_group = await self._get_user_promo_group(db, user)
            promo_block = self._format_promo_group_block(promo_group)
            type_display = self._get_promocode_type_display(promocode_data.get('type'))
            usage_info = f'{promocode_data.get("current_uses", 0)}/{promocode_data.get("max_uses", 0)}'
            user_display = self._get_user_display(user)
            user_id_label = self._get_user_identifier_label(user)
            user_id_display = self._get_user_identifier_display(user)

            message_lines = [
                '🎫 <b>АКТИВАЦИЯ ПРОМОКОДА</b>',
                '',
                f'👤 <b>Пользователь:</b> {user_display}',
                f'🆔 <b>{user_id_label}:</b> {user_id_display}',
                f'📱 <b>Username:</b> {format_username_link(getattr(user, "username", None), "отсутствует")}',
                '',
                promo_block,
                '',
                '🎟️ <b>Промокод:</b>',
                f'🔖 Код: <code>{promocode_data.get("code")}</code>',
                f'🧾 Тип: {type_display}',
                f'📊 Использования: {usage_info}',
            ]

            promo_type = promocode_data.get('type')
            balance_bonus = promocode_data.get('balance_bonus_kopeks', 0)
            subscription_days = promocode_data.get('subscription_days', 0)

            if promo_type == PromoCodeType.DISCOUNT.value:
                message_lines.append(f'💸 Скидка: {balance_bonus}%')
                if subscription_days:
                    message_lines.append(f'⏳ Срок действия скидки: {subscription_days} ч.')
                else:
                    message_lines.append('⏳ Срок действия скидки: до первой покупки')
            else:
                if balance_bonus:
                    message_lines.append(f'💰 Бонус на баланс: {settings.format_price(balance_bonus)}')
                if subscription_days:
                    message_lines.append(f'📅 Доп. дни подписки: {subscription_days}')

            valid_until = promocode_data.get('valid_until')
            if valid_until:
                message_lines.append(
                    f'⏳ Действует до: {format_local_datetime(valid_until, "%d.%m.%Y %H:%M")}'
                    if isinstance(valid_until, datetime)
                    else f'⏳ Действует до: {valid_until}'
                )

            message_lines.extend(
                [
                    '',
                    '💼 <b>Баланс:</b>',
                    (
                        f'{settings.format_price(balance_before_kopeks)} → {settings.format_price(balance_after_kopeks)}'
                        if balance_before_kopeks is not None and balance_after_kopeks is not None
                        else 'ℹ️ Баланс не изменился'
                    ),
                    '',
                    '📝 <b>Эффект:</b>',
                    effect_description.strip() or '✅ Промокод активирован',
                    '',
                    f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PROMO)

        except Exception as e:
            logger.error('Ошибка отправки уведомления об активации промокода', error=e)
            return False

    async def send_campaign_link_visit_notification(
        self,
        db: AsyncSession,
        telegram_user: types.User,
        campaign: AdvertisingCampaign,
        user: User | None = None,
    ) -> bool:
        # Дедуп: если юзер уже зарегистрирован в этой кампании
        # (AdvertisingCampaignRegistration.UniqueConstraint(campaign_id, user_id))
        # — повторный /start не должен слать новое уведомление в админ-чат, иначе
        # кол-во сообщений в чате превышает реальное число регистраций в БД и
        # вводит админа в заблуждение. Для новых юзеров (user is None) уведомление
        # уходит как раньше — это первичный переход.
        if user:
            existing_registration = await db.execute(
                select(AdvertisingCampaignRegistration.id).where(
                    AdvertisingCampaignRegistration.campaign_id == campaign.id,
                    AdvertisingCampaignRegistration.user_id == user.id,
                )
            )
            if existing_registration.scalar_one_or_none() is not None:
                logger.debug(
                    'Skip campaign visit notification: user already registered in campaign',
                    user_id=user.id,
                    campaign_id=campaign.id,
                )
                return False

            try:
                await self._record_subscription_event(
                    db,
                    event_type='referral_link_visit',
                    user=user,
                    subscription=None,
                    transaction=None,
                    amount_kopeks=None,
                    message='Referral link visit',
                    occurred_at=datetime.now(UTC),
                    extra={
                        'campaign_id': campaign.id,
                        'campaign_name': campaign.name,
                        'start_parameter': campaign.start_parameter,
                        'was_registered': bool(user),
                    },
                )
            except Exception:
                logger.error(
                    'Не удалось сохранить событие перехода по кампании для пользователя',
                    getattr=getattr(user, 'id', 'unknown'),
                    exc_info=True,
                )

        if not self._is_enabled():
            return False

        try:
            full_name = telegram_user.full_name or telegram_user.username or str(telegram_user.id)
            user_status = '🆕 Новый' if not user else '👥 Существующий'

            message_lines = [
                '📣 <b>ПЕРЕХОД ПО РК</b>',
                '',
                f'🧾 {html.escape(campaign.name)} (<code>{html.escape(campaign.start_parameter)}</code>)',
                '',
                f'👤 {html.escape(full_name)} (<code>{telegram_user.id}</code>)',
            ]

            if telegram_user.username:
                message_lines.append(f'📱 {format_username_link(telegram_user.username)}')

            message_lines.append(f'📋 {user_status}')

            # Промогруппа — только если есть
            if user:
                promo_group = await self._get_user_promo_group(db, user)
                if promo_group:
                    message_lines.append(f'🏷️ Промогруппа: {html.escape(promo_group.name)}')

            message_lines.append('')

            # Загружаем название тарифа для tariff-бонуса
            tariff_name = None
            if campaign.is_tariff_bonus and campaign.tariff_id:
                try:
                    from app.database.crud.tariff import get_tariff_by_id

                    tariff = await get_tariff_by_id(db, campaign.tariff_id)
                    if tariff:
                        tariff_name = html.escape(tariff.name)
                except Exception:
                    pass

            # Бонус кампании
            bonus_lines = self._format_campaign_bonus(campaign, tariff_name=tariff_name)
            message_lines.extend(bonus_lines)

            message_lines.extend(
                [
                    '',
                    f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PROMO)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о переходе по кампании', error=e)
            return False

    async def send_campaign_registration_notification(
        self,
        db: AsyncSession,
        telegram_user_id: int,
        telegram_user_name: str,
        telegram_username: str | None,
        campaign: AdvertisingCampaign,
        user: User,
        *,
        bonus_type: str,
        balance_kopeks: int = 0,
        subscription_days: int | None = None,
        subscription_traffic_gb: int | None = None,
        subscription_device_limit: int | None = None,
        tariff_name: str | None = None,
    ) -> bool:
        """Уведомление о СОВЕРШЁННОЙ регистрации по рекламной кампании.

        Шлётся ровно один раз на каждую новую запись в advertising_campaign_registrations
        (caller передаёт is_new_registration=True). Это даёт паритет: число сообщений
        в админ-чате равно числу регистраций в кабинете.
        """
        if not self._is_enabled():
            return False

        try:
            await self._record_subscription_event(
                db,
                event_type='campaign_registration',
                user=user,
                subscription=None,
                transaction=None,
                amount_kopeks=balance_kopeks or None,
                message='Campaign registration completed',
                occurred_at=datetime.now(UTC),
                extra={
                    'campaign_id': campaign.id,
                    'campaign_name': campaign.name,
                    'start_parameter': campaign.start_parameter,
                    'bonus_type': bonus_type,
                },
            )
        except Exception:
            logger.error(
                'Не удалось сохранить событие регистрации по кампании',
                user_id=user.id,
                campaign_id=campaign.id,
                exc_info=True,
            )

        try:
            message_lines = [
                '✅ <b>РЕГИСТРАЦИЯ ПО РК</b>',
                '',
                f'🧾 {html.escape(campaign.name)} (<code>{html.escape(campaign.start_parameter)}</code>)',
                '',
                f'👤 {html.escape(telegram_user_name)} (<code>{telegram_user_id}</code>)',
            ]
            if telegram_username:
                message_lines.append(f'📱 {format_username_link(telegram_username)}')

            promo_group = await self._get_user_promo_group(db, user)
            if promo_group:
                message_lines.append(f'🏷️ Промогруппа: {html.escape(promo_group.name)}')

            message_lines.append('')

            bonus_lines = self._format_campaign_bonus(campaign, tariff_name=tariff_name)
            message_lines.extend(bonus_lines)

            message_lines.extend(
                [
                    '',
                    f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PROMO)

        except Exception as e:
            logger.error(
                'Ошибка отправки уведомления о регистрации по кампании',
                error=str(e),
                user_id=user.id,
                campaign_id=campaign.id,
                exc_info=True,
            )
            return False

    async def send_user_promo_group_change_notification(
        self,
        db: AsyncSession,
        user: User,
        old_group: PromoGroup | None,
        new_group: PromoGroup,
        *,
        reason: str | None = None,
        initiator: User | None = None,
        automatic: bool = False,
    ) -> bool:
        try:
            await self._record_subscription_event(
                db,
                event_type='promo_group_change',
                user=user,
                subscription=None,
                transaction=None,
                message='Promo group change',
                occurred_at=datetime.now(UTC),
                extra={
                    'old_group_id': getattr(old_group, 'id', None),
                    'old_group_name': getattr(old_group, 'name', None),
                    'new_group_id': new_group.id,
                    'new_group_name': new_group.name,
                    'reason': reason,
                    'initiator_id': getattr(initiator, 'id', None),
                    'initiator_telegram_id': getattr(initiator, 'telegram_id', None),
                    'automatic': automatic,
                },
            )
        except Exception:
            logger.error(
                'Не удалось сохранить событие смены промогруппы пользователя',
                getattr=getattr(user, 'id', 'unknown'),
                exc_info=True,
            )

        if not self._is_enabled():
            return False

        try:
            title = '🤖 АВТОМАТИЧЕСКАЯ СМЕНА ПРОМОГРУППЫ' if automatic else '👥 СМЕНА ПРОМОГРУППЫ'
            initiator_line = None
            if initiator:
                initiator_line = (
                    f'👮 <b>Инициатор:</b> {html.escape(initiator.full_name)} (ID: {initiator.telegram_id})'
                )
            elif automatic:
                initiator_line = '🤖 Автоматическое назначение'
            user_display = self._get_user_display(user)
            user_id_label = self._get_user_identifier_label(user)
            user_id_display = self._get_user_identifier_display(user)

            message_lines = [
                f'{title}',
                '',
                f'👤 <b>Пользователь:</b> {user_display}',
                f'🆔 <b>{user_id_label}:</b> {user_id_display}',
                f'📱 <b>Username:</b> {format_username_link(getattr(user, "username", None), "отсутствует")}',
                '',
                self._format_promo_group_block(new_group, title='Новая промогруппа', icon='🏆'),
            ]

            if old_group and old_group.id != new_group.id:
                message_lines.extend(
                    [
                        '',
                        self._format_promo_group_block(old_group, title='Предыдущая промогруппа', icon='♻️'),
                    ]
                )

            if initiator_line:
                message_lines.extend(['', initiator_line])

            if reason:
                message_lines.extend(['', f'📝 Причина: {reason}'])

            message_lines.extend(
                [
                    '',
                    f'💰 Баланс пользователя: {settings.format_price(user.balance_kopeks)}',
                    f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PROMO)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о смене промогруппы', error=e)
            return False

    def _resolve_topic_id(self, category: NotificationCategory | None = None) -> int | None:
        """Определяет topic_id для сообщения.

        Если указана category и для неё настроен топик — возвращает его.
        Иначе — fallback на self.topic_id (общий топик).
        """
        if category:
            topic = self.category_topics.get(category)
            if topic is not None:
                return topic
        return self.topic_id

    def resolve_recipient_role(self) -> str:
        """Определяет роль получателя уведомления по chat_id.

        В личном чате chat_id совпадает с telegram_id получателя, что позволяет
        проверить права до отправки без I/O (данные читаются из памяти).

        Returns:
            'admin'     — полный набор кнопок;
            'moderator' — набор без «👤 К пользователю» (@admin_required);
            'group'     — групповой/супергруппа/канал админ-чат: только надёжные
                          (не-FSM) кнопки, т.к. конкретного получателя не определить
                          и FSM-ввод в общем чате не работает (privacy mode бота);
            'none'      — без кнопок (посторонний в личке, либо
                          ADMIN_NOTIFICATIONS_CHAT_ID задан строкой @username).
        """
        try:
            chat_id = int(self.chat_id)
        except (TypeError, ValueError):
            return 'none'  # строка @username или None — тип чата не определить
        if chat_id < 0:
            # супергруппа / канал / старая группа — доверенный админ-чат оператора,
            # но конкретного получателя не определить → только надёжные кнопки.
            return 'group'
        if chat_id == 0:
            return 'none'  # невалидный chat_id
        if settings.is_admin(chat_id):
            return 'admin'

        from app.services.support_settings_service import SupportSettingsService

        if SupportSettingsService.is_moderator(chat_id):
            return 'moderator'

        return 'none'  # личка постороннего — не показываем кнопки

    async def _send_message(
        self,
        text: str,
        reply_markup: types.InlineKeyboardMarkup | None = None,
        *,
        category: NotificationCategory | None = None,
        thread_id: int | None = None,
    ) -> bool:
        if not self._is_enabled():
            return False

        # Per-category suppression
        if category and not self.category_enabled.get(category, True):
            logger.debug('Уведомление подавлено (категория отключена)', category=category.value)
            return False

        # Явный thread_id (например, топик заявок на вывод) важнее топика категории
        if thread_id is None:
            thread_id = self._resolve_topic_id(category)

        # В групповом админ-чате работают только разрешённые callback-кнопки
        # (фильтр чатов глушит остальные): такие выкидываем здесь, а не рисуем
        # мёртвыми. URL-кнопки и разрешённые действия остаются.
        if reply_markup is not None and self.resolve_recipient_role() == 'group':
            reply_markup, dropped = strip_group_unsafe_buttons(reply_markup)
            if dropped:
                logger.warning(
                    'Кнопки не работают в групповом админ-чате — убраны из уведомления',
                    chat_id=self.chat_id,
                    dropped=dropped,
                )

        # Rich-вид (Bot API 10.1): заголовок, разделители, footer с tg-time.
        # При недоступности/ошибке молча продолжаем классическим путём ниже
        # (там ретраи и обработка flood control).
        try:
            rich_html = classic_admin_html_to_rich(text)
            if await try_send_rich_admin_message(
                self.bot, self.chat_id, rich_html, thread_id=thread_id, reply_markup=reply_markup
            ):
                logger.info('Rich-уведомление отправлено в чат', chat_id=self.chat_id, category=category)
                return True
        except Exception as rich_error:
            logger.warning('Сбой rich-рендера админ-уведомления', error=str(rich_error))

        message_kwargs: dict[str, Any] = {
            'chat_id': self.chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }
        if thread_id:
            message_kwargs['message_thread_id'] = thread_id
        if reply_markup is not None:
            message_kwargs['reply_markup'] = reply_markup

        # ВАЖНО: вся ветка ошибок ниже логируется через logger.warning, а не
        # logger.error. Иначе TelegramNotifierProcessor попытается переслать
        # ошибку в этот же админ-чат, упрётся в тот же flood control — петля
        # усиления (баг с node.connection_lost/restored, 7-8 webhook'ов подряд).
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                await self.bot.send_message(**message_kwargs)
                logger.info('Уведомление отправлено в чат', chat_id=self.chat_id, category=category)
                return True

            except TelegramForbiddenError:
                logger.warning('Бот не имеет прав для отправки в чат', chat_id=self.chat_id)
                return False

            except TelegramBadRequest as e:
                logger.warning(
                    'Ошибка отправки уведомления в админ-чат',
                    error=_redact_telegram_secrets(str(e))[:200],
                )
                return False

            except TelegramRetryAfter as e:
                # Flood control: ждём столько, сколько сказал Telegram (cap 30s),
                # потом ретраим. До фикса исключение проваливалось в bare
                # except → logger.error → петля через TelegramNotifierProcessor.
                requested_retry_after = max(1, int(getattr(e, 'retry_after', 1)))
                retry_after = min(requested_retry_after, 30)
                log_kwargs: dict[str, Any] = {
                    'chat_id': self.chat_id,
                    'retry_after': retry_after,
                    'attempt': attempt,
                }
                if requested_retry_after > retry_after:
                    # Telegram реально просит дольше cap'а — видимый сигнал,
                    # что бот аккаунт перегружен сильнее обычного flood-control'а.
                    log_kwargs['retry_after_requested'] = requested_retry_after
                    log_kwargs['clamped'] = True
                logger.warning('Telegram flood control при отправке в админ-чат', **log_kwargs)
                if attempt < max_attempts:
                    await asyncio.sleep(retry_after)
                    continue
                return False

            except (TelegramNetworkError, TelegramServerError) as e:
                # Транзиентные сетевые/5xx — warning, не error.
                logger.warning(
                    'Транзиентная сетевая ошибка отправки в админ-чат',
                    chat_id=self.chat_id,
                    error=_redact_telegram_secrets(str(e))[:200],
                    error_type=type(e).__name__,
                    attempt=attempt,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(min(2 ** (attempt - 1), 4))
                    continue
                return False

            except Exception as e:
                logger.warning(
                    'Неожиданная ошибка при отправке в админ-чат',
                    chat_id=self.chat_id,
                    error=_redact_telegram_secrets(str(e))[:200],
                    error_type=type(e).__name__,
                )
                return False

        return False

    def _is_enabled(self) -> bool:
        return self.enabled and bool(self.chat_id)

    @property
    def is_enabled(self) -> bool:
        """Public check for whether admin notifications are configured and active."""
        return self._is_enabled()

    async def send_admin_notification(
        self,
        text: str,
        reply_markup: types.InlineKeyboardMarkup | None = None,
        *,
        category: NotificationCategory | None = None,
    ) -> bool:
        """Send a generic notification to admin chat with optional inline keyboard."""
        if not self._is_enabled():
            return False
        return await self._send_message(text, reply_markup=reply_markup, category=category)

    async def send_guest_purchase_notification(
        self,
        purchase: GuestPurchase,
        tariff_name: str,
        *,
        is_pending_activation: bool = False,
    ) -> bool:
        """Send admin notification for a guest/gift purchase (landing or cabinet)."""
        if not self._is_enabled():
            return False

        try:
            is_cabinet = purchase.source == 'cabinet'

            # Event title
            if is_cabinet and purchase.is_gift:
                event_title = '🎁 ПОДАРОК ИЗ КАБИНЕТА'
            elif is_pending_activation:
                event_title = '⏳ ПОКУПКА С ЛЕНДИНГА (ожидает активации)'
            elif purchase.is_gift:
                event_title = '🎁 ПОКУПКА В ПОДАРОК С ЛЕНДИНГА'
            else:
                event_title = '🛒 ПОКУПКА С ЛЕНДИНГА'

            # Contact info
            contact_display = html.escape(purchase.contact_value or '—')
            contact_icon = '📧' if purchase.contact_type == 'email' else '📱'

            payment_method = self._get_payment_method_display(purchase.payment_method)

            message_lines = [
                f'<b>{event_title}</b>',
                '',
            ]

            if is_cabinet:
                # Cabinet gift: show buyer with link to user profile
                buyer = getattr(purchase, 'buyer', None)
                if buyer:
                    if buyer.username:
                        buyer_display = format_username_link(buyer.username)
                    else:
                        buyer_name = buyer.email or f'id:{buyer.id}'
                        buyer_display = f'<code>{html.escape(buyer_name)}</code>'
                    message_lines.append(f'👤 Покупатель: {buyer_display}')
                else:
                    message_lines.append(f'{contact_icon} Покупатель: <code>{contact_display}</code>')
            else:
                # Landing: show page slug and buyer contact
                landing_slug = '—'
                try:
                    landing = purchase.landing
                    if landing:
                        landing_slug = landing.slug
                    elif purchase.landing_id:
                        landing_slug = f'ID:{purchase.landing_id}'
                except Exception:
                    if purchase.landing_id:
                        landing_slug = f'ID:{purchase.landing_id}'
                message_lines.append(f'🌐 Страница: <b>/buy/{html.escape(landing_slug)}</b>')
                message_lines.append(f'{contact_icon} Покупатель: <code>{contact_display}</code>')

            if purchase.is_gift:
                if purchase.gift_recipient_value:
                    recipient_icon = '📧' if purchase.gift_recipient_type == 'email' else '📱'
                    recipient_value = html.escape(purchase.gift_recipient_value)
                    message_lines.append(f'{recipient_icon} Получатель: <code>{recipient_value}</code>')
                else:
                    message_lines.append('🔗 Получатель: <i>по коду активации</i>')
                if purchase.gift_message:
                    raw_msg = purchase.gift_message[:100]
                    suffix = '…' if len(purchase.gift_message) > 100 else ''
                    message_lines.append(f'💬 <i>{html.escape(raw_msg)}{suffix}</i>')

            # Payment details in blockquote
            payment_lines = [
                '<blockquote>',
                f'🏷️ Тариф: <b>{html.escape(tariff_name)}</b>',
                f'📅 Период: {purchase.period_days} дн.',
                f'💵 <b>{settings.format_price(purchase.amount_kopeks)}</b> • {payment_method}',
            ]

            if purchase.payment_id:
                payment_lines.append(f'🆔 {html.escape(str(purchase.payment_id))}')

            payment_lines.append('</blockquote>')
            message_lines.extend(payment_lines)

            message_lines.append(f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M")}</i>')

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PURCHASES)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о гостевой покупке', error=e)
            return False

    async def send_grace_access_notification(
        self,
        *,
        event: str,
        user: User,
        subscription: Subscription,
        tariff_name: str | None,
        reason: str,
        grace_until: datetime,
        hours: int,
        quota_gb: float,
        allowed: str,
        completion_reason: str | None = None,
        last_error: str | None = None,
    ) -> bool:
        """Выдача или завершение grace-доступа — в чат админов, категория «Продления».

        Владелец: «выдача втухлую — это тупо»: админ обязан видеть, кому, почему и до
        какого срока бот временно оставил доступ к тому, что оператор назвал в
        GRACE_ACCESS_ALLOWED_SERVICES (``allowed``, уже экранировано), и чем это
        закончилось — продлением, истечением срока или конфликтом с панелью.
        """
        try:
            user_display = self._get_user_display(user)
            user_id_label = self._get_user_identifier_label(user)
            user_id_display = self._get_user_identifier_display(user)
            username = format_username_link(getattr(user, 'username', None), 'отсутствует')
            subscription_line = f'#{subscription.id}'
            if tariff_name:
                subscription_line += f' «{html.escape(tariff_name)}»'
            reason_line = 'исчерпан трафик' if reason == 'limited' else 'срок подписки истёк'
            quota_text = f'{quota_gb:g} ГБ'
            until_text = format_local_datetime(grace_until, '%d.%m.%Y %H:%M')

            if event == 'granted':
                message = f"""🛟 <b>GRACE-ДОСТУП ВЫДАН</b>

👤 <b>Пользователь:</b> {user_display}
🆔 <b>{user_id_label}:</b> {user_id_display}
📱 <b>Username:</b> {username}

📋 <b>Подписка:</b> {subscription_line}
⚠️ <b>Почему:</b> {reason_line}
🛟 <b>Что выдано:</b> {allowed}, {quota_text} на {hours} ч.
⏳ <b>Действует до:</b> {until_text}

⏰ <i>{format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')}</i>"""
            else:
                outcomes = {
                    'paid': '✅ человек продлил подписку — вернули обычный тариф',
                    'timeout': '⌛ срок grace вышел, подписку не продлили — доступ закрыт',
                    'drained': '🚰 grace выключают (слив) — доступ закрыт',
                    'revoked': '🚫 отозван: пользователь заблокирован или подписка отключена',
                    'conflict': '⚠️ конфликт с панелью — доступ закрыт',
                }
                outcome = outcomes.get(completion_reason or '', f'завершён ({completion_reason or "?"})')
                error_line = f'\n❗ <code>{html.escape(last_error)}</code>' if last_error else ''
                message = f"""🛟 <b>GRACE-ДОСТУП ЗАВЕРШЁН</b>

👤 <b>Пользователь:</b> {user_display}
🆔 <b>{user_id_label}:</b> {user_id_display}
📱 <b>Username:</b> {username}

📋 <b>Подписка:</b> {subscription_line}
⚠️ <b>Был выдан:</b> {reason_line}, до {until_text}
🏁 <b>Итог:</b> {outcome}{error_line}

⏰ <i>{format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')}</i>"""

            return await self._send_message(message, category=NotificationCategory.RENEWALS)
        except Exception as error:
            logger.error('Ошибка отправки уведомления о grace-доступе', error=error)
            return False

    async def send_webhook_notification(self, text: str) -> bool:
        """Send a generic webhook/infrastructure notification to admin chat.

        Used by RemnaWaveWebhookService for node, service, and CRM events.
        The caller is responsible for HTML-escaping all untrusted data in `text`.
        """
        if not self._is_enabled():
            return False
        return await self._send_message(text, category=NotificationCategory.INFRASTRUCTURE)

    def _get_payment_method_display(self, payment_method: str | None) -> str:
        if not payment_method:
            return '💰 С баланса'

        method_names: dict[str, str] = {
            'telegram_stars': '⭐ Telegram Stars',
            'yookassa': '💳 YooKassa (карта)',
            'tribute': '💎 Tribute (карта)',
            'mulenpay': f'💳 {settings.get_mulenpay_display_name()} (карта)',
            'pal24': f'🏦 {settings.get_pal24_display_name()} (СБП)',
            'cryptobot': f'🪙 {settings.get_cryptobot_display_name()} (крипто)',
            'heleket': f'🪙 {settings.get_heleket_display_name()} (крипто)',
            'wata': f'💳 {settings.get_wata_display_name()}',
            'platega': f'💳 {settings.get_platega_display_name()}',
            'cloudpayments': f'💳 {settings.get_cloudpayments_display_name()}',
            'freekassa': f'💳 {settings.get_freekassa_display_name()}',
            'kassa_ai': f'💳 {settings.get_kassa_ai_display_name()}',
            'cispay': f'💳 {settings.get_cispay_display_name()}',
            'cashera': f'💳 {settings.get_cashera_display_name()}',
            'tabpay': f'💳 {settings.get_tabpay_display_name()}',
            'paritypay': f'💳 {settings.get_paritypay_display_name()}',
            'manual': '🛠️ Вручную (админ)',
            'balance': '💰 С баланса',
        }

        return method_names.get(payment_method, f'💳 {html.escape(payment_method)}')

    def _format_traffic(self, traffic_gb: float | None) -> str:
        if traffic_gb is None:
            return '—'
        if traffic_gb == 0:
            return '∞ Безлимит'
        val = int(traffic_gb) if traffic_gb == int(traffic_gb) else round(float(traffic_gb), 2)
        return f'{val} ГБ'

    def _get_subscription_status(self, subscription: Subscription | None) -> str:
        if not subscription:
            return '❌ Нет подписки'

        if subscription.is_trial:
            return f'🎯 Триал (до {format_local_datetime(subscription.end_date, "%d.%m")})'
        if subscription.is_active:
            return f'✅ Активна (до {format_local_datetime(subscription.end_date, "%d.%m")})'
        return '❌ Неактивна'

    async def _get_servers_info(self, squad_uuids: list) -> str:
        if not squad_uuids:
            return '❌ Нет серверов'

        try:
            from app.handlers.subscription.devices import get_servers_display_names

            servers_names = await get_servers_display_names(squad_uuids)
            return f'{len(squad_uuids)} шт. ({servers_names})'
        except Exception as e:
            logger.warning('Не удалось получить названия серверов', error=e)
            return f'{len(squad_uuids)} шт.'

    async def send_maintenance_status_notification(
        self, event_type: str, status: str, details: dict[str, Any] = None
    ) -> bool:
        if not self._is_enabled():
            return False

        try:
            details = details or {}

            if event_type == 'enable':
                if details.get('auto_enabled', False):
                    icon = '⚠️'
                    title = 'АВТОМАТИЧЕСКОЕ ВКЛЮЧЕНИЕ ТЕХРАБОТ'
                else:
                    icon = '🔧'
                    title = 'ВКЛЮЧЕНИЕ ТЕХРАБОТ'

            elif event_type == 'disable':
                icon = '✅'
                title = 'ОТКЛЮЧЕНИЕ ТЕХРАБОТ'

            elif event_type == 'api_status':
                if status == 'online':
                    icon = '🟢'
                    title = 'API REMNAWAVE ВОССТАНОВЛЕНО'
                else:
                    icon = '🔴'
                    title = 'API REMNAWAVE НЕДОСТУПНО'

            elif event_type == 'monitoring':
                if status == 'started':
                    icon = '🔍'
                    title = 'МОНИТОРИНГ ЗАПУЩЕН'
                else:
                    icon = '⏹️'
                    title = 'МОНИТОРИНГ ОСТАНОВЛЕН'
            else:
                icon = 'ℹ️'
                title = 'СИСТЕМА ТЕХРАБОТ'

            message_parts = [f'{icon} <b>{title}</b>', '']

            if event_type == 'enable':
                if details.get('reason'):
                    message_parts.append(f'📋 <b>Причина:</b> {details["reason"]}')

                if details.get('enabled_at'):
                    enabled_at = details['enabled_at']
                    if isinstance(enabled_at, str):
                        enabled_at = datetime.fromisoformat(enabled_at)
                    message_parts.append(
                        f'🕐 <b>Время включения:</b> {format_local_datetime(enabled_at, "%d.%m.%Y %H:%M:%S")}'
                    )

                message_parts.append(
                    f'🤖 <b>Автоматически:</b> {"Да" if details.get("auto_enabled", False) else "Нет"}'
                )
                message_parts.append('')
                message_parts.append('❗ Обычные пользователи временно не могут использовать бота.')

            elif event_type == 'disable':
                if details.get('disabled_at'):
                    disabled_at = details['disabled_at']
                    if isinstance(disabled_at, str):
                        disabled_at = datetime.fromisoformat(disabled_at)
                    message_parts.append(
                        f'🕐 <b>Время отключения:</b> {format_local_datetime(disabled_at, "%d.%m.%Y %H:%M:%S")}'
                    )

                if details.get('duration'):
                    duration = details['duration']
                    if isinstance(duration, (int, float)):
                        hours = int(duration // 3600)
                        minutes = int((duration % 3600) // 60)
                        if hours > 0:
                            duration_str = f'{hours}ч {minutes}мин'
                        else:
                            duration_str = f'{minutes}мин'
                        message_parts.append(f'⏱️ <b>Длительность:</b> {duration_str}')

                message_parts.append(
                    f'🤖 <b>Было автоматическим:</b> {"Да" if details.get("was_auto", False) else "Нет"}'
                )
                message_parts.append('')
                message_parts.append('✅ Сервис снова доступен для пользователей.')

            elif event_type == 'api_status':
                message_parts.append(f'🔗 <b>API URL:</b> {details.get("api_url", "неизвестно")}')

                if status == 'online':
                    if details.get('response_time'):
                        message_parts.append(f'⚡ <b>Время отклика:</b> {details["response_time"]} сек')

                    if details.get('consecutive_failures', 0) > 0:
                        message_parts.append(f'🔄 <b>Неудачных попыток было:</b> {details["consecutive_failures"]}')

                    message_parts.append('')
                    message_parts.append('API снова отвечает на запросы.')

                else:
                    if details.get('consecutive_failures'):
                        message_parts.append(f'🔄 <b>Попытка №:</b> {details["consecutive_failures"]}')

                    if details.get('error'):
                        error_msg = str(details['error'])[:100]
                        message_parts.append(f'❌ <b>Ошибка:</b> {error_msg}')

                    message_parts.append('')
                    message_parts.append('⚠️ Началась серия неудачных проверок API.')

            elif event_type == 'monitoring':
                if status == 'started':
                    if details.get('check_interval'):
                        message_parts.append(f'🔄 <b>Интервал проверки:</b> {details["check_interval"]} сек')

                    if details.get('auto_enable_configured') is not None:
                        auto_enable = 'Включено' if details['auto_enable_configured'] else 'Отключено'
                        message_parts.append(f'🤖 <b>Автовключение:</b> {auto_enable}')

                    if details.get('max_failures'):
                        message_parts.append(f'🎯 <b>Порог ошибок:</b> {details["max_failures"]}')

                    message_parts.append('')
                    message_parts.append('Система будет следить за доступностью API.')

                else:
                    message_parts.append('Автоматический мониторинг API остановлен.')

            message_parts.append('')
            message_parts.append(f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>')

            message = '\n'.join(message_parts)

            return await self._send_message(message, category=NotificationCategory.INFRASTRUCTURE)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о техработах', error=e)
            return False

    async def send_remnawave_panel_status_notification(self, status: str, details: dict[str, Any] = None) -> bool:
        if not self._is_enabled():
            return False

        try:
            details = details or {}

            status_config = {
                'online': {'icon': '🟢', 'title': 'ПАНЕЛЬ REMNAWAVE ДОСТУПНА', 'alert_type': 'success'},
                'offline': {'icon': '🔴', 'title': 'ПАНЕЛЬ REMNAWAVE НЕДОСТУПНА', 'alert_type': 'error'},
                'degraded': {'icon': '🟡', 'title': 'ПАНЕЛЬ REMNAWAVE РАБОТАЕТ СО СБОЯМИ', 'alert_type': 'warning'},
                'maintenance': {'icon': '🔧', 'title': 'ПАНЕЛЬ REMNAWAVE НА ОБСЛУЖИВАНИИ', 'alert_type': 'info'},
            }

            config = status_config.get(status, status_config['offline'])

            message_parts = [f'{config["icon"]} <b>{config["title"]}</b>', '']

            if details.get('api_url'):
                message_parts.append(f'🔗 <b>URL:</b> {details["api_url"]}')

            if details.get('response_time'):
                message_parts.append(f'⚡ <b>Время отклика:</b> {details["response_time"]} сек')

            if details.get('last_check'):
                last_check = details['last_check']
                if isinstance(last_check, str):
                    last_check = datetime.fromisoformat(last_check)
                message_parts.append(f'🕐 <b>Последняя проверка:</b> {format_local_datetime(last_check, "%H:%M:%S")}')

            if status == 'online':
                if details.get('uptime'):
                    message_parts.append(f'⏱️ <b>Время работы:</b> {details["uptime"]}')

                if details.get('users_online'):
                    message_parts.append(f'👥 <b>Пользователей онлайн:</b> {details["users_online"]}')

                message_parts.append('')
                message_parts.append('✅ Все системы работают нормально.')

            elif status == 'offline':
                if details.get('error'):
                    error_msg = str(details['error'])[:150]
                    message_parts.append(f'❌ <b>Ошибка:</b> {error_msg}')

                if details.get('consecutive_failures'):
                    message_parts.append(f'🔄 <b>Неудачных попыток:</b> {details["consecutive_failures"]}')

                message_parts.append('')
                message_parts.append('⚠️ Панель недоступна. Проверьте соединение и статус сервера.')

            elif status == 'degraded':
                if details.get('issues'):
                    issues = details['issues']
                    if isinstance(issues, list):
                        message_parts.append('⚠️ <b>Обнаруженные проблемы:</b>')
                        for issue in issues[:3]:
                            message_parts.append(f'   • {issue}')
                    else:
                        message_parts.append(f'⚠️ <b>Проблема:</b> {issues}')

                message_parts.append('')
                message_parts.append('Панель работает, но возможны задержки или сбои.')

            elif status == 'maintenance':
                if details.get('maintenance_reason'):
                    message_parts.append(f'🔧 <b>Причина:</b> {html.escape(details["maintenance_reason"])}')

                if details.get('estimated_duration'):
                    message_parts.append(f'⏰ <b>Ожидаемая длительность:</b> {details["estimated_duration"]}')

                message_parts.append('')
                message_parts.append('Панель временно недоступна для обслуживания.')

            message_parts.append('')
            message_parts.append(f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>')

            message = '\n'.join(message_parts)

            return await self._send_message(message, category=NotificationCategory.INFRASTRUCTURE)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о статусе панели Remnawave', error=e)
            return False

    async def send_subscription_update_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        update_type: str,
        old_value: Any,
        new_value: Any,
        price_paid: int = 0,
    ) -> bool:
        if not self._is_enabled():
            return False

        try:
            user_display = self._get_user_display(user)
            user_id_display = self._get_user_identifier_display(user)

            # Определяем заголовок по типу операции
            update_titles = {
                'traffic': '📊 ДОКУПКА ТРАФИКА',
                'devices': '📱 ДОКУПКА УСТРОЙСТВ',
                'servers': '🌐 СМЕНА СЕРВЕРОВ',
            }
            title = update_titles.get(update_type, '⚙️ ИЗМЕНЕНИЕ ПОДПИСКИ')

            # Получаем название тарифа
            tariff_name = await self._get_tariff_name(db, subscription)

            # Формируем компактное сообщение
            message_lines = [
                f'<b>{title}</b>',
                '',
                f'👤 {user_display} ({user_id_display})',
            ]

            # Добавляем username только если есть
            username = getattr(user, 'username', None)
            if username:
                message_lines.append(f'📱 {format_username_link(username)}')

            email = getattr(user, 'email', None)
            if email:
                message_lines.append(f'📧 {html.escape(email)}')

            sub_id = getattr(subscription, 'id', '—')
            user_pk = getattr(user, 'id', '—')
            message_lines.append(f'🆔 User: #{user_pk} | Подписка: #{sub_id}')

            # Тариф (если есть)
            if tariff_name:
                message_lines.append(f'🏷️ Тариф: <b>{tariff_name}</b> (ID {getattr(subscription, "tariff_id", "—")})')

            message_lines.append('')

            # Форматируем изменение в зависимости от типа
            if update_type == 'servers':
                old_servers_info = await self._format_servers_detailed(old_value)
                new_servers_info = await self._format_servers_detailed(new_value)
                message_lines.append(f'🔄 {old_servers_info} → {new_servers_info}')
            elif update_type == 'traffic':
                old_formatted = self._format_update_value(old_value, update_type)
                new_formatted = self._format_update_value(new_value, update_type)
                message_lines.append(f'🔄 {old_formatted} → {new_formatted}')
            elif update_type == 'devices':
                message_lines.append(f'🔄 {old_value} → {new_value} устр.')
            else:
                message_lines.append(f'🔄 {old_value} → {new_value}')

            # Стоимость операции
            if price_paid > 0:
                message_lines.append(f'💵 <b>{settings.format_price(price_paid)}</b>')
            else:
                message_lines.append('💸 Бесплатно')

            # Блок REMNAWAVE СИНХРОНИЗАЦИЯ
            rw_info = await self._verify_remnawave_state(subscription)
            message_lines.append('')
            message_lines.extend(self._format_remnawave_verification(subscription, rw_info))

            message_lines.extend(
                [
                    '',
                    f'📅 До {format_local_datetime(subscription.end_date, "%d.%m.%Y")}',
                    f'💰 Баланс: {settings.format_price(user.balance_kopeks)}',
                ]
            )

            # Реферер (только если есть)
            if user.referred_by_id:
                referrer_info = await self._get_referrer_info(db, user.referred_by_id)
                if referrer_info != 'Нет':
                    message_lines.append(f'🔗 Реф: {referrer_info}')

            message_lines.extend(
                [
                    '',
                    f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M")}</i>',
                ]
            )

            keyboard = self._build_admin_audit_keyboard(user, subscription)
            return await self._send_message(
                '\n'.join(message_lines), reply_markup=keyboard, category=NotificationCategory.ADDONS
            )

        except Exception as e:
            logger.error('Ошибка отправки уведомления об изменении подписки', error=e)
            return False

    async def _format_servers_detailed(self, server_uuids: list[str]) -> str:
        if not server_uuids:
            return 'Нет серверов'

        try:
            from app.handlers.subscription.devices import get_servers_display_names

            servers_names = await get_servers_display_names(server_uuids)

            if servers_names and servers_names != 'Нет серверов':
                return f'{len(server_uuids)} серверов ({servers_names})'
            return f'{len(server_uuids)} серверов'

        except Exception as e:
            logger.warning('Ошибка получения названий серверов для уведомления', error=e)
            return f'{len(server_uuids)} серверов'

    def _format_update_value(self, value: Any, update_type: str) -> str:
        if update_type == 'traffic':
            if value == 0:
                return '♾ Безлимитный'
            return f'{value} ГБ'
        if update_type == 'devices':
            return f'{value} устройств'
        if update_type == 'servers':
            if isinstance(value, list):
                return f'{len(value)} серверов'
            return str(value)
        return str(value)

    async def send_partner_application_notification(
        self,
        user: User,
        application_data: dict[str, Any],
    ) -> bool:
        """Уведомление о новой заявке на партнёрку."""
        if not self._is_enabled():
            return False

        try:
            user_display = self._get_user_display(user)
            user_id_display = self._get_user_identifier_display(user)

            message_lines = [
                '🤝 <b>ЗАЯВКА НА ПАРТНЁРКУ</b>',
                '',
                f'👤 {user_display} ({user_id_display})',
            ]

            username = getattr(user, 'username', None)
            if username:
                message_lines.append(f'📱 {format_username_link(username)}')

            message_lines.append('')

            if application_data.get('company_name'):
                message_lines.append(f'🏢 Компания: {html.escape(str(application_data["company_name"]))}')
            if application_data.get('telegram_channel'):
                message_lines.append(f'📢 Канал: {html.escape(str(application_data["telegram_channel"]))}')
            if application_data.get('website_url'):
                message_lines.append(f'🌐 Сайт: {html.escape(str(application_data["website_url"]))}')
            if application_data.get('description'):
                desc = str(application_data['description'])
                if len(desc) > 200:
                    desc = desc[:197] + '...'
                message_lines.append(f'📝 {html.escape(desc)}')
            if application_data.get('expected_monthly_referrals'):
                message_lines.append(f'👥 Ожидаемых рефералов: {application_data["expected_monthly_referrals"]}/мес')
            if application_data.get('desired_commission_percent'):
                message_lines.append(f'💰 Желаемая комиссия: {application_data["desired_commission_percent"]}%')

            message_lines.extend(
                [
                    '',
                    f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PARTNERS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о заявке на партнёрку', error=e)
            return False

    async def send_withdrawal_request_notification(
        self,
        user: User,
        amount_kopeks: int,
        payment_details: str | None = None,
        *,
        request_id: int | None = None,
    ) -> bool:
        """Уведомление о запросе на вывод средств.

        С ``request_id`` к уведомлению прикладываются кнопки «Одобрить»/«Отклонить»
        по роли получателя — как у заявки, поданной из бота.
        """
        if not self._is_enabled():
            return False

        try:
            user_display = self._get_user_display(user)
            user_id_display = self._get_user_identifier_display(user)

            message_lines = [
                '💸 <b>ЗАПРОС НА ВЫВОД СРЕДСТВ</b>',
                '',
                f'👤 {user_display} ({user_id_display})',
            ]

            username = getattr(user, 'username', None)
            if username:
                message_lines.append(f'📱 {format_username_link(username)}')

            message_lines.extend(
                [
                    '',
                    f'💵 <b>Сумма: {settings.format_price(amount_kopeks)}</b>',
                    f'💰 Баланс: {settings.format_price(user.balance_kopeks)}',
                ]
            )

            if payment_details:
                details = str(payment_details)
                if len(details) > 200:
                    details = details[:197] + '...'
                message_lines.extend(['', f'💳 Реквизиты: {html.escape(details)}'])

            message_lines.extend(
                [
                    '',
                    f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            reply_markup = None
            if request_id is not None:
                from app.keyboards.withdrawal import get_withdrawal_request_keyboard

                reply_markup = get_withdrawal_request_keyboard(
                    request_id,
                    WithdrawalRequestStatus.PENDING.value,
                    user_db_id=getattr(user, 'id', None),
                    role=self.resolve_recipient_role(),
                )

            return await self._send_message(
                '\n'.join(message_lines), reply_markup=reply_markup, category=NotificationCategory.PARTNERS
            )

        except Exception as e:
            logger.error('Ошибка отправки уведомления о запросе на вывод', error=e)
            return False

    async def send_withdrawal_pending_reminder(self, request: WithdrawalRequest, waited_minutes: int) -> bool:
        """Напоминание о заявке на вывод, которая ждёт решения дольше лимита.

        Аналог SLA-напоминания по тикетам. Уходит в топик заявок на вывод
        (REFERRAL_WITHDRAWAL_NOTIFICATIONS_TOPIC_ID), а без него — по категории
        PARTNERS, то есть туда же, куда пришло исходное уведомление о заявке.
        Кнопки — та же клавиатура, что у исходного уведомления, по роли получателя:
        решить заявку можно прямо отсюда.
        """
        if not self._is_enabled():
            return False

        try:
            from app.keyboards.withdrawal import get_withdrawal_request_keyboard

            user = getattr(request, 'user', None)
            user_display = self._get_user_display(user) if user else 'Unknown'
            user_id_display = self._get_user_identifier_display(user) if user else '—'
            username = getattr(user, 'username', None) if user else None

            message_lines = [
                '⏰ <b>Заявка на вывод ждёт решения</b>',
                '',
                f'🆔 <b>Заявка:</b> #{request.id}',
                f'👤 <b>Пользователь:</b> {user_display} ({user_id_display})',
            ]
            if username:
                message_lines.append(f'📱 <b>Username:</b> {format_username_link(username)}')
            message_lines.extend(
                [
                    f'💵 <b>Сумма:</b> {settings.format_price(request.amount_kopeks)}',
                    f'⏱️ <b>Ожидает решения:</b> {_format_waiting(waited_minutes)}',
                ]
            )

            # Та же клавиатура, что у исходного уведомления. Собранная вручную вела на
            # admin_user_<telegram_id> — у такого callback обработчика нет, — рисовала
            # профиль в групповом чате и давала модератору кнопки, которые ответят
            # «нет доступа».
            keyboard = get_withdrawal_request_keyboard(
                request.id,
                WithdrawalRequestStatus.PENDING.value,
                user_db_id=getattr(request, 'user_id', None) or getattr(user, 'id', None),
                role=self.resolve_recipient_role(),
            )

            topic_id = getattr(settings, 'REFERRAL_WITHDRAWAL_NOTIFICATIONS_TOPIC_ID', None) or None
            return await self._send_message(
                '\n'.join(message_lines),
                reply_markup=keyboard,
                category=NotificationCategory.PARTNERS,
                thread_id=topic_id,
            )
        except Exception as e:
            logger.error(
                'Ошибка отправки напоминания о заявке на вывод',
                request_id=getattr(request, 'id', None),
                error=e,
            )
            return False

    async def send_bulk_ban_notification(
        self,
        admin_user_id: int,
        successfully_banned: int,
        not_found: int,
        errors: int,
        admin_name: str = 'Администратор',
    ) -> bool:
        """Отправляет уведомление о массовой блокировке пользователей"""
        if not self._is_enabled():
            return False

        try:
            message_lines = [
                '🛑 <b>МАССОВАЯ БЛОКИРОВКА ПОЛЬЗОВАТЕЛЕЙ</b>',
                '',
                f'👮 <b>Администратор:</b> {html.escape(admin_name)}',
                f'🆔 <b>ID администратора:</b> {admin_user_id}',
                '',
                '📊 <b>Результаты:</b>',
                f'✅ Успешно заблокировано: {successfully_banned}',
                f'❌ Не найдено: {not_found}',
                f'💥 Ошибок: {errors}',
            ]

            total_processed = successfully_banned + not_found + errors
            if total_processed > 0:
                success_rate = (successfully_banned / total_processed) * 100
                message_lines.append(f'📈 Успешность: {success_rate:.1f}%')

            message_lines.extend(
                [
                    '',
                    f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            message = '\n'.join(message_lines)
            return await self._send_message(message, category=NotificationCategory.PARTNERS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о массовой блокировке', error=e)
            return False

    async def send_ticket_event_notification(
        self,
        text: str,
        keyboard: types.InlineKeyboardMarkup | None = None,
        *,
        media_file_id: str | None = None,
        media_type: str | None = None,
    ) -> bool:
        """Публичный метод для отправки уведомлений по тикетам в админ-топик.
        Учитывает настройки включенности в settings.
        Если передан media_file_id, отправляет медиа в тот же топик вместе с текстом.
        """
        # Respect runtime toggle for admin ticket notifications
        try:
            from app.services.support_settings_service import SupportSettingsService

            runtime_enabled = SupportSettingsService.get_admin_ticket_notifications_enabled()
        except Exception:
            runtime_enabled = True
        if not (self._is_enabled() and runtime_enabled):
            logger.info(
                'Ticket notification skipped',
                _is_enabled=self._is_enabled(),
                runtime_enabled=runtime_enabled,
            )
            return False

        # Если есть медиа, отправляем фото с текстом как caption (если влезает) или текст + фото
        if media_file_id and media_type == 'photo':
            return await self._send_ticket_photo_notification(text, media_file_id, keyboard)

        return await self._send_message(text, reply_markup=keyboard, category=NotificationCategory.TICKETS)

    async def _send_ticket_photo_notification(
        self,
        text: str,
        photo_file_id: str,
        keyboard: types.InlineKeyboardMarkup | None = None,
    ) -> bool:
        """Отправить фото с текстом в тикет-топик.
        Если текст помещается в caption (≤1024 символов после парсинга HTML) — фото с caption.
        Иначе — сначала текст, потом фото в тот же топик.
        """
        if not self.chat_id:
            return False

        thread_id = self._resolve_topic_id(category=NotificationCategory.TICKETS)

        try:
            if not caption_exceeds_telegram_limit(text):
                # Фото с caption — всё в одном сообщении
                photo_kwargs: dict = {
                    'chat_id': self.chat_id,
                    'photo': photo_file_id,
                    'caption': text,
                    'parse_mode': 'HTML',
                }
                if thread_id:
                    photo_kwargs['message_thread_id'] = thread_id
                if keyboard:
                    photo_kwargs['reply_markup'] = keyboard
                await self.bot.send_photo(**photo_kwargs)
            else:
                # Текст отдельно, фото следом в тот же топик
                await self._send_message(text, reply_markup=keyboard, category=NotificationCategory.TICKETS)
                photo_kwargs = {
                    'chat_id': self.chat_id,
                    'photo': photo_file_id,
                }
                if thread_id:
                    photo_kwargs['message_thread_id'] = thread_id
                await self.bot.send_photo(**photo_kwargs)

            return True
        except Exception as e:
            logger.error('Ошибка отправки фото-уведомления тикета', error=e)
            # Fallback: отправляем хотя бы текст
            return await self._send_message(text, reply_markup=keyboard, category=NotificationCategory.TICKETS)

    async def send_suspicious_traffic_notification(self, message: str, bot: Bot, topic_id: int | None = None) -> bool:
        """
        Отправляет уведомление о подозрительной активности трафика

        Args:
            message: текст уведомления
            bot: экземпляр бота для отправки сообщения
            topic_id: ID топика для отправки уведомления (если не указан, использует стандартный)
        """
        if not self._is_enabled() or not self.category_enabled.get(NotificationCategory.INFRASTRUCTURE, True):
            return False

        # Используем специальный топик для подозрительной активности, если он задан
        notification_topic_id = topic_id or self.topic_id

        try:
            message_kwargs = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            }

            if notification_topic_id:
                message_kwargs['message_thread_id'] = notification_topic_id

            await bot.send_message(**message_kwargs)
            logger.info(
                'Уведомление о подозрительной активности отправлено в чат топик',
                chat_id=self.chat_id,
                notification_topic_id=notification_topic_id,
            )
            return True

        except TelegramForbiddenError:
            logger.error('Бот не имеет прав для отправки в чат', chat_id=self.chat_id)
            return False
        except TelegramBadRequest as e:
            logger.error('Ошибка отправки уведомления о подозрительной активности', error=e)
            return False
        except Exception as e:
            logger.error('Неожиданная ошибка при отправке уведомления о подозрительной активности', error=e)
            return False

    async def send_new_client_notification(self, db: AsyncSession, user: User, *, source: str) -> bool:
        """Уведомление о новом клиенте: кто, когда, откуда и идентификаторы.

        Формат повторяет операторский образец: главный идентификатор, email,
        TG ID/username, внутренний ID, источник, общий счётчик клиентов,
        время и ссылка на карточку в админке.
        """
        total_clients = (
            await db.execute(select(func.count()).select_from(User))
        ).scalar_one()

        name = (getattr(user, 'first_name', None) or '').strip()
        username = getattr(user, 'username', None)
        email = getattr(user, 'email', None)
        telegram_id = getattr(user, 'telegram_id', None)

        # Главный идентификатор: то, по кому оператор сразу найдёт клиента.
        if name:
            head = f'📝 {html.escape(name)}'
        elif username:
            head = f'📝 {format_username_link(username)}'
        elif email:
            head = f'📝 {html.escape(email)}'
        else:
            head = f'📝 ID {user.id}'

        lines: list[str] = ['👤 <b>Новый клиент</b>', '', head]
        if username:
            lines.append(f'📱 Username: {format_username_link(username)}')
        if email:
            lines.append(f'📧 Email: {html.escape(email)}')
        if telegram_id:
            lines.append(f'🆔 TG ID: <code>{telegram_id}</code>')

        panel_id = getattr(user, 'remnawave_id', None)
        system_id = f'#{user.id}'
        if panel_id:
            system_id += f' · панель: {panel_id}'
        lines.append(f'🧩 ID в системе: {system_id}')

        referrer_id = getattr(user, 'referred_by_id', None)
        if referrer_id:
            lines.append(f'🤝 Партнёр: {await self._get_referrer_info(db, referrer_id)}')

        lines.append(f'🌐 Источник: {html.escape(source)}')
        lines.append(f'📊 Всего клиентов: <b>{total_clients}</b>')
        lines.append(f"🕐 {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC")

        admin_base = (settings.CABINET_URL or '').rstrip('/')
        keyboard = None
        if admin_base:
            keyboard = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text='🔗 Открыть в админке', url=f'{admin_base}/admin/users/{user.id}'
                        )
                    ]
                ]
            )

        return await self._send_message(
            '\n'.join(lines),
            keyboard,
            category=NotificationCategory.NEW_USERS,
        )

    async def send_migration_notification(
        self,
        db: AsyncSession,
        user: User,
        candidate: Any,
        result: Any,
        *,
        source: str = 'telegram',
    ) -> bool:
        """Уведомление об успешном переносе подписки из Лазейка ВПН в топик PURCHASES."""
        if not self._is_enabled():
            return False

        try:
            user_display = self._get_user_display(user)
            user_id_display = self._get_user_identifier_display(user)

            lines: list[str] = [
                '🚀 <b>ПЕРЕНОС ИЗ ЛАЗЕЙКА ВПН</b>',
                '',
                f'👤 {user_display} ({user_id_display})',
            ]

            username = getattr(user, 'username', None)
            if username:
                lines.append(f'📱 {format_username_link(username)}')

            email = getattr(user, 'email', None)
            if email:
                lines.append(f'📧 {html.escape(email)}')

            source_display = 'Telegram-бот (@invoxy_bot)' if source == 'telegram' else 'Личный кабинет (invoxy.my)'
            lines.append(f'🌐 Источник: {source_display}')
            lines.append('')
            lines.append('📋 <b>Параметры переноса:</b>')
            lazeika_tariff = getattr(candidate, 'lazeika_tariff_name', '—')
            new_tariff = getattr(result, 'tariff_name', '—')
            lines.append(f'• Исходный тариф: <b>{html.escape(str(lazeika_tariff))}</b> (Лазейка)')
            lines.append(f'• Новый тариф: <b>{html.escape(str(new_tariff))}</b> (Invoxy)')

            total_days = getattr(result, 'total_days', 0)
            rem_days = getattr(candidate, 'remaining_days', 0)
            bonus_days = getattr(candidate, 'bonus_days', 5)
            lines.append(f'• Срок доступа: <b>{total_days} дн.</b> ({rem_days} дн. остаток + {bonus_days} дн. бонус 🎁)')

            end_date = getattr(result, 'end_date_str', '—')
            lines.append(f'• Действует до: <b>{end_date}</b>')

            device_limit = getattr(result, 'device_limit', 1)
            lines.append(f'• Лимит устройств: <b>{device_limit} устр.</b>')

            traffic_gb = getattr(result, 'traffic_limit_gb', 0)
            lines.append(f'• Трафик: <b>{traffic_gb} ГБ</b> (трафик обнулён)')

            transferred_rub = getattr(candidate, 'balance_rub', 0.0) or getattr(result, 'balance_transferred_rub', 0.0)
            if transferred_rub > 0:
                lines.append(f'• Перенесённый баланс: <b>+{transferred_rub:.2f} ₽</b> 💰')

            referrer_id = getattr(user, 'referred_by_id', None)
            if referrer_id:
                lines.append(f'• Пригласитель (реферер ID): <code>{referrer_id}</code> 👥')

            panel_user_id = getattr(candidate, 'panel_user_id', None)
            if panel_user_id:
                lines.append(f'• Remnawave User ID: <code>{panel_user_id}</code>')

            client_id = getattr(candidate, 'client_id', None)
            if client_id:
                lines.append(f'• Клиент в Лазейке: <code>{client_id}</code>')

            sub_id = getattr(candidate, 'subscription_id', None)
            if sub_id:
                lines.append(f'• Аннулировано в Лазейке: <code>{sub_id}</code>')

            lines.append('')
            lines.append(f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M")}</i>')

            admin_base = (settings.CABINET_URL or '').rstrip('/')
            keyboard = None
            if admin_base and getattr(user, 'id', None):
                keyboard = types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text='🔗 Открыть в админке', url=f'{admin_base}/admin/users/{user.id}'
                            )
                        ]
                    ]
                )

            return await self._send_message(
                '\n'.join(lines),
                keyboard,
                category=NotificationCategory.PURCHASES,
            )
        except Exception as e:
            logger.warning(
                'Ошибка отправки уведомления о миграции',
                error=str(e),
                user_id=getattr(user, 'id', None),
            )
            return False


async def notify_new_client_created(db: AsyncSession, user: User, source: str) -> None:
    """Уведомление о новом клиенте в топик NEW_USERS. Никогда не поднимает исключение.

    Модульная обёртка для точек создания пользователя, где сервис ещё не собран:
    сама рассылка управляется ADMIN_NOTIFICATIONS_* настройками.
    """
    if not getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False):
        return

    try:
        from app.bot_factory import create_bot

        async with create_bot() as bot:
            service = AdminNotificationService(bot)
            await service.send_new_client_notification(db, user, source=source)
    except Exception as error:
        logger.warning(
            'Не удалось отправить уведомление о новом клиенте',
            user_id=getattr(user, 'id', None),
            error=str(error),
        )


async def notify_user_migrated(
    db: AsyncSession,
    user: User,
    candidate: Any,
    result: Any,
    *,
    source: str = 'telegram',
) -> None:
    """Уведомление о переносе пользователя в топик PURCHASES. Никогда не поднимает исключение."""
    if not getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False):
        return

    try:
        from app.bot_factory import create_bot

        async with create_bot() as bot:
            service = AdminNotificationService(bot)
            await service.send_migration_notification(db, user, candidate, result, source=source)
    except Exception as error:
        logger.warning(
            'Не удалось отправить уведомление о миграции пользователя',
            user_id=getattr(user, 'id', None),
            error=str(error),
        )
