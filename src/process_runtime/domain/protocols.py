"""Protocols for the platform workflow engine (EPIC-11, ADR-023).

These interfaces keep the domain independent of both the persistence layer and the
SpiffWorkflow engine. Concrete implementations live in ``platform_common`` (store,
runtime) and ``platform_integrations.workflow_engine`` (engine adapter, handlers).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from process_runtime.domain.entities import (
    WorkflowInstance,
    WorkflowTask,
    WorkflowTimer,
    WorkflowTransitionLog,
)
from process_runtime.domain.enums import (
    WorkflowTaskStatus,
    WorkflowTransitionTrigger,
)
from process_runtime.domain.value_objects import (
    AdvanceResult,
    ApprovalDecision,
    ApprovalRequestContext,
    ApprovalRequestRef,
    BpmnTaskRef,
    MigrationMap,
    ParsedDefinition,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; avoids a hard SQLAlchemy dep in domain
    from sqlalchemy.ext.asyncio import AsyncSession
else:  # runtime: keep the annotation resolvable without importing SQLAlchemy
    AsyncSession = Any


@dataclass(frozen=True, slots=True)
class WorkflowTaskResult:
    """Return value of a ``WorkflowTaskHandler``."""

    status: Literal["completed", "failed", "suspended"]
    output_variables: dict[str, Any] | None = None
    error: str | None = None


@runtime_checkable
class WorkflowTaskContext(Protocol):
    """Execution context handed to a service/script task handler.

    The context is tenant-scoped; ``set_variable`` mutates workflow variables and
    ``emit_event`` publishes a domain event through the outbox in the same transaction.
    """

    @property
    def workflow_instance_id(self) -> UUID: ...

    @property
    def tenant_id(self) -> UUID: ...

    @property
    def task_id(self) -> str: ...

    @property
    def variables(self) -> Mapping[str, Any]: ...

    def set_variable(self, name: str, value: Any) -> None: ...

    def emit_event(self, event_type: str, payload: Mapping[str, Any]) -> None: ...


@runtime_checkable
class WorkflowTaskHandler(Protocol):
    """Product-supplied handler for a BPMN service task."""

    async def execute(self, context: WorkflowTaskContext) -> WorkflowTaskResult: ...


@runtime_checkable
class WorkflowEngineAdapter(Protocol):
    """Isolates the BPMN engine (SpiffWorkflow) behind a swappable interface."""

    def parse(
        self,
        *,
        bpmn_xml: bytes,
        dmn_xmls: Mapping[str, bytes],
        workflow_id: str,
        version: int,
        process_id: str | None = None,
    ) -> ParsedDefinition: ...

    def build_initial_state(
        self, parsed: ParsedDefinition, variables: Mapping[str, Any]
    ) -> AdvanceResult: ...

    def advance(
        self,
        *,
        state: bytes,
        trigger: WorkflowTransitionTrigger,
        payload: Mapping[str, Any],
        parsed: ParsedDefinition,
        now: datetime,
    ) -> AdvanceResult: ...

    def migrate(
        self,
        *,
        state: bytes,
        parsed_from: ParsedDefinition,
        parsed_to: ParsedDefinition,
        migration: MigrationMap,
    ) -> bytes: ...

    def active_tasks(
        self, *, state: bytes, parsed: ParsedDefinition
    ) -> tuple[BpmnTaskRef, ...]: ...


@runtime_checkable
class WorkflowEngineStore(Protocol):
    """Async persistence for workflow instances, tasks, timers and the transition log.

    Every method takes an open ``session`` and never commits — the caller owns the
    transaction (matching ``PostgresOutboxStore``). ``load_instance(for_update=True)``
    takes a per-instance ``pg_advisory_xact_lock`` before the row ``FOR UPDATE``.
    """

    async def create_instance(
        self, instance: WorkflowInstance, *, session: AsyncSession
    ) -> WorkflowInstance: ...

    async def load_instance(
        self,
        instance_id: UUID,
        *,
        tenant_id: UUID | None = None,
        for_update: bool = False,
        session: AsyncSession,
    ) -> WorkflowInstance | None: ...

    async def save_instance(self, instance: WorkflowInstance, *, session: AsyncSession) -> None: ...

    async def append_transition(
        self, log: WorkflowTransitionLog, *, session: AsyncSession
    ) -> None: ...

    async def has_dedup_transition(
        self, instance_id: UUID, dedup_key: str, *, session: AsyncSession
    ) -> bool: ...

    async def upsert_task(self, task: WorkflowTask, *, session: AsyncSession) -> None: ...

    async def list_tasks(
        self, instance_id: UUID, *, tenant_id: UUID, session: AsyncSession
    ) -> Sequence[WorkflowTask]: ...

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
    ) -> Sequence[WorkflowTask]: ...

    async def schedule_timer(
        self, timer: WorkflowTimer, *, session: AsyncSession
    ) -> WorkflowTimer: ...

    async def cancel_unfired_timers(
        self,
        instance_id: UUID,
        *,
        keep_task_ids: Sequence[str],
        session: AsyncSession,
    ) -> int: ...

    async def list_due_timers(
        self, now: datetime, *, limit: int, session: AsyncSession
    ) -> Sequence[WorkflowTimer]: ...

    async def mark_timer_fired(
        self, timer_id: UUID, fired_at: datetime, *, session: AsyncSession
    ) -> None: ...

    async def find_active_by_subject(
        self,
        *,
        tenant_id: UUID,
        subject_entity_type: str,
        subject_entity_id: UUID,
        workflow_id: str | None = None,
        session: AsyncSession,
    ) -> Sequence[WorkflowInstance]: ...


@runtime_checkable
class WorkflowJobPort(Protocol):
    """Narrow enqueue facade the workflow layer owns.

    The single production implementation adapts ``platform_common.jobs.JobDispatcher``.
    Keeping this port in the domain means the ARQ→TaskIQ migration (EPIC-14) swaps one
    adapter, not the runtime. Returns ``False`` when the job was not enqueued.
    """

    async def enqueue_advance(
        self,
        *,
        instance_id: UUID,
        trigger: str,
        payload: Mapping[str, Any],
        tenant_id: UUID,
        dedup_key: str | None = None,
    ) -> bool: ...


@runtime_checkable
class ApprovalEngineHook(Protocol):
    """Integration seam for the Approval Engine (EPIC-12, ADR-024)."""

    async def start_approval(self, context: ApprovalRequestContext) -> ApprovalRequestRef: ...

    async def is_approval_complete(self, ref: ApprovalRequestRef) -> ApprovalDecision | None: ...


__all__ = [
    "ApprovalEngineHook",
    "WorkflowEngineAdapter",
    "WorkflowEngineStore",
    "WorkflowJobPort",
    "WorkflowTaskContext",
    "WorkflowTaskHandler",
    "WorkflowTaskResult",
]
