"""Public API of process-runtime under ``/api/v1``.

  GET  /definitions                                  registered workflow definitions
  POST /instances                                    start an instance
  GET  /instances/{id}                               instance status + active tokens
  GET  /instances/{id}/tasks                         task list
  POST /instances/{id}/tasks/{task_id}/complete      complete a human task
  POST /instances/{id}/signal                        send a BPMN signal
  POST /instances/{id}/cancel                        terminate
  POST /instances/{id}/migrate                       forced version migration (admin)
  GET  /instances/{id}/diagram                       BPMN/DMN + active tokens
  GET  /inbox                                        human tasks (assignee=me | role=…)
  GET  /events                                       process event feed with a cursor
  GET  /manifest                                     capability manifest (ADR-0023 §2)

Every path is scoped to the tenant of the presented IAM token; authority is the token's
scopes (``process:read`` / ``process:write`` / ``process:admin``). Acting on a human task
is bound to the assignee principal or, for role-assigned tasks, to any writer of the tenant
until Control Plane bindings (superproject ADR-0023 §3) take over the role check.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from process_runtime.auth import SCOPE_ADMIN, SCOPE_READ, SCOPE_WRITE, Caller
from process_runtime.domain.entities import WorkflowInstance, WorkflowTask
from process_runtime.domain.enums import (
    TERMINAL_INSTANCE_STATUSES,
    WorkflowTaskStatus,
    WorkflowTransitionTrigger,
)
from process_runtime.domain.errors import (
    WorkflowInstanceAlreadyCompleted,
    WorkflowInstanceNotFound,
    WorkflowMigrationMapInconsistent,
    WorkflowTaskNotClaimable,
    WorkflowVersionFrozen,
)
from process_runtime.domain.value_objects import MigrationMap
from process_runtime.events import ProcessEventLog
from process_runtime.manifest import MANIFEST
from process_runtime.runtime.bootstrap import Engine
from process_runtime.runtime.pg_store import PostgresWorkflowEngineStore
from process_runtime.runtime.runtime import WorkflowAdvanceService

router = APIRouter(prefix="/api/v1", tags=["process-runtime"])


# ------------------------------------------------------------------ dependencies
def get_engine(request: Request) -> Engine:
    engine: Engine | None = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "engine_not_initialized")
    return engine


def get_store(request: Request) -> PostgresWorkflowEngineStore:
    return request.app.state.store  # type: ignore[no-any-return]


def get_service(request: Request) -> WorkflowAdvanceService:
    return request.app.state.service  # type: ignore[no-any-return]


def get_events(request: Request) -> ProcessEventLog:
    return request.app.state.events  # type: ignore[no-any-return]


async def get_session(request: Request):  # type: ignore[no-untyped-def]
    async with request.app.state.session_factory() as session:
        yield session


async def get_caller(request: Request) -> Caller:
    resolver: Callable[[Request], Awaitable[Caller]] = request.app.state.resolve_caller
    return await resolver(request)


def require(scope: str) -> Callable[[Caller], Caller]:
    async def _check(caller: Caller = Depends(get_caller)) -> Caller:
        if not caller.has(scope):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "scope_not_allowed")
        return caller

    return _check  # type: ignore[return-value]


Reader = Annotated[Caller, Depends(require(SCOPE_READ))]
Writer = Annotated[Caller, Depends(require(SCOPE_WRITE))]
Admin = Annotated[Caller, Depends(require(SCOPE_ADMIN))]
Session = Annotated[AsyncSession, Depends(get_session)]
EngineDep = Annotated[Engine, Depends(get_engine)]
StoreDep = Annotated[PostgresWorkflowEngineStore, Depends(get_store)]
ServiceDep = Annotated[WorkflowAdvanceService, Depends(get_service)]
EventsDep = Annotated[ProcessEventLog, Depends(get_events)]


# ------------------------------------------------------------------------ models
class WorkflowDefinitionRead(BaseModel):
    workflow_id: str
    version: int
    description: str | None = None
    is_deprecated: bool = False
    is_retired: bool = False


class WorkflowInstanceCreate(BaseModel):
    workflow_id: str
    version: int | None = None
    subject_entity_type: str | None = None
    subject_entity_id: UUID | None = None
    initial_variables: dict[str, Any] = Field(default_factory=dict)


class BpmnTaskRefRead(BaseModel):
    task_id: str
    name: str
    type: str
    lane: str | None = None


class WorkflowInstanceRead(BaseModel):
    id: UUID
    tenant_id: UUID
    workflow_id: str
    workflow_version: int
    status: str
    subject_entity_type: str | None = None
    subject_entity_id: UUID | None = None
    current_tasks: list[str]
    variables: dict[str, Any]
    outcome: str | None = None
    error_payload: dict[str, Any] | None = None


class WorkflowTaskRead(BaseModel):
    id: UUID
    task_id: str
    status: str
    assigned_role: str | None = None
    assigned_user_id: UUID | None = None
    approval_policy_id: str | None = None


class TaskCompleteBody(BaseModel):
    form_payload: dict[str, Any] = Field(default_factory=dict)


class SignalBody(BaseModel):
    signal_name: str
    payload: dict[str, Any] = Field(default_factory=dict)


class CancelBody(BaseModel):
    reason: str


class MigrateBody(BaseModel):
    from_version: int
    to_version: int
    task_id_mapping: dict[str, str]
    variables_transformer: str | None = None


class WorkflowDiagramRead(BaseModel):
    bpmn_xml: str
    dmn_xmls: dict[str, str]
    active_tokens: list[BpmnTaskRefRead]
    variables: dict[str, Any]


class EventPage(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: int | None = Field(default=None, alias="nextCursor")

    model_config = {"populate_by_name": True}


def _instance_read(instance: WorkflowInstance) -> WorkflowInstanceRead:
    return WorkflowInstanceRead(
        id=instance.id,
        tenant_id=instance.tenant_id,
        workflow_id=instance.workflow_id,
        workflow_version=instance.workflow_version,
        status=instance.status.value,
        subject_entity_type=instance.subject_entity_type,
        subject_entity_id=instance.subject_entity_id,
        current_tasks=list(instance.current_tasks),
        variables=instance.variables,
        outcome=instance.outcome,
        error_payload=instance.error_payload,
    )


def _task_read(task: WorkflowTask) -> WorkflowTaskRead:
    return WorkflowTaskRead(
        id=task.id,
        task_id=task.task_id,
        status=task.status.value,
        assigned_role=task.assigned_role,
        assigned_user_id=task.assigned_user_id,
        approval_policy_id=task.approval_policy_id,
    )


async def _load_scoped(
    store: PostgresWorkflowEngineStore, session: AsyncSession, instance_id: UUID, tenant_id: UUID
) -> WorkflowInstance:
    instance = await store.load_instance(instance_id, tenant_id=tenant_id, session=session)
    if instance is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "WORKFLOW_INSTANCE_NOT_FOUND")
    return instance


def _may_act_on_task(task: WorkflowTask, caller: Caller) -> bool:
    if caller.is_admin:
        return True
    if task.assigned_user_id is not None:
        return task.assigned_user_id == caller.principal_id
    return True


# --------------------------------------------------------------------- endpoints
@router.get("/manifest")
async def manifest(_: Reader) -> dict[str, Any]:
    """Capability manifest (ADR-0023 §2): standards, executable subset, extension profile."""
    return MANIFEST


@router.get("/definitions", response_model=list[WorkflowDefinitionRead])
async def list_definitions(_: Reader, engine: EngineDep) -> list[WorkflowDefinitionRead]:
    return [
        WorkflowDefinitionRead(
            workflow_id=decl.workflow_id,
            version=decl.version,
            description=decl.description,
            is_deprecated=decl.is_deprecated,
            is_retired=decl.is_retired,
        )
        for decl in engine.registry.iter_declarations()
    ]


@router.post("/instances", response_model=WorkflowInstanceRead, status_code=status.HTTP_201_CREATED)
async def create_instance(
    caller: Writer, body: WorkflowInstanceCreate, service: ServiceDep, engine: EngineDep
) -> WorkflowInstanceRead:
    version = (
        body.version
        if body.version is not None
        else engine.registry.latest_version(body.workflow_id)
    )
    decl = engine.registry.get_declaration(body.workflow_id, version) if version else None
    if decl is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "WORKFLOW_DEFINITION_INVALID")
    if decl.is_deprecated or decl.is_retired:
        raise HTTPException(status.HTTP_409_CONFLICT, "WORKFLOW_VERSION_FROZEN")
    instance = await service.start_instance(
        tenant_id=caller.tenant_id,
        workflow_id=body.workflow_id,
        version=decl.version,
        subject_entity_type=body.subject_entity_type,
        subject_entity_id=body.subject_entity_id,
        initial_variables=body.initial_variables,
    )
    return _instance_read(instance)


@router.get("/instances/{instance_id}", response_model=WorkflowInstanceRead)
async def get_instance(
    caller: Reader, instance_id: UUID, session: Session, store: StoreDep
) -> WorkflowInstanceRead:
    return _instance_read(await _load_scoped(store, session, instance_id, caller.tenant_id))


@router.get("/instances/{instance_id}/tasks", response_model=list[WorkflowTaskRead])
async def list_instance_tasks(
    caller: Reader, instance_id: UUID, session: Session, store: StoreDep
) -> list[WorkflowTaskRead]:
    tasks = await store.list_tasks(instance_id, tenant_id=caller.tenant_id, session=session)
    return [_task_read(t) for t in tasks]


@router.post(
    "/instances/{instance_id}/tasks/{task_id}/complete", response_model=WorkflowInstanceRead
)
async def complete_task(
    caller: Writer,
    instance_id: UUID,
    task_id: str,
    body: TaskCompleteBody,
    session: Session,
    store: StoreDep,
    service: ServiceDep,
) -> WorkflowInstanceRead:
    instance = await _load_scoped(store, session, instance_id, caller.tenant_id)
    if instance.status in TERMINAL_INSTANCE_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "WORKFLOW_INSTANCE_ALREADY_COMPLETED")
    tasks = await store.list_tasks(instance_id, tenant_id=caller.tenant_id, session=session)
    active = next(
        (t for t in tasks if t.task_id == task_id and t.status is WorkflowTaskStatus.pending), None
    )
    if active is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "WORKFLOW_TASK_NOT_CLAIMABLE")
    if not _may_act_on_task(active, caller):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "WORKFLOW_TASK_NOT_CLAIMABLE")
    try:
        updated = await service.advance(
            instance_id=instance_id,
            trigger=WorkflowTransitionTrigger.task_complete,
            tenant_id=caller.tenant_id,
            actor_user_id=caller.principal_id,
            payload={
                "task_id": task_id,
                "form_payload": body.form_payload,
                "dedup_key": f"task:{instance_id}:{task_id}:{caller.principal_id}",
            },
        )
    except WorkflowTaskNotClaimable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "WORKFLOW_TASK_NOT_CLAIMABLE") from exc
    assert updated is not None
    return _instance_read(updated)


@router.post("/instances/{instance_id}/signal", response_model=WorkflowInstanceRead)
async def send_signal(
    caller: Writer,
    instance_id: UUID,
    body: SignalBody,
    session: Session,
    store: StoreDep,
    service: ServiceDep,
) -> WorkflowInstanceRead:
    await _load_scoped(store, session, instance_id, caller.tenant_id)
    updated = await service.advance(
        instance_id=instance_id,
        trigger=WorkflowTransitionTrigger.signal,
        tenant_id=caller.tenant_id,
        actor_user_id=caller.principal_id,
        payload={"signal_name": body.signal_name, "event_payload": body.payload},
    )
    assert updated is not None
    return _instance_read(updated)


@router.post("/instances/{instance_id}/cancel", response_model=WorkflowInstanceRead)
async def cancel_instance(
    caller: Writer, instance_id: UUID, body: CancelBody, service: ServiceDep
) -> WorkflowInstanceRead:
    try:
        updated = await service.cancel(
            instance_id=instance_id,
            reason=body.reason,
            tenant_id=caller.tenant_id,
            actor_user_id=caller.principal_id,
        )
    except WorkflowInstanceNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "WORKFLOW_INSTANCE_NOT_FOUND") from exc
    except WorkflowInstanceAlreadyCompleted as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "WORKFLOW_INSTANCE_ALREADY_COMPLETED"
        ) from exc
    return _instance_read(updated)


@router.post("/instances/{instance_id}/migrate", response_model=WorkflowInstanceRead)
async def migrate_instance(
    caller: Admin, instance_id: UUID, body: MigrateBody, service: ServiceDep
) -> WorkflowInstanceRead:
    migration = MigrationMap(
        from_version=body.from_version,
        to_version=body.to_version,
        task_id_mapping=body.task_id_mapping,
        variables_transformer=body.variables_transformer,
    )
    try:
        updated = await service.migrate(
            instance_id=instance_id,
            migration=migration,
            tenant_id=caller.tenant_id,
            actor_user_id=caller.principal_id,
        )
    except WorkflowInstanceNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "WORKFLOW_INSTANCE_NOT_FOUND") from exc
    except WorkflowInstanceAlreadyCompleted as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "WORKFLOW_INSTANCE_ALREADY_COMPLETED"
        ) from exc
    except WorkflowVersionFrozen as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "WORKFLOW_VERSION_FROZEN") from exc
    except WorkflowMigrationMapInconsistent as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "WORKFLOW_MIGRATION_MAP_INCONSISTENT"
        ) from exc
    return _instance_read(updated)


@router.get("/instances/{instance_id}/diagram", response_model=WorkflowDiagramRead)
async def get_diagram(
    caller: Reader, instance_id: UUID, session: Session, store: StoreDep, engine: EngineDep
) -> WorkflowDiagramRead:
    instance = await _load_scoped(store, session, instance_id, caller.tenant_id)
    parsed = engine.registry.get(instance.workflow_id, instance.workflow_version)
    if parsed is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "WORKFLOW_DEFINITION_INVALID")
    decl = engine.registry.get_declaration(instance.workflow_id, instance.workflow_version)
    bpmn_xml = ""
    dmn_xmls: dict[str, str] = {}
    if decl is not None:
        root = Path(engine.definitions_dir)
        try:
            bpmn_xml = (root / decl.bpmn_path).read_text(encoding="utf-8")
            for dmn in decl.dmn_paths:
                dmn_xmls[dmn] = (root / dmn).read_text(encoding="utf-8")
        except OSError:
            bpmn_xml = ""
    active = engine.adapter.active_tasks(state=instance.spiff_state, parsed=parsed)
    return WorkflowDiagramRead(
        bpmn_xml=bpmn_xml,
        dmn_xmls=dmn_xmls,
        active_tokens=[
            BpmnTaskRefRead(task_id=t.task_id, name=t.name, type=t.type.value, lane=t.lane)
            for t in active
        ],
        variables=instance.variables,
    )


@router.get("/inbox", response_model=list[WorkflowTaskRead])
async def inbox(
    caller: Reader,
    session: Session,
    store: StoreDep,
    assignee: Annotated[Literal["me"] | None, Query()] = None,
    role: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[WorkflowTaskRead]:
    assignee_id = caller.principal_id if assignee == "me" else None
    tasks = await store.list_tasks_inbox(
        tenant_id=caller.tenant_id,
        assignee_user_id=assignee_id,
        role=role,
        limit=limit,
        offset=offset,
        session=session,
    )
    return [_task_read(t) for t in tasks]


@router.get("/events", response_model=EventPage, response_model_by_alias=True)
async def list_events(
    caller: Reader,
    session: Session,
    events: EventsDep,
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> EventPage:
    items = await events.read(
        tenant_id=caller.tenant_id, after=cursor, limit=limit, session=session
    )
    next_cursor = items[-1]["cursor"] if len(items) == limit else None
    return EventPage(items=items, nextCursor=next_cursor)


__all__ = ["router"]
