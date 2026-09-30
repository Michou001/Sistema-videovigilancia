"""Zonas y reglas por camara: intrusion, cruce de linea, merodeo y conteo.

Revision: 0005
Anterior: 0004
Fecha:    2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'zones',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('camera_id', sa.String(), nullable=False),
        sa.Column('nombre', sa.String(length=80), nullable=False),
        sa.Column('tipo', sa.String(), nullable=False),
        sa.Column('puntos_json', sa.String(), nullable=False),
        sa.Column('clases', sa.String(), nullable=False),
        sa.Column('direccion', sa.String(), nullable=False),
        sa.Column('segundos', sa.Integer(), nullable=True),
        sa.Column('horario_json', sa.String(), nullable=True),
        sa.Column('severidad', sa.String(), nullable=False),
        sa.Column('activa', sa.Boolean(), nullable=False),
        sa.Column('creada_por', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['camera_id'], ['cameras.camera_id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('zones', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_zones_camera_id'), ['camera_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('zones', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_zones_camera_id'))
    op.drop_table('zones')
