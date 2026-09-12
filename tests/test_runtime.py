"""WorkflowAdvanceService runtime contract tests (EPIC-11, ADR-023).

Covers idempotency (WF-011 / exit 2), timer fire (WF-012 / exit 3), script-blocked
failure persistence (WF-007 / exit 6) and forced migration (WF-016 / exit 8) against a
real PostgreSQL container. No InMemory doubles.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from process_runtime.domain import (
    WorkflowDefinitionDeclaration,
    WorkflowInstanceStatus,
    WorkflowTransitionTrigger,
)
from process_runtime.domain.value_objects import MigrationMap
from process_runtime.engine.spiff import SpiffWorkflowAdapter
from process_runtime.events import ProcessEventLog
from process_runtime.runtime import (
    PostgresWorkflowEngineStore,
    WorkflowAdvanceService,
    WorkflowDefinitionRegistry,
)

pytestmark = [pytest.mark.db]

_ROOT = Path(__file__).resolve().parents[1] / "definitions" / "generic"


def _build_service(
    session_factory: async_sessionmaker[AsyncSession],
    declarations: list[WorkflowDefinitionDeclaration],
) -> WorkflowAdvanceService:
    adapter = SpiffWorkflowAdapter()
    registry = WorkflowDefinitionRegistry()
    for decl in declarations:
        bpmn = (_ROOT / Path(decl.bpmn_path).name).read_bytes()
        dmn = {p: (_ROOT / Path(p).name).read_bytes() for p in decl.dmn_paths}
        parsed = adapter.parse(
            bpmn_xml=bpmn, dmn_xmls=dmn, workflow_id=decl.workflow_id, version=decl.version
        )
        registry.register(decl, parsed)
    return WorkflowAdvanceService(
        session_factory=session_factory,
        store=PostgresWorkflowEngineStore(),
        adapter=adapter,
        registry=registry,
        events=ProcessEventLog(),
    )


async def test_idempotent_dedup(session_factory, tenant_id) -> None:
    svc = _build_service(
        session_factory,
        [
            WorkflowDefinitionDeclaration(
                workflow_id="three_step_approval",
                version=1,
                bpmn_path="generic/three_step_approval-v1.bpmn",
            )
        ],
    )
    inst = await svc.start_instance(
        tenant_id=tenant_id, workflow_id="three_step_approval", version=1
    )
    await svc.advance(
        instance_id=inst.id,
        trigger=WorkflowTransitionTrigger.task_complete,
        tenant_id=tenant_id,
        payload={
            "task_id": "initial_review",
            "form_payload": {"initial_decision": "approve"},
            "dedup_key": "d1",
        },
    )
    # replay same dedup_key with a payload that would otherwise complete the wf — must skip
    after = await svc.advance(
        instance_id=inst.id,
        trigger=WorkflowTransitionTrigger.task_complete,
        tenant_id=tenant_id,
        payload={
            "task_id": "final_approval",
            "form_payload": {"final_decision": "approve"},
            "dedup_key": "d1",
        },
    )
    assert after is not None
    assert after.current_tasks == ("final_approval",)  # dedup skipped the second advance


async def test_concurrent_advances_serialised(session_factory, tenant_id) -> None:
    svc = _build_service(
        session_factory,
        [
            WorkflowDefinitionDeclaration(
                workflow_id="three_step_approval",
                version=1,
                bpmn_path="generic/three_step_approval-v1.bpmn",
            )
        ],
    )
    inst = await svc.start_instance(
        tenant_id=tenant_id, workflow_id="three_step_approval", version=1
    )
    # Two concurrent identical advances (same dedup) — advisory lock + dedup make it safe.
    results = await asyncio.gather(
        svc.advance(
            instance_id=inst.id,
            trigger=WorkflowTransitionTrigger.task_complete,
            tenant_id=tenant_id,
            payload={
                "task_id": "initial_review",
                "form_payload": {"initial_decision": "approve"},
                "dedup_key": "c",
            },
        ),
        svc.advance(
            instance_id=inst.id,
            trigger=WorkflowTransitionTrigger.task_complete,
            tenant_id=tenant_id,
            payload={
                "task_id": "initial_review",
                "form_payload": {"initial_decision": "approve"},
                "dedup_key": "c",
            },
        ),
    )
    assert all(r is not None for r in results)
    # Exactly one transition carries the dedup_key (uq_wtl_dedup enforces it).
    async with session_factory() as s:
        from sqlalchemy import text

        n = (
            await s.execute(
                text(
                    "SELECT count(*) FROM workflow_transition_log WHERE workflow_instance_id=:i "
                    "AND payload->>'dedup_key' = 'c'"
                ),
                {"i": inst.id},
            )
        ).scalar()
    assert n == 1


async def test_timer_fires_through_runtime(session_factory, tenant_id) -> None:
    svc = _build_service(
        session_factory,
        [
            WorkflowDefinitionDeclaration(
                workflow_id="timer_reminder", version=1, bpmn_path="generic/timer_reminder-v1.bpmn"
            )
        ],
    )
    inst = await svc.start_instance(
        tenant_id=tenant_id,
        workflow_id="timer_reminder",
        version=1,
        initial_variables={"reminder_duration": "PT2S"},
    )
    assert inst.status is WorkflowInstanceStatus.waiting
    store = PostgresWorkflowEngineStore()
    from datetime import UTC, datetime

    await asyncio.sleep(2.5)
    async with session_factory() as s:
        due = await store.list_due_timers(datetime.now(UTC), limit=10, session=s)
    assert due, "timer should be due after sleeping"
    timer = due[0]
    updated = await svc.advance(
        instance_id=inst.id,
        trigger=WorkflowTransitionTrigger.timer,
        tenant_id=tenant_id,
        payload={
            "timer_id": str(timer.id),
            "task_id": timer.task_id,
            "dedup_key": f"timer:{timer.id}",
        },
    )
    assert updated is not None
    assert updated.status is WorkflowInstanceStatus.completed
    # timer marked fired inside the advance
    async with session_factory() as s:
        due2 = await store.list_due_timers(datetime.now(UTC), limit=10, session=s)
    assert all(t.id != timer.id for t in due2)


async def test_script_blocked_persists_failed(session_factory, tenant_id) -> None:
    # inline malicious BPMN registered as a definition
    from process_runtime.runtime import WorkflowDefinitionRegistry as _Reg

    adapter = SpiffWorkflowAdapter()
    registry = _Reg()
    mal = b"""<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:spiffworkflow="http://spiffworkflow.org/bpmn/schema/1.0/core" id="D" targetNamespace="http://t.io/w">
  <bpmn:process id="mal" isExecutable="true">
    <bpmn:startEvent id="S"><bpmn:outgoing>f1</bpmn:outgoing></bpmn:startEvent>
    <bpmn:scriptTask id="evil"><bpmn:incoming>f1</bpmn:incoming><bpmn:outgoing>f2</bpmn:outgoing>
      <bpmn:script>x = __import__('os').getpid()</bpmn:script></bpmn:scriptTask>
    <bpmn:endEvent id="E"><bpmn:incoming>f2</bpmn:incoming></bpmn:endEvent>
    <bpmn:sequenceFlow id="f1" sourceRef="S" targetRef="evil"/>
    <bpmn:sequenceFlow id="f2" sourceRef="evil" targetRef="E"/>
  </bpmn:process>
</bpmn:definitions>"""
    decl = WorkflowDefinitionDeclaration(workflow_id="mal", version=1, bpmn_path="mal.bpmn")
    registry.register(decl, adapter.parse(bpmn_xml=mal, dmn_xmls={}, workflow_id="mal", version=1))
    svc = WorkflowAdvanceService(
        session_factory=session_factory,
        store=PostgresWorkflowEngineStore(),
        adapter=adapter,
        registry=registry,
        events=ProcessEventLog(),
    )
    inst = await svc.start_instance(tenant_id=tenant_id, workflow_id="mal", version=1)
    assert inst.status is WorkflowInstanceStatus.failed
    assert inst.error_payload and "ScriptTaskSecurityError" in str(inst.error_payload)
    # .failed event published
    async with session_factory() as s:
        from sqlalchemy import text

        n = (
            await s.execute(
                text(
                    "SELECT count(*) FROM process_events WHERE tenant_id=:t "
                    "AND event_type='platform.workflow.failed.v1'"
                ),
                {"t": tenant_id},
            )
        ).scalar()
    assert n >= 1


async def test_forced_migration_version_bump(session_factory, tenant_id) -> None:
    # Register the same workflow under v1 and v2 (identity migration; no task rename).
    decls = [
        WorkflowDefinitionDeclaration(
            workflow_id="three_step_approval",
            version=1,
            bpmn_path="generic/three_step_approval-v1.bpmn",
        ),
        WorkflowDefinitionDeclaration(
            workflow_id="three_step_approval",
            version=2,
            bpmn_path="generic/three_step_approval-v1.bpmn",
        ),
    ]
    svc = _build_service(session_factory, decls)
    inst = await svc.start_instance(
        tenant_id=tenant_id, workflow_id="three_step_approval", version=1
    )
    migrated = await svc.migrate(
        instance_id=inst.id,
        migration=MigrationMap(from_version=1, to_version=2, task_id_mapping={}),
        tenant_id=tenant_id,
    )
    assert migrated.workflow_version == 2
    # wrong from_version -> inconsistent
    from process_runtime.domain.errors import WorkflowMigrationMapInconsistent

    with pytest.raises(WorkflowMigrationMapInconsistent):
        await svc.migrate(
            instance_id=inst.id,
            migration=MigrationMap(from_version=1, to_version=2, task_id_mapping={}),
            tenant_id=tenant_id,
        )
