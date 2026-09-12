"""Control Plane bridge on a real database with a fake Control Plane.

The fake keeps tasks, external references, comments and a journal in memory and answers
the slice of the SDK the bridge uses. Both loops are exercised end to end: a started
process materialises a Task, a completed Task advances the process, the echo does not
loop back, a cancelled instance releases its Tasks, and every pass is idempotent.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from process_runtime.bridge import BindingStore, BridgeConfig, ControlPlaneBridge
from process_runtime.bridge.credential import ServiceAccountCredential
from process_runtime.domain.enums import WorkflowInstanceStatus, WorkflowTransitionTrigger
from process_runtime.domain.value_objects import WorkflowDefinitionDeclaration
from process_runtime.engine.spiff import SpiffWorkflowAdapter
from process_runtime.events import ProcessEventLog
from process_runtime.runtime import (
    PostgresWorkflowEngineStore,
    WorkflowAdvanceService,
    WorkflowDefinitionRegistry,
)
from process_runtime.tables import process_task_bindings

pytestmark = [pytest.mark.db]

_ROOT = Path(__file__).resolve().parents[1] / "definitions" / "generic"


class FakeControlPlane:
    """In-memory Control Plane: the SDK surface the bridge calls, nothing more."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.references: list[dict[str, Any]] = []
        self.comments: list[tuple[str, str]] = []
        self.journal: list[dict[str, Any]] = []
        self.idempotency: dict[str, str] = {}
        self.calls: list[str] = []

    async def create_task(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("create_task")
        key = kwargs.get("idempotency_key")
        if key and key in self.idempotency:
            return self.tasks[self.idempotency[key]]
        task_id = str(uuid.uuid4())
        task = {
            "id": task_id,
            "publicId": f"TASK-{len(self.tasks) + 1:06d}",
            "title": kwargs["title"],
            "status": "todo",
            "systemStatusCategory": "active",
            "version": 1,
            "customFields": dict(kwargs.get("custom_fields") or {}),
            "workspaceId": kwargs.get("workspace_id"),
        }
        self.tasks[task_id] = task
        if key:
            self.idempotency[key] = task_id
        return task

    async def register_external_reference(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("register_external_reference")
        self.references.append(dict(kwargs))
        return {"id": str(uuid.uuid4()), **kwargs}

    async def get_task(self, task_ref: str) -> dict[str, Any]:
        return dict(self.tasks[task_ref])

    async def get_task_transitions(self, task_ref: str) -> dict[str, Any]:
        return {
            "targets": [
                {
                    "status": "cancelled",
                    "systemStatusCategory": "terminal_cancelled",
                    "route": "update",
                },
                {"status": "done", "systemStatusCategory": "terminal_success", "route": "complete"},
            ]
        }

    async def complete_task(self, task_ref: str, *, version: int, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("complete_task")
        task = self.tasks[task_ref]
        assert task["version"] == version
        task.update(status="done", systemStatusCategory="terminal_success", version=version + 1)
        return dict(task)

    async def update_task(
        self, task_ref: str, *, expected_version: int, **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append("update_task")
        task = self.tasks[task_ref]
        assert task["version"] == expected_version
        if "status" in kwargs:
            task["status"] = kwargs["status"]
            task["systemStatusCategory"] = "terminal_cancelled"
        task["version"] = expected_version + 1
        return dict(task)

    async def add_task_comment(self, task_ref: str, *, body: str, **kwargs: Any) -> dict[str, Any]:
        self.comments.append((task_ref, body))
        return {"id": str(uuid.uuid4()), "body": body}

    async def list_events(
        self, *, cursor: str | None = None, limit: int | None = None, **params: Any
    ) -> dict[str, Any]:
        start = int(cursor) if cursor else 0
        page = self.journal[start : start + (limit or 100)]
        next_cursor = str(start + len(page)) if page else cursor
        return {"items": page, "nextCursor": next_cursor}

    # -- what an operator would do in the console
    def operator_completes(self, task_id: str, fields: dict[str, Any]) -> None:
        task = self.tasks[task_id]
        task["customFields"].update(fields)
        task.update(
            status="done", systemStatusCategory="terminal_success", version=task["version"] + 1
        )
        self.journal.append(
            {
                "id": str(uuid.uuid4()),
                "cursor": str(len(self.journal) + 1),
                "type": "task.completed",
                "entityType": "task",
                "entityId": task_id,
                "actorId": str(uuid.uuid4()),
                "payload": {
                    "publicId": task["publicId"],
                    "status": "done",
                    "systemStatusCategory": "terminal_success",
                    "version": task["version"],
                },
            }
        )


def _service(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[WorkflowAdvanceService, WorkflowDefinitionRegistry, PostgresWorkflowEngineStore]:
    adapter = SpiffWorkflowAdapter()
    registry = WorkflowDefinitionRegistry()
    decl = WorkflowDefinitionDeclaration(
        workflow_id="three_step_approval",
        version=1,
        bpmn_path="generic/three_step_approval-v1.bpmn",
    )
    parsed = adapter.parse(
        bpmn_xml=(_ROOT / "three_step_approval-v1.bpmn").read_bytes(),
        dmn_xmls={},
        workflow_id=decl.workflow_id,
        version=decl.version,
    )
    registry.register(decl, parsed)
    store = PostgresWorkflowEngineStore()
    service = WorkflowAdvanceService(
        session_factory=session_factory,
        store=store,
        adapter=adapter,
        registry=registry,
        events=ProcessEventLog(),
    )
    return service, registry, store


@pytest.fixture
async def bridge_env(session_factory, tenant_id):
    service, registry, store = _service(session_factory)
    cp = FakeControlPlane()
    bridge = ControlPlaneBridge(
        session_factory=session_factory,
        cp=cp,
        config=BridgeConfig(tenant_id=tenant_id, workspace_id=str(uuid.uuid4())),
        store=store,
        service=service,
        registry=registry,
    )
    return bridge, cp, service, store


async def _bindings(session_factory) -> list[Any]:
    async with session_factory() as session:
        rows = (await session.execute(select(*process_task_bindings.c))).fetchall()
    return list(rows)


async def test_activity_becomes_a_task_and_task_completion_advances_process(
    bridge_env, session_factory, tenant_id
) -> None:
    bridge, cp, service, store = bridge_env
    inst = await service.start_instance(
        tenant_id=tenant_id,
        workflow_id="three_step_approval",
        version=1,
        subject_entity_type="order",
        subject_entity_id=uuid.uuid4(),
    )

    first = await bridge.outbound_once()
    assert first.processed >= 1 and not first.stalled
    assert cp.calls.count("create_task") == 1
    (task,) = cp.tasks.values()
    assert task["title"].startswith("Initial Review") or "initial_review" in task["title"]
    assert task["customFields"]["processTaskId"] == "initial_review"
    assert task["customFields"]["processLane"] == "reviewers"
    assert cp.references[0]["external_system"] == "process-runtime"
    assert cp.references[0]["external_id"].startswith(f"{inst.id}/initial_review/")
    bindings = await _bindings(session_factory)
    assert len(bindings) == 1 and bindings[0].status == "open"
    # Idempotent: a second pass has nothing to do and creates nothing.
    again = await bridge.outbound_once()
    assert again.processed == 0 and cp.calls.count("create_task") == 1

    # The operator completes the Task in the Control Plane with form data.
    cp.operator_completes(task["id"], {"initial_decision": "approve", "note": "ok"})
    inbound = await bridge.inbound_once()
    assert inbound.processed == 1 and not inbound.stalled
    async with session_factory() as s:
        advanced = await store.load_instance(inst.id, session=s)
    assert advanced is not None and advanced.current_tasks == ("final_approval",)

    # Outbound: the completion is not echoed back (binding was 'completing'),
    # and the next activity gets its own Task.
    out2 = await bridge.outbound_once()
    assert out2.processed >= 2 and not out2.stalled
    assert cp.calls.count("complete_task") == 0
    assert cp.calls.count("create_task") == 2
    statuses = sorted(b.status for b in await _bindings(session_factory))
    assert statuses == ["completed", "open"]
    # Replaying the same journal does not advance twice (dedup by event id).
    replay = await bridge.inbound_once()
    assert replay.processed == 0


async def test_process_side_completion_closes_cp_task(
    bridge_env, session_factory, tenant_id
) -> None:
    bridge, cp, service, _store = bridge_env
    inst = await service.start_instance(
        tenant_id=tenant_id, workflow_id="three_step_approval", version=1
    )
    await bridge.outbound_once()
    (task_id,) = cp.tasks
    await service.advance(
        instance_id=inst.id,
        trigger=WorkflowTransitionTrigger.task_complete,
        tenant_id=tenant_id,
        payload={
            "task_id": "initial_review",
            "form_payload": {"initial_decision": "approve"},
            "dedup_key": "api-1",
        },
    )
    result = await bridge.outbound_once()
    assert not result.stalled
    assert cp.tasks[task_id]["systemStatusCategory"] == "terminal_success"
    assert cp.calls.count("complete_task") == 1


async def test_cancelled_instance_releases_open_tasks(
    bridge_env, session_factory, tenant_id
) -> None:
    bridge, cp, service, _store = bridge_env
    inst = await service.start_instance(
        tenant_id=tenant_id, workflow_id="three_step_approval", version=1
    )
    await bridge.outbound_once()
    (task_id,) = cp.tasks
    await service.cancel(instance_id=inst.id, reason="приоритеты изменились", tenant_id=tenant_id)
    result = await bridge.outbound_once()
    assert not result.stalled
    assert cp.tasks[task_id]["status"] == "cancelled"
    assert any("приоритеты изменились" in body for _, body in cp.comments)
    assert [b.status for b in await _bindings(session_factory)] == ["cancelled"]


async def test_cp_task_cancelled_keeps_process_waiting(
    bridge_env, session_factory, tenant_id
) -> None:
    bridge, cp, service, store = bridge_env
    inst = await service.start_instance(
        tenant_id=tenant_id, workflow_id="three_step_approval", version=1
    )
    await bridge.outbound_once()
    (task_id,) = cp.tasks
    cp.journal.append(
        {
            "id": str(uuid.uuid4()),
            "cursor": "1",
            "type": "task.updated",
            "entityType": "task",
            "entityId": task_id,
            "payload": {
                "fromStatus": "todo",
                "status": "cancelled",
                "systemStatusCategory": "cancelled",
            },
        }
    )
    result = await bridge.inbound_once()
    assert result.processed == 1
    async with session_factory() as s:
        still = await store.load_instance(inst.id, session=s)
    assert still is not None and still.status is WorkflowInstanceStatus.waiting
    assert [b.status for b in await _bindings(session_factory)] == ["cancelled"]


async def test_other_tenants_and_unknown_tasks_are_ignored(bridge_env, session_factory) -> None:
    bridge, cp, service, _store = bridge_env
    foreign = uuid.uuid4()
    await service.start_instance(tenant_id=foreign, workflow_id="three_step_approval", version=1)
    assert (await bridge.outbound_once()).processed == 0
    cp.journal.append(
        {
            "id": str(uuid.uuid4()),
            "cursor": "1",
            "type": "task.completed",
            "entityType": "task",
            "entityId": str(uuid.uuid4()),
            "payload": {"status": "done", "systemStatusCategory": "terminal_success"},
        }
    )
    assert (await bridge.inbound_once()).processed == 1  # consumed, nothing bound
    async with session_factory() as s:
        cursor = await BindingStore().get_cursor("inbound", session=s)
    assert cursor == "1"


async def test_poison_event_stops_the_cursor(bridge_env, session_factory, tenant_id) -> None:
    bridge, cp, service, _store = bridge_env
    await service.start_instance(tenant_id=tenant_id, workflow_id="three_step_approval", version=1)

    async def boom(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("control plane down")

    cp.create_task = boom  # type: ignore[method-assign]
    result = await bridge.outbound_once()
    assert result.stalled
    async with session_factory() as s:
        cursor = await BindingStore().get_cursor("outbound", session=s)
        started = (
            await s.execute(text("SELECT id FROM process_events ORDER BY id LIMIT 1"))
        ).scalar()
    # The cursor stopped right before the task_created event (the 'started' event passed).
    assert cursor == str(started)


def test_service_account_credential_contract() -> None:
    cred = ServiceAccountCredential(iam_base_url="http://iam", client_id="c", client_secret="s")
    assert cred.refreshable is True
