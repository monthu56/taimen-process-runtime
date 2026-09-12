"""Value objects for the platform workflow engine (EPIC-11, ADR-023).

Pure-domain, frozen, and free of any integration imports. The SpiffWorkflow engine
is reachable only through the ``WorkflowEngineAdapter`` protocol; adapter-specific
state is carried opaquely inside ``ParsedDefinition.opaque`` and serialized instance
state as ``bytes`` — the domain never inspects either.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from process_runtime.domain.enums import (
    ApprovalDecisionStatus,
    BpmnTaskType,
    WorkflowInstanceStatus,
    WorkflowTimerType,
)

# Sentinel used inside MigrationMap.task_id_mapping to cancel a task on migration.
SKIP_TASK: Literal["__skip__"] = "__skip__"


@dataclass(frozen=True, slots=True)
class BpmnTaskRef:
    """Lightweight reference to a BPMN activity within a parsed definition."""

    task_id: str
    name: str
    type: BpmnTaskType
    lane: str | None = None


@dataclass(frozen=True, slots=True)
class CorrelationRule:
    """Maps an inbound domain event onto a BPMN signal/message for a subscribed workflow.

    ``subject_field`` names the key in the event payload whose value identifies the
    subject entity; ``subject_entity_type`` is matched against
    ``WorkflowInstance.subject_entity_type`` when locating active instances.
    """

    event_type: str
    signal_name: str
    subject_field: str
    subject_entity_type: str
    payload_mapping: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkflowDefinitionDeclaration:
    """Declarative registration of a workflow (git-versioned BPMN + optional DMN)."""

    workflow_id: str
    version: int
    bpmn_path: str
    dmn_paths: tuple[str, ...] = ()
    process_id: str | None = None
    task_handlers: Mapping[str, type] = field(default_factory=dict)
    signal_correlations: tuple[CorrelationRule, ...] = ()
    description: str | None = None
    is_deprecated: bool = False
    is_retired: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "dmn_paths", tuple(self.dmn_paths))
        object.__setattr__(self, "signal_correlations", tuple(self.signal_correlations))
        if self.version < 1:
            raise ValueError("workflow version must be a positive integer")


@dataclass(frozen=True, slots=True)
class MigrationMap:
    """Explicit task mapping for forced migration of a running instance across versions."""

    from_version: int
    to_version: int
    task_id_mapping: Mapping[str, str]
    variables_transformer: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedDefinition:
    """Parser output: BPMN task refs, extension attributes and opaque engine spec.

    ``opaque`` holds the adapter-specific compiled spec (e.g. a SpiffWorkflow
    ``BpmnProcessSpec`` plus subprocess specs). The domain never introspects it.
    """

    workflow_id: str
    version: int
    process_id: str
    user_tasks: tuple[BpmnTaskRef, ...] = ()
    service_tasks: tuple[BpmnTaskRef, ...] = ()
    script_tasks: tuple[BpmnTaskRef, ...] = ()
    business_rule_tasks: tuple[BpmnTaskRef, ...] = ()
    task_extensions: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    opaque: Any = None


@dataclass(frozen=True, slots=True)
class NewHumanTask:
    """A BPMN UserTask that became active during an advance."""

    task_id: str
    name: str
    lane: str | None = None
    form_schema: Mapping[str, Any] | None = None
    approval_policy_id: str | None = None
    sla_due_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NewTimer:
    """A BPMN timer that started waiting during an advance."""

    task_id: str
    fires_at: datetime
    timer_type: WorkflowTimerType


@dataclass(frozen=True, slots=True)
class TransitionRecord:
    """One state transition recorded for the append-only transition log."""

    from_task_id: str | None
    to_task_id: str | None
    event_type: str


@dataclass(frozen=True, slots=True)
class AdvanceResult:
    """Outcome of ``WorkflowEngineAdapter.advance`` — pure data, no side effects."""

    new_state: bytes
    instance_status: WorkflowInstanceStatus
    variables: Mapping[str, Any]
    new_tasks: tuple[NewHumanTask, ...] = ()
    completed_task_ids: tuple[str, ...] = ()
    new_timers: tuple[NewTimer, ...] = ()
    transitions: tuple[TransitionRecord, ...] = ()
    emitted_events: tuple[str, ...] = ()
    active_tokens: tuple[BpmnTaskRef, ...] = ()
    outcome: str | None = None
    error_payload: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ApprovalRequestContext:
    """Context handed to an ApprovalEngineHook when a policy-bearing UserTask activates."""

    workflow_instance_id: UUID
    tenant_id: UUID
    task_id: str
    policy_id: str
    requester_user_id: UUID | None
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ApprovalRequestRef:
    """Opaque handle returned by an ApprovalEngineHook for later polling."""

    value: str


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """Terminal approval decision (stub; EPIC-12 supplies the real approval runtime)."""

    status: ApprovalDecisionStatus
    comment: str | None = None
    decided_by: UUID | None = None
    decided_at: datetime | None = None


__all__ = [
    "SKIP_TASK",
    "AdvanceResult",
    "ApprovalDecision",
    "ApprovalRequestContext",
    "ApprovalRequestRef",
    "BpmnTaskRef",
    "CorrelationRule",
    "MigrationMap",
    "NewHumanTask",
    "NewTimer",
    "ParsedDefinition",
    "TransitionRecord",
    "WorkflowDefinitionDeclaration",
]
