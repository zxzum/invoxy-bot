from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.cabinet.routes import auth as auth_routes, websocket as cabinet_websocket
from app.config import Settings
from app.services import web_auth_service
from app.webapi.routes import websocket as webapi_websocket
from app.webapi.routes.websocket import (
    WEBSOCKET_AUTH_SUBPROTOCOL,
    _extract_websocket_auth,
)


class InMemoryCache:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self.expiries: dict[str, int | None] = {}

    async def getdel(self, key: str) -> Any | None:
        raw = self._store.pop(key, None)
        if raw is None:
            return None
        return json.loads(raw)

    async def set(self, key: str, value: Any, expire: int | None = None) -> bool:
        self._store[key] = json.dumps(value)
        self.expiries[key] = expire
        return True


@pytest.fixture
def fake_cache(monkeypatch: pytest.MonkeyPatch) -> InMemoryCache:
    cache = InMemoryCache()
    monkeypatch.setattr(web_auth_service, 'cache', cache)
    return cache


def _request() -> Request:
    return Request(
        {
            'type': 'http',
            'method': 'POST',
            'path': '/cabinet/auth/ws-ticket',
            'headers': [],
            'query_string': b'',
            'client': ('127.0.0.1', 1234),
        }
    )


@pytest.mark.asyncio
async def test_cabinet_ws_ticket_endpoint_contract_and_single_use(
    fake_cache: InMemoryCache,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_routes.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))

    response = await auth_routes.create_ws_ticket(_request(), SimpleNamespace(id=42))

    assert response.expires_in == 45
    assert len(response.ticket) >= 16
    assert fake_cache.expiries[next(iter(fake_cache.expiries))] == 45

    data = await web_auth_service.consume_cabinet_ws_ticket(response.ticket)
    assert data is not None
    assert data['purpose'] == 'cabinet_ws'
    assert data['user_id'] == 42
    assert await web_auth_service.consume_cabinet_ws_ticket(response.ticket) is None


def test_webapi_ws_auth_prefers_header_and_supports_subprotocol() -> None:
    header_ws = SimpleNamespace(
        headers={'authorization': 'Bearer header-key'},
        query_params={'token': 'legacy-key'},
    )
    assert _extract_websocket_auth(header_ws) == ('header-key', None, 'authorization')

    subprotocol_ws = SimpleNamespace(
        headers={'sec-websocket-protocol': f'{WEBSOCKET_AUTH_SUBPROTOCOL}, protocol-key'},
        query_params={},
    )
    assert _extract_websocket_auth(subprotocol_ws) == (
        'protocol-key',
        WEBSOCKET_AUTH_SUBPROTOCOL,
        'subprotocol',
    )

    query_ws = SimpleNamespace(
        headers={},
        query_params={'token': 'legacy-key', 'api_key': 'legacy-key-2'},
    )
    assert _extract_websocket_auth(query_ws) == (None, None, 'none')


def test_cabinet_ws_accepts_one_time_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_verify(ticket: str) -> tuple[int, bool]:
        assert ticket == 'one-time-ticket'
        return 42, False

    monkeypatch.setattr(cabinet_websocket, 'verify_cabinet_ws_ticket', fake_verify)
    app = FastAPI()
    app.include_router(cabinet_websocket.router, prefix='/cabinet')

    with TestClient(app) as client:
        with client.websocket_connect('/cabinet/ws?ticket=one-time-ticket') as websocket:
            assert websocket.receive_json() == {'type': 'connected', 'user_id': 42, 'is_admin': False}


def test_cabinet_ws_rejects_query_token_without_ticket() -> None:
    from starlette.websockets import WebSocketDisconnect

    app = FastAPI()
    app.include_router(cabinet_websocket.router, prefix='/cabinet')

    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect('/cabinet/ws?token=legacy-token') as websocket:
                websocket.receive_json()
        assert exc_info.value.code == 1008


def test_webapi_ws_accepts_authorized_subprotocol(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_verify(websocket, token: str | None = None) -> bool:
        assert token == 'protocol-key'
        return True

    monkeypatch.setattr(webapi_websocket, 'verify_websocket_token', fake_verify)
    app = FastAPI()
    app.include_router(webapi_websocket.router, prefix='/api')

    with TestClient(app) as client:
        with client.websocket_connect(
            '/api/ws',
            subprotocols=[WEBSOCKET_AUTH_SUBPROTOCOL, 'protocol-key'],
        ) as websocket:
            assert websocket.accepted_subprotocol == WEBSOCKET_AUTH_SUBPROTOCOL
            assert websocket.receive_json()['status'] == 'connected'


def test_production_cors_wildcard_fails_closed_but_debug_keeps_compatibility() -> None:
    production = Settings(
        BOT_TOKEN='123:test',
        DEBUG=False,
        WEB_API_ENABLED=True,
        WEB_API_ALLOWED_ORIGINS='*',
        CABINET_ALLOWED_ORIGINS='*',
    )
    assert production.get_web_api_allowed_origins() == []
    assert production.get_cabinet_allowed_origins() == []

    debug = Settings(
        BOT_TOKEN='123:test',
        DEBUG=True,
        WEB_API_ENABLED=True,
        WEB_API_ALLOWED_ORIGINS='*',
        CABINET_ALLOWED_ORIGINS='*',
    )
    assert debug.get_web_api_allowed_origins() == ['*']
    assert debug.get_cabinet_allowed_origins() == ['*']
