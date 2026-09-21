from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import websocket
from app.cabinet.services import active_invoice
from app.database.crud.ticket_notification import TicketNotificationCRUD
from app.handlers import tickets as user_tickets
from app.handlers.admin import tickets as admin_tickets


class _Db:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def _ticket() -> SimpleNamespace:
    return SimpleNamespace(id=42, user_id=7, title='Connection issue')


@pytest.mark.asyncio
async def test_telegram_admin_reply_reaches_cabinet_user(monkeypatch: pytest.MonkeyPatch) -> None:
    created = AsyncMock(return_value=object())
    record = AsyncMock()
    push = AsyncMock()
    monkeypatch.setattr(TicketNotificationCRUD, 'create_user_notification_for_admin_reply', created)
    monkeypatch.setattr(active_invoice, 'record_cabinet_notification', record)
    monkeypatch.setattr(websocket, 'notify_user_ticket_reply', push)

    db = _Db()
    ticket = _ticket()
    await admin_tickets.notify_cabinet_user_about_ticket_reply(ticket, 'Please reconnect', db)

    created.assert_awaited_once_with(db, ticket, 'Please reconnect')
    record.assert_awaited_once_with(
        db,
        ticket.user_id,
        'ticket_reply',
        'Ответ поддержки по тикету #42',
        'Please reconnect',
        payload_json={'ticket_id': 42},
    )
    push.assert_awaited_once_with(ticket.user_id, ticket.id, 'Please reconnect')
    assert db.commits == 1
    assert db.rollbacks == 0


@pytest.mark.asyncio
async def test_telegram_user_reply_reaches_cabinet_admins(monkeypatch: pytest.MonkeyPatch) -> None:
    created = AsyncMock(return_value=object())
    push = AsyncMock()
    monkeypatch.setattr(TicketNotificationCRUD, 'create_admin_notification_for_user_reply', created)
    monkeypatch.setattr(websocket, 'notify_admins_ticket_reply', push)

    db = _Db()
    ticket = _ticket()
    await user_tickets.notify_cabinet_admins_about_ticket_reply(ticket, 'I still need help', db)

    created.assert_awaited_once_with(db, ticket, 'I still need help')
    push.assert_awaited_once_with(ticket.id, 'I still need help', ticket.user_id)
    assert db.commits == 0
    assert db.rollbacks == 0


@pytest.mark.asyncio
async def test_telegram_ticket_creation_reaches_cabinet_admins(monkeypatch: pytest.MonkeyPatch) -> None:
    created = AsyncMock(return_value=object())
    push = AsyncMock()
    monkeypatch.setattr(TicketNotificationCRUD, 'create_admin_notification_for_new_ticket', created)
    monkeypatch.setattr(websocket, 'notify_admins_new_ticket', push)

    db = _Db()
    ticket = _ticket()
    await user_tickets.notify_cabinet_admins_about_new_ticket(ticket, db)

    created.assert_awaited_once_with(db, ticket)
    push.assert_awaited_once_with(ticket.id, ticket.title, ticket.user_id)
    assert db.commits == 0
    assert db.rollbacks == 0


@pytest.mark.asyncio
async def test_cabinet_bridge_failure_does_not_break_ticket_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        TicketNotificationCRUD,
        'create_user_notification_for_admin_reply',
        AsyncMock(side_effect=RuntimeError('database unavailable')),
    )

    db = _Db()
    await admin_tickets.notify_cabinet_user_about_ticket_reply(_ticket(), 'Reply', db)

    assert db.rollbacks == 1
