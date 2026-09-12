"""DummyAdapter — deterministic in-memory engine for contract tests and dev (WF-004).

A 3-step state machine: start → single user task ``review`` → completed. It exercises the
full ``WorkflowEngineAdapter`` protocol without SpiffWorkflow, so store/runtime/API tests
run fast and hermetically. State is a small JSON document carried as ``bytes``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from process_runtime.domain.enums import (
    BpmnTaskType,
    WorkflowInstanceStatus,
    WorkflowTransitionTrigger,
)
from process_runtime.domain.value_objects import (
    AdvanceResult,
    BpmnTaskRef,
    MigrationMap,
    NewHumanTask,
    ParsedDefinition,
    TransitionRecord,
)

_REVIEW_TASK = "review"


class DummyAdapter:
    """Deterministic fake adapter. ``parse`` ignores XML and returns a fixed shape."""

    def parse(
        self,
        *,
        bpmn_xml: bytes,
        dmn_xmls: Mapping[str, bytes],
        workflow_id: str,
        version: int,
        process_id: str | None = None,
    ) -> ParsedDefinition:
        review = BpmnTaskRef(task_id=_REVIEW_TASK, name="Review", type=BpmnTaskType.user)
        return ParsedDefinition(
            workflow_id=workflow_id,
            version=version,
            process_id=process_id or f"{workflow_id}_process",
            user_tasks=(review,),
            opaque={"kind": "dummy"},
        )

    def build_initial_state(
        self, parsed: ParsedDefinition, variables: Mapping[str, Any]
    ) -> AdvanceResult:
        state = {"step": _REVIEW_TASK, "variables": dict(variables)}
        return AdvanceResult(
            new_state=self._dump(state),
            instance_status=WorkflowInstanceStatus.waiting,
            variables=dict(variables),
            new_tasks=(NewHumanTask(task_id=_REVIEW_TASK, name="Review"),),
            transitions=(
                TransitionRecord(from_task_id=None, to_task_id=_REVIEW_TASK, event_type="started"),
            ),
            emitted_events=("platform.workflow.started", "platform.workflow.task_created"),
            active_tokens=(
                BpmnTaskRef(task_id=_REVIEW_TASK, name="Review", type=BpmnTaskType.user),
            ),
        )

    def advance(
        self,
        *,
        state: bytes,
        trigger: WorkflowTransitionTrigger,
        payload: Mapping[str, Any],
        parsed: ParsedDefinition,
        now: datetime,
    ) -> AdvanceResult:
        current = json.loads(state.decode("utf-8"))
        variables = dict(current.get("variables", {}))
        if current.get("step") != _REVIEW_TASK:
            # Already terminal — idempotent no-op.
            return AdvanceResult(
                new_state=state,
                instance_status=WorkflowInstanceStatus.completed,
                variables=variables,
                outcome=str(variables.get("outcome", "completed")),
            )
        if trigger is not WorkflowTransitionTrigger.task_complete:
            # No matching waiter for other triggers — no-op, still waiting.
            return AdvanceResult(
                new_state=state,
                instance_status=WorkflowInstanceStatus.waiting,
                variables=variables,
                active_tokens=(
                    BpmnTaskRef(task_id=_REVIEW_TASK, name="Review", type=BpmnTaskType.user),
                ),
            )
        form = dict(payload.get("form_payload", {}))
        variables.update(form)
        outcome = str(form.get("decision", "approved"))
        variables["outcome"] = outcome
        state_out = {"step": "__done__", "variables": variables}
        return AdvanceResult(
            new_state=self._dump(state_out),
            instance_status=WorkflowInstanceStatus.completed,
            variables=variables,
            completed_task_ids=(_REVIEW_TASK,),
            transitions=(
                TransitionRecord(
                    from_task_id=_REVIEW_TASK, to_task_id=None, event_type="task_completed"
                ),
            ),
            emitted_events=(
                "platform.workflow.task_completed",
                "platform.workflow.completed",
            ),
            outcome=outcome,
        )

    def migrate(
        self,
        *,
        state: bytes,
        parsed_from: ParsedDefinition,
        parsed_to: ParsedDefinition,
        migration: MigrationMap,
    ) -> bytes:
        current = json.loads(state.decode("utf-8"))
        step = current.get("step")
        mapped = migration.task_id_mapping.get(step, step)
        current["step"] = mapped
        return self._dump(current)

    def active_tasks(self, *, state: bytes, parsed: ParsedDefinition) -> tuple[BpmnTaskRef, ...]:
        current = json.loads(state.decode("utf-8"))
        if current.get("step") == _REVIEW_TASK:
            return (BpmnTaskRef(task_id=_REVIEW_TASK, name="Review", type=BpmnTaskType.user),)
        return ()

    @staticmethod
    def _dump(state: dict[str, Any]) -> bytes:
        return json.dumps(state, ensure_ascii=False).encode("utf-8")


__all__ = ["DummyAdapter"]
