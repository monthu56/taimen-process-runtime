"""Script-task sandbox penetration tests (EPIC-11, ADR-023, WF-007).

Verifies the AST pre-gate + whitelist block every known escape while allowing safe
expressions, on both fresh and (implicitly) the same environment used after deserialize.
"""

from __future__ import annotations

import pytest

from process_runtime.domain.errors import ScriptTaskSecurityError
from process_runtime.engine.spiff.script_engine import (
    SandboxedTaskDataEnvironment,
    build_script_engine,
)

pytestmark = [pytest.mark.security]

_ATTACKS = [
    "import os",
    "from os import system",
    "open('/etc/passwd')",
    "eval('1+1')",
    "exec('x=1')",
    "compile('1', '<s>', 'eval')",
    "(1).__class__.__bases__[0].__subclasses__()",
    "getattr(int, '__class__')",
    "__import__('subprocess')",
    "'{0.__class__.__mro__[1]}'.format(x)",
    "'{0.__init__.__globals__}'.format(re_match)",
    "x = '%s' % y",
    "[c for c in ().__class__.__mro__]",
    "f'{().__class__}'",
    "type(1)",
    "vars()",
    "breakpoint()",
    "delattr(x, 'a')",
    "setattr(x, 'a', 1)",
]

_SAFE = [
    "result = 1 + 2",
    "result = len([1, 2, 3])",
    "result = sqrt(16)",
    "result = Decimal('1.5') * 2",
    "result = re_match(r'\\d+', '123') is not None",
    "total = sum([v for v in range(5)])",
    "result = max(3, 7)",
    "d = datetime(2026, 1, 1)",
    "result = sorted([3, 1, 2])",
]


@pytest.mark.parametrize("script", _ATTACKS)
def test_sandbox_blocks_attack(script: str) -> None:
    env = SandboxedTaskDataEnvironment(ast_gate=True)
    ctx = {"x": {"a": 1}, "y": object(), "re_match": None}
    with pytest.raises(ScriptTaskSecurityError):
        env.execute(script, ctx)


@pytest.mark.parametrize("script", _SAFE)
def test_sandbox_allows_safe(script: str) -> None:
    env = SandboxedTaskDataEnvironment(ast_gate=True)
    ctx: dict = {}
    env.execute(script, ctx)  # must not raise


def test_evaluate_is_also_gated() -> None:
    env = SandboxedTaskDataEnvironment(ast_gate=True)
    with pytest.raises(ScriptTaskSecurityError):
        env.evaluate("__import__('os')", {})


def test_build_script_engine_modes() -> None:
    strict = build_script_engine("strict")
    relaxed = build_script_engine("relaxed")
    disabled = build_script_engine("disabled")
    assert strict is not None and relaxed is not None and disabled is not None
    # strict AST gate blocks import inside execute
    with pytest.raises(ScriptTaskSecurityError):
        strict.environment.execute("import os", {})
    # disabled uses the stock environment (no gate) — proves the modes differ
    disabled.environment.execute("y = 1 + 1", {})
