"""Invoxy VPN client analytics: anonymous usage events and the client's own error reports."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0134'
# The head is the renumbered upstream chain, which already sits on top of 0133.
down_revision: Union[str, None] = 'invoxy_upstream_0131'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if 'app_events' not in tables:
        op.create_table(
            'app_events',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('device_id', sa.Integer(), sa.ForeignKey('app_devices.id', ondelete='CASCADE'), nullable=False),
            sa.Column('session', sa.String(length=24), nullable=True),
            sa.Column('name', sa.String(length=40), nullable=False),
            sa.Column('props', sa.JSON(), nullable=True),
            sa.Column('app_version', sa.String(length=32), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index('ix_app_events_device_id', 'app_events', ['device_id'])
        op.create_index('ix_app_events_created_at', 'app_events', ['created_at'])
        op.create_index('ix_app_events_name_created', 'app_events', ['name', 'created_at'])
    if 'app_errors' not in tables:
        op.create_table(
            'app_errors',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('fingerprint', sa.String(length=32), nullable=False),
            sa.Column('app_version', sa.String(length=32), nullable=False, server_default=''),
            sa.Column('platform', sa.String(length=16), nullable=False, server_default=''),
            sa.Column('kind', sa.String(length=24), nullable=False),
            sa.Column('message', sa.Text(), nullable=False, server_default=''),
            sa.Column('stack', sa.Text(), nullable=True),
            sa.Column('context', sa.JSON(), nullable=True),
            sa.Column('count', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('devices', sa.Integer(), nullable=False, server_default='1'),
            sa.Column(
                'last_device_id', sa.Integer(), sa.ForeignKey('app_devices.id', ondelete='SET NULL'), nullable=True
            ),
            sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('last_alert_at', sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint('fingerprint', 'app_version', 'platform', name='uq_app_errors_fingerprint'),
        )
        op.create_index('ix_app_errors_fingerprint', 'app_errors', ['fingerprint'])
        op.create_index('ix_app_errors_last_seen_at', 'app_errors', ['last_seen_at'])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if 'app_errors' in tables:
        op.drop_table('app_errors')
    if 'app_events' in tables:
        op.drop_table('app_events')
