"""Which tasks of a conditional flow a partial run starts (``kbagent flow run``, #725).

Pure functions over a parsed ``keboola.flow`` configuration dict. No I/O, so
every selection rule is unit-tested without an HTTP client.

The Queue API runs a subset of a flow when the job body carries
``onlyFlowTaskIds``. The job-queue daemon then runs only the selected tasks,
does NOT evaluate transition conditions, chains the phases that hold a
selected task one after another (routing-only phases run empty), keeps going
after a failed task, and reports the status of the last phase as the flow
status. Unselected variable tasks do not run. That is why every partial run
carries the ``PARTIAL_RUN_IGNORES_CONDITIONS`` warning.

``--from-phase-id P`` expands to P plus the phases reached by following only
the DEFAULT transition of each phase: the single ``next[]`` entry without a
``condition`` key. It never means "as if everything succeeded": a success
condition can lead somewhere other than the default.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..errors import ErrorCode, KeboolaApiError

PARTIAL_RUN_IGNORES_CONDITIONS = "PARTIAL_RUN_IGNORES_CONDITIONS"
CONDITIONAL_PHASES_SKIPPED = "CONDITIONAL_PHASES_SKIPPED"
SELECTED_TASK_FAILED = "SELECTED_TASK_FAILED"

# A selected task whose result reports one of these statuses did its job.
_OK_TASK_STATUSES = frozenset({"success", "warning"})
MISSING_TASK_STATUS = "missing"


@dataclass(frozen=True)
class FlowWarning:
    """One warning of a flow run: a ``warnings[]`` entry on the CLI, a header entry over REST."""

    code: str
    message: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FlowRunSelection:
    """The tasks a flow run starts, and how they were chosen.

    ``phases`` is membership (the phases that hold a selected task, in
    configuration order), NOT the execution order of the daemon. For a whole
    flow (``partial`` False) ``task_ids`` are the enabled tasks that MAY run:
    the transition conditions still decide.
    """

    partial: bool
    task_ids: list[str]
    from_phase_id: str | None
    phases: list[dict[str, Any]]
    tasks: list[dict[str, Any]]
    skipped_disabled_task_ids: list[str]
    default_path_cycle: bool
    conditional_phase_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        """The ``selection`` block of the dry-run and real-run payloads."""
        return {
            "task_ids": list(self.task_ids),
            "from_phase_id": self.from_phase_id,
            "phases": [dict(phase) for phase in self.phases],
            "skipped_disabled_task_ids": list(self.skipped_disabled_task_ids),
            "default_path_cycle": self.default_path_cycle,
        }


@dataclass(frozen=True)
class TaskResults:
    """The outcome of every selected task of a finished partial run (D12)."""

    task_results: list[dict[str, Any]]
    failed_task_ids: list[str]
    missing_task_ids: list[str]


class FlowRunError(KeboolaApiError):
    """An API error of a flow run raised after its tasks were selected.

    Carries the run's warnings (``PARTIAL_RUN_IGNORES_CONDITIONS``, ...), so
    the CLI error envelope and the REST error body still list them (O3, V3).
    Code, message, ``retryable`` and ``details`` are those of the cause.
    """

    def __init__(
        self,
        cause: KeboolaApiError,
        warnings: list[FlowWarning],
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=cause.message,
            status_code=cause.status_code,
            error_code=cause.error_code,
            retryable=cause.retryable,
            details=details if details is not None else cause.details,
        )
        self.warnings = list(warnings)


@dataclass
class _DefaultPath:
    phase_ids: list[str]
    cycle: bool
    errors: list[str]


def _invalid(message: str) -> KeboolaApiError:
    return KeboolaApiError(message=message, status_code=0, error_code=ErrorCode.INVALID_ARGUMENT)


def _is_blank(value: Any) -> bool:
    return not isinstance(value, str) or not value.strip()


def check_selector_values(task_ids: list[str] | None, from_phase_id: str | None) -> None:
    """Refuse an empty, blank or comma-joined selector BEFORE anything normalizes it (D2, R1-R2).

    ``None`` means "no selector". An empty list or a blank entry is a caller
    error, never a whole-flow run. A comma in an id is refused (spec A4: one
    id per value) on every surface, so REST ``task_ids: ["t,1"]`` and
    ``--from-phase-id p,1`` fail like ``--task-id t,1``.
    """
    if task_ids is not None:
        if not task_ids:
            raise _invalid("task ids must not be an empty list; omit them to run the whole flow.")
        if any(_is_blank(task_id) for task_id in task_ids):
            raise _invalid("A task id must not be empty or whitespace.")
        with_comma = [task_id for task_id in task_ids if "," in task_id]
        if with_comma:
            raise _invalid(
                f"A task id must not contain a comma, got: {', '.join(map(repr, with_comma))}. "
                "Give one id per --task-id (one entry per id in task_ids)."
            )
    if from_phase_id is not None:
        if _is_blank(from_phase_id):
            raise _invalid("The from-phase id must not be empty or whitespace.")
        if "," in from_phase_id:
            raise _invalid(
                f"The from-phase id takes one phase id, got {from_phase_id!r} (it contains a comma)."
            )


def _phases_by_id(configuration: dict[str, Any]) -> dict[str, dict[str, Any]]:
    phases: dict[str, dict[str, Any]] = {}
    for phase in configuration.get("phases") or []:
        if isinstance(phase, dict) and phase.get("id") is not None:
            phases.setdefault(str(phase["id"]), phase)
    return phases


def _tasks(configuration: dict[str, Any]) -> list[dict[str, Any]]:
    """The tasks with an id, one per id: a malformed config with a repeated id keeps the first."""
    unique: dict[str, dict[str, Any]] = {}
    for task in configuration.get("tasks") or []:
        if isinstance(task, dict) and task.get("id") is not None:
            unique.setdefault(str(task["id"]), task)
    return list(unique.values())


def _transitions(phase: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in phase.get("next") or [] if isinstance(entry, dict)]


def _default_path(phases: dict[str, dict[str, Any]], start: str) -> _DefaultPath:
    """Follow the default transition (the one ``next[]`` entry without a ``condition`` key) (D4).

    ``condition: null`` and ``condition: {}`` are conditions, not defaults. A
    phase without transitions, or whose default goes to ``null``, ends the
    path; so does a phase with only conditional transitions. Two defaults, or
    a default to an unknown phase, is a malformed flow. A visited phase ends
    the path and marks the cycle.
    """
    path: list[str] = []
    seen: set[str] = set()
    current: str | None = start
    while current is not None:
        if current in seen:
            return _DefaultPath(path, cycle=True, errors=[])
        seen.add(current)
        path.append(current)
        defaults = [t for t in _transitions(phases[current]) if "condition" not in t]
        if len(defaults) > 1:
            return _DefaultPath(
                path,
                cycle=False,
                errors=[f"phase '{current}' has {len(defaults)} default transitions (malformed)"],
            )
        if not defaults or defaults[0].get("goto") is None:
            break
        target = str(defaults[0]["goto"])
        if target not in phases:
            return _DefaultPath(
                path,
                cycle=False,
                errors=[
                    (
                        f"the default transition of phase '{current}' goes to unknown phase "
                        f"'{target}' (malformed)"
                    )
                ],
            )
        current = target
    return _DefaultPath(path, cycle=False, errors=[])


def _conditional_reach(phases: dict[str, dict[str, Any]], path: list[str]) -> set[str]:
    """Phases reachable from the default path through a conditional transition (D5).

    Starts from the targets of conditional transitions out of path phases and
    then follows every transition, so a deeper branch is included. Cycle-safe.
    """
    stack = [
        str(t["goto"])
        for pid in path
        for t in _transitions(phases[pid])
        if "condition" in t and t.get("goto") is not None
    ]
    reached: set[str] = set()
    while stack:
        pid = stack.pop()
        if pid in reached or pid not in phases:
            continue
        reached.add(pid)
        stack.extend(str(t["goto"]) for t in _transitions(phases[pid]) if t.get("goto") is not None)
    return reached


def _task_view(task: dict[str, Any]) -> dict[str, Any]:
    inner = task.get("task")
    if not isinstance(inner, dict):
        inner = {}
    return {
        "id": str(task["id"]),
        "name": task.get("name"),
        "phase_id": None if task.get("phase") is None else str(task.get("phase")),
        "type": inner.get("type"),
    }


def _is_enabled(task: dict[str, Any]) -> bool:
    return task.get("enabled", True) is not False


def _phase_blocks(
    phases: dict[str, dict[str, Any]], selected: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_phase: dict[str, list[str]] = {}
    for view in selected:
        by_phase.setdefault(str(view["phase_id"]), []).append(view["id"])
    return [
        {"id": pid, "name": phase.get("name"), "task_ids": by_phase[pid]}
        for pid, phase in phases.items()
        if pid in by_phase
    ]


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def select_flow_tasks(
    configuration: dict[str, Any],
    task_ids: list[str] | None = None,
    from_phase_id: str | None = None,
) -> FlowRunSelection:
    """Resolve the tasks of one flow run from one read of its configuration (D3-D6).

    ``task_ids`` and ``from_phase_id`` both ``None`` is the whole flow. Any
    selector makes a partial run: the union of the given tasks and the
    enabled tasks of the default path from ``from_phase_id``, de-duplicated,
    in configuration task order.

    Raises:
        KeboolaApiError: ``INVALID_ARGUMENT`` listing every offender at once:
            unknown task ids, an unknown phase, a disabled task given by id,
            a malformed default transition, or an empty final selection.
    """
    check_selector_values(task_ids, from_phase_id)
    phases = _phases_by_id(configuration)
    tasks = _tasks(configuration)
    if task_ids is None and from_phase_id is None:
        enabled = [_task_view(t) for t in tasks if _is_enabled(t)]
        return FlowRunSelection(
            partial=False,
            task_ids=[view["id"] for view in enabled],
            from_phase_id=None,
            phases=_phase_blocks(phases, enabled),
            tasks=enabled,
            skipped_disabled_task_ids=[str(t["id"]) for t in tasks if not _is_enabled(t)],
            default_path_cycle=False,
            conditional_phase_ids=[],
        )

    by_id = {str(t["id"]): t for t in tasks}
    requested = _dedupe(task_id.strip() for task_id in task_ids or [])
    errors: list[str] = []
    unknown = [tid for tid in requested if tid not in by_id]
    if unknown:
        errors.append(f"unknown task id(s): {', '.join(unknown)}")
    disabled = [tid for tid in requested if tid in by_id and not _is_enabled(by_id[tid])]
    if disabled:
        errors.append(f"disabled task(s) cannot be selected: {', '.join(disabled)}")

    start = from_phase_id.strip() if from_phase_id is not None else None
    path = _DefaultPath([], cycle=False, errors=[])
    if start is not None:
        if start in phases:
            path = _default_path(phases, start)
            errors.extend(path.errors)
        else:
            errors.append(f"unknown phase id: {start}")

    on_path = set(path.phase_ids)
    wanted = set(requested)
    chosen = [t for t in tasks if str(t["id"]) in wanted or str(t.get("phase")) in on_path]
    selected = [_task_view(t) for t in chosen if _is_enabled(t)]
    skipped = [str(t["id"]) for t in chosen if not _is_enabled(t) and str(t["id"]) not in wanted]
    orphans = [view["id"] for view in selected if view["phase_id"] not in phases]
    if orphans:
        errors.append(f"task(s) in a phase that the flow does not define: {', '.join(orphans)}")
    if not errors and not selected:
        errors.append("the selection is empty (every task it names is disabled or missing)")
    if errors:
        raise _invalid("Cannot select the tasks of this flow run: " + "; ".join(errors) + ".")

    selected_phases = {view["phase_id"] for view in selected}
    reach = _conditional_reach(phases, path.phase_ids) if start is not None else set()
    # Only phases with an enabled task: for an empty routing phase or a
    # disabled-only phase, "add its tasks with --task-id" is no advice (F6).
    actionable = {str(t.get("phase")) for t in tasks if _is_enabled(t)}
    conditional = [
        pid
        for pid in phases
        if pid in reach and pid not in on_path and pid not in selected_phases and pid in actionable
    ]
    return FlowRunSelection(
        partial=True,
        task_ids=[view["id"] for view in selected],
        from_phase_id=start,
        phases=_phase_blocks(phases, selected),
        tasks=selected,
        skipped_disabled_task_ids=skipped,
        default_path_cycle=path.cycle,
        conditional_phase_ids=conditional,
    )


def selection_warnings(selection: FlowRunSelection) -> list[FlowWarning]:
    """The warnings every partial run carries, dry run and real run alike (D5, D7)."""
    if not selection.partial:
        return []
    found = [
        FlowWarning(
            code=PARTIAL_RUN_IGNORES_CONDITIONS,
            message=(
                "A partial flow run does not evaluate transition conditions: every selected "
                "task runs, phase after phase, also after a task failed; routing-only phases "
                "may run empty; the flow status is the status of the last phase and can hide "
                "a failed task; unselected variable tasks do not run."
            ),
        )
    ]
    if selection.conditional_phase_ids:
        names = ", ".join(selection.conditional_phase_ids)
        found.append(
            FlowWarning(
                code=CONDITIONAL_PHASES_SKIPPED,
                message=(
                    f"Phase(s) not on the default path from {selection.from_phase_id}, "
                    f"reachable from it only through a conditional transition, are not "
                    f"selected: {names}. Add their tasks with --task-id to run them."
                ),
                extra={"phase_ids": list(selection.conditional_phase_ids)},
            )
        )
    return found


def summarize_task_results(job: dict[str, Any], selection: FlowRunSelection) -> TaskResults:
    """Every selected task's status from a finished flow job's ``result.tasks[]`` (D12).

    A selected task absent from the result has status ``missing`` and counts
    as failed: the run gives no evidence that it ran.
    """
    result = job.get("result")
    items = result.get("tasks") if isinstance(result, dict) else None
    reported: dict[str, dict[str, Any]] = {}
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("id") is not None:
            reported.setdefault(str(item["id"]), item)
    phase_of = {view["id"]: view["phase_id"] for view in selection.tasks}
    task_results: list[dict[str, Any]] = []
    for task_id in selection.task_ids:
        item = reported.get(task_id)
        if item is None:
            task_results.append(
                {
                    "task_id": task_id,
                    "phase_id": phase_of.get(task_id),
                    "status": MISSING_TASK_STATUS,
                    "job_id": None,
                }
            )
            continue
        phase = item.get("phase")
        task_results.append(
            {
                "task_id": task_id,
                "phase_id": str(phase) if phase is not None else phase_of.get(task_id),
                "status": item.get("status"),
                "job_id": item.get("jobId"),
            }
        )
    failed = [r["task_id"] for r in task_results if r["status"] not in _OK_TASK_STATUSES]
    missing = [r["task_id"] for r in task_results if r["status"] == MISSING_TASK_STATUS]
    return TaskResults(task_results=task_results, failed_task_ids=failed, missing_task_ids=missing)


def task_failure_warning(results: TaskResults) -> FlowWarning:
    """``SELECTED_TASK_FAILED``: which selected tasks failed, and which left no result."""
    observed = [tid for tid in results.failed_task_ids if tid not in results.missing_task_ids]
    parts = []
    if observed:
        parts.append(f"failed: {', '.join(observed)}")
    if results.missing_task_ids:
        parts.append(
            f"no result in the flow job (did not run): {', '.join(results.missing_task_ids)}"
        )
    return FlowWarning(
        code=SELECTED_TASK_FAILED,
        message="Selected task(s) did not succeed -- " + "; ".join(parts) + ".",
        extra={
            "failed_task_ids": list(results.failed_task_ids),
            "missing_task_ids": list(results.missing_task_ids),
        },
    )
