"""Cabinet and Admin routes for in-app banners."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.services.app_banner_service import (
    create_app_banner,
    delete_app_banner,
    get_app_banners,
    update_app_banner,
)

from ..dependencies import get_cabinet_db, require_permission


router = APIRouter(prefix='/app/banners', tags=['App Banners'])
admin_router = APIRouter(prefix='/admin/app-banners', tags=['Admin App Banners'])


# ============ Schemas ============


class AppBannerResponse(BaseModel):
    id: str
    title: str
    text: str = ''
    type: str = 'info'
    action_url: str | None = None
    icon: str | None = None
    is_active: bool = True
    sort_order: int = 0


class AppBannerCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    text: str = Field('', max_length=500)
    type: str = Field('info', max_length=32)
    action_url: str | None = Field(None, max_length=500)
    icon: str | None = Field(None, max_length=100)
    is_active: bool = True
    sort_order: int = 0


class AppBannerUpdateRequest(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=120)
    text: str | None = Field(None, max_length=500)
    type: str | None = Field(None, max_length=32)
    action_url: str | None = Field(None, max_length=500)
    icon: str | None = Field(None, max_length=100)
    is_active: bool | None = None
    sort_order: int | None = None


# ============ Public / User Routes ============


@router.get('', response_model=list[AppBannerResponse])
async def list_active_app_banners(
    db: AsyncSession = Depends(get_cabinet_db),
) -> list[dict[str, Any]]:
    """List active in-app banners for the mobile and desktop clients."""
    return await get_app_banners(db, only_active=True)


# ============ Admin Routes ============


@admin_router.get('', response_model=list[AppBannerResponse])
async def list_all_app_banners(
    admin: User = Depends(require_permission('settings:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> list[dict[str, Any]]:
    """List all in-app banners for administration."""
    return await get_app_banners(db, only_active=False)


@admin_router.post('', response_model=AppBannerResponse)
async def create_new_app_banner(
    request: AppBannerCreateRequest,
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Create a new in-app banner."""
    return await create_app_banner(db, request.model_dump())


@admin_router.put('/{banner_id}', response_model=AppBannerResponse)
async def update_existing_app_banner(
    banner_id: str,
    request: AppBannerUpdateRequest,
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Update an existing in-app banner."""
    data = {k: v for k, v in request.model_dump().items() if v is not None}
    banner = await update_app_banner(db, banner_id, data)
    if not banner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Banner not found')
    return banner


@admin_router.delete('/{banner_id}')
async def delete_existing_app_banner(
    banner_id: str,
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, bool]:
    """Delete an in-app banner."""
    deleted = await delete_app_banner(db, banner_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Banner not found')
    return {'success': True}
