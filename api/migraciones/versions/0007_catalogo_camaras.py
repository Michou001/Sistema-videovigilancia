"""Ficha de catalogo de cada camara: funcion e identificacion.

Revision: 0007
Anterior: 0006
Fecha:    2026-10-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.add_column(sa.Column('funcion', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('ficha_json', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.drop_column('ficha_json')
        batch_op.drop_column('funcion')
