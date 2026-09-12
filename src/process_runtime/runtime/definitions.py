"""Declarative workflow catalogue: ``definitions.json`` next to the BPMN/DMN files.

In platform-core declarations were Python objects registered by an in-process product.
A standalone service has no product in its process, so the catalogue is data:

```json
{
  "workflows": [
    {"workflow_id": "three_step_approval", "version": 1,
     "bpmn_path": "generic/three_step_approval-v1.bpmn",
     "dmn_paths": [], "description": "…", "task_handlers": {"notify": "notification"}}
  ]
}
```

Paths are relative to the definitions directory. Handler names resolve to built-in
handlers (``process_runtime.engine.handlers.BUILTIN_HANDLERS``). Publishing a definition
is a git change reviewed like code (superproject ADR-0023 §6: published versions are
immutable, a change is a new version).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from process_runtime.domain.errors import WorkflowDefinitionInvalid
from process_runtime.domain.value_objects import CorrelationRule, WorkflowDefinitionDeclaration
from process_runtime.engine.handlers import resolve_handlers

CATALOGUE_FILE = "definitions.json"


def load_declarations(definitions_dir: str | Path) -> Sequence[WorkflowDefinitionDeclaration]:
    """Read the catalogue; a missing file means an empty catalogue (the service still boots)."""
    path = Path(definitions_dir) / CATALOGUE_FILE
    if not path.is_file():
        return ()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkflowDefinitionInvalid(f"cannot read {path}: {exc}") from exc
    entries = raw.get("workflows", []) if isinstance(raw, dict) else raw
    declarations: list[WorkflowDefinitionDeclaration] = []
    for entry in entries:
        try:
            correlations = tuple(
                CorrelationRule(
                    event_type=str(rule["event_type"]),
                    signal_name=str(rule["signal_name"]),
                    subject_field=str(rule["subject_field"]),
                    subject_entity_type=str(rule["subject_entity_type"]),
                    payload_mapping=dict(rule.get("payload_mapping", {})),
                )
                for rule in entry.get("signal_correlations", [])
            )
            declarations.append(
                WorkflowDefinitionDeclaration(
                    workflow_id=str(entry["workflow_id"]),
                    version=int(entry["version"]),
                    bpmn_path=str(entry["bpmn_path"]),
                    dmn_paths=tuple(str(p) for p in entry.get("dmn_paths", [])),
                    process_id=entry.get("process_id"),
                    task_handlers=resolve_handlers(dict(entry.get("task_handlers", {}))),
                    signal_correlations=correlations,
                    description=entry.get("description"),
                    is_deprecated=bool(entry.get("is_deprecated", False)),
                    is_retired=bool(entry.get("is_retired", False)),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowDefinitionInvalid(f"invalid declaration in {path}: {exc}") from exc
    return tuple(declarations)


__all__ = ["CATALOGUE_FILE", "load_declarations"]
