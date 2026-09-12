"""Concrete WorkflowTaskContext for service-task handlers (EPIC-11, ADR-023, WF-010).

A lightweight, tenant-scoped context that records variable mutations and emitted events.
The runtime collects ``pending_variables`` back into the instance and flushes
``pending_events`` through the outbox in the same transaction.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID


class SimpleWorkflowTaskContext:
    """In-transaction context passed to a ``WorkflowTaskHandler.execute``."""

    def __init__(
        self,
        *,
        workflow_instance_id: UUID,
        tenant_id: UUID,
        task_id: str,
        variables: Mapping[str, Any],
    ) -> None:
        self._workflow_instance_id = workflow_instance_id
        self._tenant_id = tenant_id
        self._task_id = task_id
        self._variables = dict(variables)
        self.pending_variables: dict[str, Any] = {}
        self.pending_events: list[tuple[str, dict[str, Any]]] = []

    @property
    def workflow_instance_id(self) -> UUID:
        return self._workflow_instance_id

    @property
    def tenant_id(self) -> UUID:
        return self._tenant_id

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def variables(self) -> Mapping[str, Any]:
        return dict(self._variables)

    def set_variable(self, name: str, value: Any) -> None:
        self._variables[name] = value
        self.pending_variables[name] = value

    def emit_event(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self.pending_events.append((event_type, dict(payload)))


__all__ = ["SimpleWorkflowTaskContext"]
