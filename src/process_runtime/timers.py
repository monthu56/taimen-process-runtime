"""Timer worker: fires due BPMN timers from inside the API process.

platform-core ran this as an ARQ cron; a standalone service keeps one asyncio loop in its
lifespan instead of a second process and a broker. Each due timer is advanced in-process
and marked fired inside the advance transaction (idempotent by ``timer:<id>``), so two
replicas racing on the same timer resolve on the per-instance advisory lock plus dedup.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from process_runtime.domain.enums import WorkflowTransitionTrigger
from process_runtime.runtime import metrics
from process_runtime.runtime.pg_store import PostgresWorkflowEngineStore
from process_runtime.runtime.runtime import WorkflowAdvanceService

_log = logging.getLogger("process_runtime.timers")


async def fire_due_timers(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    store: PostgresWorkflowEngineStore,
    service: WorkflowAdvanceService,
    batch_size: int,
) -> dict[str, int]:
    """One tick: advance every due timer. Returns ``{"due": n, "fired": m}``."""
    now = datetime.now(UTC)
    async with session_factory() as session:
        due = await store.list_due_timers(now, limit=batch_size, session=session)
    metrics.set_timer_backlog(len(due) if len(due) >= batch_size else 0)
    fired = 0
    for timer in due:
        async with session_factory() as session:
            instance = await store.load_instance(timer.workflow_instance_id, session=session)
        if instance is None:
            continue
        await service.advance(
            instance_id=timer.workflow_instance_id,
            trigger=WorkflowTransitionTrigger.timer,
            payload={
                "timer_id": str(timer.id),
                "task_id": timer.task_id,
                "dedup_key": f"timer:{timer.id}",
            },
            tenant_id=instance.tenant_id,
        )
        metrics.record_timer_fired(instance.workflow_id, timer.timer_type.value)
        fired += 1
    return {"due": len(due), "fired": fired}


async def timer_loop(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    store: PostgresWorkflowEngineStore,
    service: WorkflowAdvanceService,
    interval_seconds: int,
    batch_size: int,
) -> None:
    """Run ``fire_due_timers`` forever; one failing tick is logged and never stops the loop."""
    while True:
        try:
            await fire_due_timers(
                session_factory=session_factory, store=store, service=service, batch_size=batch_size
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("timer tick failed")
        await asyncio.sleep(interval_seconds)


__all__ = ["fire_due_timers", "timer_loop"]
