"""PostgresWorkflowEngineStore contract tests (EPIC-11, ADR-023, WF-003)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from process_runtime.domain import (
    WorkflowEngineStore,
    WorkflowInstance,
    WorkflowInstanceStatus,
    WorkflowTask,
    WorkflowTaskStatus,
    WorkflowTimer,
    WorkflowTimerType,
    WorkflowTransitionLog,
    WorkflowTransitionTrigger,
)
from process_runtime.runtime import PostgresWorkflowEngineStore

pytestmark = [pytest.mark.db]


def _make_instance(tenant_id: UUID, **kw) -> WorkflowInstance:
    now = datetime.now(UTC)
    return WorkflowInstance(
        id=kw.get("id", uuid4()),
        tenant_id=tenant_id,
        workflow_id=kw.get("workflow_id", "wf"),
        workflow_version=1,
        status=kw.get("status", WorkflowInstanceStatus.waiting),
        spiff_state=kw.get("spiff_state", b'{"serializer_version": "1.4", "x": 1}'),
        started_at=now,
        updated_at=now,
        subject_entity_type=kw.get("subject_entity_type"),
        subject_entity_id=kw.get("subject_entity_id"),
        current_tasks=kw.get("current_tasks", ()),
        variables=kw.get("variables", {}),
    )


def test_store_satisfies_protocol() -> None:
    assert isinstance(PostgresWorkflowEngineStore(), WorkflowEngineStore)


async def test_create_load_save_roundtrip(session_factory, tenant_id) -> None:
    store = PostgresWorkflowEngineStore()
    inst = _make_instance(tenant_id, variables={"big": 10_000_000_000, "f": 1.5})
    async with session_factory() as s:
        await store.create_instance(inst, session=s)
        await s.commit()
    async with session_factory() as s:
        loaded = await store.load_instance(inst.id, tenant_id=tenant_id, session=s)
    assert loaded is not None
    # spiff_state survives the JSONB round-trip with serializer_version intact.
    assert b"serializer_version" in loaded.spiff_state
    assert loaded.variables["big"] == 10_000_000_000
    assert loaded.variables["f"] == 1.5


async def test_tenant_scoping(session_factory, tenant_id) -> None:
    store = PostgresWorkflowEngineStore()
    inst = _make_instance(tenant_id)
    async with session_factory() as s:
        await store.create_instance(inst, session=s)
        await s.commit()
    async with session_factory() as s:
        # wrong tenant -> None
        assert await store.load_instance(inst.id, tenant_id=uuid4(), session=s) is None


async def test_dedup_transition(session_factory, tenant_id) -> None:
    store = PostgresWorkflowEngineStore()
    inst = _make_instance(tenant_id)
    async with session_factory() as s:
        await store.create_instance(inst, session=s)
        await store.append_transition(
            WorkflowTransitionLog(
                id=None,
                workflow_instance_id=inst.id,
                trigger=WorkflowTransitionTrigger.task_complete,
                transitioned_at=datetime.now(UTC),
                payload={"dedup_key": "k1"},
            ),
            session=s,
        )
        await s.commit()
    async with session_factory() as s:
        assert await store.has_dedup_transition(inst.id, "k1", session=s) is True
        assert await store.has_dedup_transition(inst.id, "nope", session=s) is False


async def test_task_upsert_and_inbox(session_factory, tenant_id, seeded_user) -> None:
    store = PostgresWorkflowEngineStore()
    inst = _make_instance(tenant_id)
    user_id = seeded_user
    async with session_factory() as s:
        await store.create_instance(inst, session=s)
        task = WorkflowTask(
            id=uuid4(),
            workflow_instance_id=inst.id,
            task_id="review",
            status=WorkflowTaskStatus.pending,
            created_at=datetime.now(UTC),
            assigned_user_id=user_id,
        )
        await store.upsert_task(task, session=s)
        # idempotent update to same active occurrence
        await store.upsert_task(task, session=s)
        await s.commit()
    async with session_factory() as s:
        inbox = await store.list_tasks_inbox(
            tenant_id=tenant_id, assignee_user_id=user_id, limit=10, offset=0, session=s
        )
    assert len(inbox) == 1
    assert inbox[0].task_id == "review"


async def test_timer_schedule_due_fire(session_factory, tenant_id) -> None:
    store = PostgresWorkflowEngineStore()
    inst = _make_instance(tenant_id)
    async with session_factory() as s:
        await store.create_instance(inst, session=s)
        timer = WorkflowTimer(
            id=uuid4(),
            workflow_instance_id=inst.id,
            task_id="wait",
            fires_at=datetime.now(UTC) - timedelta(seconds=1),
            timer_type=WorkflowTimerType.duration,
            created_at=datetime.now(UTC),
        )
        await store.schedule_timer(timer, session=s)
        await s.commit()
    async with session_factory() as s:
        due = await store.list_due_timers(datetime.now(UTC), limit=10, session=s)
        assert any(t.id == timer.id for t in due)
        await store.mark_timer_fired(timer.id, datetime.now(UTC), session=s)
        await s.commit()
    async with session_factory() as s:
        due2 = await store.list_due_timers(datetime.now(UTC), limit=10, session=s)
        assert all(t.id != timer.id for t in due2)


async def test_advisory_lock_serialises_concurrent_for_update(session_factory, tenant_id) -> None:
    store = PostgresWorkflowEngineStore()
    inst = _make_instance(tenant_id)
    async with session_factory() as s:
        await store.create_instance(inst, session=s)
        await s.commit()

    order: list[str] = []
    a_acquired = asyncio.Event()

    async def worker_a() -> None:
        async with session_factory() as s:
            await store.load_instance(inst.id, for_update=True, session=s)
            order.append("a-acquired")
            a_acquired.set()
            await asyncio.sleep(0.4)
            order.append("a-release")
            await s.commit()

    async def worker_b() -> None:
        await a_acquired.wait()  # ensure a holds the lock first (deterministic ordering)
        async with session_factory() as s:
            # This blocks on the advisory lock until worker_a commits.
            await store.load_instance(inst.id, for_update=True, session=s)
            order.append("b-acquired")
            await s.commit()

    await asyncio.gather(worker_a(), worker_b())
    # b acquired only after a released — the advisory lock serialised them.
    assert order == ["a-acquired", "a-release", "b-acquired"]


async def test_find_active_by_subject(session_factory, tenant_id) -> None:
    store = PostgresWorkflowEngineStore()
    subject = uuid4()
    inst = _make_instance(
        tenant_id,
        subject_entity_type="order",
        subject_entity_id=subject,
        status=WorkflowInstanceStatus.active,
    )
    async with session_factory() as s:
        await store.create_instance(inst, session=s)
        await s.commit()
    async with session_factory() as s:
        found = await store.find_active_by_subject(
            tenant_id=tenant_id,
            subject_entity_type="order",
            subject_entity_id=subject,
            session=s,
        )
    assert len(found) == 1 and found[0].id == inst.id
