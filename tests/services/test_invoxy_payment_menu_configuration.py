import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.database.models import Base, PaymentMethodConfig, SystemSetting
from tests.fixtures.sqlite_memory import memory_session


def _script():
    path = Path(__file__).resolve().parents[3] / 'deploy' / 'configure_payment_menu.py'
    spec = importlib.util.spec_from_file_location('invoxy_payment_menu', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_menu_configuration_preserves_options_and_requires_backup(monkeypatch, tmp_path):
    module = _script()
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        configs = [
            PaymentMethodConfig(
                method_id=method,
                display_name='Before',
                sort_order=9,
                sub_options={'sbp': True, 'crypto': False},
                min_amount_kopeks=1234,
            )
            for method in (*module.NAMES, 'yookassa')
        ]
        db.add_all(configs)
        db.add(SystemSetting(key='PLATEGA_INLINE_METHODS', value='true'))
        db.add(SystemSetting(key='ROLLYPAY_API_KEY', value='secret-not-for-backup'))
        await db.commit()
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=db)
        context.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(module, 'AsyncSessionLocal', lambda: context)

        await module.configure()
        assert configs[0].display_name == 'Before'
        with pytest.raises(RuntimeError, match='--backup'):
            await module.configure(apply=True)

        backup = tmp_path / 'before.json'
        await module.configure(apply=True, backup=backup)
        snapshot = json.loads(backup.read_text())
        assert snapshot['settings'] == {'PLATEGA_INLINE_METHODS': 'true'}
        assert 'secret-not-for-backup' not in backup.read_text()
        assert backup.stat().st_mode & 0o777 == 0o600
        for order, config in enumerate(configs[:3]):
            assert (config.display_name, config.sort_order, config.is_enabled) == (
                module.NAMES[config.method_id],
                order,
                True,
            )
            assert config.sub_options == {'sbp': True, 'crypto': False}
            assert config.min_amount_kopeks == 1234
        assert configs[3].is_enabled is False
        assert (
            await db.execute(select(SystemSetting.value).where(SystemSetting.key == 'PLATEGA_INLINE_METHODS'))
        ).scalar_one() == 'false'
        with pytest.raises(FileExistsError):
            await module.configure(apply=True, backup=backup)
