"""FastAPI application of process-runtime."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status

from process_runtime.api import router
from process_runtime.auth import (
    Caller,
    IamContextError,
    IamContextVerifier,
    IamVerificationUnavailable,
)
from process_runtime.bridge import build_bridge, run_forever
from process_runtime.config import Settings
from process_runtime.db import Database
from process_runtime.events import ProcessEventLog
from process_runtime.runtime.bootstrap import build_engine
from process_runtime.runtime.pg_store import PostgresWorkflowEngineStore
from process_runtime.runtime.runtime import WorkflowAdvanceService
from process_runtime.tables import metadata
from process_runtime.timers import timer_loop


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or Settings()
    database = Database(runtime_settings)
    verifier = IamContextVerifier(
        issuer=runtime_settings.iam_issuer,
        audience=runtime_settings.iam_audience,
        public_key_pem=runtime_settings.resolved_iam_public_key(),
        jwks_url=runtime_settings.iam_jwks_url,
    )
    store = PostgresWorkflowEngineStore()
    events = ProcessEventLog()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if runtime_settings.create_schema_on_startup:
            async with database.engine.begin() as connection:
                await connection.run_sync(metadata.create_all)
        engine = build_engine(runtime_settings)
        service = WorkflowAdvanceService(
            session_factory=database.sessions,
            store=store,
            adapter=engine.adapter,
            registry=engine.registry,
            events=events,
        )
        app.state.engine = engine
        app.state.service = service
        tasks: list[asyncio.Task[None]] = []
        closers = []
        if runtime_settings.bridge_enabled():
            bridge, client = build_bridge(
                runtime_settings,
                session_factory=database.sessions,
                store=store,
                service=service,
                registry=engine.registry,
            )
            app.state.bridge = bridge
            closers.append(client.aclose)
            tasks.append(
                asyncio.create_task(
                    run_forever(bridge, poll_seconds=runtime_settings.bridge_poll_seconds)
                )
            )
        worker: asyncio.Task[None] | None = None
        if runtime_settings.timer_worker_enabled:
            worker = asyncio.create_task(
                timer_loop(
                    session_factory=database.sessions,
                    store=store,
                    service=service,
                    interval_seconds=runtime_settings.timer_tick_seconds,
                    batch_size=runtime_settings.timer_due_batch_size,
                )
            )
        if worker is not None:
            tasks.append(worker)
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for close in closers:
                await close()
            await database.close()

    app = FastAPI(title="Process Runtime", version="0.1.0", lifespan=lifespan)
    app.state.settings = runtime_settings
    app.state.session_factory = database.sessions
    app.state.store = store
    app.state.events = events

    async def resolve_caller(request: Request) -> Caller:
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "iam_context_required")
        try:
            return await verifier.verify(token)
        except IamVerificationUnavailable as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "iam_verification_unavailable"
            ) from exc
        except IamContextError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_iam_context") from exc

    app.state.resolve_caller = resolve_caller

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        engine = getattr(app.state, "engine", None)
        return {
            "status": "ok",
            "definitions": len(list(engine.registry.iter_declarations())) if engine else 0,
            "controlPlaneBridge": getattr(app.state, "bridge", None) is not None,
        }

    app.include_router(router)
    return app


app = create_app()

__all__ = ["app", "create_app"]
