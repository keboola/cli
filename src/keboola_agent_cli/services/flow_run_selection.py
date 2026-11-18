"""Which tasks of a conditional flow a partial run starts (``kbagent flow run``, #725).

Pure functions over a parsed ``keboola.flow`` configuration dict. No I/O, so
every selection rule is unit-tested without an HTTP client.

The Queue API runs a subset of a flow when the job body carries
``onlyFlowTaskIds``. The job-queue daemon then runs only the selected tasks,
does NOT evaluate transition conditions, chains the phases that hold a
selected task one after another (routing-only phases run empty), keeps going
after a failed task, and reports the status of the last phase as the flow
status. Unselected tasks of every type (job, variable, notification) do not
run. That is why every partial run carries the ``PARTIAL_RUN_IGNORES_CONDITIONS``
warning.

A flow or a selection the daemon refuses is refused here first, before the
POST (``_structure_errors``, ``_run_order_errors``): otherwise a dry run
reports success and the real job fails after it was created.

``--from-phase-id P`` expands to P plus the phases reached by following only
the DEFAULT transition of each phase: the single ``next[]`` entry without a
``condition`` key. It never means "as if everything succeeded": a success
condition can lead somewhere other than the default.
"""

from __future__ import annotations

from collections import Counter
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


def _id(value: Any) -> str:
    """An id as the job-queue daemon compares it: PHP's string cast.

    ``1`` and ``"1"`` are one id, and so is ``1.0`` (PHP casts an integral
    float to ``"1"``, Python to ``"1.0"``); ``True`` is ``"1"``.
    """
    if isinstance(value, bool):
        return "1" if value else ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _phases_by_id(configuration: dict[str, Any]) -> dict[str, dict[str, Any]]:
    phases: dict[str, dict[str, Any]] = {}
    for phase in configuration.get("phases") or []:
        if isinstance(phase, dict) and phase.get("id") is not None:
            phases.setdefault(_id(phase["id"]), phase)
    return phases


def _tasks(configuration: dict[str, Any]) -> list[dict[str, Any]]:
    """The tasks with an id (a repeated id is refused first, by ``_structure_errors``)."""
    return [
        task
        for task in configuration.get("tasks") or []
        if isinstance(task, dict) and task.get("id") is not None
    ]


# ---------------------------------------------------------------------------
# What the job-queue daemon refuses. Ported from keboola/job-queue-daemon:
# FlowContainerTypeHandler::checkPhaseAndTaskIds and Flow::getFirstPhaseId for
# every flow run, SelectedTasksFlowRewriter for a run with onlyFlowTaskIds.
# Refusing the same flows here, before the POST, means a dry run never reports
# a run the daemon fails after the job was created.
# ---------------------------------------------------------------------------


def _phase_list(configuration: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        phase
        for phase in configuration.get("phases") or []
        if isinstance(phase, dict) and phase.get("id") is not None
    ]


def _edges(configuration: dict[str, Any]) -> dict[str, list[str]]:
    """Phase id -> its distinct ``goto`` targets in ``next[]`` order, conditional ones too."""
    edges: dict[str, list[str]] = {}
    for phase in _phase_list(configuration):
        targets = [_id(t["goto"]) for t in _transitions(phase) if t.get("goto") is not None]
        edges[_id(phase["id"])] = _dedupe(targets)
    return edges


def _repeated(values: list[str]) -> list[str]:
    counts = Counter(values)
    return [value for value in _dedupe(values) if counts[value] > 1]


def _structure_errors(configuration: dict[str, Any]) -> tuple[list[str], str | None]:
    """The reasons the daemon refuses ANY run of this flow, and its first phase.

    Repeated phase or task ids, a ``goto`` to a phase the flow does not
    define, and not exactly one phase that no transition goes to (the
    daemon's "Flow must have exactly one first step").
    """
    errors: list[str] = []
    phase_ids = [_id(phase["id"]) for phase in _phase_list(configuration)]
    task_ids = [_id(task["id"]) for task in _tasks(configuration)]
    if repeated := _repeated(phase_ids):
        errors.append(f"repeated phase id(s): {', '.join(repeated)}")
    if repeated := _repeated(task_ids):
        errors.append(f"repeated task id(s): {', '.join(repeated)}")
    edges = _edges(configuration)
    unknown = _dedupe(t for targets in edges.values() for t in targets if t not in edges)
    if unknown:
        errors.append(
            f"transition(s) go to phase(s) the flow does not define: {', '.join(unknown)}"
        )
    incoming = {target for targets in edges.values() for target in targets}
    roots = [pid for pid in edges if pid not in incoming]
    if len(roots) != 1:
        found = ", ".join(roots) if roots else "none"
        errors.append(
            "the flow must have exactly one first phase (a phase no transition goes to); "
            f"found: {found}"
        )
    return errors, roots[0] if len(roots) == 1 else None


def _reachable(start: str, edges: dict[str, list[str]]) -> list[str]:
    """Phase ids reachable from ``start`` over every transition, ``start`` included (BFS)."""
    visited = [start]
    queue = [start]
    while queue:
        for target in edges.get(queue.pop(0), []):
            if target not in visited:
                visited.append(target)
                queue.append(target)
    return visited


def _shortest_path(start: str, goal: str, edges: dict[str, list[str]]) -> list[str]:
    """The phases after ``start`` on the shortest route to ``goal``; config order breaks ties."""
    if start == goal:
        return []
    previous: dict[str, str] = {}
    queue = [start]
    while queue and goal not in previous:
        current = queue.pop(0)
        for target in edges.get(current, []):
            if target != start and target not in previous:
                previous[target] = current
                queue.append(target)
    path: list[str] = []
    phase_id = goal
    while phase_id != start:
        path.insert(0, phase_id)
        phase_id = previous[phase_id]
    return path


def _run_order_errors(
    edges: dict[str, list[str]], root: str, selected_phase_ids: list[str]
) -> list[str]:
    """The reasons the daemon refuses this selection (SelectedTasksFlowRewriter).

    A phase with a selected task that the first phase cannot reach; or kept
    phases (the first phase plus the shortest route to each selected phase)
    that cannot be put in one order because they reach each other (a loop).
    """
    reachable = set(_reachable(root, edges))
    unreachable = [pid for pid in selected_phase_ids if pid not in reachable]
    if unreachable:
        names = ", ".join(unreachable)
        return [
            f"phase(s) with a selected task cannot be reached from the first phase '{root}': {names}"
        ]
    kept = [root]
    for pid in selected_phase_ids:
        kept.extend(step for step in _shortest_path(root, pid, edges) if step not in kept)
    reach = {pid: set(_reachable(pid, edges)) for pid in kept}
    pending = [pid for pid in edges if pid != root and pid in kept]
    while pending:
        ready = next(
            (
                pid
                for pid in pending
                if not any(other != pid and pid in reach[other] for other in pending)
            ),
            None,
        )
        if ready is None:
            loop = ", ".join(pending)
            return [f"the phases needed to reach the selected tasks form a loop ({loop})"]
        pending.remove(ready)
    return []


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
        target = _id(defaults[0]["goto"])
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
        _id(t["goto"])
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
        stack.extend(_id(t["goto"]) for t in _transitions(phases[pid]) if t.get("goto") is not None)
    return reached


def _task_view(task: dict[str, Any]) -> dict[str, Any]:
    inner = task.get("task")
    if not isinstance(inner, dict):
        inner = {}
    return {
        "id": _id(task["id"]),
        "name": task.get("name"),
        "phase_id": None if task.get("phase") is None else _id(task.get("phase")),
        "type": inner.get("type"),
    }


def _is_enabled(task: dict[str, Any]) -> bool:
    return task.get("enabled", True) is not False


def _phase_blocks(
    phases: dict[str, dict[str, Any]], selected: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_phase: dict[str, list[str]] = {}
    for view in selected:
        by_phase.setdefault(_id(view["phase_id"]), []).append(view["id"])
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
    structure, root = _structure_errors(configuration)
    if structure:
        # The flow itself is broken, not the selection: the code flow new/update use.
        raise KeboolaApiError(
            message=(
                "The job queue would refuse this flow, so it was not started: "
                + "; ".join(structure)
                + ". Fix the flow (kbagent flow detail shows it)."
            ),
            status_code=400,
            error_code=ErrorCode.INVALID_FLOW_DEFINITION,
            retryable=False,
        )
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
            skipped_disabled_task_ids=[_id(t["id"]) for t in tasks if not _is_enabled(t)],
            default_path_cycle=False,
            conditional_phase_ids=[],
        )

    by_id = {_id(t["id"]): t for t in tasks}
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
    chosen = [t for t in tasks if _id(t["id"]) in wanted or _id(t.get("phase")) in on_path]
    selected = [_task_view(t) for t in chosen if _is_enabled(t)]
    skipped = [_id(t["id"]) for t in chosen if not _is_enabled(t) and _id(t["id"]) not in wanted]
    orphans = [view["id"] for view in selected if view["phase_id"] not in phases]
    if orphans:
        errors.append(f"task(s) in a phase that the flow does not define: {', '.join(orphans)}")
    if not errors and not selected:
        errors.append("the selection is empty (every task it names is disabled or missing)")
    if not errors and root is not None:
        errors.extend(
            _run_order_errors(
                _edges(configuration), root, _dedupe(view["phase_id"] for view in selected)
            )
        )
    if errors:
        raise _invalid("Cannot select the tasks of this flow run: " + "; ".join(errors) + ".")

    selected_phases = {view["phase_id"] for view in selected}
    reach = _conditional_reach(phases, path.phase_ids) if start is not None else set()
    # Only phases with an enabled task: for an empty routing phase or a
    # disabled-only phase, "add its tasks with --task-id" is no advice (F6).
    actionable = {_id(t.get("phase")) for t in tasks if _is_enabled(t)}
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
                "a failed task; only the selected tasks run, so unselected tasks of every "
                "type (job, variable, notification) do not run."
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
