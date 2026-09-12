"""Adapter protocol + DummyAdapter + task handler + approval hook contract tests.

Covers WF-004 (DummyAdapter), WF-010 (WorkflowTaskHandler), WF-017 (ApprovalEngineHook).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from process_runtime.domain import (
    ApprovalEngineHook,
    WorkflowEngineAdapter,
    WorkflowInstanceStatus,
    WorkflowTaskHandler,
    WorkflowTransitionTrigger,
)
from process_runtime.domain.enums import ApprovalDecisionStatus
from process_runtime.domain.errors import WorkflowApprovalHookNotConfigured
from process_runtime.domain.value_objects import ApprovalRequestContext, ApprovalRequestRef
from process_runtime.engine import DummyAdapter
from process_runtime.engine.handlers import SimpleNotificationHandler
from process_runtime.runtime.approval import (
    AlwaysApprovedHook,
    NoopApprovalEngineHook,
)
from process_runtime.runtime.context import SimpleWorkflowTaskContext

pytestmark = []


def test_dummy_satisfies_protocol() -> None:
    assert isinstance(DummyAdapter(), WorkflowEngineAdapter)


def test_dummy_end_to_end() -> None:
    a = DummyAdapter()
    parsed = a.parse(bpmn_xml=b"", dmn_xmls={}, workflow_id="wf", version=1)
    r0 = a.build_initial_state(parsed, {"requester": "alice"})
    assert r0.instance_status is WorkflowInstanceStatus.waiting
    assert "platform.workflow.started" in r0.emitted_events
    r1 = a.advance(
        state=r0.new_state,
        trigger=WorkflowTransitionTrigger.task_complete,
        payload={"form_payload": {"decision": "approve"}},
        parsed=parsed,
        now=datetime.now(UTC),
    )
    assert r1.instance_status is WorkflowInstanceStatus.completed
    assert r1.outcome == "approve"
    assert "platform.workflow.completed" in r1.emitted_events


async def test_simple_notification_handler() -> None:
    handler = SimpleNotificationHandler()
    assert isinstance(handler, WorkflowTaskHandler)
    ctx = SimpleWorkflowTaskContext(
        workflow_instance_id=uuid4(), tenant_id=uuid4(), task_id="notify", variables={}
    )
    result = await handler.execute(ctx)
    assert result.status == "completed"
    assert ctx.pending_events
    assert ctx.pending_events[0][0] == "platform.notification.requested"


async def test_task_context_records_mutations() -> None:
    ctx = SimpleWorkflowTaskContext(
        workflow_instance_id=uuid4(), tenant_id=uuid4(), task_id="t", variables={"a": 1}
    )
    ctx.set_variable("b", 2)
    assert ctx.pending_variables == {"b": 2}
    assert ctx.variables["b"] == 2


async def test_noop_approval_hook_fails_fast() -> None:
    hook = NoopApprovalEngineHook()
    assert isinstance(hook, ApprovalEngineHook)
    ctx = ApprovalRequestContext(
        workflow_instance_id=uuid4(),
        tenant_id=uuid4(),
        task_id="approve",
        policy_id="order_approval",
        requester_user_id=None,
    )
    with pytest.raises(WorkflowApprovalHookNotConfigured):
        await hook.start_approval(ctx)


async def test_always_approved_hook_stub() -> None:
    hook = AlwaysApprovedHook()
    assert isinstance(hook, ApprovalEngineHook)
    ctx = ApprovalRequestContext(
        workflow_instance_id=uuid4(),
        tenant_id=uuid4(),
        task_id="approve",
        policy_id="order_approval",
        requester_user_id=None,
    )
    ref = await hook.start_approval(ctx)
    assert isinstance(ref, ApprovalRequestRef)
    decision = await hook.is_approval_complete(ref)
    assert decision is not None
    assert decision.status is ApprovalDecisionStatus.approved
