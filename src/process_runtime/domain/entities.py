"""Persistence entities for the platform workflow engine (EPIC-11, ADR-023).

Frozen dataclasses mirroring the four workflow-engine tables. ``tenant_id`` is
mandatory on ``WorkflowInstance`` and enforces the multi-tenant boundary. The
``spiff_state`` field carries the adapter-serialized instance state as ``bytes``
(UTF-8 JSON) and is treated opaquely by the domain and API layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from process_runtime.domain.enums import (
    WorkflowInstanceStatus,
    WorkflowTaskStatus,
    WorkflowTimerType,
    WorkflowTransitionTrigger,
)


@dataclass(frozen=True, slots=True)
class WorkflowInstance:
    """A durable workflow instance: persistence wrapper around serialized engine state."""

    id: UUID
    tenant_id: UUID
    workflow_id: str
    workflow_version: int
    status: WorkflowInstanceStatus
    spiff_state: bytes
    started_at: datetime
    updated_at: datetime
    subject_entity_type: str | None = None
    subject_entity_id: UUID | None = None
    current_tasks: tuple[str, ...] = ()
    variables: dict[str, Any] = field(default_factory=dict)
    completed_at: datetime | None = None
    outcome: str | None = None
    error_payload: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "current_tasks", tuple(self.current_tasks))


@dataclass(frozen=True, slots=True)
class WorkflowTransitionLog:
    """Append-only record of a single workflow transition (ADR-035 audit precursor)."""

    id: int | None
    workflow_instance_id: UUID
    trigger: WorkflowTransitionTrigger
    transitioned_at: datetime
    from_task_id: str | None = None
    to_task_id: str | None = None
    actor_user_id: UUID | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkflowTimer:
    """A scheduled BPMN timer awaiting its firing instant."""

    id: UUID
    workflow_instance_id: UUID
    task_id: str
    fires_at: datetime
    timer_type: WorkflowTimerType
    created_at: datetime
    fired_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class WorkflowTask:
    """A materialised BPMN human task for the inbox."""

    id: UUID
    workflow_instance_id: UUID
    task_id: str
    status: WorkflowTaskStatus
    created_at: datetime
    assigned_role: str | None = None
    assigned_user_id: UUID | None = None
    form_payload: dict[str, Any] = field(default_factory=dict)
    claimed_at: datetime | None = None
    completed_at: datetime | None = None
    sla_due_at: datetime | None = None
    approval_policy_id: str | None = None


__all__ = [
    "WorkflowInstance",
    "WorkflowTask",
    "WorkflowTimer",
    "WorkflowTransitionLog",
]
