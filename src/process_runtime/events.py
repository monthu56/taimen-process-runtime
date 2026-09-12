"""Process event log: typed ``platform.workflow.*`` payloads and the transactional feed.

The runtime appends an event in the same transaction as the state change (transactional
outbox); consumers read the log through ``GET /api/v1/events`` with an opaque cursor, the
same contract shape as the Control Plane and IAM journals. Nothing is pushed anywhere: a
consumer (Control Plane reconciliation, superproject ADR-0023) pulls at its own pace and
keeps its own cursor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from process_runtime.tables import process_events


class _BaseWorkflowPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_instance_id: UUID
    workflow_id: str
    workflow_version: int


class WorkflowStartedV1(_BaseWorkflowPayload):
    subject_entity_type: str | None = None
    subject_entity_id: UUID | None = None


class WorkflowTaskCreatedV1(_BaseWorkflowPayload):
    task_id: str
    assigned_role: str | None = None
    approval_policy_id: str | None = None


class WorkflowTaskClaimedV1(_BaseWorkflowPayload):
    task_id: str
    assigned_user_id: UUID | None = None


class WorkflowTaskCompletedV1(_BaseWorkflowPayload):
    task_id: str
    actor_user_id: UUID | None = None


class WorkflowTransitionedV1(_BaseWorkflowPayload):
    from_task_id: str | None = None
    to_task_id: str | None = None
    trigger: str


class WorkflowSlaBreachedV1(_BaseWorkflowPayload):
    task_id: str


class WorkflowCompletedV1(_BaseWorkflowPayload):
    outcome: str | None = None


class WorkflowFailedV1(_BaseWorkflowPayload):
    error_type: str | None = None
    error: str | None = None


class WorkflowCancelledV1(_BaseWorkflowPayload):
    reason: str | None = None


class WorkflowMigratedV1(_BaseWorkflowPayload):
    from_version: int
    to_version: int
    mapping: dict[str, Any]


# event type base -> payload class; the runtime publishes ``<base>.v1``.
WORKFLOW_EVENT_PAYLOADS: dict[str, type[BaseModel]] = {
    "platform.workflow.started": WorkflowStartedV1,
    "platform.workflow.task_created": WorkflowTaskCreatedV1,
    "platform.workflow.task_claimed": WorkflowTaskClaimedV1,
    "platform.workflow.task_completed": WorkflowTaskCompletedV1,
    "platform.workflow.transitioned": WorkflowTransitionedV1,
    "platform.workflow.sla_breached": WorkflowSlaBreachedV1,
    "platform.workflow.completed": WorkflowCompletedV1,
    "platform.workflow.failed": WorkflowFailedV1,
    "platform.workflow.cancelled": WorkflowCancelledV1,
    "platform.workflow.migrated": WorkflowMigratedV1,
}


class ProcessEventLog:
    """Append-only event log written inside the caller's transaction."""

    async def append(
        self,
        *,
        type: str,
        source: str,
        subject: str | None,
        data: BaseModel,
        tenant_id: UUID,
        session: AsyncSession,
    ) -> UUID:
        base = type.removesuffix(".v1")
        expected = WORKFLOW_EVENT_PAYLOADS.get(base)
        if expected is None or not isinstance(data, expected):
            raise ValueError(f"event '{type}' is not a registered process event")
        event_id = uuid4()
        await session.execute(
            process_events.insert().values(
                event_id=event_id,
                tenant_id=tenant_id,
                event_type=type,
                source=source,
                subject=subject,
                payload=data.model_dump(mode="json"),
                occurred_at=datetime.now(UTC),
            )
        )
        return event_id

    async def read(
        self,
        *,
        tenant_id: UUID,
        after: int,
        limit: int,
        session: AsyncSession,
    ) -> list[dict[str, Any]]:
        stmt = (
            select(*process_events.c)
            .where(process_events.c.tenant_id == tenant_id, process_events.c.id > after)
            .order_by(process_events.c.id.asc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).fetchall()
        return [
            {
                "cursor": row.id,
                "id": str(row.event_id),
                "type": row.event_type,
                "source": row.source,
                "subject": row.subject,
                "tenantId": str(row.tenant_id),
                "time": row.occurred_at.isoformat(),
                "data": row.payload,
            }
            for row in rows
        ]


__all__ = [
    "WORKFLOW_EVENT_PAYLOADS",
    "ProcessEventLog",
    "WorkflowCancelledV1",
    "WorkflowCompletedV1",
    "WorkflowFailedV1",
    "WorkflowMigratedV1",
    "WorkflowSlaBreachedV1",
    "WorkflowStartedV1",
    "WorkflowTaskClaimedV1",
    "WorkflowTaskCompletedV1",
    "WorkflowTaskCreatedV1",
    "WorkflowTransitionedV1",
]
