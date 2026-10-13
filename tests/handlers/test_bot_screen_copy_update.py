import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


spec = importlib.util.spec_from_file_location(
    'screen_copy_update', Path(__file__).resolve().parents[3] / 'deploy/update_bot_screen_copy.py'
)
copy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(copy)


def records():
    return [
        SimpleNamespace(
            id=idx,
            name=name,
            description=f'old-{idx}',
            is_active=idx != 5,
            is_trial_available=idx == 5,
            is_daily=False,
            trial_duration_days=2,
            period_prices={} if idx == 5 else {'30': 10000},
            traffic_limit_gb=300,
            whitelist_traffic_limit_gb=5 if idx == 5 else 0,
            device_limit=5,
        )
        for idx, name in enumerate(copy.DESCRIPTIONS, 2)
    ]


def database(tariffs, welcome):
    return SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=tariffs)))),
                MagicMock(scalar_one_or_none=MagicMock(return_value=welcome)),
            ]
        ),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )


async def test_dry_run_changes_nothing(monkeypatch, tmp_path):
    tariffs = records()
    welcome = SimpleNamespace(id=9, text_content='old welcome')
    db = database(tariffs, welcome)
    monkeypatch.setattr(copy, 'get_setting_value', AsyncMock(return_value='old support'))
    assert await copy.update_copy(db, apply=False, dynamic_welcome=False, backup_dir=tmp_path) is None
    assert welcome.text_content == 'old welcome' and tariffs[0].description == 'old-2'
    db.commit.assert_not_called()
    db.rollback.assert_awaited_once()
    assert not list(tmp_path.iterdir())


async def test_apply_snapshots_before_commit_and_changes_only_copy(monkeypatch, tmp_path):
    tariffs = records()
    welcome = SimpleNamespace(id=9, text_content='old welcome')
    db = database(tariffs, welcome)
    monkeypatch.setattr(copy, 'get_setting_value', AsyncMock(return_value='old support'))
    setter = AsyncMock()
    monkeypatch.setattr(copy, 'upsert_system_setting', setter)

    async def check_backup_before_commit():
        paths = list(tmp_path.iterdir())
        assert len(paths) == 1
        backup = json.loads(paths[0].read_text())
        assert backup['welcome']['text_content'] == 'old welcome'
        assert backup['tariffs'][0]['description'] == 'old-2'
        assert backup['support_info_text_ru'] == 'old support'

    db.commit.side_effect = check_backup_before_commit
    path = await copy.update_copy(db, apply=True, dynamic_welcome=False, backup_dir=tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert tariffs[0].traffic_limit_gb == 300 and tariffs[0].period_prices == {'30': 10000}
    assert tariffs[0].device_limit == 5 and tariffs[0].description == copy.BASIC_DESC
    assert '300 ГБ' in welcome.text_content and '{tariffs}' not in welcome.text_content
    setter.assert_awaited_once_with(db, 'SUPPORT_INFO_TEXT_RU', copy.SUPPORT_INFO)
    db.commit.assert_awaited_once()


async def test_backup_failure_prevents_writes(monkeypatch, tmp_path):
    tariffs = records()
    welcome = SimpleNamespace(id=9, text_content='old welcome')
    db = database(tariffs, welcome)
    monkeypatch.setattr(copy, 'get_setting_value', AsyncMock(return_value=None))
    monkeypatch.setattr(copy, 'save_snapshot', MagicMock(side_effect=OSError('disk full')))
    setter = AsyncMock()
    monkeypatch.setattr(copy, 'upsert_system_setting', setter)
    with pytest.raises(OSError, match='disk full'):
        await copy.update_copy(db, apply=True, dynamic_welcome=False, backup_dir=tmp_path)
    assert welcome.text_content == 'old welcome' and tariffs[0].description == 'old-2'
    db.commit.assert_not_called()
    setter.assert_not_called()


async def test_dynamic_welcome_refuses_old_bot(monkeypatch, tmp_path):
    monkeypatch.setattr(copy, 'get_available_placeholders', lambda: {'{user_name}': 'name'})
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
    with pytest.raises(RuntimeError, match='Deploy dynamic welcome'):
        await copy.update_copy(db, apply=True, dynamic_welcome=True, backup_dir=tmp_path)
    db.execute.assert_not_called()
    db.commit.assert_not_called()
