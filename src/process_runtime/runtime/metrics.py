"""OpenTelemetry metrics for the platform workflow engine (EPIC-11, ADR-023, WF-019).

Module-level meter + instruments, mirroring ``platform_common.events.metrics``. Counters
and histograms are recorded at their event points; the timer backlog is an observable
gauge fed by module-global state via a setter.
"""

from __future__ import annotations

from collections.abc import Iterable

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

_meter = metrics.get_meter("process_runtime.runtime", "0.1.0")

workflow_advance_total = _meter.create_counter(
    "workflow_advance_total",
    description="Workflow advance attempts by workflow_id/trigger/result.",
)
workflow_advance_duration_seconds = _meter.create_histogram(
    "workflow_advance_duration_seconds",
    unit="s",
    description="Wall-clock duration of a workflow advance.",
)
workflow_transition_total = _meter.create_counter(
    "workflow_transition_total",
    description="Recorded workflow transitions by workflow_id.",
)
workflow_human_task_created_total = _meter.create_counter(
    "workflow_human_task_created_total",
    description="Human tasks created by workflow_id.",
)
workflow_sla_breach_total = _meter.create_counter(
    "workflow_sla_breach_total",
    description="SLA timer breaches by workflow_id/task_id.",
)
workflow_timer_fired_total = _meter.create_counter(
    "workflow_timer_fired_total",
    description="Timers fired by workflow_id/timer_type.",
)
workflow_script_blocked_total = _meter.create_counter(
    "workflow_script_blocked_total",
    description="Script-task sandbox violations by workflow_id (security signal).",
)
workflow_advance_dedup_skipped_total = _meter.create_counter(
    "workflow_advance_dedup_skipped_total",
    description="Advance calls skipped as idempotent duplicates.",
)

_timer_backlog: int = 0


def set_timer_backlog(value: int) -> None:
    """Set the current due-timer backlog gauge (called from the timer tick)."""
    global _timer_backlog
    _timer_backlog = int(value)


def _observe_timer_backlog(_options: CallbackOptions) -> Iterable[Observation]:
    yield Observation(_timer_backlog)


workflow_timer_backlog = _meter.create_observable_gauge(
    "workflow_timer_backlog_total",
    callbacks=[_observe_timer_backlog],
    description="Due timers observed in the last tick that exceeded the batch limit.",
)


def record_advance(workflow_id: str, trigger: str, result: str, duration_s: float) -> None:
    workflow_advance_total.add(
        1, {"workflow_id": workflow_id, "trigger": trigger, "result": result}
    )
    workflow_advance_duration_seconds.record(duration_s, {"workflow_id": workflow_id})


def record_script_blocked(workflow_id: str) -> None:
    workflow_script_blocked_total.add(1, {"workflow_id": workflow_id})


def record_timer_fired(workflow_id: str, timer_type: str) -> None:
    workflow_timer_fired_total.add(1, {"workflow_id": workflow_id, "timer_type": timer_type})


__all__ = [
    "record_advance",
    "record_script_blocked",
    "record_timer_fired",
    "set_timer_backlog",
    "workflow_advance_dedup_skipped_total",
    "workflow_advance_duration_seconds",
    "workflow_advance_total",
    "workflow_human_task_created_total",
    "workflow_script_blocked_total",
    "workflow_sla_breach_total",
    "workflow_timer_backlog",
    "workflow_timer_fired_total",
    "workflow_transition_total",
]
