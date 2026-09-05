"""Enable squads created unavailable by the legacy RemnaWave sync."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0113'
down_revision: Union[str, None] = '0112'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'server_squads' not in inspector.get_table_names():
        return

    columns = {column['name'] for column in inspector.get_columns('server_squads')}
    if 'is_available' in columns:
        op.execute(
            sa.text('UPDATE server_squads SET is_available = TRUE WHERE is_available = FALSE')
        )


def downgrade() -> None:
    # Availability can be changed by an administrator after this repair;
    # restoring every row to false would overwrite that state.
    pass
