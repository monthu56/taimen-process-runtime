"""SpiffWorkflow adapter subpackage (EPIC-11, ADR-023).

LGPL-3.0: SpiffWorkflow is used as an unmodified library — do not fork; send fixes
upstream. See ADR-023 §Последствия.
"""

from __future__ import annotations

from process_runtime.engine.spiff.adapter import SpiffWorkflowAdapter
from process_runtime.engine.spiff.script_engine import (
    SandboxedTaskDataEnvironment,
    build_script_engine,
)

__all__ = [
    "SandboxedTaskDataEnvironment",
    "SpiffWorkflowAdapter",
    "build_script_engine",
]
