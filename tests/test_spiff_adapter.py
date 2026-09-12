"""SpiffWorkflow adapter contract tests: round-trip, DMN, branching, sandbox (WF-005/006/007).

No database — these exercise the pure engine adapter against the git-versioned sample BPMN.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from process_runtime.domain import (
    WorkflowEngineAdapter,
    WorkflowInstanceStatus,
    WorkflowTransitionTrigger,
)
from process_runtime.domain.errors import WorkflowDefinitionInvalid
from process_runtime.engine.spiff import SpiffWorkflowAdapter

pytestmark = []

_ROOT = Path(__file__).resolve().parents[1] / "definitions" / "generic"


def _bpmn(name: str) -> bytes:
    return (_ROOT / name).read_bytes()


def _now() -> datetime:
    return datetime.now(UTC)


def test_adapter_satisfies_protocol() -> None:
    assert isinstance(SpiffWorkflowAdapter(), WorkflowEngineAdapter)


def test_parse_extracts_tasks_and_lanes() -> None:
    a = SpiffWorkflowAdapter()
    parsed = a.parse(
        bpmn_xml=_bpmn("three_step_approval-v1.bpmn"),
        dmn_xmls={},
        workflow_id="three_step_approval",
        version=1,
    )
    ids = {t.task_id for t in parsed.user_tasks}
    assert ids == {"initial_review", "final_approval"}
    lanes = {t.task_id: t.lane for t in parsed.user_tasks}
    assert lanes["initial_review"] == "reviewers"
    assert lanes["final_approval"] == "approvers"


def test_round_trip_serialization_stable() -> None:
    a = SpiffWorkflowAdapter()
    parsed = a.parse(
        bpmn_xml=_bpmn("three_step_approval-v1.bpmn"),
        dmn_xmls={},
        workflow_id="three_step_approval",
        version=1,
    )
    r0 = a.build_initial_state(parsed, {"requester": "alice"})
    assert r0.instance_status is WorkflowInstanceStatus.waiting
    assert {t.task_id for t in r0.new_tasks} == {"initial_review"}
    # serialize -> deserialize in a FRESH adapter, advance the human task
    fresh = SpiffWorkflowAdapter()
    r1 = fresh.advance(
        state=r0.new_state,
        trigger=WorkflowTransitionTrigger.task_complete,
        payload={"task_id": "initial_review", "form_payload": {"initial_decision": "approve"}},
        parsed=parsed,
        now=_now(),
    )
    assert r1.instance_status is WorkflowInstanceStatus.waiting
    assert {t.task_id for t in r1.new_tasks} == {"final_approval"}
    assert r1.completed_task_ids == ("initial_review",)
    # complete final -> completed
    r2 = a.advance(
        state=r1.new_state,
        trigger=WorkflowTransitionTrigger.task_complete,
        payload={"task_id": "final_approval", "form_payload": {"final_decision": "approve"}},
        parsed=parsed,
        now=_now(),
    )
    assert r2.instance_status is WorkflowInstanceStatus.completed
    assert r2.outcome == "approve"


def test_reject_branch() -> None:
    a = SpiffWorkflowAdapter()
    parsed = a.parse(
        bpmn_xml=_bpmn("three_step_approval-v1.bpmn"),
        dmn_xmls={},
        workflow_id="three_step_approval",
        version=1,
    )
    r0 = a.build_initial_state(parsed, {})
    r1 = a.advance(
        state=r0.new_state,
        trigger=WorkflowTransitionTrigger.task_complete,
        payload={"task_id": "initial_review", "form_payload": {"initial_decision": "reject"}},
        parsed=parsed,
        now=_now(),
    )
    assert r1.instance_status is WorkflowInstanceStatus.completed


@pytest.mark.parametrize("score,category", [(20, "A"), (50, "B"), (88, "C")])
def test_dmn_business_rule(score: int, category: str) -> None:
    a = SpiffWorkflowAdapter()
    parsed = a.parse(
        bpmn_xml=_bpmn("score_routing-v1.bpmn"),
        dmn_xmls={"sample_decision-v1.dmn": _bpmn("sample_decision-v1.dmn")},
        workflow_id="score_routing",
        version=1,
    )
    r = a.build_initial_state(parsed, {"score": score})
    assert r.instance_status is WorkflowInstanceStatus.completed
    assert r.variables["category"] == category


def test_invalid_bpmn_raises() -> None:
    a = SpiffWorkflowAdapter()
    with pytest.raises(WorkflowDefinitionInvalid):
        a.parse(bpmn_xml=b"<not-bpmn/>", dmn_xmls={}, workflow_id="bad", version=1)


def test_malicious_script_fails_on_rehydrated_workflow() -> None:
    """A ScriptTask with 'import os' must fail the instance after a serialize/deserialize."""
    a = SpiffWorkflowAdapter()
    mal = b"""<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:spiffworkflow="http://spiffworkflow.org/bpmn/schema/1.0/core"
  id="D" targetNamespace="http://t.io/w">
  <bpmn:process id="mal_proc" isExecutable="true">
    <bpmn:startEvent id="S"><bpmn:outgoing>f1</bpmn:outgoing></bpmn:startEvent>
    <bpmn:userTask id="gate" name="Gate">
      <bpmn:incoming>f1</bpmn:incoming><bpmn:outgoing>f2</bpmn:outgoing></bpmn:userTask>
    <bpmn:scriptTask id="evil" name="Evil">
      <bpmn:incoming>f2</bpmn:incoming><bpmn:outgoing>f3</bpmn:outgoing>
      <bpmn:script>x = __import__('os').getpid()</bpmn:script>
    </bpmn:scriptTask>
    <bpmn:endEvent id="E"><bpmn:incoming>f3</bpmn:incoming></bpmn:endEvent>
    <bpmn:sequenceFlow id="f1" sourceRef="S" targetRef="gate"/>
    <bpmn:sequenceFlow id="f2" sourceRef="gate" targetRef="evil"/>
    <bpmn:sequenceFlow id="f3" sourceRef="evil" targetRef="E"/>
  </bpmn:process>
</bpmn:definitions>"""
    parsed = a.parse(bpmn_xml=mal, dmn_xmls={}, workflow_id="mal", version=1)
    r0 = a.build_initial_state(parsed, {})
    fresh = SpiffWorkflowAdapter()
    r1 = fresh.advance(
        state=r0.new_state,
        trigger=WorkflowTransitionTrigger.task_complete,
        payload={"task_id": "gate"},
        parsed=parsed,
        now=_now(),
    )
    assert r1.instance_status is WorkflowInstanceStatus.failed
    assert "ScriptTaskSecurityError" in str(r1.error_payload)
