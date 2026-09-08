"""Remember the last paid traffic top-up per subscription."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0127'
down_revision: Union[str, None] = '0126'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if 'subscriptions' not in tables:
        return

    columns = {column['name'] for column in inspector.get_columns('subscriptions')}
    if 'traffic_topup_last_purchased_at' not in columns:
        op.add_column(
            'subscriptions',
            sa.Column('traffic_topup_last_purchased_at', sa.DateTime(timezone=True), nullable=True),
        )

    if 'traffic_purchases' in tables:
        op.execute(
            sa.text(
                """
                UPDATE subscriptions
                SET traffic_topup_last_purchased_at = (
                    SELECT MAX(created_at)
                    FROM traffic_purchases
                    WHERE traffic_purchases.subscription_id = subscriptions.id
                )
                WHERE EXISTS (
                    SELECT 1
                    FROM traffic_purchases
                    WHERE traffic_purchases.subscription_id = subscriptions.id
                )
                """
            )
        )

    if 'whitelist_traffic_purchases' in tables:
        op.execute(
            sa.text(
                """
                UPDATE subscriptions
                SET traffic_topup_last_purchased_at = CASE
                    WHEN traffic_topup_last_purchased_at IS NULL THEN (
                        SELECT MAX(created_at)
                        FROM whitelist_traffic_purchases
                        WHERE whitelist_traffic_purchases.subscription_id = subscriptions.id
                    )
                    WHEN (
                        SELECT MAX(created_at)
                        FROM whitelist_traffic_purchases
                        WHERE whitelist_traffic_purchases.subscription_id = subscriptions.id
                    ) > traffic_topup_last_purchased_at THEN (
                        SELECT MAX(created_at)
                        FROM whitelist_traffic_purchases
                        WHERE whitelist_traffic_purchases.subscription_id = subscriptions.id
                    )
                    ELSE traffic_topup_last_purchased_at
                END
                WHERE EXISTS (
                    SELECT 1
                    FROM whitelist_traffic_purchases
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
    if 'traffic_topup_last_purchased_at' in columns:
        with op.batch_alter_table('subscriptions') as batch:
            batch.drop_column('traffic_topup_last_purchased_at')
