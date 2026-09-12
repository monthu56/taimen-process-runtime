"""Capability manifest of the process runtime (superproject ADR-0023 §2).

"BPMN-compatible" without a manifest is not compatibility: the adapter states which
standard versions, which executable subset and which extension profile it implements,
so a definition outside the profile is refused before publish rather than at runtime.
"""

from __future__ import annotations

from typing import Any

MANIFEST: dict[str, Any] = {
    "adapter": {"name": "spiffworkflow", "version": "3.1", "runtime": "process-runtime/0.1"},
    "standards": {"bpmn": "2.0.2", "dmn": "1.5"},
    "executableProfile": {
        "activities": [
            "userTask",
            "manualTask",
            "serviceTask",
            "scriptTask",
            "businessRuleTask",
            "callActivity",
            "subProcess",
        ],
        "gateways": ["exclusive", "parallel", "inclusive", "eventBased"],
        "events": ["start", "end", "timer", "signal", "message", "boundary"],
        "scriptTask": {"language": "python", "sandbox": "strict"},
        "notExecutable": ["compensation", "transaction", "escalation", "conditional"],
    },
    "extensionProfile": {
        "namespace": "http://spiffworkflow.org/bpmn/schema/1.0/core",
        "properties": ["approval_policy"],
        "version": "1.0",
    },
    "operations": {
        "definitions": "GET /api/v1/definitions",
        "start": "POST /api/v1/instances",
        "completeActivity": "POST /api/v1/instances/{id}/tasks/{taskId}/complete",
        "signal": "POST /api/v1/instances/{id}/signal",
        "cancel": "POST /api/v1/instances/{id}/cancel",
        "migrate": "POST /api/v1/instances/{id}/migrate",
        "events": "GET /api/v1/events?cursor=",
    },
    "controlPlaneBinding": {
        "externalSystem": "process-runtime",
        "externalType": "activity",
        "customFieldPrefix": "process",
        "completion": "task.completed | task.updated(systemStatusCategory=terminal_success)",
        "release": "task.updated(systemStatusCategory=terminal_cancelled)",
    },
}

__all__ = ["MANIFEST"]
