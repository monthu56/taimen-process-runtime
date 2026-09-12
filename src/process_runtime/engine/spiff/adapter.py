"""SpiffWorkflow engine adapter (EPIC-11, ADR-023, WF-005/006).

Implements ``WorkflowEngineAdapter`` on SpiffWorkflow 3.1.2. Serialization invariant
(design amendment A1): instance state is produced ONLY via ``serialize_json`` (never
``to_dict``) and read ONLY via ``deserialize_json`` — the ``serializer_version`` key must
survive the JSONB round-trip. The custom sandbox script engine is re-installed as the
FIRST action after every deserialize, before any ``run``/``do_engine_steps`` (amendment
A2/A5), because a deserialized workflow otherwise carries the stock, non-sandboxed engine.

The adapter NEVER raises domain/engine errors outward: any failure yields
``AdvanceResult(instance_status=failed, error_payload=..., emitted_events=(..., '.failed'))``
so the runtime has a single persist path (amendment A5). Only genuine programming errors
propagate.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from SpiffWorkflow.bpmn.serializer.workflow import BpmnWorkflowSerializer
from SpiffWorkflow.bpmn.util.event import BpmnEvent
from SpiffWorkflow.bpmn.workflow import BpmnWorkflow
from SpiffWorkflow.spiff.parser.process import SpiffBpmnParser
from SpiffWorkflow.spiff.serializer.config import SPIFF_CONFIG
from SpiffWorkflow.spiff.specs.event_definitions import (
    MessageEventDefinition,
    SignalEventDefinition,
)
from SpiffWorkflow.util.task import TaskState

from process_runtime.domain.enums import (
    BpmnTaskType,
    WorkflowInstanceStatus,
    WorkflowTimerType,
    WorkflowTransitionTrigger,
)
from process_runtime.domain.errors import WorkflowDefinitionInvalid
from process_runtime.domain.value_objects import (
    AdvanceResult,
    BpmnTaskRef,
    MigrationMap,
    NewHumanTask,
    NewTimer,
    ParsedDefinition,
    TransitionRecord,
)
from process_runtime.engine.spiff.script_engine import build_script_engine

_WF_EVENT_PREFIX = "platform.workflow"


def _serializer() -> BpmnWorkflowSerializer:
    return BpmnWorkflowSerializer(BpmnWorkflowSerializer.configure(SPIFF_CONFIG))


# BPMN activity classes we treat as tasks. Everything else (events, gateways,
# sequence flows) is NOT a task and is skipped during classification.
_TASK_TYPE_BY_CLASS: dict[str, BpmnTaskType] = {
    "UserTask": BpmnTaskType.user,
    "ManualTask": BpmnTaskType.manual,
    "NoneTask": BpmnTaskType.manual,
    "ScriptTask": BpmnTaskType.script,
    "ServiceTask": BpmnTaskType.service,
    "BusinessRuleTask": BpmnTaskType.business_rule,
    "CallActivity": BpmnTaskType.call_activity,
    "SubWorkflowTask": BpmnTaskType.sub_process,
    "TransactionSubprocess": BpmnTaskType.sub_process,
}


def _task_type(task_spec: Any) -> BpmnTaskType | None:
    """Return the task type for a genuine BPMN task, or None for events/gateways/flows."""
    return _TASK_TYPE_BY_CLASS.get(type(task_spec).__name__)


def _timer_type(event_definition: Any) -> WorkflowTimerType:
    name = type(event_definition).__name__
    if "Date" in name:
        return WorkflowTimerType.due_date
    if "Cycle" in name:
        return WorkflowTimerType.cycle
    return WorkflowTimerType.duration


class SpiffWorkflowAdapter:
    """WorkflowEngineAdapter backed by SpiffWorkflow 3.1.2."""

    def __init__(self, *, sandbox_mode: str = "strict") -> None:
        self._sandbox_mode = sandbox_mode

    # ------------------------------------------------------------------ parse
    def parse(
        self,
        *,
        bpmn_xml: bytes,
        dmn_xmls: Mapping[str, bytes],
        workflow_id: str,
        version: int,
        process_id: str | None = None,
    ) -> ParsedDefinition:
        parser = SpiffBpmnParser()
        try:
            parser.add_bpmn_str(bpmn_xml)
            for _filename, dmn in dmn_xmls.items():
                parser.add_dmn_str(dmn)
            resolved_pid = process_id or self._resolve_process_id(parser)
            spec = parser.get_spec(resolved_pid)
            subprocess_specs = parser.get_subprocess_specs(resolved_pid)
        except Exception as exc:
            raise WorkflowDefinitionInvalid(
                f"failed to parse BPMN for workflow '{workflow_id}' v{version}: {exc}"
            ) from exc

        user_tasks: list[BpmnTaskRef] = []
        service_tasks: list[BpmnTaskRef] = []
        script_tasks: list[BpmnTaskRef] = []
        rule_tasks: list[BpmnTaskRef] = []
        extensions: dict[str, Mapping[str, str]] = {}
        for name, task_spec in spec.task_specs.items():
            ttype = _task_type(task_spec)
            if ttype is None:
                continue  # events, gateways, sequence flows are not tasks
            ref = BpmnTaskRef(
                task_id=getattr(task_spec, "bpmn_id", None) or name,
                name=getattr(task_spec, "bpmn_name", None) or name,
                type=ttype,
                lane=getattr(task_spec, "lane", None),
            )
            ext = getattr(task_spec, "extensions", None)
            if ext:
                props = ext.get("properties") if isinstance(ext, dict) else None
                if isinstance(props, dict):
                    extensions[ref.task_id] = dict(props)
            if ttype is BpmnTaskType.user:
                user_tasks.append(ref)
            elif ttype is BpmnTaskType.script:
                script_tasks.append(ref)
            elif ttype is BpmnTaskType.business_rule:
                rule_tasks.append(ref)
            elif ttype is BpmnTaskType.service:
                service_tasks.append(ref)

        return ParsedDefinition(
            workflow_id=workflow_id,
            version=version,
            process_id=resolved_pid,
            user_tasks=tuple(user_tasks),
            service_tasks=tuple(service_tasks),
            script_tasks=tuple(script_tasks),
            business_rule_tasks=tuple(rule_tasks),
            task_extensions=extensions,
            opaque={"spec": spec, "subprocess_specs": subprocess_specs},
        )

    @staticmethod
    def _resolve_process_id(parser: SpiffBpmnParser) -> str:
        ids = parser.get_process_ids()
        if not ids:
            raise WorkflowDefinitionInvalid("no executable process found in BPMN")
        return ids[0]

    # ------------------------------------------------------- build_initial_state
    def build_initial_state(
        self, parsed: ParsedDefinition, variables: Mapping[str, Any]
    ) -> AdvanceResult:
        wf = self._new_workflow(parsed)
        try:
            start = wf.get_next_task(state=TaskState.READY)
            if start is not None and variables:
                start.set_data(**dict(variables))
            wf.do_engine_steps()
            wf.refresh_waiting_tasks()
        except Exception as exc:
            return self._failed_result(wf, parsed, exc, first=True)
        result = self._collect(wf, parsed, completed_ids=(), first=True)
        return result

    # -------------------------------------------------------------- advance
    def advance(
        self,
        *,
        state: bytes,
        trigger: WorkflowTransitionTrigger,
        payload: Mapping[str, Any],
        parsed: ParsedDefinition,
        now: datetime,
    ) -> AdvanceResult:
        wf = self._load(state, parsed)
        completed_ids: tuple[str, ...] = ()
        try:
            if trigger is WorkflowTransitionTrigger.task_complete:
                completed_ids = self._complete_task(wf, payload)
            elif trigger in (
                WorkflowTransitionTrigger.signal,
                WorkflowTransitionTrigger.message,
            ):
                self._dispatch_event(wf, trigger, payload)
            elif trigger is WorkflowTransitionTrigger.timer:
                wf.refresh_waiting_tasks()
            wf.do_engine_steps()
            wf.refresh_waiting_tasks()
            wf.do_engine_steps()
        except Exception as exc:
            return self._failed_result(wf, parsed, exc, first=False)
        return self._collect(wf, parsed, completed_ids=completed_ids, first=False)

    # -------------------------------------------------------------- migrate
    def migrate(
        self,
        *,
        state: bytes,
        parsed_from: ParsedDefinition,
        parsed_to: ParsedDefinition,
        migration: MigrationMap,
    ) -> bytes:
        # Manipulate via to_dict/from_dict (deserialize_json/get_version break on a bare
        # dict in 3.1.2). Final state is produced with serialize_json (amendment A3).
        serializer = _serializer()
        state_dict = json.loads(state.decode("utf-8"))
        template = self._new_workflow(parsed_to)
        template_dict = serializer.to_dict(template)
        # Swap spec/subprocess_specs to the new version, keep running task tree.
        state_dict["spec"] = template_dict["spec"]
        state_dict["subprocess_specs"] = template_dict["subprocess_specs"]
        # Rename task_spec references per mapping.
        mapping = dict(migration.task_id_mapping)
        for task in state_dict.get("tasks", {}).values():
            spec_name = task.get("task_spec")
            if spec_name in mapping and mapping[spec_name] != "__skip__":
                task["task_spec"] = mapping[spec_name]
        wf = serializer.from_dict(state_dict)
        self._install_engine(wf)
        return serializer.serialize_json(wf).encode("utf-8")

    # --------------------------------------------------------- active_tasks
    def active_tasks(self, *, state: bytes, parsed: ParsedDefinition) -> tuple[BpmnTaskRef, ...]:
        wf = self._load(state, parsed)
        return self._active_human_refs(wf)

    # ------------------------------------------------------------ internals
    def _new_workflow(self, parsed: ParsedDefinition) -> BpmnWorkflow:
        opaque = parsed.opaque or {}
        wf = BpmnWorkflow(opaque["spec"], opaque.get("subprocess_specs", {}))
        self._install_engine(wf)
        return wf

    def _load(self, state: bytes, parsed: ParsedDefinition) -> BpmnWorkflow:
        wf = _serializer().deserialize_json(state.decode("utf-8"))
        # Re-install the sandbox engine FIRST — the deserialized wf carries the stock,
        # non-sandboxed PythonScriptEngine (amendment A2/A5).
        self._install_engine(wf)
        return wf

    def _install_engine(self, wf: BpmnWorkflow) -> None:
        wf.script_engine = build_script_engine(self._sandbox_mode)

    def _complete_task(self, wf: BpmnWorkflow, payload: Mapping[str, Any]) -> tuple[str, ...]:
        task_id = payload.get("task_id")
        form = dict(payload.get("form_payload", {}))
        ready = wf.get_tasks(state=TaskState.READY, manual=True)
        target = next(
            (t for t in ready if getattr(t.task_spec, "bpmn_id", t.task_spec.name) == task_id),
            None,
        )
        if target is None:
            from process_runtime.domain.errors import WorkflowTaskNotClaimable

            raise WorkflowTaskNotClaimable(f"task '{task_id}' is not ready for completion")
        if form:
            target.set_data(**form)
        target.run()
        return (str(task_id),)

    def _dispatch_event(
        self, wf: BpmnWorkflow, trigger: WorkflowTransitionTrigger, payload: Mapping[str, Any]
    ) -> None:
        name = payload.get("signal_name") or payload.get("message_name") or payload.get("name")
        data = payload.get("event_payload") or payload.get("payload") or {}
        if trigger is WorkflowTransitionTrigger.signal:
            event_def = SignalEventDefinition(name)
        else:
            event_def = MessageEventDefinition(name)
        wf.catch(BpmnEvent(event_def, payload=data))

    # ---------------------------------------------------------- collection
    def _active_human_refs(self, wf: BpmnWorkflow) -> tuple[BpmnTaskRef, ...]:
        refs: list[BpmnTaskRef] = []
        for task in wf.get_tasks(state=TaskState.READY, manual=True):
            spec = task.task_spec
            refs.append(
                BpmnTaskRef(
                    task_id=getattr(spec, "bpmn_id", None) or spec.name,
                    name=getattr(spec, "bpmn_name", None) or spec.name,
                    type=_task_type(spec) or BpmnTaskType.user,
                    lane=getattr(spec, "lane", None),
                )
            )
        return tuple(refs)

    def _collect_timers(self, wf: BpmnWorkflow) -> tuple[NewTimer, ...]:
        timers: list[NewTimer] = []
        for task in wf.get_tasks(state=TaskState.WAITING):
            event_def = getattr(task.task_spec, "event_definition", None)
            if event_def is None:
                continue
            value = task.internal_data.get("event_value")
            if not value:
                continue
            try:
                fires_at = datetime.fromisoformat(str(value))
            except (TypeError, ValueError):
                continue
            timers.append(
                NewTimer(
                    task_id=getattr(task.task_spec, "bpmn_id", None) or task.task_spec.name,
                    fires_at=fires_at,
                    timer_type=_timer_type(event_def),
                )
            )
        return tuple(timers)

    def _collect(
        self,
        wf: BpmnWorkflow,
        parsed: ParsedDefinition,
        *,
        completed_ids: tuple[str, ...],
        first: bool,
    ) -> AdvanceResult:
        errored = wf.get_tasks(state=TaskState.ERROR)
        if errored:
            return self._failed_result(
                wf, parsed, RuntimeError("workflow task entered ERROR state"), first=first
            )

        active_refs = self._active_human_refs(wf)
        new_tasks = tuple(
            NewHumanTask(
                task_id=ref.task_id,
                name=ref.name,
                lane=ref.lane,
                approval_policy_id=(parsed.task_extensions.get(ref.task_id, {}) or {}).get(
                    "approval_policy"
                ),
            )
            for ref in active_refs
        )
        timers = self._collect_timers(wf)
        variables = dict(wf.data)

        if wf.is_completed():
            status = WorkflowInstanceStatus.completed
            outcome = self._derive_outcome(variables)
        elif active_refs or timers:
            status = WorkflowInstanceStatus.waiting
            outcome = None
        else:
            status = WorkflowInstanceStatus.active
            outcome = None

        events: list[str] = []
        if first:
            events.append(f"{_WF_EVENT_PREFIX}.started")
        for _ in completed_ids:
            events.append(f"{_WF_EVENT_PREFIX}.task_completed")
        for _ in new_tasks:
            events.append(f"{_WF_EVENT_PREFIX}.task_created")
        events.append(f"{_WF_EVENT_PREFIX}.transitioned")
        if status is WorkflowInstanceStatus.completed:
            events.append(f"{_WF_EVENT_PREFIX}.completed")

        transitions: list[TransitionRecord] = []
        for cid in completed_ids:
            transitions.append(
                TransitionRecord(from_task_id=cid, to_task_id=None, event_type="task_completed")
            )
        for ref in active_refs:
            transitions.append(
                TransitionRecord(
                    from_task_id=None, to_task_id=ref.task_id, event_type="task_created"
                )
            )
        if first and not transitions:
            transitions.append(
                TransitionRecord(from_task_id=None, to_task_id=None, event_type="started")
            )

        return AdvanceResult(
            new_state=_serializer().serialize_json(wf).encode("utf-8"),
            instance_status=status,
            variables=variables,
            new_tasks=new_tasks,
            completed_task_ids=completed_ids,
            new_timers=timers,
            transitions=tuple(transitions),
            emitted_events=tuple(events),
            active_tokens=active_refs,
            outcome=outcome,
        )

    @staticmethod
    def _derive_outcome(variables: Mapping[str, Any]) -> str:
        for key in ("outcome", "final_decision", "decision", "initial_decision"):
            value = variables.get(key)
            if value:
                return str(value)
        return "completed"

    def _failed_result(
        self, wf: BpmnWorkflow, parsed: ParsedDefinition, exc: Exception, *, first: bool
    ) -> AdvanceResult:
        try:
            state_bytes = _serializer().serialize_json(wf).encode("utf-8")
        except Exception:
            state_bytes = b"{}"
        error_payload = {
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        return AdvanceResult(
            new_state=state_bytes,
            instance_status=WorkflowInstanceStatus.failed,
            variables=dict(getattr(wf, "data", {}) or {}),
            emitted_events=(f"{_WF_EVENT_PREFIX}.failed",),
            error_payload=error_payload,
        )


__all__ = ["SpiffWorkflowAdapter"]
