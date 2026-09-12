"""Reconciliation metadata of the Control Plane bridge: bindings and cursors."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from process_runtime.tables import bridge_cursors, process_task_bindings

OPEN_STATUSES = ("open", "completing")


@dataclass(frozen=True, slots=True)
class TaskBinding:
    id: UUID
    tenant_id: UUID
    workflow_instance_id: UUID
    task_id: str
    source_event_id: int
    cp_task_id: UUID
    cp_public_id: str | None
    status: str


def _row(row: Any) -> TaskBinding:
    return TaskBinding(
        id=row.id,
        tenant_id=row.tenant_id,
        workflow_instance_id=row.workflow_instance_id,
        task_id=row.task_id,
        source_event_id=row.source_event_id,
        cp_task_id=row.cp_task_id,
        cp_public_id=row.cp_public_id,
        status=row.status,
    )


class BindingStore:
    async def get_cursor(self, name: str, *, session: AsyncSession) -> str | None:
        row = (
            await session.execute(
                select(bridge_cursors.c.cursor).where(bridge_cursors.c.name == name)
            )
        ).fetchone()
        return row.cursor if row is not None else None

    async def set_cursor(self, name: str, cursor: str | None, *, session: AsyncSession) -> None:
        now = datetime.now(UTC)
        stmt = pg_insert(bridge_cursors).values(name=name, cursor=cursor, updated_at=now)
        stmt = stmt.on_conflict_do_update(
            index_elements=[bridge_cursors.c.name], set_={"cursor": cursor, "updated_at": now}
        )
        await session.execute(stmt)

    async def create(
        self,
        *,
        tenant_id: UUID,
        workflow_instance_id: UUID,
        task_id: str,
        source_event_id: int,
        cp_task_id: UUID,
        cp_public_id: str | None,
        session: AsyncSession,
    ) -> TaskBinding:
        now = datetime.now(UTC)
        binding_id = uuid4()
        await session.execute(
            process_task_bindings.insert().values(
                id=binding_id,
                tenant_id=tenant_id,
                workflow_instance_id=workflow_instance_id,
                task_id=task_id,
                source_event_id=source_event_id,
                cp_task_id=cp_task_id,
                cp_public_id=cp_public_id,
                status="open",
                created_at=now,
                updated_at=now,
            )
        )
        return TaskBinding(
            id=binding_id,
            tenant_id=tenant_id,
            workflow_instance_id=workflow_instance_id,
            task_id=task_id,
            source_event_id=source_event_id,
            cp_task_id=cp_task_id,
            cp_public_id=cp_public_id,
            status="open",
        )

    async def by_source_event(self, event_id: int, *, session: AsyncSession) -> TaskBinding | None:
        row = (
            await session.execute(
                select(*process_task_bindings.c).where(
                    process_task_bindings.c.source_event_id == event_id
                )
            )
        ).fetchone()
        return _row(row) if row is not None else None

    async def by_cp_task(self, cp_task_id: UUID, *, session: AsyncSession) -> TaskBinding | None:
        row = (
            await session.execute(
                select(*process_task_bindings.c).where(
                    process_task_bindings.c.cp_task_id == cp_task_id
                )
            )
        ).fetchone()
        return _row(row) if row is not None else None

    async def open_for_activity(
        self, workflow_instance_id: UUID, task_id: str, *, session: AsyncSession
    ) -> TaskBinding | None:
        row = (
            await session.execute(
                select(*process_task_bindings.c)
                .where(
                    and_(
                        process_task_bindings.c.workflow_instance_id == workflow_instance_id,
                        process_task_bindings.c.task_id == task_id,
                        process_task_bindings.c.status.in_(OPEN_STATUSES),
                    )
                )
                .order_by(process_task_bindings.c.created_at.desc())
                .limit(1)
            )
        ).fetchone()
        return _row(row) if row is not None else None

    async def open_for_instance(
        self, workflow_instance_id: UUID, *, session: AsyncSession
    ) -> Sequence[TaskBinding]:
        rows = (
            await session.execute(
                select(*process_task_bindings.c).where(
                    and_(
                        process_task_bindings.c.workflow_instance_id == workflow_instance_id,
                        process_task_bindings.c.status.in_(OPEN_STATUSES),
                    )
                )
            )
        ).fetchall()
        return [_row(r) for r in rows]

    async def set_status(self, binding_id: UUID, status: str, *, session: AsyncSession) -> None:
        await session.execute(
            update(process_task_bindings)
            .where(process_task_bindings.c.id == binding_id)
            .values(status=status, updated_at=datetime.now(UTC))
        )


__all__ = ["OPEN_STATUSES", "BindingStore", "TaskBinding"]
