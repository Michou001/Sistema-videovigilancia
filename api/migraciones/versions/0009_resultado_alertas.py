"""Resultado de la verificacion de cada alerta (confirmado, falso aviso, ensayo...).

Revision: 0009
Anterior: 0008
Fecha:    2026-10-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('resultado', sa.String(), nullable=True))
        batch_op.create_index(batch_op.f('ix_alerts_resultado'), ['resultado'], unique=False)
    # Lo que ya se habia descartado como falso positivo es un falso aviso.
    op.execute("UPDATE alerts SET resultado = 'falso_aviso' "
               "WHERE status = 'dismissed' AND dismissed_reason = 'falso positivo'")


def downgrade() -> None:
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_alerts_resultado'))
        batch_op.drop_column('resultado')
