"""Плитка фавикона из логотипа: квадрат со скруглёнными углами.

Safari берёт иконку вкладки только по статической ссылке из index.html и смену
через JS не замечает, а скруглённую плитку из логотипа кабинет рисует на canvas
уже после загрузки — Safari оставался с сырым квадратом. Скругляем здесь.
"""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageOps


FAVICON_TILE_SIZE = 256
# Доля стороны под радиус скругления. Меньше, чем у плитки в шапке кабинета (0,3):
# эту иконку видит только Safari, а он в тёмной теме подрисовывает иконке с
# прозрачными углами белую плитку-подложку, если скругление заметное — при 0,16
# стороны и больше подложка есть, при 0,12 нет (Safari 26.6, замерено на живых
# вкладках). Тот же радиус у подсказки первого кадра в кабинете (SAFARI_TILE_RADIUS).
FAVICON_CORNER_RATIO = 0.12
# Что Pillow умеет растеризовать; SVG отдаётся файлом как есть.
_RASTER_SUFFIXES = frozenset({'.png', '.jpg', '.jpeg', '.webp', '.gif'})


def is_raster_logo(logo_path: Path) -> bool:
    return logo_path.suffix.lower() in _RASTER_SUFFIXES


def rounded_logo_tile(
    logo_path: Path,
    size: int = FAVICON_TILE_SIZE,
    corner_ratio: float = FAVICON_CORNER_RATIO,
) -> bytes:
    """PNG ``size``×``size``: логотип вписан по принципу object-fit: cover, углы прозрачные."""
    with Image.open(logo_path) as source:
        tile = ImageOps.fit(source.convert('RGBA'), (size, size), Image.Resampling.LANCZOS)
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1),
        radius=round(size * corner_ratio),
        fill=255,
    )
    tile.putalpha(ImageChops.multiply(tile.getchannel('A'), mask))
    buffer = BytesIO()
    tile.save(buffer, format='PNG', optimize=True)
    return buffer.getvalue()


@lru_cache(maxsize=4)
def _render_cached(logo_path: str, mtime_ns: int, file_size: int) -> bytes:
    # mtime и размер в ключе: новый логотип из админки — новый файл, старая плитка забывается.
    return rounded_logo_tile(Path(logo_path))


def cached_rounded_logo_tile(logo_path: Path) -> bytes:
    """Плитка по файлу логотипа; перерисовывается только когда файл заменили."""
    stat = logo_path.stat()
    return _render_cached(str(logo_path), stat.st_mtime_ns, stat.st_size)


# Самый крупный потребитель логотипа — иконка ярлыка 512×512 в манифесте кабинета.
# Админка принимает файлы до 5 МБ, а исходник в 1254 px весил 1,7 МБ и уходил
# каждому визиту ради фавиконок.
LOGO_DISPLAY_MAX_SIDE = 512


def downscaled_logo(logo_path: Path, max_side: int = LOGO_DISPLAY_MAX_SIDE) -> bytes | None:
    """WebP не крупнее ``max_side`` по большей стороне; ``None``, если файл и так не больше."""
    with Image.open(logo_path) as source:
        if max(source.size) <= max_side:
            return None
        image = source.convert('RGBA')
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buffer = BytesIO()
    image.save(buffer, format='WEBP', quality=90, method=6)
    return buffer.getvalue()


@lru_cache(maxsize=4)
def _downscaled_cached(logo_path: str, mtime_ns: int, file_size: int) -> bytes | None:
    return downscaled_logo(Path(logo_path))


def cached_downscaled_logo(logo_path: Path) -> bytes | None:
    """Уменьшенный логотип по файлу; пересчитывается только когда файл заменили."""
    stat = logo_path.stat()
    return _downscaled_cached(str(logo_path), stat.st_mtime_ns, stat.st_size)
