"""Track regular and whitelist traffic top-ups independently."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0128'
down_revision: Union[str, None] = '0127'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'subscriptions' not in inspector.get_table_names():
        return

    columns = {column['name'] for column in inspector.get_columns('subscriptions')}
    if 'whitelist_traffic_topup_last_purchased_at' not in columns:
        op.add_column(
            'subscriptions',
            sa.Column('whitelist_traffic_topup_last_purchased_at', sa.DateTime(timezone=True), nullable=True),
        )

    op.execute(
        sa.text(
            """
            UPDATE subscriptions
            SET traffic_topup_last_purchased_at = (
                    SELECT MAX(created_at) FROM traffic_purchases
                    WHERE traffic_purchases.subscription_id = subscriptions.id
                ),
                whitelist_traffic_topup_last_purchased_at = (
                    SELECT MAX(created_at) FROM whitelist_traffic_purchases
                    WHERE whitelist_traffic_purchases.subscription_id = subscriptions.id
                )
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'subscriptions' not in inspector.get_table_names():
        return
    columns = {column['name'] for column in inspector.get_columns('subscriptions')}
    if 'whitelist_traffic_topup_last_purchased_at' not in columns:
        return

    op.execute(
        sa.text(
            """
            UPDATE subscriptions
            SET traffic_topup_last_purchased_at = CASE
                WHEN traffic_topup_last_purchased_at IS NULL
                    THEN whitelist_traffic_topup_last_purchased_at
                WHEN whitelist_traffic_topup_last_purchased_at > traffic_topup_last_purchased_at
                    THEN whitelist_traffic_topup_last_purchased_at
                ELSE traffic_topup_last_purchased_at
            END
            """
        )
    )
    with op.batch_alter_table('subscriptions') as batch:
        batch.drop_column('whitelist_traffic_topup_last_purchased_at')
