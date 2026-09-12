"""Platform workflow engine domain module (EPIC-11, ADR-023).

Product-neutral core for BPMN 2.0 workflow orchestration: enums, frozen entities,
value objects, protocols and errors. This package MUST NOT import ``platform_*``
or ``platform_integrations.workflow_engine`` — the SpiffWorkflow engine is reachable
only through the ``WorkflowEngineAdapter`` protocol.
"""

from __future__ import annotations

from process_runtime.domain.entities import (
    WorkflowInstance,
    WorkflowTask,
    WorkflowTimer,
    WorkflowTransitionLog,
)
from process_runtime.domain.enums import (
    TERMINAL_INSTANCE_STATUSES,
    ApprovalDecisionStatus,
    BpmnTaskType,
    WorkflowInstanceStatus,
    WorkflowTaskStatus,
    WorkflowTimerType,
    WorkflowTransitionTrigger,
)
from process_runtime.domain.errors import (
    RegistryConflictError,
    ScriptTaskSecurityError,
    WorkflowApprovalHookNotConfigured,
    WorkflowDefinitionInvalid,
    WorkflowEngineError,
    WorkflowInstanceAlreadyCompleted,
    WorkflowInstanceNotFound,
    WorkflowMigrationMapInconsistent,
    WorkflowTaskNotClaimable,
    WorkflowVersionFrozen,
)
from process_runtime.domain.protocols import (
    ApprovalEngineHook,
    WorkflowEngineAdapter,
    WorkflowEngineStore,
    WorkflowJobPort,
    WorkflowTaskContext,
    WorkflowTaskHandler,
    WorkflowTaskResult,
)
from process_runtime.domain.value_objects import (
    SKIP_TASK,
    AdvanceResult,
    ApprovalDecision,
    ApprovalRequestContext,
    ApprovalRequestRef,
    BpmnTaskRef,
    CorrelationRule,
    MigrationMap,
    NewHumanTask,
    NewTimer,
    ParsedDefinition,
    TransitionRecord,
    WorkflowDefinitionDeclaration,
)

__all__ = [
    "SKIP_TASK",
    "TERMINAL_INSTANCE_STATUSES",
    "AdvanceResult",
    "ApprovalDecision",
    "ApprovalDecisionStatus",
    "ApprovalEngineHook",
    "ApprovalRequestContext",
    "ApprovalRequestRef",
    "BpmnTaskRef",
    "BpmnTaskType",
    "CorrelationRule",
    "MigrationMap",
    "NewHumanTask",
    "NewTimer",
    "ParsedDefinition",
    "RegistryConflictError",
    "ScriptTaskSecurityError",
    "TransitionRecord",
    "WorkflowApprovalHookNotConfigured",
    "WorkflowDefinitionDeclaration",
    "WorkflowDefinitionInvalid",
    "WorkflowEngineAdapter",
    "WorkflowEngineError",
    "WorkflowEngineStore",
    "WorkflowInstance",
    "WorkflowInstanceAlreadyCompleted",
    "WorkflowInstanceNotFound",
    "WorkflowInstanceStatus",
    "WorkflowJobPort",
    "WorkflowMigrationMapInconsistent",
    "WorkflowTask",
    "WorkflowTaskContext",
    "WorkflowTaskHandler",
    "WorkflowTaskNotClaimable",
    "WorkflowTaskResult",
    "WorkflowTaskStatus",
    "WorkflowTimer",
    "WorkflowTimerType",
    "WorkflowTransitionLog",
    "WorkflowTransitionTrigger",
    "WorkflowVersionFrozen",
]
