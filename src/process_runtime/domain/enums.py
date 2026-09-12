"""Enumerations for the platform workflow engine (EPIC-11, ADR-023)."""

from __future__ import annotations

from enum import StrEnum


class WorkflowInstanceStatus(StrEnum):
    """Lifecycle status of a durable workflow instance."""

    active = "active"
    waiting = "waiting"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class WorkflowTaskStatus(StrEnum):
    """Status of a materialised BPMN human task (inbox item)."""

    pending = "pending"
    claimed = "claimed"
    completed = "completed"
    cancelled = "cancelled"


class WorkflowTimerType(StrEnum):
    """Kind of BPMN timer event that produced a scheduled timer."""

    due_date = "due_date"
    cycle = "cycle"
    duration = "duration"


class WorkflowTransitionTrigger(StrEnum):
    """What caused a workflow transition to be attempted."""

    task_complete = "task_complete"
    signal = "signal"
    message = "message"
    timer = "timer"
    migration = "migration"
    cancellation = "cancellation"


class BpmnTaskType(StrEnum):
    """BPMN activity types the platform layer distinguishes."""

    user = "user"
    service = "service"
    script = "script"
    business_rule = "business_rule"
    manual = "manual"
    call_activity = "call_activity"
    sub_process = "sub_process"


class ApprovalDecisionStatus(StrEnum):
    """Terminal decision of an approval request (stub for EPIC-12)."""

    approved = "approved"
    rejected = "rejected"
    changes_requested = "changes_requested"


# Terminal instance statuses — advance is a no-op past these.
TERMINAL_INSTANCE_STATUSES: frozenset[WorkflowInstanceStatus] = frozenset(
    {
        WorkflowInstanceStatus.completed,
        WorkflowInstanceStatus.failed,
        WorkflowInstanceStatus.cancelled,
    }
)

__all__ = [
    "TERMINAL_INSTANCE_STATUSES",
    "ApprovalDecisionStatus",
    "BpmnTaskType",
    "WorkflowInstanceStatus",
    "WorkflowTaskStatus",
    "WorkflowTimerType",
    "WorkflowTransitionTrigger",
]
