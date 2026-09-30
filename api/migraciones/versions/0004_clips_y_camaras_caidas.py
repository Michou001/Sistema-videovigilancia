"""Clip de video de cada alerta y estado de camara caida.

Revision: 0004
Anterior: 0003
Fecha:    2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('clip_path', sa.String(), nullable=True))

    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.add_column(sa.Column('caida_desde', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.drop_column('caida_desde')

    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.drop_column('clip_path')
