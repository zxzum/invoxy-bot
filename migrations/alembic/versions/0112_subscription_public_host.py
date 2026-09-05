"""Move stored subscription links to the Invoxy public host."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0112'
down_revision: Union[str, None] = '0111'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_HOST = 'https://sub.vascogama.pt'
NEW_HOST = 'https://sub.lazeika.xyz'
TABLES = ('subscriptions', 'traffic_purchases', 'guest_purchases')


def _rewrite_host(old_host: str, new_host: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    for table_name in TABLES:
        if table_name not in tables:
            continue
        columns = {column['name'] for column in inspector.get_columns(table_name)}
        if 'subscription_url' not in columns:
            continue

        table = sa.table(table_name, sa.column('subscription_url', sa.Text()))
        replacement = sa.func.replace(table.c.subscription_url, old_host, new_host)
        if new_host == NEW_HOST:
            replacement = sa.func.replace(replacement, f'{new_host}/sub/', f'{new_host}/')
        op.execute(
            table.update()
            .where(table.c.subscription_url.like(f'{old_host}%'))
            .values(subscription_url=replacement)
        )


def upgrade() -> None:
    _rewrite_host(OLD_HOST, NEW_HOST)


def downgrade() -> None:
    _rewrite_host(NEW_HOST, OLD_HOST)
