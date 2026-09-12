"""Workflow advance/cancel/migrate runtime (EPIC-11, ADR-023, WF-011/016).

The transactional heart. Every mutating operation runs in a single transaction under a
per-instance ``pg_advisory_xact_lock`` (via ``load_instance(for_update=True)``):

- idempotency marker is written unconditionally whenever a ``dedup_key`` is present, even
  for a no-op advance (design amendment A5);
- the adapter never raises domain errors — a failure is persisted as ``failed`` through the
  single persist path;
- for a timer trigger the firing timer is marked fired INSIDE this transaction, before any
  re-arm;
- timers are reconciled by cancelling all unfired timers and re-inserting the adapter's
  current waiting set (keeps ``uq_wtimer_active`` satisfied);
- ``platform.workflow.*`` events are appended to the process event log in the same
  transaction (``GET /api/v1/events`` feeds them to consumers with a cursor).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from process_runtime.domain.entities import (
    WorkflowInstance,
    WorkflowTask,
    WorkflowTimer,
    WorkflowTransitionLog,
)
from process_runtime.domain.enums import (
    TERMINAL_INSTANCE_STATUSES,
    WorkflowInstanceStatus,
    WorkflowTaskStatus,
    WorkflowTransitionTrigger,
)
from process_runtime.domain.errors import (
    WorkflowInstanceAlreadyCompleted,
    WorkflowInstanceNotFound,
)
from process_runtime.domain.protocols import (
    WorkflowEngineAdapter,
    WorkflowEngineStore,
)
from process_runtime.domain.value_objects import (
    AdvanceResult,
    MigrationMap,
    TransitionRecord,
)
from process_runtime.events import (
    WORKFLOW_EVENT_PAYLOADS,
    ProcessEventLog,
    WorkflowCancelledV1,
    WorkflowCompletedV1,
    WorkflowFailedV1,
    WorkflowMigratedV1,
    WorkflowStartedV1,
    WorkflowTaskCompletedV1,
    WorkflowTaskCreatedV1,
    WorkflowTransitionedV1,
)
from process_runtime.runtime import metrics
from process_runtime.runtime.loader import WorkflowDefinitionRegistry

_SOURCE = "/process-runtime"


def _now() -> datetime:
    return datetime.now(UTC)


class WorkflowAdvanceService:
    """Coordinates durable advance/cancel/migrate over the store + engine adapter."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: WorkflowEngineStore,
        adapter: WorkflowEngineAdapter,
        registry: WorkflowDefinitionRegistry,
        events: ProcessEventLog,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._adapter = adapter
        self._registry = registry
        self._events = events

    # ------------------------------------------------------------------ start
    async def start_instance(
        self,
        *,
        tenant_id: UUID,
        workflow_id: str,
        version: int,
        subject_entity_type: str | None = None,
        subject_entity_id: UUID | None = None,
        initial_variables: Mapping[str, Any] | None = None,
    ) -> WorkflowInstance:
        parsed = self._registry.get(workflow_id, version)
        if parsed is None:
            raise WorkflowInstanceNotFound(
                f"workflow definition '{workflow_id}' v{version} is not registered"
            )
        now = _now()
        result = self._adapter.build_initial_state(parsed, dict(initial_variables or {}))
        instance = WorkflowInstance(
            id=uuid4(),
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            workflow_version=version,
            status=result.instance_status,
            spiff_state=result.new_state,
            started_at=now,
            updated_at=now,
            subject_entity_type=subject_entity_type,
            subject_entity_id=subject_entity_id,
            current_tasks=tuple(t.task_id for t in result.active_tokens),
            variables=dict(result.variables),
            completed_at=now if result.instance_status in TERMINAL_INSTANCE_STATUSES else None,
            outcome=result.outcome,
            error_payload=dict(result.error_payload) if result.error_payload else None,
        )
        async with self._session_factory() as session:
            await self._store.create_instance(instance, session=session)
            await self._persist_result(
                session,
                instance=instance,
                result=result,
                trigger=WorkflowTransitionTrigger.task_complete,
                dedup_key=None,
                actor_user_id=None,
                first=True,
            )
            await session.commit()
        return instance

    # ---------------------------------------------------------------- advance
    async def advance(
        self,
        *,
        instance_id: UUID,
        trigger: WorkflowTransitionTrigger,
        payload: Mapping[str, Any],
        tenant_id: UUID | None = None,
        actor_user_id: UUID | None = None,
    ) -> WorkflowInstance | None:
        started = time.monotonic()
        now = _now()
        dedup_key = payload.get("dedup_key")
        async with self._session_factory() as session:
            instance = await self._store.load_instance(
                instance_id, tenant_id=tenant_id, for_update=True, session=session
            )
            if instance is None:
                raise WorkflowInstanceNotFound(str(instance_id))
            if instance.status in TERMINAL_INSTANCE_STATUSES:
                metrics.workflow_advance_dedup_skipped_total.add(
                    1, {"workflow_id": instance.workflow_id, "reason": "terminal"}
                )
                return instance
            if dedup_key and await self._store.has_dedup_transition(
                instance_id, str(dedup_key), session=session
            ):
                metrics.workflow_advance_dedup_skipped_total.add(
                    1, {"workflow_id": instance.workflow_id, "reason": "dedup"}
                )
                return instance

            # A5: mark the firing timer fired inside this transaction, before any re-arm.
            timer_id = payload.get("timer_id")
            if trigger is WorkflowTransitionTrigger.timer and timer_id:
                await self._store.mark_timer_fired(UUID(str(timer_id)), now, session=session)

            parsed = self._registry.get(instance.workflow_id, instance.workflow_version)
            if parsed is None:
                result = AdvanceResult(
                    new_state=instance.spiff_state,
                    instance_status=WorkflowInstanceStatus.failed,
                    variables=instance.variables,
                    emitted_events=("platform.workflow.failed",),
                    error_payload={
                        "error_type": "WorkflowDefinitionInvalid",
                        "error": "definition not registered at advance time",
                    },
                )
            else:
                result = self._adapter.advance(
                    state=instance.spiff_state,
                    trigger=trigger,
                    payload=payload,
                    parsed=parsed,
                    now=now,
                )

            updated = self._apply(instance, result, now)
            await self._store.save_instance(updated, session=session)
            await self._persist_result(
                session,
                instance=updated,
                result=result,
                trigger=trigger,
                dedup_key=str(dedup_key) if dedup_key else None,
                actor_user_id=actor_user_id,
                first=False,
            )
            await session.commit()

        result_label = updated.status.value
        metrics.record_advance(
            updated.workflow_id, trigger.value, result_label, time.monotonic() - started
        )
        if (
            updated.status is WorkflowInstanceStatus.failed
            and result.error_payload
            and (
                "Script" in str(result.error_payload.get("error_type", ""))
                or "Script" in str(result.error_payload.get("error", ""))
            )
        ):
            metrics.record_script_blocked(updated.workflow_id)
        return updated

    # ----------------------------------------------------------------- cancel
    async def cancel(
        self,
        *,
        instance_id: UUID,
        reason: str,
        tenant_id: UUID | None = None,
        actor_user_id: UUID | None = None,
    ) -> WorkflowInstance:
        now = _now()
        async with self._session_factory() as session:
            instance = await self._store.load_instance(
                instance_id, tenant_id=tenant_id, for_update=True, session=session
            )
            if instance is None:
                raise WorkflowInstanceNotFound(str(instance_id))
            if instance.status in TERMINAL_INSTANCE_STATUSES:
                raise WorkflowInstanceAlreadyCompleted(str(instance_id))
            updated = replace(
                instance,
                status=WorkflowInstanceStatus.cancelled,
                current_tasks=(),
                updated_at=now,
                completed_at=now,
                outcome="cancelled",
            )
            await self._store.save_instance(updated, session=session)
            await self._store.cancel_unfired_timers(instance_id, keep_task_ids=(), session=session)
            await self._store.append_transition(
                WorkflowTransitionLog(
                    id=None,
                    workflow_instance_id=instance_id,
                    trigger=WorkflowTransitionTrigger.cancellation,
                    transitioned_at=now,
                    actor_user_id=actor_user_id,
                    payload={"reason": reason},
                ),
                session=session,
            )
            await self._publish(
                session,
                instance=updated,
                event_base="platform.workflow.cancelled",
                data=WorkflowCancelledV1(
                    workflow_instance_id=updated.id,
                    workflow_id=updated.workflow_id,
                    workflow_version=updated.workflow_version,
                    reason=reason,
                ),
            )
            await session.commit()
        return updated

    # ---------------------------------------------------------------- migrate
    async def migrate(
        self,
        *,
        instance_id: UUID,
        migration: MigrationMap,
        tenant_id: UUID | None = None,
        actor_user_id: UUID | None = None,
    ) -> WorkflowInstance:
        from process_runtime.domain.errors import (
            WorkflowMigrationMapInconsistent,
            WorkflowVersionFrozen,
        )

        now = _now()
        async with self._session_factory() as session:
            instance = await self._store.load_instance(
                instance_id, tenant_id=tenant_id, for_update=True, session=session
            )
            if instance is None:
                raise WorkflowInstanceNotFound(str(instance_id))
            if instance.status in TERMINAL_INSTANCE_STATUSES:
                raise WorkflowInstanceAlreadyCompleted(str(instance_id))
            if instance.workflow_version != migration.from_version:
                raise WorkflowMigrationMapInconsistent(
                    f"instance is v{instance.workflow_version}, map.from_version="
                    f"{migration.from_version}"
                )
            parsed_from = self._registry.get(instance.workflow_id, migration.from_version)
            parsed_to = self._registry.get(instance.workflow_id, migration.to_version)
            if parsed_from is None or parsed_to is None:
                raise WorkflowMigrationMapInconsistent("source or target version not registered")
            decl_to = self._registry.get_declaration(instance.workflow_id, migration.to_version)
            if decl_to is not None and decl_to.is_retired:
                raise WorkflowVersionFrozen(f"target version v{migration.to_version} is retired")
            new_state = self._adapter.migrate(
                state=instance.spiff_state,
                parsed_from=parsed_from,
                parsed_to=parsed_to,
                migration=migration,
            )
            updated = replace(
                instance,
                workflow_version=migration.to_version,
                spiff_state=new_state,
                updated_at=now,
            )
            await self._store.save_instance(updated, session=session)
            await self._store.append_transition(
                WorkflowTransitionLog(
                    id=None,
                    workflow_instance_id=instance_id,
                    trigger=WorkflowTransitionTrigger.migration,
                    transitioned_at=now,
                    actor_user_id=actor_user_id,
                    payload={
                        "from_version": migration.from_version,
                        "to_version": migration.to_version,
                        "mapping": dict(migration.task_id_mapping),
                    },
                ),
                session=session,
            )
            await self._publish(
                session,
                instance=updated,
                event_base="platform.workflow.migrated",
                data=WorkflowMigratedV1(
                    workflow_instance_id=updated.id,
                    workflow_id=updated.workflow_id,
                    workflow_version=updated.workflow_version,
                    from_version=migration.from_version,
                    to_version=migration.to_version,
                    mapping=dict(migration.task_id_mapping),
                ),
            )
            await session.commit()
        return updated

    # ----------------------------------------------------------- persistence
    def _apply(
        self, instance: WorkflowInstance, result: AdvanceResult, now: datetime
    ) -> WorkflowInstance:
        terminal = result.instance_status in TERMINAL_INSTANCE_STATUSES
        return replace(
            instance,
            status=result.instance_status,
            spiff_state=result.new_state,
            variables=dict(result.variables),
            current_tasks=tuple(t.task_id for t in result.active_tokens),
            updated_at=now,
            completed_at=now if terminal else instance.completed_at,
            outcome=result.outcome if terminal else instance.outcome,
            error_payload=dict(result.error_payload) if result.error_payload else None,
        )

    async def _persist_result(
        self,
        session: AsyncSession,
        *,
        instance: WorkflowInstance,
        result: AdvanceResult,
        trigger: WorkflowTransitionTrigger,
        dedup_key: str | None,
        actor_user_id: UUID | None,
        first: bool,
    ) -> None:
        now = instance.updated_at
        # Completed tasks.
        for task_id in result.completed_task_ids:
            await self._store.upsert_task(
                WorkflowTask(
                    id=uuid4(),
                    workflow_instance_id=instance.id,
                    task_id=task_id,
                    status=WorkflowTaskStatus.completed,
                    created_at=now,
                    completed_at=now,
                    assigned_user_id=actor_user_id,
                ),
                session=session,
            )
        # New human tasks.
        for new_task in result.new_tasks:
            await self._store.upsert_task(
                WorkflowTask(
                    id=uuid4(),
                    workflow_instance_id=instance.id,
                    task_id=new_task.task_id,
                    status=WorkflowTaskStatus.pending,
                    created_at=now,
                    assigned_role=new_task.lane,
                    approval_policy_id=new_task.approval_policy_id,
                    sla_due_at=new_task.sla_due_at,
                ),
                session=session,
            )
            metrics.workflow_human_task_created_total.add(1, {"workflow_id": instance.workflow_id})
        # Timers: cancel all unfired, re-insert the adapter's current waiting set.
        await self._store.cancel_unfired_timers(instance.id, keep_task_ids=(), session=session)
        for new_timer in result.new_timers:
            await self._store.schedule_timer(
                WorkflowTimer(
                    id=uuid4(),
                    workflow_instance_id=instance.id,
                    task_id=new_timer.task_id,
                    fires_at=new_timer.fires_at,
                    timer_type=new_timer.timer_type,
                    created_at=now,
                ),
                session=session,
            )
        # Transition log + idempotency marker (A5): dedup_key on exactly one row.
        transitions: list[TransitionRecord | None] = list(result.transitions)
        if dedup_key and not transitions:
            transitions = [None]  # marker-only transition
        for index, transition in enumerate(transitions):
            payload: dict[str, Any] = {}
            if dedup_key and index == 0:
                payload["dedup_key"] = dedup_key
            await self._store.append_transition(
                WorkflowTransitionLog(
                    id=None,
                    workflow_instance_id=instance.id,
                    trigger=trigger,
                    transitioned_at=now,
                    from_task_id=transition.from_task_id if transition else None,
                    to_task_id=transition.to_task_id if transition else None,
                    actor_user_id=actor_user_id,
                    payload=payload,
                ),
                session=session,
            )
        metrics.workflow_transition_total.add(
            len(transitions), {"workflow_id": instance.workflow_id}
        )
        # Outbox events derived from the structured result (correct per-event data).
        await self._publish_result_events(
            session,
            instance=instance,
            result=result,
            trigger=trigger,
            actor_user_id=actor_user_id,
            first=first,
        )

    async def _publish_result_events(
        self,
        session: AsyncSession,
        *,
        instance: WorkflowInstance,
        result: AdvanceResult,
        trigger: WorkflowTransitionTrigger,
        actor_user_id: UUID | None,
        first: bool,
    ) -> None:
        iid = instance.id
        wid = instance.workflow_id
        ver = instance.workflow_version
        if first:
            await self._publish(
                session,
                instance=instance,
                event_base="platform.workflow.started",
                data=WorkflowStartedV1(
                    workflow_instance_id=iid,
                    workflow_id=wid,
                    workflow_version=ver,
                    subject_entity_type=instance.subject_entity_type,
                    subject_entity_id=instance.subject_entity_id,
                ),
            )
        for task_id in result.completed_task_ids:
            await self._publish(
                session,
                instance=instance,
                event_base="platform.workflow.task_completed",
                data=WorkflowTaskCompletedV1(
                    workflow_instance_id=iid,
                    workflow_id=wid,
                    workflow_version=ver,
                    task_id=task_id,
                    actor_user_id=actor_user_id,
                ),
            )
        for new_task in result.new_tasks:
            await self._publish(
                session,
                instance=instance,
                event_base="platform.workflow.task_created",
                data=WorkflowTaskCreatedV1(
                    workflow_instance_id=iid,
                    workflow_id=wid,
                    workflow_version=ver,
                    task_id=new_task.task_id,
                    assigned_role=new_task.lane,
                    approval_policy_id=new_task.approval_policy_id,
                ),
            )
        for transition in result.transitions:
            await self._publish(
                session,
                instance=instance,
                event_base="platform.workflow.transitioned",
                data=WorkflowTransitionedV1(
                    workflow_instance_id=iid,
                    workflow_id=wid,
                    workflow_version=ver,
                    from_task_id=transition.from_task_id,
                    to_task_id=transition.to_task_id,
                    trigger=trigger.value,
                ),
            )
        if instance.status is WorkflowInstanceStatus.completed:
            await self._publish(
                session,
                instance=instance,
                event_base="platform.workflow.completed",
                data=WorkflowCompletedV1(
                    workflow_instance_id=iid,
                    workflow_id=wid,
                    workflow_version=ver,
                    outcome=instance.outcome,
                ),
            )
        elif instance.status is WorkflowInstanceStatus.failed:
            payload = result.error_payload or {}
            await self._publish(
                session,
                instance=instance,
                event_base="platform.workflow.failed",
                data=WorkflowFailedV1(
                    workflow_instance_id=iid,
                    workflow_id=wid,
                    workflow_version=ver,
                    error_type=str(payload.get("error_type"))
                    if payload.get("error_type")
                    else None,
                    error=str(payload.get("error")) if payload.get("error") else None,
                ),
            )

    async def _publish(
        self,
        session: AsyncSession,
        *,
        instance: WorkflowInstance,
        event_base: str,
        data: BaseModel,
    ) -> None:
        if event_base not in WORKFLOW_EVENT_PAYLOADS:
            return
        await self._events.append(
            type=f"{event_base}.v1",
            source=_SOURCE,
            subject=f"workflow_instance/{instance.id}",
            data=data,
            tenant_id=instance.tenant_id,
            session=session,
        )


__all__ = ["WorkflowAdvanceService"]
