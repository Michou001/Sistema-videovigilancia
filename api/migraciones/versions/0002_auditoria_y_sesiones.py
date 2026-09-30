"""Bitacora de auditoria, control de sesiones por usuario y coordenadas de camara.

Revision: 0002
Anterior: 0001
Fecha:    2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('audit_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('usuario', sa.String(), nullable=True),
    sa.Column('accion', sa.String(), nullable=False),
    sa.Column('objetivo', sa.String(), nullable=True),
    sa.Column('detalle', sa.String(), nullable=True),
    sa.Column('ip', sa.String(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audit_log_accion'), ['accion'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_log_ts'), ['ts'], unique=False)
        batch_op.create_index('ix_audit_log_ts_accion', ['ts', 'accion'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_log_usuario'), ['usuario'], unique=False)

    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.add_column(sa.Column('lat', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('lon', sa.Float(), nullable=True))

    with op.batch_alter_table('operators', schema=None) as batch_op:
        # server_default: los usuarios que ya existen empiezan en la version 0,
        # la misma que llevan los tokens que ya tienen emitidos.
        batch_op.add_column(sa.Column('token_version', sa.Integer(), nullable=False,
                                      server_default='0'))
        batch_op.add_column(sa.Column('last_login', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('operators', schema=None) as batch_op:
        batch_op.drop_column('last_login')
        batch_op.drop_column('token_version')

    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.drop_column('lon')
        batch_op.drop_column('lat')

    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_log_usuario'))
        batch_op.drop_index('ix_audit_log_ts_accion')
        batch_op.drop_index(batch_op.f('ix_audit_log_ts'))
        batch_op.drop_index(batch_op.f('ix_audit_log_accion'))

    op.drop_table('audit_log')
