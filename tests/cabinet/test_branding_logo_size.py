"""GET /cabinet/branding/logo отдаёт логотип не крупнее иконки манифеста (512 px)."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from fastapi.responses import FileResponse
from PIL import Image

from app.cabinet.routes import branding as branding_routes
from app.cabinet.utils.favicon_tile import LOGO_DISPLAY_MAX_SIDE


def _logo(tmp_path: Path, size: int) -> Path:
    path = tmp_path / 'logo.png'
    # Прозрачные поля, как у настоящего логотипа: альфа должна пережить пересжатие.
    image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    image.paste((165, 232, 196, 255), (size // 4, size // 4, size * 3 // 4, size * 3 // 4))
    image.save(path)
    return path


async def test_large_raster_logo_is_served_downscaled(tmp_path, monkeypatch) -> None:
    path = _logo(tmp_path, 1254)
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: path)

    response = await branding_routes.get_logo()

    assert response.media_type == 'image/webp'
    with Image.open(BytesIO(response.body)) as image:
        assert image.size == (LOGO_DISPLAY_MAX_SIDE, LOGO_DISPLAY_MAX_SIDE)
        assert image.mode == 'RGBA'
    assert response.headers['x-content-type-options'] == 'nosniff'


async def test_small_logo_is_served_as_is(tmp_path, monkeypatch) -> None:
    path = _logo(tmp_path, 192)
    monkeypatch.setattr(branding_routes, 'get_logo_path', lambda: path)

    response = await branding_routes.get_logo()

    assert isinstance(response, FileResponse)
    assert Path(response.path) == path
