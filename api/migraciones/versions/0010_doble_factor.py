"""Verificacion en dos pasos (TOTP) para los usuarios del dashboard.

Revision: 0010
Anterior: 0009
Fecha:    2026-10-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('operators', schema=None) as batch_op:
        batch_op.add_column(sa.Column('totp_secreto', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('totp_activo', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
        batch_op.add_column(sa.Column('totp_ultimo_paso', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('totp_respaldo_json', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('operators', schema=None) as batch_op:
        batch_op.drop_column('totp_respaldo_json')
        batch_op.drop_column('totp_ultimo_paso')
        batch_op.drop_column('totp_activo')
        batch_op.drop_column('totp_secreto')
