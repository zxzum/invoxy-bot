"""Tariff mapping from Lazeika VPN to Invoxy VPN."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Tariff


@dataclass
class TargetTariffInfo:
    tariff_id: int
    name: str
    traffic_limit_gb: int
    whitelist_traffic_limit_gb: int
    device_limit: int
    allowed_squads: list[str]


# Mapping rules: Lazeika tariff name lowercase -> Invoxy tariff target search name
LAZEIKA_TO_INVOXY_NAME_MAP: dict[str, str] = {
    'премиум': 'Премиум LTE',
    'стандарт': 'Стандарт LTE',
    'оптимальный': 'Стандарт LTE',
    'обычный впн': 'Базовый',
}

# Fallback IDs and definitions if DB lookup cannot find by name
FALLBACK_TARIFFS: dict[str, TargetTariffInfo] = {
    'Премиум LTE': TargetTariffInfo(
        tariff_id=4,
        name='Премиум LTE',
        traffic_limit_gb=1000,
        whitelist_traffic_limit_gb=150,
        device_limit=10,
        allowed_squads=['259fafa0-7e54-434b-89c3-1efaca5ef26f', '96f1944f-6c61-4b6d-95cb-48d441eae952'],
    ),
    'Стандарт LTE': TargetTariffInfo(
        tariff_id=3,
        name='Стандарт LTE',
        traffic_limit_gb=600,
        whitelist_traffic_limit_gb=50,
        device_limit=5,
        allowed_squads=['259fafa0-7e54-434b-89c3-1efaca5ef26f', '96f1944f-6c61-4b6d-95cb-48d441eae952'],
    ),
    'Базовый': TargetTariffInfo(
        tariff_id=2,
        name='Базовый',
        traffic_limit_gb=300,
        whitelist_traffic_limit_gb=0,
        device_limit=5,
        allowed_squads=['259fafa0-7e54-434b-89c3-1efaca5ef26f'],
    ),
}


async def resolve_target_tariff(db: AsyncSession, lazeika_tariff_name: str) -> TargetTariffInfo | None:
    """Finds the corresponding Invoxy tariff based on Lazeika tariff name.

    Returns None if the tariff name is empty, unknown, or represents a trial.
    """
    clean_name = (lazeika_tariff_name or '').strip().lower()
    if not clean_name or clean_name not in LAZEIKA_TO_INVOXY_NAME_MAP:
        return None

    target_name = LAZEIKA_TO_INVOXY_NAME_MAP[clean_name]

    # Look up tariff in DB
    result = await db.execute(select(Tariff).where(Tariff.name == target_name, Tariff.is_active == True))
    tariff = result.scalars().first()
    if not tariff:
        # Fallback to any active tariff matching name or default fallback
        result = await db.execute(select(Tariff).where(Tariff.name == target_name))
        tariff = result.scalars().first()

    if tariff:
        squads = tariff.allowed_squads if isinstance(tariff.allowed_squads, list) else []
        return TargetTariffInfo(
            tariff_id=tariff.id,
            name=tariff.name,
            traffic_limit_gb=tariff.traffic_limit_gb,
            whitelist_traffic_limit_gb=tariff.whitelist_traffic_limit_gb,
            device_limit=tariff.device_limit,
            allowed_squads=squads,
        )

    # Return predefined fallback info
    return FALLBACK_TARIFFS.get(target_name)

