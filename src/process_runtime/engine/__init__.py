"""Concrete ``WorkflowEngineAdapter`` implementations: SpiffWorkflow (production) and the
deterministic ``DummyAdapter`` (tests, dev). The domain never imports this package."""

from __future__ import annotations

from process_runtime.engine.dummy import DummyAdapter

__all__ = ["DummyAdapter"]
