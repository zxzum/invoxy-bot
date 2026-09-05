"""Add local traffic accounting for WHITELIST nodes.

The new counters are deliberately local. No RemnaWave user or squad is
modified by this feature; the worker only reads node usage and applies deltas
to the bot database.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0111'
down_revision: Union[str, None] = '0110'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    tariff_columns = {column['name'] for column in inspector.get_columns('tariffs')} if 'tariffs' in tables else set()
    for name, column in (
        (
            'whitelist_traffic_limit_gb',
            sa.Column('whitelist_traffic_limit_gb', sa.Integer(), nullable=False, server_default='0'),
        ),
        (
            'whitelist_traffic_topup_enabled',
            sa.Column('whitelist_traffic_topup_enabled', sa.Boolean(), nullable=False, server_default='false'),
        ),
        (
            'whitelist_traffic_topup_packages',
            sa.Column('whitelist_traffic_topup_packages', sa.JSON(), nullable=True),
        ),
    ):
        if 'tariffs' in tables and name not in tariff_columns:
            op.add_column('tariffs', column)

    subscription_columns = (
        ('whitelist_traffic_limit_gb', sa.Column('whitelist_traffic_limit_gb', sa.Integer(), nullable=False, server_default='0')),
        (
            'whitelist_traffic_used_bytes',
            sa.Column('whitelist_traffic_used_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        ),
        (
            'whitelist_traffic_purchased_gb',
            sa.Column('whitelist_traffic_purchased_gb', sa.Integer(), nullable=False, server_default='0'),
        ),
        ('whitelist_traffic_reset_at', sa.Column('whitelist_traffic_reset_at', sa.DateTime(timezone=True), nullable=True)),
    )
    existing_subscription_columns = (
        {column['name'] for column in inspector.get_columns('subscriptions')}
        if 'subscriptions' in tables
        else set()
    )
    for name, column in subscription_columns:
        if 'subscriptions' in tables and name not in existing_subscription_columns:
            op.add_column('subscriptions', column)

    if 'whitelist_traffic_purchases' not in tables:
        op.create_table(
            'whitelist_traffic_purchases',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='CASCADE'), nullable=False),
            sa.Column('traffic_gb', sa.Integer(), nullable=False),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            'ix_whitelist_traffic_purchases_created_at',
            'whitelist_traffic_purchases',
            ['created_at'],
        )
        op.create_index(
            'ix_whitelist_traffic_purchases_sub_expires',
            'whitelist_traffic_purchases',
            ['subscription_id', 'expires_at'],
        )

    if 'whitelist_traffic_usage_snapshots' not in tables:
        op.create_table(
            'whitelist_traffic_usage_snapshots',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='CASCADE'), nullable=False),
            sa.Column('panel_user_id', sa.BigInteger(), nullable=False),
            sa.Column('period_key', sa.String(length=7), nullable=False),
            sa.Column('measured_bytes', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('sampled_at', sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint('subscription_id', 'period_key', name='uq_whitelist_usage_sub_period'),
        )
        op.create_index(
            'ix_whitelist_usage_period_sampled',
            'whitelist_traffic_usage_snapshots',
            ['period_key', 'sampled_at'],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if 'whitelist_traffic_usage_snapshots' in tables:
        op.drop_index('ix_whitelist_usage_period_sampled', table_name='whitelist_traffic_usage_snapshots')
        op.drop_table('whitelist_traffic_usage_snapshots')
    if 'whitelist_traffic_purchases' in tables:
        op.drop_index(
            'ix_whitelist_traffic_purchases_sub_expires',
            table_name='whitelist_traffic_purchases',
        )
        op.drop_index(
            'ix_whitelist_traffic_purchases_created_at',
            table_name='whitelist_traffic_purchases',
        )
        op.drop_table('whitelist_traffic_purchases')

    for table, names in (
        (
            'subscriptions',
            (
                'whitelist_traffic_reset_at',
                'whitelist_traffic_purchased_gb',
                'whitelist_traffic_used_bytes',
                'whitelist_traffic_limit_gb',
            ),
        ),
        (
            'tariffs',
            (
                'whitelist_traffic_topup_packages',
                'whitelist_traffic_topup_enabled',
                'whitelist_traffic_limit_gb',
            ),
        ),
    ):
        if table not in tables:
            continue
        columns = {column['name'] for column in inspector.get_columns(table)}
        for name in names:
            if name in columns:
                op.drop_column(table, name)
