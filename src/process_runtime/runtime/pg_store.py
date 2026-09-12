"""PostgreSQL-backed WorkflowEngineStore (EPIC-11, ADR-023, WF-003).

The caller owns the transaction boundary — every method receives an already-open
``AsyncSession`` and does NOT commit (matching ``PostgresOutboxStore``).

``spiff_state`` crosses the domain boundary as ``bytes`` (UTF-8 of the adapter's
``serialize_json`` output) but is stored in a JSONB column as a Python ``dict``:
``save`` binds ``json.loads(state)``, ``load`` returns ``json.dumps(dict).encode()``.
Never bind raw bytes/str into the JSONB column — Postgres would store a JSON *scalar
string* and double-encode the state (design amendment A1).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from process_runtime.domain.entities import (
    WorkflowInstance,
    WorkflowTask,
    WorkflowTimer,
    WorkflowTransitionLog,
)
from process_runtime.domain.enums import (
    WorkflowInstanceStatus,
    WorkflowTaskStatus,
    WorkflowTimerType,
)
from process_runtime.tables import (
    workflow_instances,
    workflow_tasks,
    workflow_timers,
    workflow_transition_log,
)

_ADVISORY_LOCK_PREFIX = "workflow_instance:"


def _state_to_jsonb(state: bytes) -> dict[str, Any]:
    """Decode adapter state bytes into a JSON object for the JSONB column."""
    decoded: dict[str, Any] = json.loads(state.decode("utf-8"))
    return decoded


def _jsonb_to_state(value: Any) -> bytes:
    """Encode a JSONB object read from the DB back into adapter state bytes."""
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _row_to_instance(row: Any) -> WorkflowInstance:
    return WorkflowInstance(
        id=row.id,
        tenant_id=row.tenant_id,
        workflow_id=row.workflow_id,
        workflow_version=row.workflow_version,
        status=WorkflowInstanceStatus(row.status),
        spiff_state=_jsonb_to_state(row.spiff_state),
        started_at=row.started_at,
        updated_at=row.updated_at,
        subject_entity_type=row.subject_entity_type,
        subject_entity_id=row.subject_entity_id,
        current_tasks=tuple(row.current_tasks or ()),
        variables=dict(row.variables or {}),
        completed_at=row.completed_at,
        outcome=row.outcome,
        error_payload=dict(row.error_payload) if row.error_payload is not None else None,
    )


def _row_to_task(row: Any) -> WorkflowTask:
    return WorkflowTask(
        id=row.id,
        workflow_instance_id=row.workflow_instance_id,
        task_id=row.task_id,
        status=WorkflowTaskStatus(row.status),
        created_at=row.created_at,
        assigned_role=row.assigned_role,
        assigned_user_id=row.assigned_user_id,
        form_payload=dict(row.form_payload or {}),
        claimed_at=row.claimed_at,
        completed_at=row.completed_at,
        sla_due_at=row.sla_due_at,
        approval_policy_id=row.approval_policy_id,
    )


def _row_to_timer(row: Any) -> WorkflowTimer:
    return WorkflowTimer(
        id=row.id,
        workflow_instance_id=row.workflow_instance_id,
        task_id=row.task_id,
        fires_at=row.fires_at,
        timer_type=WorkflowTimerType(row.timer_type),
        created_at=row.created_at,
        fired_at=row.fired_at,
    )


class PostgresWorkflowEngineStore:
    """WorkflowEngineStore backed by PostgreSQL + asyncpg (SQLAlchemy 2.0 Core)."""

    async def create_instance(
        self, instance: WorkflowInstance, *, session: AsyncSession
    ) -> WorkflowInstance:
        stmt = (
            workflow_instances.insert()
            .values(
                id=instance.id,
                tenant_id=instance.tenant_id,
                workflow_id=instance.workflow_id,
                workflow_version=instance.workflow_version,
                status=instance.status.value,
                subject_entity_type=instance.subject_entity_type,
                subject_entity_id=instance.subject_entity_id,
                current_tasks=list(instance.current_tasks),
                spiff_state=_state_to_jsonb(instance.spiff_state),
                variables=dict(instance.variables),
                started_at=instance.started_at,
                updated_at=instance.updated_at,
                completed_at=instance.completed_at,
                outcome=instance.outcome,
                error_payload=instance.error_payload,
            )
            .returning(*workflow_instances.c)
        )
        result = await session.execute(stmt)
        return _row_to_instance(result.fetchone())

    async def load_instance(
        self,
        instance_id: UUID,
        *,
        tenant_id: UUID | None = None,
        for_update: bool = False,
        session: AsyncSession,
    ) -> WorkflowInstance | None:
        if for_update:
            # int8 advisory lock (hashtextextended) to minimise cross-feature collision
            # vs the 32-bit hashtext used elsewhere (design amendment A6).
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"{_ADVISORY_LOCK_PREFIX}{instance_id}"},
            )
        stmt = select(*workflow_instances.c).where(workflow_instances.c.id == instance_id)
        if tenant_id is not None:
            stmt = stmt.where(workflow_instances.c.tenant_id == tenant_id)
        if for_update:
            stmt = stmt.with_for_update()
        result = await session.execute(stmt)
        row = result.fetchone()
        return _row_to_instance(row) if row is not None else None

    async def save_instance(self, instance: WorkflowInstance, *, session: AsyncSession) -> None:
        stmt = (
            update(workflow_instances)
            .where(workflow_instances.c.id == instance.id)
            .values(
                status=instance.status.value,
                # workflow_version is normally frozen, but forced migration bumps it —
                # persist it here so the version guard sees the new value.
                workflow_version=instance.workflow_version,
                subject_entity_type=instance.subject_entity_type,
                subject_entity_id=instance.subject_entity_id,
                current_tasks=list(instance.current_tasks),
                spiff_state=_state_to_jsonb(instance.spiff_state),
                variables=dict(instance.variables),
                updated_at=instance.updated_at,
                completed_at=instance.completed_at,
                outcome=instance.outcome,
                error_payload=instance.error_payload,
            )
        )
        await session.execute(stmt)

    async def append_transition(self, log: WorkflowTransitionLog, *, session: AsyncSession) -> None:
        stmt = workflow_transition_log.insert().values(
            workflow_instance_id=log.workflow_instance_id,
            from_task_id=log.from_task_id,
            to_task_id=log.to_task_id,
            trigger=log.trigger.value,
            actor_user_id=log.actor_user_id,
            transitioned_at=log.transitioned_at,
            payload=dict(log.payload),
        )
        await session.execute(stmt)

    async def has_dedup_transition(
        self, instance_id: UUID, dedup_key: str, *, session: AsyncSession
    ) -> bool:
        stmt = (
            select(workflow_transition_log.c.id)
            .where(
                and_(
                    workflow_transition_log.c.workflow_instance_id == instance_id,
                    workflow_transition_log.c.payload["dedup_key"].astext == dedup_key,
                )
            )
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.fetchone() is not None

    async def upsert_task(self, task: WorkflowTask, *, session: AsyncSession) -> None:
        """Insert or update a task by ``(instance, task_id)`` active occurrence.

        Read-modify-write is race-free because ``upsert_task`` is only ever called from
        within an advance already holding the per-instance advisory lock.
        """
        existing = await session.execute(
            select(workflow_tasks.c.id).where(
                and_(
                    workflow_tasks.c.workflow_instance_id == task.workflow_instance_id,
                    workflow_tasks.c.task_id == task.task_id,
                    workflow_tasks.c.status.in_(
                        (WorkflowTaskStatus.pending.value, WorkflowTaskStatus.claimed.value)
                    ),
                )
            )
        )
        row = existing.fetchone()
        if row is None:
            await session.execute(
                workflow_tasks.insert().values(
                    id=task.id,
                    workflow_instance_id=task.workflow_instance_id,
                    task_id=task.task_id,
                    status=task.status.value,
                    assigned_role=task.assigned_role,
                    assigned_user_id=task.assigned_user_id,
                    form_payload=dict(task.form_payload),
                    created_at=task.created_at,
                    claimed_at=task.claimed_at,
                    completed_at=task.completed_at,
                    sla_due_at=task.sla_due_at,
                    approval_policy_id=task.approval_policy_id,
                )
            )
        else:
            await session.execute(
                update(workflow_tasks)
                .where(workflow_tasks.c.id == row.id)
                .values(
                    status=task.status.value,
                    assigned_role=task.assigned_role,
                    assigned_user_id=task.assigned_user_id,
                    form_payload=dict(task.form_payload),
                    claimed_at=task.claimed_at,
                    completed_at=task.completed_at,
                    sla_due_at=task.sla_due_at,
                    approval_policy_id=task.approval_policy_id,
                )
            )

    async def list_tasks(
        self, instance_id: UUID, *, tenant_id: UUID, session: AsyncSession
    ) -> Sequence[WorkflowTask]:
        stmt = (
            select(*workflow_tasks.c)
            .select_from(
                workflow_tasks.join(
                    workflow_instances,
                    workflow_tasks.c.workflow_instance_id == workflow_instances.c.id,
                )
            )
            .where(
                and_(
                    workflow_tasks.c.workflow_instance_id == instance_id,
                    workflow_instances.c.tenant_id == tenant_id,
                )
            )
            .order_by(workflow_tasks.c.created_at.asc())
        )
        result = await session.execute(stmt)
        return [_row_to_task(r) for r in result.fetchall()]

    async def list_tasks_inbox(
        self,
        *,
        tenant_id: UUID,
        assignee_user_id: UUID | None = None,
        role: str | None = None,
        status_in: Sequence[WorkflowTaskStatus] = (
            WorkflowTaskStatus.pending,
            WorkflowTaskStatus.claimed,
        ),
        limit: int,
        offset: int,
        session: AsyncSession,
    ) -> Sequence[WorkflowTask]:
        conditions = [
            workflow_instances.c.tenant_id == tenant_id,
            workflow_tasks.c.status.in_([s.value for s in status_in]),
        ]
        if assignee_user_id is not None:
            conditions.append(workflow_tasks.c.assigned_user_id == assignee_user_id)
        if role is not None:
            conditions.append(workflow_tasks.c.assigned_role == role)
        stmt = (
            select(*workflow_tasks.c)
            .select_from(
                workflow_tasks.join(
                    workflow_instances,
                    workflow_tasks.c.workflow_instance_id == workflow_instances.c.id,
                )
            )
            .where(and_(*conditions))
            .order_by(workflow_tasks.c.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        result = await session.execute(stmt)
        return [_row_to_task(r) for r in result.fetchall()]

    async def schedule_timer(self, timer: WorkflowTimer, *, session: AsyncSession) -> WorkflowTimer:
        stmt = (
            workflow_timers.insert()
            .values(
                id=timer.id,
                workflow_instance_id=timer.workflow_instance_id,
                task_id=timer.task_id,
                fires_at=timer.fires_at,
                timer_type=timer.timer_type.value,
                fired_at=timer.fired_at,
                created_at=timer.created_at,
            )
            .returning(*workflow_timers.c)
        )
        result = await session.execute(stmt)
        return _row_to_timer(result.fetchone())

    async def cancel_unfired_timers(
        self,
        instance_id: UUID,
        *,
        keep_task_ids: Sequence[str],
        session: AsyncSession,
    ) -> int:
        """Mark fired all unfired timers of an instance whose task_id is not kept.

        ``fired_at`` is never reset to NULL; a cancelled timer simply leaves the
        active (partial-unique) set (design amendment A5).
        """
        conditions = [
            workflow_timers.c.workflow_instance_id == instance_id,
            workflow_timers.c.fired_at.is_(None),
        ]
        if keep_task_ids:
            conditions.append(workflow_timers.c.task_id.notin_(list(keep_task_ids)))
        stmt = update(workflow_timers).where(and_(*conditions)).values(fired_at=text("now()"))
        result = await session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)

    async def list_due_timers(
        self, now: datetime, *, limit: int, session: AsyncSession
    ) -> Sequence[WorkflowTimer]:
        stmt = (
            select(*workflow_timers.c)
            .where(
                and_(
                    workflow_timers.c.fired_at.is_(None),
                    workflow_timers.c.fires_at <= now,
                )
            )
            .order_by(workflow_timers.c.fires_at.asc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return [_row_to_timer(r) for r in result.fetchall()]

    async def mark_timer_fired(
        self, timer_id: UUID, fired_at: datetime, *, session: AsyncSession
    ) -> None:
        stmt = (
            update(workflow_timers)
            .where(
                and_(
                    workflow_timers.c.id == timer_id,
                    workflow_timers.c.fired_at.is_(None),
                )
            )
            .values(fired_at=fired_at)
        )
        await session.execute(stmt)

    async def find_active_by_subject(
        self,
        *,
        tenant_id: UUID,
        subject_entity_type: str,
        subject_entity_id: UUID,
        workflow_id: str | None = None,
        session: AsyncSession,
    ) -> Sequence[WorkflowInstance]:
        conditions = [
            workflow_instances.c.tenant_id == tenant_id,
            workflow_instances.c.subject_entity_type == subject_entity_type,
            workflow_instances.c.subject_entity_id == subject_entity_id,
            workflow_instances.c.status.in_(
                (WorkflowInstanceStatus.active.value, WorkflowInstanceStatus.waiting.value)
            ),
        ]
        if workflow_id is not None:
            conditions.append(workflow_instances.c.workflow_id == workflow_id)
        stmt = select(*workflow_instances.c).where(and_(*conditions))
        result = await session.execute(stmt)
        return [_row_to_instance(r) for r in result.fetchall()]


__all__ = ["PostgresWorkflowEngineStore"]
