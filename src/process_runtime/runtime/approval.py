"""ApprovalEngineHook default + test implementations (EPIC-11, ADR-023, WF-017).

The real approval runtime ships in EPIC-12 (ADR-024). Here we provide the fail-fast
default (``NoopApprovalEngineHook``) and a test stub (``AlwaysApprovedHook``). The Noop
hook refuses any policy-bearing UserTask so a misconfiguration surfaces immediately rather
than silently ignoring the approval gate.
"""

from __future__ import annotations

from uuid import uuid4

from process_runtime.domain.enums import ApprovalDecisionStatus
from process_runtime.domain.errors import WorkflowApprovalHookNotConfigured
from process_runtime.domain.value_objects import (
    ApprovalDecision,
    ApprovalRequestContext,
    ApprovalRequestRef,
)


class NoopApprovalEngineHook:
    """Default hook: fail-fast when a UserTask actually carries an approval policy."""

    async def start_approval(self, context: ApprovalRequestContext) -> ApprovalRequestRef:
        raise WorkflowApprovalHookNotConfigured(
            f"task '{context.task_id}' requires approval policy '{context.policy_id}' "
            "but no ApprovalEngineHook is configured (EPIC-12 supplies the real one)"
        )

    async def is_approval_complete(self, ref: ApprovalRequestRef) -> ApprovalDecision | None:
        return None


class AlwaysApprovedHook:
    """Test stub: immediately approves any request. Contract/dev use only."""

    async def start_approval(self, context: ApprovalRequestContext) -> ApprovalRequestRef:
        return ApprovalRequestRef(value=str(uuid4()))

    async def is_approval_complete(self, ref: ApprovalRequestRef) -> ApprovalDecision | None:
        return ApprovalDecision(status=ApprovalDecisionStatus.approved)


__all__ = ["AlwaysApprovedHook", "NoopApprovalEngineHook"]
