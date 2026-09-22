import pytest

from app.config import settings
from app.external.remnawave_api import RemnaWaveAPI


@pytest.mark.asyncio
async def test_insecure_local_remnawave_tls_is_debug_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'REMNAWAVE_ALLOW_INSECURE_TLS', True)
    monkeypatch.setattr(settings, 'DEBUG', False)

    with pytest.raises(RuntimeError, match='requires DEBUG=true'):
        await RemnaWaveAPI('https://remnawave', 'api-key').__aenter__()


@pytest.mark.asyncio
async def test_external_remnawave_requires_https() -> None:
    with pytest.raises(RuntimeError, match='Insecure RemnaWave connection: external URL must use HTTPS'):
        await RemnaWaveAPI('http://external-panel.example.com', 'api-key').__aenter__()

