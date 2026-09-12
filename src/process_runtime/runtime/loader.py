"""BPMN definition loader + in-process definition registry (EPIC-11, ADR-023, WF-008).

Reads git-versioned BPMN/DMN from ``WORKFLOW_ENGINE_BPMN_ROOT``, parses them through the
engine adapter, and caches the ``ParsedDefinition`` alongside its declaration. Validation
is fail-fast on boot: every ServiceTask needs a handler; every UserTask carrying an
``approval_policy`` needs a real ApprovalEngineHook (not the Noop default).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from process_runtime.domain.errors import (
    RegistryConflictError,
    WorkflowApprovalHookNotConfigured,
    WorkflowDefinitionInvalid,
)
from process_runtime.domain.protocols import WorkflowEngineAdapter
from process_runtime.domain.value_objects import (
    CorrelationRule,
    ParsedDefinition,
    WorkflowDefinitionDeclaration,
)


def _key(workflow_id: str, version: int) -> str:
    return f"{workflow_id}@v{version}"


class WorkflowDefinitionRegistry:
    """Process-global cache of parsed workflow definitions and correlation rules."""

    def __init__(self) -> None:
        self._parsed: dict[str, ParsedDefinition] = {}
        self._declarations: dict[str, WorkflowDefinitionDeclaration] = {}
        self._latest: dict[str, int] = {}

    def register(
        self, declaration: WorkflowDefinitionDeclaration, parsed: ParsedDefinition
    ) -> None:
        key = _key(declaration.workflow_id, declaration.version)
        if key in self._declarations:
            raise RegistryConflictError(f"workflow definition already registered: {key}")
        self._declarations[key] = declaration
        self._parsed[key] = parsed
        current = self._latest.get(declaration.workflow_id)
        if current is None or declaration.version > current:
            self._latest[declaration.workflow_id] = declaration.version

    def get(self, workflow_id: str, version: int) -> ParsedDefinition | None:
        return self._parsed.get(_key(workflow_id, version))

    def get_declaration(
        self, workflow_id: str, version: int
    ) -> WorkflowDefinitionDeclaration | None:
        return self._declarations.get(_key(workflow_id, version))

    def latest_version(self, workflow_id: str) -> int | None:
        return self._latest.get(workflow_id)

    def iter_declarations(self) -> Iterable[WorkflowDefinitionDeclaration]:
        return tuple(self._declarations.values())

    def correlation_index(
        self,
    ) -> Mapping[str, tuple[tuple[WorkflowDefinitionDeclaration, CorrelationRule], ...]]:
        """Map event_type -> ((declaration, rule), ...) for the signal bridge."""
        index: dict[str, list[tuple[WorkflowDefinitionDeclaration, CorrelationRule]]] = {}
        for decl in self._declarations.values():
            for rule in decl.signal_correlations:
                index.setdefault(rule.event_type, []).append((decl, rule))
        return {event_type: tuple(items) for event_type, items in index.items()}

    def reset(self) -> None:
        self._parsed.clear()
        self._declarations.clear()
        self._latest.clear()


class BpmnDefinitionLoader:
    """Loads and validates workflow definitions from the BPMN root directory."""

    def __init__(
        self,
        *,
        adapter: WorkflowEngineAdapter,
        bpmn_root: str | Path,
        registry: WorkflowDefinitionRegistry | None = None,
    ) -> None:
        self._adapter = adapter
        self._root = Path(bpmn_root)
        self.registry = registry or WorkflowDefinitionRegistry()

    def _read(self, relative: str) -> bytes:
        path = self._root / relative
        if not path.is_file():
            raise WorkflowDefinitionInvalid(f"BPMN/DMN file not found: {path}")
        return path.read_bytes()

    def load(self, declaration: WorkflowDefinitionDeclaration) -> ParsedDefinition:
        bpmn_xml = self._read(declaration.bpmn_path)
        dmn_xmls = {dmn: self._read(dmn) for dmn in declaration.dmn_paths}
        parsed = self._adapter.parse(
            bpmn_xml=bpmn_xml,
            dmn_xmls=dmn_xmls,
            workflow_id=declaration.workflow_id,
            version=declaration.version,
            process_id=declaration.process_id,
        )
        self.registry.register(declaration, parsed)
        return parsed

    def validate_on_boot(
        self,
        declarations: Iterable[WorkflowDefinitionDeclaration],
        *,
        global_task_handlers: Mapping[str, type] | None = None,
        approval_hook_configured: bool = False,
    ) -> None:
        """Load + validate every declaration; raise on the first problem (fail-fast)."""
        shared = dict(global_task_handlers or {})
        for declaration in declarations:
            parsed = self.load(declaration)
            handlers = {**shared, **dict(declaration.task_handlers)}
            for ref in parsed.service_tasks:
                if ref.task_id not in handlers:
                    raise WorkflowDefinitionInvalid(
                        f"workflow '{declaration.workflow_id}' v{declaration.version}: "
                        f"service task '{ref.task_id}' has no registered handler"
                    )
            for ref in parsed.user_tasks:
                policy = (parsed.task_extensions.get(ref.task_id, {}) or {}).get("approval_policy")
                if policy and not approval_hook_configured:
                    raise WorkflowApprovalHookNotConfigured(
                        f"workflow '{declaration.workflow_id}' v{declaration.version}: "
                        f"user task '{ref.task_id}' requires approval policy '{policy}' "
                        "but no ApprovalEngineHook is configured"
                    )


__all__ = [
    "BpmnDefinitionLoader",
    "WorkflowDefinitionRegistry",
]
