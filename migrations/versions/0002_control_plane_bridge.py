"""Control Plane bridge: activity bindings and reconciliation cursors

Revision ID: 0002_cp_bridge
Revises: 0001_process_runtime
Create Date: 2026-09-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_cp_bridge"
down_revision = "0001_process_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "process_task_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_instance_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("source_event_id", sa.BigInteger(), nullable=False),
        sa.Column("cp_task_id", sa.Uuid(), nullable=False),
        sa.Column("cp_public_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('open','completing','completed','cancelled')",
            name="ck_process_task_bindings_status",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_instance_id"], ["workflow_instances.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_event_id", name="uq_process_task_bindings_event"),
        sa.UniqueConstraint("cp_task_id", name="uq_process_task_bindings_cp_task"),
    )
    op.create_index(
        "idx_ptb_instance_open",
        "process_task_bindings",
        ["workflow_instance_id", "task_id"],
        postgresql_where=sa.text("status IN ('open','completing')"),
    )
    op.create_table(
        "bridge_cursors",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("bridge_cursors")
    op.drop_table("process_task_bindings")
