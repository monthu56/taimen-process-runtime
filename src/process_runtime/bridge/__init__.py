"""Control Plane bridge: the process runtime as the fourth kind of executor.

``build_bridge`` wires the canonical Control Plane client with the service account of
process-runtime; ``run_forever`` drives both reconciliation loops from the application
lifespan. Everything the bridge knows about the Control Plane goes through
``control_plane_client`` (superproject ADR-0030).
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from control_plane_client import ControlPlaneClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from process_runtime.bridge.credential import ServiceAccountCredential
from process_runtime.bridge.reconcile import BridgeConfig, ControlPlaneBridge, PassResult
from process_runtime.bridge.store import BindingStore, TaskBinding
from process_runtime.config import Settings
from process_runtime.runtime.loader import WorkflowDefinitionRegistry
from process_runtime.runtime.pg_store import PostgresWorkflowEngineStore
from process_runtime.runtime.runtime import WorkflowAdvanceService

_log = logging.getLogger("process_runtime.bridge")


def build_bridge(
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    store: PostgresWorkflowEngineStore,
    service: WorkflowAdvanceService,
    registry: WorkflowDefinitionRegistry,
) -> tuple[ControlPlaneBridge, ControlPlaneClient]:
    credential = ServiceAccountCredential(
        iam_base_url=settings.iam_base_url,
        client_id=settings.iam_client_id,
        client_secret=settings.iam_client_secret,
    )
    client = ControlPlaneClient(
        settings.cp_base_url, credential, timeout=15.0, user_agent="process-runtime/0.1"
    )
    bridge = ControlPlaneBridge(
        session_factory=session_factory,
        cp=client,
        config=BridgeConfig(
            tenant_id=UUID(settings.bridge_tenant_id),
            workspace_id=settings.cp_workspace_id,
            task_type_key=settings.cp_task_type_key or None,
            batch_size=settings.bridge_batch_size,
        ),
        store=store,
        service=service,
        registry=registry,
    )
    return bridge, client


async def run_forever(bridge: ControlPlaneBridge, *, poll_seconds: float) -> None:
    """Alternate outbound and inbound passes; back off when idle or stalled."""
    while True:
        try:
            outbound = await bridge.outbound_once()
            inbound = await bridge.inbound_once()
            idle = outbound.processed == 0 and inbound.processed == 0
            if idle or outbound.stalled or inbound.stalled:
                await asyncio.sleep(poll_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("bridge pass failed")
            await asyncio.sleep(poll_seconds)


__all__ = [
    "BindingStore",
    "BridgeConfig",
    "ControlPlaneBridge",
    "PassResult",
    "ServiceAccountCredential",
    "TaskBinding",
    "build_bridge",
    "run_forever",
]
