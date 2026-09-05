"""Local accounting for traffic sent through RemnaWave WHITELIST nodes.

The regular RemnaWave counter remains untouched. The worker removes/restores
only the configured White Internet squad when its independent quota changes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.subscription import (
    housekeep_whitelist_traffic_purchases,
)
from app.database.database import AsyncSessionLocal
from app.database.models import (
    Subscription,
    SubscriptionStatus,
    WhitelistTrafficUsageSnapshot,
)
from app.services.remnawave_service import RemnaWaveService


logger = structlog.get_logger(__name__)


def _panel_squad_ids(panel_user: Any) -> set[str]:
    result: set[str] = set()
    for squad in getattr(panel_user, 'active_internal_squads', None) or []:
        if isinstance(squad, str):
            result.add(squad.strip().lower())
            continue
        if isinstance(squad, dict):
            for key in ('uuid', 'squadUuid', 'name'):
                value = squad.get(key)
                if value:
                    result.add(str(value).strip().lower())
    return result


def _usage_by_user(payload: dict[str, Any]) -> dict[int, int]:
    totals: dict[int, int] = {}
    for node in payload.get('nodes', []) or []:
        for item in node.get('users', []) or []:
            try:
                panel_user_id = int(item.get('id'))
                total_bytes = max(0, int(item.get('totalBytes') or 0))
            except (TypeError, ValueError):
                continue
            totals[panel_user_id] = totals.get(panel_user_id, 0) + total_bytes
    return totals


def _effective_whitelist_squads(subscription: Any, whitelist_squad_uuid: str) -> list[str]:
    """Return canonical squads with White Internet removed after quota exhaustion."""
    squads = list(getattr(subscription, 'connected_squads', None) or [])
    target = whitelist_squad_uuid.strip().lower()
    limit_bytes = max(0, int(getattr(subscription, 'whitelist_traffic_limit_gb', 0) or 0)) * 1024**3
    used_bytes = max(0, int(getattr(subscription, 'whitelist_traffic_used_bytes', 0) or 0))
    if limit_bytes and used_bytes >= limit_bytes:
        return [squad for squad in squads if str(squad).strip().lower() != target]
    return squads


class WhitelistTrafficAccountingService:
    def __init__(self) -> None:
        self._running = False
        self._lock = asyncio.Lock()

    def is_enabled(self) -> bool:
        if not getattr(settings, 'WHITELIST_TRAFFIC_ACCOUNTING_ENABLED', False):
            return False
        if not str(getattr(settings, 'WHITELIST_SQUAD_UUID', '') or '').strip():
            return False
        return RemnaWaveService().is_configured

    def get_interval_minutes(self) -> int:
        return max(10, min(15, int(getattr(settings, 'WHITELIST_TRAFFIC_SYNC_INTERVAL_MINUTES', 15))))

    async def start_monitoring(self) -> None:
        if self._running:
            return
        self._running = True
        logger.info('Запущен учет трафика по WHITELIST')
        while self._running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Ошибка учета трафика по WHITELIST')
            await asyncio.sleep(self.get_interval_minutes() * 60)

    def stop_monitoring(self) -> None:
        self._running = False

    async def run_once(self) -> dict[str, int]:
        if not self.is_enabled() or self._lock.locked():
            return {'subscriptions': 0, 'bytes_added': 0}

        async with self._lock:
            now = datetime.now(UTC)
            period_key = now.strftime('%Y-%m')
            start_date = f'{now.year:04d}-{now.month:02d}-01'
            end_date = now.date().isoformat()
            panel_squad_uuid = str(settings.WHITELIST_SQUAD_UUID).strip().lower()
            remnawave = RemnaWaveService()

            async with remnawave.get_api_client() as api:
                whitelist_nodes = await api.get_internal_squad_accessible_nodes(
                    settings.WHITELIST_SQUAD_UUID
                )
                node_uuids = [node.uuid for node in whitelist_nodes if node.uuid]
                if not node_uuids:
                    logger.warning('У WHITELIST нет доступных нод, учет пропущен')
                    return {'subscriptions': 0, 'bytes_added': 0}

                panel_users = await api.get_all_users_stream(size=1000, enrich_happ_links=False)
                whitelist_panel_ids = {
                    user.id
                    for user in panel_users
                    if panel_squad_uuid in _panel_squad_ids(user)
                }
                usage_payload = await api.get_bandwidth_stats_nodes_usage(
                    node_uuids,
                    start_date,
                    end_date,
                    min_total_bytes=0,
                )

            async with AsyncSessionLocal() as db:
                subscriptions = await self._get_accounted_subscriptions(db)
                by_panel_id: dict[int, list[Subscription]] = {}
                for subscription in subscriptions:
                    panel_id = self._panel_id(subscription)
                    if panel_id is not None and panel_id in whitelist_panel_ids:
                        by_panel_id.setdefault(panel_id, []).append(subscription)

                usage = _usage_by_user(usage_payload)
                bytes_added = 0
                for subscription in subscriptions:
                    await housekeep_whitelist_traffic_purchases(db, subscription, now=now)

                for panel_id, matched_subscriptions in by_panel_id.items():
                    for subscription in matched_subscriptions:
                        bytes_added += await self._apply_sample(
                            db,
                            subscription,
                            panel_id,
                            usage.get(panel_id, 0),
                            period_key,
                            now,
                        )
                squad_updates: list[tuple[int, int, list[str]]] = []
                for subscription in subscriptions:
                    panel_id = self._panel_id(subscription)
                    if panel_id is None:
                        continue
                    desired_squads = _effective_whitelist_squads(
                        subscription, settings.WHITELIST_SQUAD_UUID
                    )
                    desired_has_whitelist = panel_squad_uuid in {
                        str(squad).strip().lower() for squad in desired_squads
                    }
                    current_has_whitelist = panel_id in whitelist_panel_ids
                    if desired_has_whitelist != current_has_whitelist:
                        squad_updates.append((subscription.id, panel_id, desired_squads))
                await db.commit()

            if squad_updates:
                from app.services.grace_access_runtime import update_panel_user_grace_safe

                async with remnawave.get_api_client() as api:
                    for subscription_id, panel_id, desired_squads in squad_updates:
                        try:
                            await update_panel_user_grace_safe(
                                api,
                                subscription_id,
                                user_id=panel_id,
                                active_internal_squads=desired_squads,
                            )
                        except Exception as error:
                            logger.error(
                                'Не удалось обновить доступ к Белому интернету',
                                subscription_id=subscription_id,
                                panel_user_id=panel_id,
                                error=error,
                            )

            logger.info(
                'Учет WHITELIST-трафика завершен',
                subscriptions=len(by_panel_id),
                bytes_added=bytes_added,
                nodes=len(node_uuids),
                squad_updates=len(squad_updates),
            )
            return {'subscriptions': len(by_panel_id), 'bytes_added': bytes_added}

    @staticmethod
    async def _get_accounted_subscriptions(db: AsyncSession) -> list[Subscription]:
        result = await db.execute(
            select(Subscription)
            .options(selectinload(Subscription.user))
            .where(
                Subscription.status.in_(
                    [SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value]
                ),
                or_(
                    Subscription.whitelist_traffic_limit_gb > 0,
                    Subscription.whitelist_traffic_purchased_gb > 0,
                ),
            )
        )
        return list(result.scalars().all())

    @staticmethod
    def _panel_id(subscription: Subscription) -> int | None:
        value = subscription.remnawave_id or getattr(subscription.user, 'remnawave_id', None)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    async def _apply_sample(
        db: AsyncSession,
        subscription: Subscription,
        panel_user_id: int,
        measured_bytes: int,
        period_key: str,
        sampled_at: datetime,
    ) -> int:
        result = await db.execute(
            select(WhitelistTrafficUsageSnapshot)
            .where(
                WhitelistTrafficUsageSnapshot.subscription_id == subscription.id,
                WhitelistTrafficUsageSnapshot.period_key == period_key,
            )
            .with_for_update()
        )
        snapshot = result.scalar_one_or_none()
        measured_bytes = max(0, measured_bytes)
        if snapshot is None:
            # First sample is a baseline: never charge traffic that was used
            # before this worker started or before a new accounting month.
            subscription.whitelist_traffic_used_bytes = 0
            db.add(
                WhitelistTrafficUsageSnapshot(
                    subscription_id=subscription.id,
                    panel_user_id=panel_user_id,
                    period_key=period_key,
                    measured_bytes=measured_bytes,
                    sampled_at=sampled_at,
                )
            )
            return 0

        if snapshot.panel_user_id != panel_user_id:
            delta = 0
        else:
            delta = max(0, measured_bytes - (snapshot.measured_bytes or 0))
        subscription.whitelist_traffic_used_bytes = (
            subscription.whitelist_traffic_used_bytes or 0
        ) + delta
        snapshot.panel_user_id = panel_user_id
        snapshot.measured_bytes = measured_bytes
        snapshot.sampled_at = sampled_at
        return delta


whitelist_traffic_service = WhitelistTrafficAccountingService()
