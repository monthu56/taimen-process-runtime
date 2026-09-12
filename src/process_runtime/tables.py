"""SQLAlchemy Core tables of process-runtime (own database, own Alembic chain).

Four engine tables carried over from platform-core (``workflow_*``) plus the process event
log. There are no foreign keys to tenants or users: the tenant is the IAM tenant of the
presented token and every ``*_user_id`` column holds an IAM principal id. Partial and
expression indexes live only in the migration (Alembic autogenerate does not reflect
``postgresql_where``), which stays the source of truth.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData()

workflow_instances = sa.Table(
    "workflow_instances",
    metadata,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
    sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
    sa.Column("workflow_id", sa.Text(), nullable=False),
    sa.Column("workflow_version", sa.Integer(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("subject_entity_type", sa.Text(), nullable=True),
    sa.Column("subject_entity_id", sa.Uuid(as_uuid=True), nullable=True),
    sa.Column(
        "current_tasks",
        JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    ),
    sa.Column("spiff_state", JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column(
        "variables",
        JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("outcome", sa.Text(), nullable=True),
    sa.Column("error_payload", JSONB(astext_type=sa.Text()), nullable=True),
    sa.CheckConstraint(
        "status IN ('active','waiting','completed','failed','cancelled')",
        name="ck_workflow_instances_status",
    ),
)

workflow_transition_log = sa.Table(
    "workflow_transition_log",
    metadata,
    sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
    sa.Column(
        "workflow_instance_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("workflow_instances.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("from_task_id", sa.Text(), nullable=True),
    sa.Column("to_task_id", sa.Text(), nullable=True),
    sa.Column("trigger", sa.Text(), nullable=False),
    sa.Column("actor_user_id", sa.Uuid(as_uuid=True), nullable=True),
    sa.Column("transitioned_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column(
        "payload",
        JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ),
)

workflow_timers = sa.Table(
    "workflow_timers",
    metadata,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
    sa.Column(
        "workflow_instance_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("workflow_instances.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("task_id", sa.Text(), nullable=False),
    sa.Column("fires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("timer_type", sa.Text(), nullable=False),
    sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(
        "timer_type IN ('due_date','cycle','duration')", name="ck_workflow_timers_type"
    ),
)

workflow_tasks = sa.Table(
    "workflow_tasks",
    metadata,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
    sa.Column(
        "workflow_instance_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("workflow_instances.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("task_id", sa.Text(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("assigned_role", sa.Text(), nullable=True),
    sa.Column("assigned_user_id", sa.Uuid(as_uuid=True), nullable=True),
    sa.Column(
        "form_payload",
        JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
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
)

process_events = sa.Table(
    "process_events",
    metadata,
    sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
    sa.Column("event_id", sa.Uuid(as_uuid=True), nullable=False, unique=True),
    sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
    sa.Column("event_type", sa.Text(), nullable=False),
    sa.Column("source", sa.Text(), nullable=False),
    sa.Column("subject", sa.Text(), nullable=True),
    sa.Column("payload", JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
)

sa.Index("idx_wi_tenant_status", workflow_instances.c.tenant_id, workflow_instances.c.status)
sa.Index(
    "idx_wi_subject",
    workflow_instances.c.subject_entity_type,
    workflow_instances.c.subject_entity_id,
)
sa.Index("idx_wi_workflow", workflow_instances.c.workflow_id, workflow_instances.c.workflow_version)
sa.Index(
    "idx_wtl_instance_time",
    workflow_transition_log.c.workflow_instance_id,
    workflow_transition_log.c.transitioned_at.desc(),
)
sa.Index(
    "idx_wtask_instance_status", workflow_tasks.c.workflow_instance_id, workflow_tasks.c.status
)
sa.Index("idx_process_events_tenant", process_events.c.tenant_id, process_events.c.id)

__all__ = [
    "metadata",
    "process_events",
    "workflow_instances",
    "workflow_tasks",
    "workflow_timers",
    "workflow_transition_log",
]
