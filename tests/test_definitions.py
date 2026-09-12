"""Catalogue (definitions.json) and engine boot."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from process_runtime.config import Settings
from process_runtime.domain.errors import WorkflowDefinitionInvalid
from process_runtime.engine.handlers import SimpleNotificationHandler
from process_runtime.runtime import build_engine, load_declarations
from tests.conftest import DEFINITIONS_DIR


def test_catalogue_loads_shipped_definitions() -> None:
    declarations = load_declarations(DEFINITIONS_DIR)
    ids = {(d.workflow_id, d.version) for d in declarations}
    assert {("three_step_approval", 1), ("score_routing", 1), ("timer_reminder", 1)} <= ids
    score = next(d for d in declarations if d.workflow_id == "score_routing")
    assert score.dmn_paths == ("generic/sample_decision-v1.dmn",)


def test_engine_boots_from_catalogue_with_spiff() -> None:
    engine = build_engine(Settings(definitions_dir=DEFINITIONS_DIR))
    assert engine.registry.latest_version("three_step_approval") == 1
    parsed = engine.registry.get("three_step_approval", 1)
    assert parsed is not None and {t.task_id for t in parsed.user_tasks} == {
        "initial_review",
        "final_approval",
    }


def test_missing_catalogue_is_empty(tmp_path: Path) -> None:
    assert load_declarations(tmp_path) == ()
    engine = build_engine(Settings(definitions_dir=str(tmp_path), adapter="dummy"))
    assert list(engine.registry.iter_declarations()) == []


def test_handler_names_resolve_and_unknown_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "definitions.json").write_text(
        json.dumps(
            {
                "workflows": [
                    {
                        "workflow_id": "wf",
                        "version": 1,
                        "bpmn_path": "wf.bpmn",
                        "task_handlers": {"notify": "notification"},
                    }
                ]
            }
        )
    )
    (decl,) = load_declarations(tmp_path)
    assert decl.task_handlers["notify"] is SimpleNotificationHandler
    (tmp_path / "definitions.json").write_text(
        json.dumps(
            {
                "workflows": [
                    {
                        "workflow_id": "wf",
                        "version": 1,
                        "bpmn_path": "wf.bpmn",
                        "task_handlers": {"x": "nope"},
                    }
                ]
            }
        )
    )
    with pytest.raises(WorkflowDefinitionInvalid):
        load_declarations(tmp_path)


def test_sandbox_guard_refuses_disabled_without_opt_in() -> None:
    with pytest.raises(RuntimeError, match="PR_ALLOW_SANDBOX_DISABLED"):
        build_engine(Settings(definitions_dir=DEFINITIONS_DIR, script_task_sandbox="disabled"))
