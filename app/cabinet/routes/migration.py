"""Cabinet routes for migrating users from Lazeika VPN (temporary)."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.database.models import User
from app.services.migration import check_migration_eligibility, execute_user_migration


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/migration', tags=['Migration'])


@router.get('/check')
async def check_migration(
    current_user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Checks if the authenticated cabinet user has an active subscription in Lazeika VPN eligible for migration."""
    email = current_user.email
    telegram_id = current_user.telegram_id
    if not email and not telegram_id:
        return {'eligible': False, 'reason': 'No email or telegram associated with account'}

    try:
        candidate = await check_migration_eligibility(
            db,
            email=email,
            telegram_id=current_user.telegram_id,
            user=current_user,
        )
        if not candidate:
            return {'eligible': False}

        return {
            'eligible': True,
            'candidate': {
                'lazeika_tariff_name': candidate.lazeika_tariff_name,
                'target_tariff_name': candidate.target_tariff_name,
                'remaining_days': candidate.remaining_days,
                'bonus_days': candidate.bonus_days,
                'total_days': candidate.total_days,
                'device_limit': candidate.device_limit,
                'traffic_limit_gb': candidate.traffic_limit_gb,
                'whitelist_traffic_limit_gb': candidate.whitelist_traffic_limit_gb,
                'expires_at': candidate.expires_at_str,
            },
        }
    except Exception as e:
        logger.error('Failed to check migration eligibility for user', user_id=current_user.id, error=str(e))
        return {'eligible': False, 'error': str(e)}


@router.post('/execute')
async def execute_migration_endpoint(
    current_user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Executes the transfer of Lazeika VPN subscription to Invoxy VPN for the authenticated user."""
    email = current_user.email
    if not email and not current_user.telegram_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Для переноса требуется привязанный Email или Telegram',
        )

    try:
        result = await execute_user_migration(
            db,
            current_user,
            email=email,
            telegram_id=current_user.telegram_id,
            source='cabinet',
        )
        return {
            'success': True,
            'result': {
                'tariff_name': result.tariff_name,
                'total_days': result.total_days,
                'device_limit': result.device_limit,
                'end_date_str': result.end_date_str,
                'traffic_limit_gb': result.traffic_limit_gb,
                'whitelist_traffic_limit_gb': result.whitelist_traffic_limit_gb,
                'subscription_url': result.subscription_url,
                'message': result.message,
            },
        }
    except ValueError as val_err:
        logger.warning(
            'Cabinet migration skipped or candidate not found',
            user_id=current_user.id,
            reason=str(val_err),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err),
        )
    except Exception as e:
        logger.error('Migration execution failed', user_id=current_user.id, error=str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Ошибка при выполнении переноса: {e!s}',
        )
