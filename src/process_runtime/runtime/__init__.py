"""Persistence, runtime and boot wiring of the process engine.

Imports ``process_runtime.domain`` (protocols/entities) and ``process_runtime.tables``;
the concrete engine adapter is built in ``bootstrap`` from settings.
"""

from __future__ import annotations

from process_runtime.runtime.approval import AlwaysApprovedHook, NoopApprovalEngineHook
from process_runtime.runtime.bootstrap import build_adapter, build_engine
from process_runtime.runtime.definitions import load_declarations
from process_runtime.runtime.loader import BpmnDefinitionLoader, WorkflowDefinitionRegistry
from process_runtime.runtime.pg_store import PostgresWorkflowEngineStore
from process_runtime.runtime.runtime import WorkflowAdvanceService

__all__ = [
    "AlwaysApprovedHook",
    "BpmnDefinitionLoader",
    "NoopApprovalEngineHook",
    "PostgresWorkflowEngineStore",
    "WorkflowAdvanceService",
    "WorkflowDefinitionRegistry",
    "build_adapter",
    "build_engine",
    "load_declarations",
]
