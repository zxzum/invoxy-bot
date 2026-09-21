import pytest

from app.config import Settings


def test_cabinet_jwt_secret_fails_closed_outside_debug() -> None:
    settings = Settings(BOT_TOKEN='123:test', CABINET_ENABLED=True, DEBUG=False)

    with pytest.raises(RuntimeError, match='CABINET_JWT_SECRET must be configured'):
        settings.get_cabinet_jwt_secret()


def test_cabinet_jwt_secret_fallback_is_dev_only() -> None:
    settings = Settings(BOT_TOKEN='123:test', CABINET_ENABLED=True, DEBUG=True)

    with pytest.warns(UserWarning, match='falling back to BOT_TOKEN'):
        assert settings.get_cabinet_jwt_secret() == '123:test'
