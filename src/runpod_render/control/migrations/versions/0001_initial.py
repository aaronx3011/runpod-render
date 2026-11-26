"""initial schema

Revision ID: 0001
Revises: (none)
Create Date: 2026-11-26 17:17:04.677583
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('projects',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('api_key_hash', sa.String(length=64), nullable=False),
    sa.Column('max_active', sa.Integer(), server_default='1', nullable=False),
    sa.Column('last_admitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('api_key_hash'),
    sa.UniqueConstraint('name')
    )
    op.create_table('jobs',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('project_id', sa.String(length=40), nullable=False),
    sa.Column('operation_id', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('reference_upload_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('params_hash', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('current_fence', sa.Integer(), server_default='0', nullable=False),
    sa.Column('current_attempt_id', sa.String(length=40), nullable=True),
    sa.Column('attempt_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('not_before', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('result_video_key', sa.String(length=500), nullable=True),
    sa.Column('result_manifest_key', sa.String(length=500), nullable=True),
    sa.Column('result_video_sha256', sa.String(length=64), nullable=True),
    sa.Column('result_video_bytes', sa.BigInteger(), nullable=True),
    sa.Column('timings', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'operation_id', name='uq_jobs_project_operation')
    )
    op.create_index('ix_jobs_pending', 'jobs', ['project_id', 'created_at'], unique=False, postgresql_where=sa.text("status = 'queued' AND current_attempt_id IS NULL"))
    op.create_index('ix_jobs_project_created', 'jobs', ['project_id', 'created_at', 'id'], unique=False)
    op.create_table('uploads',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('project_id', sa.String(length=40), nullable=False),
    sa.Column('object_key', sa.String(length=500), nullable=False),
    sa.Column('media_type', sa.String(length=20), nullable=False),
    sa.Column('content_type', sa.String(length=200), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_uploads_project_id'), 'uploads', ['project_id'], unique=False)
    op.create_table('attempts',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('job_id', sa.String(length=40), nullable=False),
    sa.Column('project_id', sa.String(length=40), nullable=False),
    sa.Column('fence', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('runpod_endpoint', sa.String(length=100), nullable=False),
    sa.Column('runpod_job_id', sa.String(length=100), nullable=True),
    sa.Column('runpod_status', sa.String(length=30), nullable=True),
    sa.Column('worker_id', sa.String(length=200), nullable=True),
    sa.Column('stage', sa.String(length=30), nullable=True),
    sa.Column('progress', sa.Float(), nullable=True),
    sa.Column('output_prefix', sa.String(length=500), nullable=False),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('error_type', sa.String(length=50), nullable=True),
    sa.Column('retryable', sa.Boolean(), nullable=True),
    sa.Column('timings', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_reconciled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('job_id', 'fence', name='uq_attempts_job_fence')
    )
    op.create_index('ix_attempts_active', 'attempts', ['project_id', 'status'], unique=False, postgresql_where=sa.text("status IN ('submitting','submitted','uncertain','running')"))
    op.create_index('ix_attempts_job', 'attempts', ['job_id'], unique=False)
    op.create_index('ix_attempts_runpod_job', 'attempts', ['runpod_job_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_attempts_runpod_job', table_name='attempts')
    op.drop_index('ix_attempts_job', table_name='attempts')
    op.drop_index('ix_attempts_active', table_name='attempts', postgresql_where=sa.text("status IN ('submitting','submitted','uncertain','running')"))
    op.drop_table('attempts')
    op.drop_index(op.f('ix_uploads_project_id'), table_name='uploads')
    op.drop_table('uploads')
    op.drop_index('ix_jobs_project_created', table_name='jobs')
    op.drop_index('ix_jobs_pending', table_name='jobs', postgresql_where=sa.text("status = 'queued' AND current_attempt_id IS NULL"))
    op.drop_table('jobs')
    op.drop_table('projects')
