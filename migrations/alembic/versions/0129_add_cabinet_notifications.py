"""Add cabinet_notifications table for in-app notifications history."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0129'
down_revision: Union[str, None] = '0128'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'cabinet_notifications' not in inspector.get_table_names():
        op.create_table(
            'cabinet_notifications',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column('type', sa.String(length=50), nullable=False),
            sa.Column('title', sa.String(length=255), nullable=False),
            sa.Column('body', sa.Text(), nullable=False),
            sa.Column('payload_json', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index('ix_cabinet_notifications_user_id', 'cabinet_notifications', ['user_id'])
        op.create_index('ix_cabinet_notifications_created_at', 'cabinet_notifications', ['created_at'])
        op.create_index('ix_cabinet_notifications_type', 'cabinet_notifications', ['type'])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'cabinet_notifications' in inspector.get_table_names():
        op.drop_index('ix_cabinet_notifications_type', table_name='cabinet_notifications')
        op.drop_index('ix_cabinet_notifications_created_at', table_name='cabinet_notifications')
        op.drop_index('ix_cabinet_notifications_user_id', table_name='cabinet_notifications')
        op.drop_table('cabinet_notifications')
