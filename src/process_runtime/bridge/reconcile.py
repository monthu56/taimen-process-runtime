"""Reconciliation between the process event log and the Control Plane work graph.

Superproject ADR-0023: activities that need organisational action are materialised as
``Task`` in the Control Plane through a durable binding; the engine never completes a
Task directly and the Control Plane never advances a token without a confirmed command.
Both directions are pull loops over journals with durable cursors, idempotent on
retry, and stop on the first event they cannot process (a poison event is retried,
never skipped — the same policy as the bidops orchestrator).

Outbound (process log → Control Plane):
  ``task_created``   → ``POST /tasks`` (Idempotency-Key from the event) + generic
                       external reference ``process-runtime/activity`` on the Task;
  ``task_completed`` → the Task is completed in the Control Plane, unless the
                       completion came from the Control Plane itself (binding is
                       ``completing``);
  instance terminal  → open Tasks get a comment and, where the type allows, move to a
                       cancelled status.

Inbound (Control Plane journal → process):
  ``task.completed`` / ``task.updated`` with ``systemStatusCategory=terminal_success`` →
  the bound activity is completed with the Task's custom fields as form payload (dedup
  key = the event id); ``terminal_cancelled`` releases the binding and leaves the token
  waiting for an operator.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from process_runtime.bridge.store import BindingStore, TaskBinding
from process_runtime.domain.enums import WorkflowTransitionTrigger
from process_runtime.domain.errors import (
    WorkflowInstanceAlreadyCompleted,
    WorkflowInstanceNotFound,
    WorkflowTaskNotClaimable,
)
from process_runtime.runtime.loader import WorkflowDefinitionRegistry
from process_runtime.runtime.pg_store import PostgresWorkflowEngineStore
from process_runtime.runtime.runtime import WorkflowAdvanceService
from process_runtime.tables import process_events

_log = logging.getLogger("process_runtime.bridge")

EXTERNAL_SYSTEM = "process-runtime"
EXTERNAL_TYPE = "activity"
OUTBOUND_CURSOR = "outbound"
INBOUND_CURSOR = "inbound"
FIELD_PREFIX = "process"
# Control Plane system status categories (CP-ADR-0031/0048): the core branches only on
# these; the status keys themselves are declared per task type and never compared here.
DONE_CATEGORIES = frozenset({"terminal_success"})
CANCELLED_CATEGORIES = frozenset({"terminal_cancelled"})


class ControlPlane(Protocol):
    """The slice of ``control_plane_client.ControlPlaneClient`` the bridge uses."""

    async def create_task(self, **kwargs: Any) -> dict[str, Any]: ...
    async def register_external_reference(self, **kwargs: Any) -> dict[str, Any]: ...
    async def get_task(self, task_ref: str) -> dict[str, Any]: ...
    async def get_task_transitions(self, task_ref: str) -> dict[str, Any]: ...
    async def complete_task(
        self, task_ref: str, *, version: int, **kwargs: Any
    ) -> dict[str, Any]: ...
    async def update_task(
        self, task_ref: str, *, expected_version: int, **kwargs: Any
    ) -> dict[str, Any]: ...
    async def add_task_comment(
        self, task_ref: str, *, body: str, **kwargs: Any
    ) -> dict[str, Any]: ...
    async def list_events(
        self, *, cursor: str | None = None, limit: int | None = None, **params: Any
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    tenant_id: UUID
    workspace_id: str
    task_type_key: str | None = None
    batch_size: int = 100


@dataclass(frozen=True, slots=True)
class PassResult:
    processed: int
    stalled: bool = False


class ControlPlaneBridge:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        cp: ControlPlane,
        config: BridgeConfig,
        store: PostgresWorkflowEngineStore,
        service: WorkflowAdvanceService,
        registry: WorkflowDefinitionRegistry,
        bindings: BindingStore | None = None,
    ) -> None:
        self._sessions = session_factory
        self._cp = cp
        self._config = config
        self._store = store
        self._service = service
        self._registry = registry
        self._bindings = bindings or BindingStore()

    # ------------------------------------------------------------- outbound
    async def outbound_once(self) -> PassResult:
        async with self._sessions() as session:
            raw = await self._bindings.get_cursor(OUTBOUND_CURSOR, session=session)
            after = int(raw) if raw else 0
            rows = (
                await session.execute(
                    select(*process_events.c)
                    .where(
                        process_events.c.id > after,
                        process_events.c.tenant_id == self._config.tenant_id,
                    )
                    .order_by(process_events.c.id.asc())
                    .limit(self._config.batch_size)
                )
            ).fetchall()
        processed = 0
        stalled = False
        durable = after
        for row in rows:
            try:
                await self._handle_process_event(row)
            except Exception:
                _log.exception(
                    "bridge outbound: event %s (%s) failed; cursor stays before it",
                    row.id,
                    row.event_type,
                )
                stalled = True
                break
            processed += 1
            durable = row.id
        if durable != after:
            async with self._sessions() as session:
                await self._bindings.set_cursor(OUTBOUND_CURSOR, str(durable), session=session)
                await session.commit()
        return PassResult(processed=processed, stalled=stalled)

    async def _handle_process_event(self, row: Any) -> None:
        base = row.event_type.removesuffix(".v1")
        data = dict(row.payload)
        instance_id = UUID(str(data["workflow_instance_id"]))
        if base == "platform.workflow.task_created":
            await self._materialise_task(row.id, instance_id, data)
        elif base == "platform.workflow.task_completed":
            await self._complete_cp_task(instance_id, str(data["task_id"]))
        elif base in (
            "platform.workflow.cancelled",
            "platform.workflow.failed",
            "platform.workflow.completed",
        ):
            await self._release_instance_tasks(instance_id, base.rsplit(".", 1)[-1], data)

    async def _materialise_task(
        self, event_id: int, instance_id: UUID, data: dict[str, Any]
    ) -> None:
        task_id = str(data["task_id"])
        async with self._sessions() as session:
            if await self._bindings.by_source_event(event_id, session=session) is not None:
                return  # already materialised (retry after a crash between CP and commit)
            instance = await self._store.load_instance(instance_id, session=session)
        if instance is None:
            return
        name = task_id
        parsed = self._registry.get(instance.workflow_id, instance.workflow_version)
        if parsed is not None:
            for ref in parsed.user_tasks:
                if ref.task_id == task_id:
                    name = ref.name
                    break
        lane = data.get("assigned_role")
        fields: dict[str, Any] = {
            f"{FIELD_PREFIX}InstanceId": str(instance_id),
            f"{FIELD_PREFIX}WorkflowId": instance.workflow_id,
            f"{FIELD_PREFIX}WorkflowVersion": instance.workflow_version,
            f"{FIELD_PREFIX}TaskId": task_id,
        }
        if lane:
            fields[f"{FIELD_PREFIX}Lane"] = lane
        if instance.subject_entity_type and instance.subject_entity_id:
            fields[f"{FIELD_PREFIX}Subject"] = (
                f"{instance.subject_entity_type}/{instance.subject_entity_id}"
            )
        description = (
            f"Шаг процесса `{instance.workflow_id}` v{instance.workflow_version}, "
            f"activity `{task_id}`, instance `{instance_id}`.\n\n"
            "Завершение этой задачи продвигает процесс; значения customFields "
            "передаются в BPMN как данные формы."
        )
        task = await self._cp.create_task(
            title=f"{name} · {instance.workflow_id}"[:500],
            description=description,
            priority="medium",
            type_key=self._config.task_type_key or None,
            workspace_id=self._config.workspace_id,
            custom_fields=fields,
            idempotency_key=f"{EXTERNAL_SYSTEM}:{instance_id}:{task_id}:{event_id}",
        )
        cp_task_id = UUID(str(task["id"]))
        await self._cp.register_external_reference(
            entity_type="task",
            entity_id=str(cp_task_id),
            external_system=EXTERNAL_SYSTEM,
            external_type=EXTERNAL_TYPE,
            external_id=f"{instance_id}/{task_id}/{event_id}",
            metadata={"workflowId": instance.workflow_id, "lane": lane},
        )
        async with self._sessions() as session:
            await self._bindings.create(
                tenant_id=instance.tenant_id,
                workflow_instance_id=instance_id,
                task_id=task_id,
                source_event_id=event_id,
                cp_task_id=cp_task_id,
                cp_public_id=task.get("publicId"),
                session=session,
            )
            await session.commit()

    async def _complete_cp_task(self, instance_id: UUID, task_id: str) -> None:
        async with self._sessions() as session:
            binding = await self._bindings.open_for_activity(instance_id, task_id, session=session)
        if binding is None:
            return
        if binding.status == "completing":
            # The completion came from the Control Plane; nothing to echo back.
            await self._set_status(binding, "completed")
            return
        cp_task = await self._cp.get_task(str(binding.cp_task_id))
        category = str(cp_task.get("systemStatusCategory") or "")
        if category not in DONE_CATEGORIES | CANCELLED_CATEGORIES:
            try:
                await self._cp.complete_task(
                    str(binding.cp_task_id), version=int(cp_task["version"])
                )
            except Exception as exc:
                _log.warning("bridge: cannot complete cp task %s: %s", binding.cp_public_id, exc)
                await self._cp.add_task_comment(
                    str(binding.cp_task_id),
                    body=f"Шаг `{task_id}` завершён на стороне процесса; закройте задачу.",
                )
        await self._set_status(binding, "completed")

    async def _release_instance_tasks(
        self, instance_id: UUID, outcome: str, data: dict[str, Any]
    ) -> None:
        async with self._sessions() as session:
            open_bindings = await self._bindings.open_for_instance(instance_id, session=session)
        for binding in open_bindings:
            note = f"Процесс `{data.get('workflow_id')}` завершился ({outcome})"
            if data.get("reason"):
                note += f": {data['reason']}"
            await self._cp.add_task_comment(str(binding.cp_task_id), body=note)
            cp_task = await self._cp.get_task(str(binding.cp_task_id))
            category = str(cp_task.get("systemStatusCategory") or "")
            if category not in DONE_CATEGORIES | CANCELLED_CATEGORIES:
                target = await self._cancel_target(str(binding.cp_task_id))
                if target is not None:
                    await self._cp.update_task(
                        str(binding.cp_task_id),
                        expected_version=int(cp_task["version"]),
                        status=target,
                    )
            await self._set_status(binding, "cancelled")

    async def _cancel_target(self, task_ref: str) -> str | None:
        transitions = await self._cp.get_task_transitions(task_ref)
        for target in transitions.get("targets", []):
            if str(target.get("systemStatusCategory")) in CANCELLED_CATEGORIES:
                return str(target["status"])
        return None

    async def _set_status(self, binding: TaskBinding, status: str) -> None:
        async with self._sessions() as session:
            await self._bindings.set_status(binding.id, status, session=session)
            await session.commit()

    # -------------------------------------------------------------- inbound
    async def inbound_once(self) -> PassResult:
        async with self._sessions() as session:
            cursor = await self._bindings.get_cursor(INBOUND_CURSOR, session=session)
        page = await self._cp.list_events(cursor=cursor, limit=self._config.batch_size)
        processed = 0
        stalled = False
        durable = cursor
        items = page.get("items", [])
        for event in items:
            try:
                await self._handle_cp_event(event)
            except Exception:
                _log.exception(
                    "bridge inbound: event %s (%s) failed; cursor stays before it",
                    event.get("id"),
                    event.get("type"),
                )
                stalled = True
                break
            processed += 1
            durable = event.get("cursor", durable)
        else:
            if items and page.get("nextCursor"):
                durable = page["nextCursor"]
        if durable != cursor:
            async with self._sessions() as session:
                await self._bindings.set_cursor(INBOUND_CURSOR, durable, session=session)
                await session.commit()
        return PassResult(processed=processed, stalled=stalled)

    async def _handle_cp_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type", ""))
        payload = event.get("payload") or {}
        if event_type == "task.completed":
            category = str(payload.get("systemStatusCategory") or "terminal_success")
        elif event_type == "task.updated" and payload.get("systemStatusCategory"):
            category = str(payload["systemStatusCategory"])
        else:
            return
        if category not in DONE_CATEGORIES | CANCELLED_CATEGORIES:
            return
        cp_task_id = UUID(str(event["entityId"]))
        async with self._sessions() as session:
            binding = await self._bindings.by_cp_task(cp_task_id, session=session)
        if binding is None or binding.status not in ("open",):
            return
        if category in CANCELLED_CATEGORIES:
            _log.info(
                "bridge: cp task %s cancelled; activity %s of %s stays waiting for an operator",
                binding.cp_public_id,
                binding.task_id,
                binding.workflow_instance_id,
            )
            await self._set_status(binding, "cancelled")
            return
        cp_task = await self._cp.get_task(str(cp_task_id))
        form = {
            key: value
            for key, value in (cp_task.get("customFields") or {}).items()
            if not str(key).startswith(FIELD_PREFIX)
        }
        await self._set_status(binding, "completing")
        actor = event.get("actorId")
        try:
            await self._service.advance(
                instance_id=binding.workflow_instance_id,
                trigger=WorkflowTransitionTrigger.task_complete,
                tenant_id=binding.tenant_id,
                actor_user_id=UUID(str(actor)) if actor else None,
                payload={
                    "task_id": binding.task_id,
                    "form_payload": form,
                    "dedup_key": f"cp:{event.get('id')}",
                },
            )
        except (
            WorkflowTaskNotClaimable,
            WorkflowInstanceAlreadyCompleted,
            WorkflowInstanceNotFound,
        ) as exc:
            # The activity is no longer waiting (completed through the process API, or the
            # instance ended): the binding is closed, nothing to advance.
            _log.info("bridge: activity %s not advanced: %s", binding.task_id, exc)
            await self._set_status(binding, "completed")
            return
        # The engine records the completion event; ``_complete_cp_task`` will see
        # ``completing`` and close the binding without echoing to the Control Plane.


__all__ = [
    "EXTERNAL_SYSTEM",
    "EXTERNAL_TYPE",
    "BridgeConfig",
    "ControlPlane",
    "ControlPlaneBridge",
    "PassResult",
]
