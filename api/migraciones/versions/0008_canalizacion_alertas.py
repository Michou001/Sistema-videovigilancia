"""Canalizacion de alertas: a quien se paso el caso y con que folio externo.

Revision: 0008
Anterior: 0007
Fecha:    2026-10-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('canalizaciones_json', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.drop_column('canalizaciones_json')
