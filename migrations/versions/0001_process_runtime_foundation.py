"""process-runtime foundation: engine tables carried over from platform-core + event log

Revision ID: 0001_process_runtime
Revises:
Create Date: 2026-09-12
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_process_runtime"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_instances",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.Text(), nullable=False),
        sa.Column("workflow_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("subject_entity_type", sa.Text(), nullable=True),
        sa.Column("subject_entity_id", sa.Uuid(), nullable=True),
        sa.Column(
            "current_tasks",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("spiff_state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "variables",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("error_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "status IN ('active','waiting','completed','failed','cancelled')",
            name="ck_workflow_instances_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_wi_tenant_status", "workflow_instances", ["tenant_id", "status"])
    op.create_index(
        "idx_wi_subject", "workflow_instances", ["subject_entity_type", "subject_entity_id"]
    )
    op.create_index("idx_wi_workflow", "workflow_instances", ["workflow_id", "workflow_version"])

    op.create_table(
        "workflow_transition_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workflow_instance_id", sa.Uuid(), nullable=False),
        sa.Column("from_task_id", sa.Text(), nullable=True),
        sa.Column("to_task_id", sa.Text(), nullable=True),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("transitioned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workflow_instance_id"], ["workflow_instances.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_wtl_instance_time",
        "workflow_transition_log",
        ["workflow_instance_id", sa.text("transitioned_at DESC")],
    )
    op.create_index(
        "uq_wtl_dedup",
        "workflow_transition_log",
        ["workflow_instance_id", sa.text("(payload ->> 'dedup_key')")],
        unique=True,
        postgresql_where=sa.text("(payload ->> 'dedup_key') IS NOT NULL"),
    )

    op.create_table(
        "workflow_timers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_instance_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("fires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timer_type", sa.Text(), nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "timer_type IN ('due_date','cycle','duration')", name="ck_workflow_timers_type"
        ),
        sa.ForeignKeyConstraint(
            ["workflow_instance_id"], ["workflow_instances.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_wtimer_due",
        "workflow_timers",
        ["fires_at"],
        postgresql_where=sa.text("fired_at IS NULL"),
    )
    op.create_index(
        "uq_wtimer_active",
        "workflow_timers",
        ["workflow_instance_id", "task_id"],
        unique=True,
        postgresql_where=sa.text("fired_at IS NULL"),
    )

    op.create_table(
        "workflow_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_instance_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("assigned_role", sa.Text(), nullable=True),
        sa.Column("assigned_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "form_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sla_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_policy_id", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','claimed','completed','cancelled')",
            name="ck_workflow_tasks_status",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_instance_id"], ["workflow_instances.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_wtask_instance_status", "workflow_tasks", ["workflow_instance_id", "status"]
    )
    op.create_index(
        "idx_wtask_inbox",
        "workflow_tasks",
        ["assigned_user_id", "status"],
        postgresql_where=sa.text("status IN ('pending','claimed')"),
    )
    op.create_index(
        "idx_wtask_role_inbox",
        "workflow_tasks",
        ["assigned_role", "status"],
        postgresql_where=sa.text("assigned_user_id IS NULL AND status = 'pending'"),
    )
    op.create_index(
        "uq_wtask_active",
        "workflow_tasks",
        ["workflow_instance_id", "task_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','claimed')"),
    )

    op.create_table(
        "process_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id"),
    )
    op.create_index("idx_process_events_tenant", "process_events", ["tenant_id", "id"])


def downgrade() -> None:
    op.drop_table("process_events")
    op.drop_table("workflow_tasks")
    op.drop_table("workflow_timers")
    op.drop_table("workflow_transition_log")
    op.drop_table("workflow_instances")
