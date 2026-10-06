"""Invoxy VPN client devices: the model, the last address and the administrator's sign-out mark."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0133'
down_revision: Union[str, None] = '0132'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ('device_model', sa.String(length=96)),
    ('last_ip', sa.String(length=64)),
    ('revoked_at', sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if 'app_devices' not in inspector.get_table_names():
        return
    existing = {column['name'] for column in inspector.get_columns('app_devices')}
    for name, kind in _COLUMNS:
        if name not in existing:
            op.add_column('app_devices', sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if 'app_devices' not in inspector.get_table_names():
        return
    existing = {column['name'] for column in inspector.get_columns('app_devices')}
    for name, _ in reversed(_COLUMNS):
        if name in existing:
            op.drop_column('app_devices', name)
