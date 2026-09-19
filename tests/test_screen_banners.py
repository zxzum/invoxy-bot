"""Tests for the per-screen banner resolver (INVOXY).

Handlers must only name a screen ("main", "subscription", "referral",
"paid_successful") — filesystem paths stay centralized here so a future
upstream merge touches at most the one-line call site.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from app.utils import screen_banners
from app.utils.screen_banners import get_screen_banner


def _write_png(path: Path, size: tuple[int, int] = (256, 256)) -> Path:
    Image.new('RGBA', size, color=(0, 255, 0, 255)).save(path, format='PNG')
    return path


@pytest.fixture
def banners_dir(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / 'banners'
    d.mkdir()
    monkeypatch.setattr(screen_banners, '_BANNERS_DIR', d)
    monkeypatch.setattr(screen_banners, '_file_id_cache', {})
    return d


def test_unknown_kind_returns_none(banners_dir: Path) -> None:
    assert get_screen_banner('nope') is None


def test_missing_file_returns_none(banners_dir: Path) -> None:
    assert get_screen_banner('main') is None


def test_known_kind_returns_fs_input_file(banners_dir: Path) -> None:
    src = _write_png(banners_dir / 'main.png')
    media = get_screen_banner('main')
    assert media is not None
    assert Path(str(media.path)).resolve() == src.resolve()


def test_oversized_banner_is_resized(banners_dir: Path) -> None:
    _write_png(banners_dir / 'ref.png', (1672, 941))
    media = get_screen_banner('referral')
    assert media is not None
    served = Path(str(media.path))
    assert served.exists()
    with Image.open(served) as img:
        assert max(img.size) <= 1280


def test_kind_mapping_points_at_expected_files(banners_dir: Path) -> None:
    _write_png(banners_dir / 'sub-banner.png')
    _write_png(banners_dir / 'paid_successful.png')
    sub = get_screen_banner('subscription')
    paid = get_screen_banner('paid_successful')
    assert Path(str(sub.path)).name == 'sub-banner.png'
    assert Path(str(paid.path)).name == 'paid_successful.png'
