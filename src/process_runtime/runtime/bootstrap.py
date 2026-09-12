"""Engine boot wiring: adapter + parsed-definition registry from settings.

Fail-fast: an invalid BPMN, a service task without a handler or a policy-bearing user
task without an approval hook aborts the boot instead of failing at the first advance.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from process_runtime.config import Settings
from process_runtime.domain.protocols import WorkflowEngineAdapter
from process_runtime.domain.value_objects import WorkflowDefinitionDeclaration
from process_runtime.runtime.definitions import load_declarations
from process_runtime.runtime.loader import BpmnDefinitionLoader, WorkflowDefinitionRegistry


@dataclass(frozen=True, slots=True)
class Engine:
    adapter: WorkflowEngineAdapter
    registry: WorkflowDefinitionRegistry
    definitions_dir: str


def build_adapter(settings: Settings) -> WorkflowEngineAdapter:
    """Instantiate the configured engine adapter (spiff | dummy)."""
    if settings.adapter == "dummy":
        from process_runtime.engine.dummy import DummyAdapter

        return DummyAdapter()
    from process_runtime.engine.spiff import SpiffWorkflowAdapter

    if (
        settings.script_task_sandbox in ("disabled", "relaxed")
        and not settings.allow_sandbox_disabled
    ):
        raise RuntimeError(
            f"PR_SCRIPT_TASK_SANDBOX={settings.script_task_sandbox} requires "
            "PR_ALLOW_SANDBOX_DISABLED=1 (local development only)"
        )
    return SpiffWorkflowAdapter(sandbox_mode=settings.script_task_sandbox)


def build_engine(
    settings: Settings,
    *,
    declarations: Iterable[WorkflowDefinitionDeclaration] | None = None,
    global_task_handlers: Mapping[str, type] | None = None,
    approval_hook_configured: bool = False,
) -> Engine:
    """Load + validate the catalogue and return the adapter with its registry."""
    adapter = build_adapter(settings)
    registry = WorkflowDefinitionRegistry()
    loader = BpmnDefinitionLoader(
        adapter=adapter, bpmn_root=settings.definitions_dir, registry=registry
    )
    resolved = (
        tuple(declarations)
        if declarations is not None
        else load_declarations(settings.definitions_dir)
    )
    loader.validate_on_boot(
        resolved,
        global_task_handlers=global_task_handlers,
        approval_hook_configured=approval_hook_configured,
    )
    return Engine(adapter=adapter, registry=registry, definitions_dir=settings.definitions_dir)


__all__ = ["Engine", "build_adapter", "build_engine"]
