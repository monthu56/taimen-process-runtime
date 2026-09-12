"""Sample workflow task handler (EPIC-11, ADR-023, WF-010).

``SimpleNotificationHandler`` logs the task and emits a ``platform.notification.requested``
event, then completes. Built-in handlers are addressed by name from ``definitions.json``
(``task_handlers: {"<bpmn task id>": "notification"}``); see ``BUILTIN_HANDLERS``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from process_runtime.domain.protocols import (
    WorkflowTaskContext,
    WorkflowTaskResult,
)

_log = logging.getLogger(__name__)


class SimpleNotificationHandler:
    """Emits a notification-requested event and completes the task."""

    async def execute(self, context: WorkflowTaskContext) -> WorkflowTaskResult:
        _log.info(
            "workflow.handler.notification instance=%s tenant=%s task=%s",
            context.workflow_instance_id,
            context.tenant_id,
            context.task_id,
        )
        context.emit_event(
            "platform.notification.requested",
            {
                "workflow_instance_id": str(context.workflow_instance_id),
                "task_id": context.task_id,
            },
        )
        return WorkflowTaskResult(status="completed")


BUILTIN_HANDLERS: dict[str, type] = {"notification": SimpleNotificationHandler}


def resolve_handlers(mapping: Mapping[str, str]) -> dict[str, type]:
    """Map ``task_id -> handler name`` from a definitions file onto handler classes."""
    resolved: dict[str, type] = {}
    for task_id, name in mapping.items():
        handler = BUILTIN_HANDLERS.get(name)
        if handler is None:
            raise ValueError(f"unknown task handler '{name}' for task '{task_id}'")
        resolved[task_id] = handler
    return resolved


__all__ = ["BUILTIN_HANDLERS", "SimpleNotificationHandler", "resolve_handlers"]
