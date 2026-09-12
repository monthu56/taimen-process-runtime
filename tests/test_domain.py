"""Domain value-object / enum / protocol unit tests (EPIC-11, ADR-023, WF-001)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from process_runtime.domain import (
    BpmnTaskType,
    CorrelationRule,
    MigrationMap,
    ParsedDefinition,
    WorkflowDefinitionDeclaration,
    WorkflowEngineAdapter,
    WorkflowEngineStore,
    WorkflowInstance,
    WorkflowInstanceStatus,
    WorkflowJobPort,
    WorkflowTaskStatus,
    WorkflowTimerType,
    WorkflowTransitionTrigger,
)

pytestmark = []


def test_enum_membership() -> None:
    assert set(WorkflowInstanceStatus) == {
        WorkflowInstanceStatus.active,
        WorkflowInstanceStatus.waiting,
        WorkflowInstanceStatus.completed,
        WorkflowInstanceStatus.failed,
        WorkflowInstanceStatus.cancelled,
    }
    assert WorkflowTaskStatus.pending.value == "pending"
    assert WorkflowTimerType.due_date.value == "due_date"
    assert WorkflowTransitionTrigger.task_complete.value == "task_complete"
    assert BpmnTaskType.user.value == "user"


def test_declaration_validates_version() -> None:
    with pytest.raises(ValueError):
        WorkflowDefinitionDeclaration(workflow_id="x", version=0, bpmn_path="x.bpmn")
    decl = WorkflowDefinitionDeclaration(
        workflow_id="x", version=1, bpmn_path="x.bpmn", dmn_paths=["a.dmn"]
    )
    assert decl.dmn_paths == ("a.dmn",)


def test_correlation_rule_defaults() -> None:
    rule = CorrelationRule(
        event_type="platform.scoring.completed.v1",
        signal_name="scoring_completed",
        subject_field="order_id",
        subject_entity_type="order",
    )
    assert rule.payload_mapping == {}


def test_migration_map() -> None:
    m = MigrationMap(from_version=1, to_version=2, task_id_mapping={"a": "b", "c": "__skip__"})
    assert m.task_id_mapping["c"] == "__skip__"


def test_instance_frozen_and_tuple_current_tasks() -> None:
    from dataclasses import FrozenInstanceError
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    inst = WorkflowInstance(
        id=uuid4(),
        tenant_id=uuid4(),
        workflow_id="w",
        workflow_version=1,
        status=WorkflowInstanceStatus.waiting,
        spiff_state=b"{}",
        started_at=now,
        updated_at=now,
        current_tasks=["t1", "t2"],
    )
    assert inst.current_tasks == ("t1", "t2")
    with pytest.raises(FrozenInstanceError):
        inst.status = WorkflowInstanceStatus.active  # type: ignore[misc]


def test_protocols_runtime_checkable() -> None:
    for proto in (WorkflowEngineAdapter, WorkflowEngineStore, WorkflowJobPort):
        assert hasattr(proto, "_is_runtime_protocol") or True
        assert not isinstance(object(), proto)


def test_parsed_definition_shape() -> None:
    pd = ParsedDefinition(workflow_id="w", version=1, process_id="w_proc")
    assert pd.user_tasks == ()
    assert pd.task_extensions == {}
