"""Lecturas de placa corregidas por el operador y placas extranjeras en la lista negra.

Revision: 0003
Anterior: 0002
Fecha:    2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('blacklist_plates', schema=None) as batch_op:
        batch_op.add_column(sa.Column('extranjera', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))

    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.add_column(sa.Column('corregido', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
        batch_op.create_index(batch_op.f('ix_events_corregido'), ['corregido'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_events_corregido'))
        batch_op.drop_column('corregido')

    with op.batch_alter_table('blacklist_plates', schema=None) as batch_op:
        batch_op.drop_column('extranjera')
