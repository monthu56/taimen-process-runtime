"""Domain errors for the platform workflow engine (EPIC-11, ADR-023).

All errors are pure-domain and carry no integration/infrastructure dependencies.
``RegistryConflictError`` and the validation errors subclass ``ValueError`` so the
bootstrap phase machine (``apps/api/src/platform_api/bootstrap.py``) wraps them into
a ``StartupFailureRecord`` via its existing ``except (RuntimeError, ValueError)`` guard.
"""

from __future__ import annotations


class WorkflowEngineError(Exception):
    """Base class for all workflow-engine domain errors."""


class WorkflowInstanceNotFound(WorkflowEngineError):
    """Raised when a workflow instance id does not resolve within the tenant scope."""


class WorkflowTaskNotClaimable(WorkflowEngineError):
    """Raised when a task cannot be completed: unknown id, wrong state, or wrong assignee."""


class WorkflowInstanceAlreadyCompleted(WorkflowEngineError):
    """Raised when a mutating action targets an instance in a terminal status."""


class WorkflowVersionFrozen(WorkflowEngineError):
    """Raised when a running instance version cannot be used as-is (e.g. retired target)."""


class WorkflowDefinitionInvalid(ValueError, WorkflowEngineError):
    """Raised when a BPMN/DMN definition fails to parse or validate on boot."""


class WorkflowMigrationMapInconsistent(WorkflowEngineError):
    """Raised when a forced-migration MigrationMap does not cover all active tasks."""


class ScriptTaskSecurityError(WorkflowEngineError):
    """Raised when a BPMN script task attempts a sandbox-forbidden operation."""


class WorkflowApprovalHookNotConfigured(WorkflowEngineError):
    """Raised when a UserTask carries an approval policy but no real hook is registered."""


class RegistryConflictError(ValueError, WorkflowEngineError):
    """Raised when two extensions register the same ``(workflow_id, version)``."""


__all__ = [
    "RegistryConflictError",
    "ScriptTaskSecurityError",
    "WorkflowApprovalHookNotConfigured",
    "WorkflowDefinitionInvalid",
    "WorkflowEngineError",
    "WorkflowInstanceAlreadyCompleted",
    "WorkflowInstanceNotFound",
    "WorkflowMigrationMapInconsistent",
    "WorkflowTaskNotClaimable",
    "WorkflowVersionFrozen",
]
