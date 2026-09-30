"""Vectores de las capturas para la busqueda en lenguaje natural.

Revision: 0006
Anterior: 0005
Fecha:    2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'semantic_embeddings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('event_id', sa.String(), nullable=False),
        sa.Column('vector', sa.LargeBinary(), nullable=False),
        sa.Column('dim', sa.Integer(), nullable=False),
        sa.Column('modelo', sa.String(length=120), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['event_id'], ['events.event_id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('semantic_embeddings', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_semantic_embeddings_event_id'), ['event_id'], unique=True)


def downgrade() -> None:
    with op.batch_alter_table('semantic_embeddings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_semantic_embeddings_event_id'))
    op.drop_table('semantic_embeddings')
