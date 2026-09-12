"""Sandboxed Python script engine for SpiffWorkflow (EPIC-11, ADR-023, WF-007).

SpiffWorkflow runs inline Python for ScriptTasks, gateway conditions, timer
expressions, DMN input/output entries and ``spiffworkflow:preScript``/``postScript``
hooks — ALL of them through ``PythonScriptEngineEnvironment.execute``/``.evaluate``.
Overriding those two methods is therefore the single choke point that sandboxes every
untrusted expression (design amendment A2).

Defense is two-layer:

1. An AST pre-gate (full ``ast.walk``) rejects imports, dunder attribute access, the
   ``str.format``/``format_map`` runtime-traversal bypass, ``%``-formatting, and calls to
   blacklisted names — before the code ever executes.
2. Execution runs against an explicit whitelist of callables (never whole modules — a
   module exposes its imported submodules as non-dunder attributes, enabling
   module-hopping to ``os``/``sys``) with ``{"__builtins__": {}}``.

A violation raises :class:`process_runtime.domain.ScriptTaskSecurityError`.
"""

from __future__ import annotations

import ast
import math
import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from SpiffWorkflow.bpmn.script_engine import PythonScriptEngine, TaskDataEnvironment

from process_runtime.domain.errors import ScriptTaskSecurityError

# Names that may never appear as a called function or bare reference.
_BLACKLIST_NAMES: frozenset[str] = frozenset(
    {
        "__import__",
        "open",
        "eval",
        "exec",
        "compile",
        "globals",
        "locals",
        "vars",
        "dir",
        "getattr",
        "setattr",
        "delattr",
        "hasattr",
        "breakpoint",
        "input",
        "exit",
        "quit",
        "help",
        "memoryview",
        "type",
        "object",
        "super",
        "classmethod",
        "staticmethod",
        "property",
        "__build_class__",
    }
)

# Attribute names that may never be *called* (runtime traversal bypasses AST dunder check).
_BLACKLIST_METHODS: frozenset[str] = frozenset({"format", "format_map"})

_SAFE_BUILTINS: dict[str, Any] = {
    "len": len,
    "range": range,
    "sum": sum,
    "min": min,
    "max": max,
    "abs": abs,
    "round": round,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
    "frozenset": frozenset,
    "sorted": sorted,
    "reversed": reversed,
    "all": all,
    "any": any,
    "isinstance": isinstance,
    "enumerate": enumerate,
    "zip": zip,
    "map": map,
    "filter": filter,
}

# Specific callables — NEVER whole modules (module-hopping via re.functools etc.).
_SAFE_GLOBALS: dict[str, Any] = {
    **_SAFE_BUILTINS,
    "Decimal": Decimal,
    "datetime": datetime,
    "date": date,
    "time": time,
    "timedelta": timedelta,
    "timezone": timezone,
    "re_match": re.match,
    "re_search": re.search,
    "re_sub": re.sub,
    "re_fullmatch": re.fullmatch,
    "re_findall": re.findall,
    "sqrt": math.sqrt,
    "floor": math.floor,
    "ceil": math.ceil,
    "pow": pow,
    "fabs": math.fabs,
    "log": math.log,
    "exp": math.exp,
}


def _validate_source(source: str) -> None:
    """AST pre-gate over the full tree; raise ScriptTaskSecurityError on any violation."""
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise ScriptTaskSecurityError(f"script syntax error: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ScriptTaskSecurityError("import statements are not allowed in scripts")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ScriptTaskSecurityError(f"dunder attribute access is forbidden: {node.attr}")
        if isinstance(node, ast.Name) and node.id in _BLACKLIST_NAMES:
            raise ScriptTaskSecurityError(f"use of '{node.id}' is forbidden in scripts")
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _BLACKLIST_METHODS:
                raise ScriptTaskSecurityError(
                    f"calling '.{func.attr}()' is forbidden (format-string traversal bypass)"
                )
        # Reject %-formatting whose right operand traverses objects, e.g. "%s" % obj.
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Mod)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
        ):
            raise ScriptTaskSecurityError("%-string formatting is forbidden in scripts")


class SandboxedTaskDataEnvironment(TaskDataEnvironment):
    """TaskDataEnvironment whose execute/evaluate run the sandbox for ALL script paths."""

    def __init__(self, *, ast_gate: bool = True) -> None:
        super().__init__(dict(_SAFE_GLOBALS))
        self._ast_gate = ast_gate

    def _guard(self, script: str) -> None:
        if self._ast_gate:
            _validate_source(script)

    def execute(
        self,
        script: str,
        context: dict[str, Any],
        external_context: Mapping[str, Any] | None = None,
    ) -> bool:
        self._guard(script)
        return super().execute(script, context, external_context)

    def evaluate(
        self,
        expression: str,
        context: dict[str, Any],
        external_context: Mapping[str, Any] | None = None,
    ) -> Any:
        self._guard(expression)
        return super().evaluate(expression, context, external_context)


def build_script_engine(sandbox_mode: str = "strict") -> PythonScriptEngine:
    """Construct a PythonScriptEngine for the given sandbox mode.

    - ``strict``   — AST pre-gate + whitelist globals (default, production).
    - ``relaxed``  — whitelist globals only (no AST gate); still guarded in prod.
    - ``disabled`` — stock TaskDataEnvironment (local dev ONLY).
    """
    mode = sandbox_mode.lower()
    if mode == "disabled":
        return PythonScriptEngine(environment=TaskDataEnvironment())
    if mode == "relaxed":
        return PythonScriptEngine(environment=SandboxedTaskDataEnvironment(ast_gate=False))
    return PythonScriptEngine(environment=SandboxedTaskDataEnvironment(ast_gate=True))


__all__ = [
    "SandboxedTaskDataEnvironment",
    "build_script_engine",
]
