"""Core orchestration service for transferring users from Lazeika VPN to Invoxy VPN."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import (
    generate_unique_short_id,
    get_active_subscriptions_by_user_id,
    get_subscription_by_user_id,
)
from app.database.crud.tariff import get_tariff_by_id
from app.database.models import Subscription, SubscriptionStatus, Transaction, TransactionType, User
from app.external.remnawave_api import UserStatus
from app.services.migration.lazeika_client import LazeikaClient
from app.services.migration.tariff_mapping import resolve_target_tariff
from app.services.remnawave_service import RemnaWaveService


logger = structlog.get_logger(__name__)


@dataclass
class MigrationCandidate:
    client_id: str
    subscription_id: str
    lazeika_tariff_name: str
    target_tariff_id: int
    target_tariff_name: str
    remaining_days: int
    bonus_days: int
    total_days: int
    device_limit: int
    traffic_limit_gb: int
    whitelist_traffic_limit_gb: int
    remnawave_uuid: str
    expires_at_str: str
    new_end_date: datetime
    balance_rub: float = 0.0


@dataclass
class MigrationResult:
    success: bool
    tariff_name: str
    total_days: int
    device_limit: int
    end_date_str: str
    traffic_limit_gb: int
    whitelist_traffic_limit_gb: int
    subscription_url: str = ''
    balance_transferred_rub: float = 0.0
    balance_kopeks: int = 0
    message: str = ''


async def check_migration_eligibility(
    db: AsyncSession,
    *,
    telegram_id: int | None = None,
    email: str | None = None,
    user: User | None = None,
) -> MigrationCandidate | None:
    """Checks if a user is eligible for migration from Lazeika VPN.

    Returns MigrationCandidate if found and eligible, None otherwise.
    """
    if not settings.LAZEIKA_MIGRATION_ENABLED:
        return None

    # If user object is provided, check if they already have an active paid subscription in Invoxy
    if user:
        subs = await get_active_subscriptions_by_user_id(db, user.id)
        active_paid = [s for s in subs if not s.is_trial and s.status == SubscriptionStatus.ACTIVE.value]
        if active_paid:
            logger.info(
                'User already has active paid subscription in Invoxy, skipping migration check', user_id=user.id
            )
            return None

    # Query Lazeika API
    client = LazeikaClient()
    data = await client.get_client_context(telegram_id=telegram_id, email=email)
    if not data or not data.get('found'):
        return None

    client_info = data.get('client') or {}
    if client_info.get('isBlocked'):
        logger.info('Lazeika client is already blocked/migrated', client_id=client_info.get('id'))
        return None

    subscriptions = data.get('subscriptions') or []
    # Find active paid subscription (trials are strictly excluded from migration)
    trial_names = {'', 'триал', 'пробный', 'пробный период', 'trial', 'free', 'test'}
    active_sub = None
    target_tariff = None
    for sub in subscriptions:
        if sub.get('status') != 'ACTIVE':
            continue
        if sub.get('isTrial'):
            continue
        tariff_name = (sub.get('tariffName') or '').strip()
        if not tariff_name or tariff_name.lower() in trial_names:
            continue
        resolved = await resolve_target_tariff(db, tariff_name)
        if not resolved:
            continue
        # In Lazeika, trial subscriptions have empty or 0 trafficLimitBytes
        raw_traffic = str(sub.get('trafficLimitBytes') or '').strip()
        if not raw_traffic or raw_traffic == '0':
            continue
        active_sub = sub
        target_tariff = resolved
        break

    if not active_sub or not target_tariff:
        logger.info(
            'No active paid subscription found in Lazeika for user (trials are not eligible for migration)',
            telegram_id=telegram_id,
            email=email,
        )
        return None

    remnawave_uuid = str(active_sub.get('remnawaveUuid') or '').strip()
    if not remnawave_uuid or not remnawave_uuid.isdigit():
        logger.warning('Active subscription in Lazeika has missing or invalid remnawaveUuid', sub=active_sub)
        return None

    # Parse expiration
    raw_exp = active_sub.get('expiresAt')
    now = datetime.now(UTC)
    try:
        exp_dt = datetime.fromisoformat(raw_exp.replace('Z', '+00:00'))
    except Exception:
        exp_dt = now + timedelta(days=30)

    remaining_seconds = (exp_dt - now).total_seconds()
    remaining_days = max(1, int(remaining_seconds // 86400))
    bonus_days = getattr(settings, 'LAZEIKA_BONUS_DAYS', 5)
    total_days = remaining_days + bonus_days
    new_end_date = now + timedelta(days=total_days)

    lazeika_tariff_name = (active_sub.get('tariffName') or '').strip()

    # Device limit: preserve max between Lazeika devices and target tariff
    lazeika_devices = active_sub.get('deviceLimit') or 1
    device_limit = max(int(lazeika_devices), target_tariff.device_limit)

    raw_balance = client_info.get('balance')
    try:
        balance_rub = float(raw_balance) if raw_balance is not None else 0.0
    except (ValueError, TypeError):
        balance_rub = 0.0


    return MigrationCandidate(
        client_id=str(client_info.get('id')),
        subscription_id=str(active_sub.get('id')),
        lazeika_tariff_name=lazeika_tariff_name,
        target_tariff_id=target_tariff.tariff_id,
        target_tariff_name=target_tariff.name,
        remaining_days=remaining_days,
        bonus_days=bonus_days,
        total_days=total_days,
        device_limit=device_limit,
        traffic_limit_gb=target_tariff.traffic_limit_gb,
        whitelist_traffic_limit_gb=target_tariff.whitelist_traffic_limit_gb,
        remnawave_uuid=remnawave_uuid,
        expires_at_str=exp_dt.strftime('%d.%m.%Y'),
        new_end_date=new_end_date,
        balance_rub=balance_rub,
    )


async def execute_user_migration(
    db: AsyncSession,
    user: User,
    *,
    telegram_id: int | None = None,
    email: str | None = None,
    source: str = 'telegram',
) -> MigrationResult:
    """Executes the complete migration sequence for a user."""
    candidate = await check_migration_eligibility(db, telegram_id=telegram_id, email=email, user=user)
    if not candidate:
        if user:
            subs = await get_active_subscriptions_by_user_id(db, user.id)
            active_paid = [s for s in subs if not s.is_trial and s.status == SubscriptionStatus.ACTIVE.value]
            if active_paid:
                sub = active_paid[0]
                tariff = await get_tariff_by_id(db, sub.tariff_id)
                tariff_name = tariff.name if tariff else 'Стандарт'
                end_date_str = sub.end_date.strftime('%d.%m.%Y') if sub.end_date else '—'
                days_left = max(0, (sub.end_date - datetime.now(UTC)).days) if sub.end_date else 0
                return MigrationResult(
                    success=True,
                    tariff_name=tariff_name,
                    total_days=days_left,
                    device_limit=sub.device_limit or 1,
                    end_date_str=end_date_str,
                    traffic_limit_gb=sub.traffic_limit_gb or 0,
                    whitelist_traffic_limit_gb=getattr(sub, 'whitelist_traffic_limit_gb', 0) or 0,
                    subscription_url=sub.subscription_url or '',
                    message='Ваша подписка уже успешно перенесена в Invoxy VPN!',
                )
        raise ValueError('Подписка в Лазейка ВПН не найдена или уже перенесена.')

    panel_user_id = int(candidate.remnawave_uuid)
    logger.info(
        'Starting migration for user',
        user_id=user.id,
        panel_user_id=panel_user_id,
        target_tariff=candidate.target_tariff_name,
        total_days=candidate.total_days,
        balance_rub=candidate.balance_rub,
    )

    # 0. Attach referrer if missing (e.g. from Redis pending referral)
    if user and user.referred_by_id is None:
        try:
            from app.services.referral_service import attach_referrer_if_missing
            await attach_referrer_if_missing(db, user, source=f'migration_{source}')
        except Exception as e:
            logger.warning('Failed to attach referrer during migration (non-fatal)', user_id=user.id, error=str(e))

    # 1. Update Remnawave user
    new_username = f'invoxy_{user.telegram_id}' if user.telegram_id else f'invoxy_u{user.id}'
    traffic_bytes = candidate.traffic_limit_gb * 1024 * 1024 * 1024 if candidate.traffic_limit_gb > 0 else 0

    target_tariff = await resolve_target_tariff(db, candidate.lazeika_tariff_name)
    if not target_tariff:
        return MigrationResult(success=False, message='Тариф подписки не поддерживается для переноса')
    squads = target_tariff.allowed_squads

    remna_service = RemnaWaveService()
    async with remna_service.get_api_client() as api:
        # Patch Remnawave user: username, limits, expiration, squad
        updated_panel_user = await api.update_user(
            user_id=panel_user_id,
            username=new_username,
            status=UserStatus.ACTIVE,
            traffic_limit_bytes=traffic_bytes,
            expire_at=candidate.new_end_date,
            hwid_device_limit=candidate.device_limit,
            active_internal_squads=squads,
            telegram_id=user.telegram_id if user.telegram_id else None,
            email=user.email if user.email else None,
        )

        # Reset traffic as bonus!
        try:
            await api.reset_user_traffic(panel_user_id)
            logger.info('Traffic counter reset in Remnawave', panel_user_id=panel_user_id)
        except Exception as e:
            logger.warning('Failed to reset traffic in Remnawave (non-fatal)', error=str(e))

    # 2. Update Invoxy Database
    now = datetime.now(UTC)
    user.remnawave_id = panel_user_id
    user.remnawave_uuid = str(panel_user_id)

    # Check if user has an existing subscription record
    existing_sub = await get_subscription_by_user_id(db, user.id)

    sub_url = (
        updated_panel_user.subscription_url
        if updated_panel_user and updated_panel_user.subscription_url
        else f'{settings.REMNAWAVE_SUBSCRIPTION_PUBLIC_URL}/{updated_panel_user.short_uuid}'
    )

    if existing_sub:
        subscription = existing_sub
        if not getattr(subscription, 'remnawave_short_id', None):
            subscription.remnawave_short_id = await generate_unique_short_id(db)
        # Replace existing subscription parameters
        subscription.tariff_id = candidate.target_tariff_id
        subscription.status = SubscriptionStatus.ACTIVE.value
        subscription.is_trial = False
        subscription.start_date = now
        subscription.end_date = candidate.new_end_date
        subscription.traffic_limit_gb = candidate.traffic_limit_gb
        subscription.traffic_used_gb = 0.0
        subscription.device_limit = candidate.device_limit
        subscription.whitelist_traffic_limit_gb = candidate.whitelist_traffic_limit_gb
        subscription.whitelist_traffic_used_bytes = 0
        subscription.whitelist_traffic_purchased_gb = 0
        subscription.remnawave_id = panel_user_id
        subscription.remnawave_uuid = str(panel_user_id)
        subscription.remnawave_short_uuid = updated_panel_user.short_uuid
        subscription.subscription_url = sub_url
        subscription.connected_squads = squads
    else:
        # Create new subscription row
        short_id = await generate_unique_short_id(db)
        subscription = Subscription(
            user_id=user.id,
            tariff_id=candidate.target_tariff_id,
            status=SubscriptionStatus.ACTIVE.value,
            is_trial=False,
            start_date=now,
            end_date=candidate.new_end_date,
            traffic_limit_gb=candidate.traffic_limit_gb,
            traffic_used_gb=0.0,
            device_limit=candidate.device_limit,
            whitelist_traffic_limit_gb=candidate.whitelist_traffic_limit_gb,
            whitelist_traffic_used_bytes=0,
            whitelist_traffic_purchased_gb=0,
            remnawave_id=panel_user_id,
            remnawave_uuid=str(panel_user_id),
            remnawave_short_uuid=updated_panel_user.short_uuid,
            remnawave_short_id=short_id,
            subscription_url=sub_url,
            connected_squads=squads,
            autopay_enabled=False,
        )
        db.add(subscription)

    # 2.5 Credit migrated balance if present
    balance_kopeks = int(round(candidate.balance_rub * 100)) if candidate.balance_rub > 0 else 0
    if balance_kopeks > 0:
        user.balance_kopeks = (user.balance_kopeks or 0) + balance_kopeks
        tx = Transaction(
            user_id=user.id,
            amount_kopeks=balance_kopeks,
            type=TransactionType.DEPOSIT.value,
            payment_method='migration',
            description=f'Перенос баланса из Лазейка ВПН ({candidate.balance_rub:.2f} ₽)',
            is_completed=True,
            created_at=now,
            completed_at=now,
        )
        db.add(tx)
        logger.info(
            'Migrated balance credited',
            user_id=user.id,
            balance_rub=candidate.balance_rub,
            balance_kopeks=balance_kopeks,
        )

    db.add(user)
    await db.commit()
    await db.refresh(user)

    # 3. Annul old subscription in Lazeika
    lazeika_client = LazeikaClient()
    try:
        await lazeika_client.annul_subscription(
            client_id=candidate.client_id,
            subscription_id=candidate.subscription_id,
            telegram_id=telegram_id or user.telegram_id,
            reason='Мигрирован в Invoxy VPN (@invoxy_bot / invoxy.my)',
        )
        logger.info('Annulled Lazeika subscription', client_id=candidate.client_id, sub_id=candidate.subscription_id)
    except Exception as e:
        logger.error('Failed to annul Lazeika subscription (Invoxy migration completed)', error=str(e))

    end_date_str = candidate.new_end_date.strftime('%d.%m.%Y')
    msg_parts = ['Подписка успешно перенесена! Начислен бонус +5 дней и сброшены лимиты трафика.']
    if candidate.balance_rub > 0:
        msg_parts.append(f'Перенесён баланс: {candidate.balance_rub:.2f} ₽.')

    result = MigrationResult(
        success=True,
        tariff_name=candidate.target_tariff_name,
        total_days=candidate.total_days,
        device_limit=candidate.device_limit,
        end_date_str=end_date_str,
        traffic_limit_gb=candidate.traffic_limit_gb,
        whitelist_traffic_limit_gb=candidate.whitelist_traffic_limit_gb,
        subscription_url=sub_url,
        balance_transferred_rub=candidate.balance_rub,
        balance_kopeks=balance_kopeks,
        message=' '.join(msg_parts),
    )

    # 4. Notify admin chat with detailed mini-log
    try:
        from app.services.admin_notification_service import notify_user_migrated
        await notify_user_migrated(
            db,
            user=user,
            candidate=candidate,
            result=result,
            source=source,
        )
    except Exception as e:
        logger.warning('Failed to send admin migration notification (non-fatal)', error=str(e))

    return result
