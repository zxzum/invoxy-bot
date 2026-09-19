"""Add traffic reset and monthly limit fields to tariffs and subscriptions.

Revision ID: 0130
Revises: 0129
Create Date: 2026-09-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0130'
down_revision: Union[str, None] = '0129'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if 'tariffs' in table_names:
        columns = {c['name'] for c in inspector.get_columns('tariffs')}
        with op.batch_alter_table('tariffs') as batch:
            if 'traffic_topup_max_per_month' not in columns:
                batch.add_column(
                    sa.Column(
                        'traffic_topup_max_per_month',
                        sa.Integer(),
                        server_default='0',
                        nullable=False,
                    )
                )
            if 'whitelist_reset_enabled' not in columns:
                batch.add_column(
                    sa.Column(
                        'whitelist_reset_enabled',
                        sa.Boolean(),
                        server_default='false',
                        nullable=False,
                    )
                )
            if 'whitelist_reset_chunk_gb' not in columns:
                batch.add_column(
                    sa.Column(
                        'whitelist_reset_chunk_gb',
                        sa.Integer(),
                        server_default='50',
                        nullable=False,
                    )
                )
            if 'whitelist_reset_price_kopeks' not in columns:
                batch.add_column(
                    sa.Column(
                        'whitelist_reset_price_kopeks',
                        sa.Integer(),
                        server_default='15000',
                        nullable=False,
                    )
                )
            if 'whitelist_reset_min_used_gb' not in columns:
                batch.add_column(
                    sa.Column(
                        'whitelist_reset_min_used_gb',
                        sa.Integer(),
                        server_default='10',
                        nullable=False,
                    )
                )
            if 'whitelist_reset_max_per_month' not in columns:
                batch.add_column(
                    sa.Column(
                        'whitelist_reset_max_per_month',
                        sa.Integer(),
                        server_default='0',
                        nullable=False,
                    )
                )

    if 'subscriptions' in table_names:
        sub_columns = {c['name'] for c in inspector.get_columns('subscriptions')}
        with op.batch_alter_table('subscriptions') as batch:
            if 'whitelist_reset_period_key' not in sub_columns:
                batch.add_column(
                    sa.Column(
                        'whitelist_reset_period_key',
                        sa.String(length=7),
                        nullable=True,
                    )
                )
            if 'whitelist_reset_count' not in sub_columns:
                batch.add_column(
                    sa.Column(
                        'whitelist_reset_count',
                        sa.Integer(),
                        server_default='0',
                        nullable=False,
                    )
                )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if 'subscriptions' in table_names:
        sub_columns = {c['name'] for c in inspector.get_columns('subscriptions')}
        with op.batch_alter_table('subscriptions') as batch:
            if 'whitelist_reset_count' in sub_columns:
                batch.drop_column('whitelist_reset_count')
            if 'whitelist_reset_period_key' in sub_columns:
                batch.drop_column('whitelist_reset_period_key')

    if 'tariffs' in table_names:
        columns = {c['name'] for c in inspector.get_columns('tariffs')}
        with op.batch_alter_table('tariffs') as batch:
            if 'whitelist_reset_max_per_month' in columns:
                batch.drop_column('whitelist_reset_max_per_month')
            if 'whitelist_reset_min_used_gb' in columns:
                batch.drop_column('whitelist_reset_min_used_gb')
            if 'whitelist_reset_price_kopeks' in columns:
                batch.drop_column('whitelist_reset_price_kopeks')
            if 'whitelist_reset_chunk_gb' in columns:
                batch.drop_column('whitelist_reset_chunk_gb')
            if 'whitelist_reset_enabled' in columns:
                batch.drop_column('whitelist_reset_enabled')
            if 'traffic_topup_max_per_month' in columns:
                batch.drop_column('traffic_topup_max_per_month')
