"""Invoxy VPN client devices and connection events; drop settings of the previous clients."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0132'
down_revision: Union[str, None] = '0131'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    if 'app_devices' not in tables:
        op.create_table(
            'app_devices',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('device_hash', sa.String(length=64), nullable=False, unique=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('platform', sa.String(length=16), nullable=False),
            sa.Column('os_version', sa.String(length=64), nullable=True),
            sa.Column('app_version', sa.String(length=32), nullable=True),
            sa.Column('notification_prefs', sa.JSON(), nullable=True),
            sa.Column('last_connect_ok', sa.Boolean(), nullable=True),
            sa.Column('last_error_code', sa.String(length=40), nullable=True),
            sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index('ix_app_devices_user_id', 'app_devices', ['user_id'])
        op.create_index('ix_app_devices_platform', 'app_devices', ['platform'])
        op.create_index('ix_app_devices_last_seen_at', 'app_devices', ['last_seen_at'])
    if 'app_device_events' not in tables:
        op.create_table(
            'app_device_events',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('device_id', sa.Integer(), sa.ForeignKey('app_devices.id', ondelete='CASCADE'), nullable=False),
            sa.Column('kind', sa.String(length=16), nullable=False),
            sa.Column('ok', sa.Boolean(), nullable=False),
            sa.Column('error_code', sa.String(length=40), nullable=True),
            sa.Column('protocol', sa.String(length=40), nullable=True),
            sa.Column('network', sa.String(length=16), nullable=True),
            sa.Column('restricted', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index('ix_app_device_events_device_id', 'app_device_events', ['device_id'])
        op.create_index('ix_app_device_events_created_at', 'app_device_events', ['created_at'])


def downgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    if 'app_device_events' in tables:
        op.drop_table('app_device_events')
    if 'app_devices' in tables:
        op.drop_table('app_devices')
