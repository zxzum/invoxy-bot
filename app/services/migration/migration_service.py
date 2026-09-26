"""Core orchestration service for transferring users from Lazeika VPN to Invoxy VPN."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import get_active_subscriptions_by_user_id
from app.database.models import Subscription, SubscriptionStatus, User
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
    # Find active subscription
    active_sub = None
    for sub in subscriptions:
        if sub.get('status') == 'ACTIVE':
            active_sub = sub
            break

    if not active_sub:
        logger.info('No active subscription found in Lazeika for user', telegram_id=telegram_id, email=email)
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

    lazeika_tariff_name = active_sub.get('tariffName') or 'Стандарт'
    target_tariff = await resolve_target_tariff(db, lazeika_tariff_name)

    # Device limit: preserve max between Lazeika devices and target tariff
    lazeika_devices = active_sub.get('deviceLimit') or 1
    device_limit = max(int(lazeika_devices), target_tariff.device_limit)

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
    )


async def execute_user_migration(
    db: AsyncSession,
    user: User,
    *,
    telegram_id: int | None = None,
    email: str | None = None,
) -> MigrationResult:
    """Executes the complete migration sequence for a user."""
    candidate = await check_migration_eligibility(db, telegram_id=telegram_id, email=email, user=user)
    if not candidate:
        raise ValueError('Подписка в Лазейка ВПН не найдена или уже перенесена.')

    panel_user_id = int(candidate.remnawave_uuid)
    logger.info(
        'Starting migration for user',
        user_id=user.id,
        panel_user_id=panel_user_id,
        target_tariff=candidate.target_tariff_name,
        total_days=candidate.total_days,
    )

    # 1. Update Remnawave user
    new_username = f'invoxy_{user.telegram_id}' if user.telegram_id else f'invoxy_u{user.id}'
    traffic_bytes = candidate.traffic_limit_gb * 1024 * 1024 * 1024 if candidate.traffic_limit_gb > 0 else 0

    target_tariff = await resolve_target_tariff(db, candidate.lazeika_tariff_name)
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
    existing_subs = await get_active_subscriptions_by_user_id(db, user.id)
    subscription: Subscription | None = existing_subs[0] if existing_subs else None

    sub_url = (
        updated_panel_user.subscription_url
        if updated_panel_user and updated_panel_user.subscription_url
        else f'{settings.REMNAWAVE_SUBSCRIPTION_PUBLIC_URL}/{updated_panel_user.short_uuid}'
    )

    if subscription:
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
            subscription_url=sub_url,
            connected_squads=squads,
            autopay_enabled=False,
        )
        db.add(subscription)

    db.add(user)
    await db.commit()
    await db.refresh(user)

    # 3. Annul old subscription in Lazeika
    lazeika_client = LazeikaClient()
    try:
        await lazeika_client.annul_subscription(
            client_id=candidate.client_id,
            subscription_id=candidate.subscription_id,
            reason='Мигрирован в Invoxy VPN (@invoxy_bot / invoxy.my)',
        )
        logger.info('Annulled Lazeika subscription', client_id=candidate.client_id, sub_id=candidate.subscription_id)
    except Exception as e:
        logger.error('Failed to annul Lazeika subscription (Invoxy migration completed)', error=str(e))

    end_date_str = candidate.new_end_date.strftime('%d.%m.%Y')
    return MigrationResult(
        success=True,
        tariff_name=candidate.target_tariff_name,
        total_days=candidate.total_days,
        device_limit=candidate.device_limit,
        end_date_str=end_date_str,
        traffic_limit_gb=candidate.traffic_limit_gb,
        whitelist_traffic_limit_gb=candidate.whitelist_traffic_limit_gb,
        subscription_url=sub_url,
        message='Подписка успешно перенесена! Начислен бонус +5 дней и сброшены лимиты трафика.',
    )
