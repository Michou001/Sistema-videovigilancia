"""Esquema base: las tablas tal como existian antes de usar Alembic.

Una base de datos creada por versiones anteriores (con create_all) ya tiene
exactamente esto; init_db() la marca en esta revision sin tocarla y aplica
solo las migraciones posteriores.

Revision: 0001
Anterior: (ninguna)
Fecha:    2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('blacklist_faces',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('label', sa.String(), nullable=False),
    sa.Column('vector', sa.LargeBinary(), nullable=False),
    sa.Column('dim', sa.Integer(), nullable=False),
    sa.Column('reason', sa.String(), nullable=False),
    sa.Column('legal_basis', sa.String(), nullable=True),
    sa.Column('severity', sa.String(), nullable=False),
    sa.Column('photo_path', sa.String(), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('blacklist_faces', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_blacklist_faces_active'), ['active'], unique=False)
        batch_op.create_index(batch_op.f('ix_blacklist_faces_label'), ['label'], unique=False)

    op.create_table('blacklist_plates',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('plate', sa.String(), nullable=False),
    sa.Column('plate_normalized', sa.String(), nullable=False),
    sa.Column('reason', sa.String(), nullable=False),
    sa.Column('severity', sa.String(), nullable=False),
    sa.Column('notes', sa.String(), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('blacklist_plates', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_blacklist_plates_active'), ['active'], unique=False)
        batch_op.create_index(batch_op.f('ix_blacklist_plates_plate'), ['plate'], unique=False)
        batch_op.create_index(batch_op.f('ix_blacklist_plates_plate_normalized'), ['plate_normalized'], unique=False)

    op.create_table('cameras',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('camera_id', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('location', sa.String(), nullable=True),
    sa.Column('source_spec', sa.String(), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('last_heartbeat', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status_json', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cameras_camera_id'), ['camera_id'], unique=True)
        batch_op.create_index(batch_op.f('ix_cameras_last_heartbeat'), ['last_heartbeat'], unique=False)

    op.create_table('operators',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(), nullable=False),
    sa.Column('display_name', sa.String(), nullable=False),
    sa.Column('password_hash', sa.String(), nullable=False),
    sa.Column('role', sa.String(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('operators', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_operators_username'), ['username'], unique=True)

    op.create_table('events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('event_id', sa.String(), nullable=False),
    sa.Column('dedupe_key', sa.String(), nullable=False),
    sa.Column('camera_id', sa.String(), nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('type', sa.String(), nullable=False),
    sa.Column('track_id', sa.Integer(), nullable=True),
    sa.Column('value', sa.String(), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('bbox_x1', sa.Integer(), nullable=True),
    sa.Column('bbox_y1', sa.Integer(), nullable=True),
    sa.Column('bbox_x2', sa.Integer(), nullable=True),
    sa.Column('bbox_y2', sa.Integer(), nullable=True),
    sa.Column('observations', sa.Integer(), nullable=False),
    sa.Column('snapshot_path', sa.String(), nullable=True),
    sa.Column('severity', sa.String(), nullable=False),
    sa.Column('match_kind', sa.String(), nullable=False),
    sa.Column('matched_blacklist_id', sa.Integer(), nullable=True),
    sa.Column('match_score', sa.Float(), nullable=True),
    sa.Column('meta_json', sa.String(), nullable=True),
    sa.ForeignKeyConstraint(['camera_id'], ['cameras.camera_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_events_camera_id'), ['camera_id'], unique=False)
        batch_op.create_index('ix_events_camera_ts', ['camera_id', 'ts'], unique=False)
        batch_op.create_index(batch_op.f('ix_events_dedupe_key'), ['dedupe_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_events_event_id'), ['event_id'], unique=True)
        batch_op.create_index(batch_op.f('ix_events_matched_blacklist_id'), ['matched_blacklist_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_events_severity'), ['severity'], unique=False)
        batch_op.create_index('ix_events_severity_ts', ['severity', 'ts'], unique=False)
        batch_op.create_index(batch_op.f('ix_events_ts'), ['ts'], unique=False)
        batch_op.create_index(batch_op.f('ix_events_type'), ['type'], unique=False)
        batch_op.create_index('ix_events_type_ts', ['type', 'ts'], unique=False)
        batch_op.create_index('ix_events_type_value', ['type', 'value'], unique=False)
        batch_op.create_index(batch_op.f('ix_events_value'), ['value'], unique=False)

    op.create_table('alerts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('event_id', sa.String(), nullable=False),
    sa.Column('camera_id', sa.String(), nullable=False),
    sa.Column('type', sa.String(), nullable=False),
    sa.Column('severity', sa.String(), nullable=False),
    sa.Column('title', sa.String(), nullable=False),
    sa.Column('detail', sa.String(), nullable=True),
    sa.Column('match_kind', sa.String(), nullable=False),
    sa.Column('match_score', sa.Float(), nullable=True),
    sa.Column('snapshot_path', sa.String(), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('acknowledged_by', sa.String(), nullable=True),
    sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('dismissed_reason', sa.String(), nullable=True),
    sa.Column('notes', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['event_id'], ['events.event_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_alerts_camera_id'), ['camera_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_alerts_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_alerts_event_id'), ['event_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_alerts_severity'), ['severity'], unique=False)
        batch_op.create_index(batch_op.f('ix_alerts_status'), ['status'], unique=False)
        batch_op.create_index('ix_alerts_status_created', ['status', 'created_at'], unique=False)

    op.create_table('face_embeddings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('event_id', sa.String(), nullable=False),
    sa.Column('vector', sa.LargeBinary(), nullable=False),
    sa.Column('dim', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['event_id'], ['events.event_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('face_embeddings', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_face_embeddings_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_face_embeddings_event_id'), ['event_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('face_embeddings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_face_embeddings_event_id'))
        batch_op.drop_index(batch_op.f('ix_face_embeddings_created_at'))

    op.drop_table('face_embeddings')
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.drop_index('ix_alerts_status_created')
        batch_op.drop_index(batch_op.f('ix_alerts_status'))
        batch_op.drop_index(batch_op.f('ix_alerts_severity'))
        batch_op.drop_index(batch_op.f('ix_alerts_event_id'))
        batch_op.drop_index(batch_op.f('ix_alerts_created_at'))
        batch_op.drop_index(batch_op.f('ix_alerts_camera_id'))

    op.drop_table('alerts')
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_events_value'))
        batch_op.drop_index('ix_events_type_value')
        batch_op.drop_index('ix_events_type_ts')
        batch_op.drop_index(batch_op.f('ix_events_type'))
        batch_op.drop_index(batch_op.f('ix_events_ts'))
        batch_op.drop_index('ix_events_severity_ts')
        batch_op.drop_index(batch_op.f('ix_events_severity'))
        batch_op.drop_index(batch_op.f('ix_events_matched_blacklist_id'))
        batch_op.drop_index(batch_op.f('ix_events_event_id'))
        batch_op.drop_index(batch_op.f('ix_events_dedupe_key'))
        batch_op.drop_index('ix_events_camera_ts')
        batch_op.drop_index(batch_op.f('ix_events_camera_id'))

    op.drop_table('events')
    with op.batch_alter_table('operators', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_operators_username'))

    op.drop_table('operators')
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cameras_last_heartbeat'))
        batch_op.drop_index(batch_op.f('ix_cameras_camera_id'))

    op.drop_table('cameras')
    with op.batch_alter_table('blacklist_plates', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_blacklist_plates_plate_normalized'))
        batch_op.drop_index(batch_op.f('ix_blacklist_plates_plate'))
        batch_op.drop_index(batch_op.f('ix_blacklist_plates_active'))

    op.drop_table('blacklist_plates')
    with op.batch_alter_table('blacklist_faces', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_blacklist_faces_label'))
        batch_op.drop_index(batch_op.f('ix_blacklist_faces_active'))

    op.drop_table('blacklist_faces')
