"""App sessions stay signed in: a long refresh JWT and a sliding database deadline."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import jwt
import pytest

from app.cabinet.auth import jwt_handler
from app.cabinet.routes import auth as route
from app.cabinet.schemas.auth import RefreshTokenRequest
from app.services import app_client_service as svc


class _Result:
    def __init__(self, record: Any) -> None:
        self.record = record

    def scalar_one_or_none(self) -> Any:
        return self.record


class _Db:
    def __init__(self, record: Any) -> None:
        self.record = record
        self.commits = 0

    async def execute(self, *args: Any, **kwargs: Any) -> _Result:
        return _Result(self.record)

    async def commit(self) -> None:
        self.commits += 1


def _record(device_info: str | None, expires_at: datetime) -> Any:
    record = SimpleNamespace(device_info=device_info, expires_at=expires_at, revoked_at=None)
    record.is_valid = True
    return record


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    monkeypatch.setattr(type(route.settings), 'get_cabinet_jwt_secret', lambda _self: 'test-secret')

    async def user(_db: Any, user_id: int) -> Any:
        return SimpleNamespace(id=user_id, telegram_id=None, status='active')

    async def perms(_db: Any, _user_id: int) -> tuple[list, list, int]:
        return [], [], 0

    saved: list[dict[str, Any]] = []

    async def store(_db: Any, user_id: int, token: str, device_info: str | None = None, expires_at: Any = None) -> None:
        saved.append({'token': token, 'device_info': device_info, 'expires_at': expires_at})

    monkeypatch.setattr(route, 'get_user_by_id', user)
    monkeypatch.setattr(route.UserRoleCRUD, 'get_user_permissions', perms)
    monkeypatch.setattr(route, '_store_refresh_token', store)
    return saved


def _exp(token: str) -> datetime:
    return datetime.fromtimestamp(jwt.decode(token, 'test-secret', algorithms=['HS256'])['exp'], UTC)


async def test_app_refresh_slides_and_swaps_a_short_token_once(stored: list[dict[str, Any]]) -> None:
    short = jwt_handler.create_refresh_token(7)  # issued before app sessions were long (7 days)
    record = _record(svc.app_session_tag('device-1'), datetime.now(UTC) + timedelta(days=1))
    db = _Db(record)

    answer = await route.refresh_token(RefreshTokenRequest(refresh_token=short), db)

    assert record.expires_at > datetime.now(UTC) + timedelta(days=360)
    assert answer.refresh_token != short
    assert _exp(answer.refresh_token) > datetime.now(UTC) + timedelta(days=3000)
    assert stored == [{'token': answer.refresh_token, 'device_info': record.device_info, 'expires_at': record.expires_at}]
    assert record.revoked_at is None  # the old one runs out by itself: a parallel refresh is not signed out

    # The long token is kept as it is from now on; only the deadline moves.
    record.expires_at = datetime.now(UTC) + timedelta(days=1)
    again = await route.refresh_token(RefreshTokenRequest(refresh_token=answer.refresh_token), db)
    assert again.refresh_token == answer.refresh_token
    assert record.expires_at > datetime.now(UTC) + timedelta(days=360)
    assert len(stored) == 1


async def test_cabinet_refresh_keeps_its_own_lifetime(stored: list[dict[str, Any]]) -> None:
    token = jwt_handler.create_refresh_token(7)
    deadline = datetime.now(UTC) + timedelta(days=3)
    record = _record(None, deadline)

    answer = await route.refresh_token(RefreshTokenRequest(refresh_token=token), _Db(record))

    assert answer.refresh_token == token
    assert record.expires_at == deadline
    assert stored == []


def test_app_session_tag_is_recognised() -> None:
    assert svc.is_app_session(svc.app_session_tag('device-1'))
    assert svc.is_app_session(svc.APP_SESSION_PREFIX)
    assert not svc.is_app_session(None)
    assert not svc.is_app_session('invoxy_application')
