"""Choose the branch a command uses, and record the choice for the output.

``kbagent branch use`` saves an active branch per project
(``ProjectConfig.active_branch_id``). :func:`resolve_branch` is the only code
that applies it: commands and services call it for the branch ID, and it
records which project and branch the command used and why (issue #766).
``OutputFormatter`` reports the records: a ``Target:`` line on stderr in human
mode and ``targets`` in the ``--json`` envelope, also for ``--dry-run``.
``tests/test_effective_branch.py`` fails on a new read of ``active_branch_id``
outside this module.

The record also names where the project came from (``project_source``, spec
T6): :func:`record_project` keeps one source per alias, and
:func:`recorded_targets` merges it into every record of that alias. A project
with no branch record gets one record with ``branch_source: "none"``.

A CLI command opens the record with :func:`record_targets`, and the
``kbagent serve`` middleware opens one per request with
:func:`open_request_record` (spec V3: the ``Kbagent-Targets`` header). The
SDK opens none, so there :func:`resolve_branch` only returns the ID.
"""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .config_store import ConfigStore
    from .models import ProjectConfig

BranchSource = Literal[
    "explicit", "active_branch", "git_mapping", "manifest", "merge_request", "production", "none"
]
# Where the project came from: a flag, KBAGENT_PROJECT, the saved active
# project (`project use`), the only registered project, or --all-projects.
ProjectSource = Literal["explicit", "env", "active_project", "sole", "all"]
TargetRole = Literal["target", "source"]


@dataclass(frozen=True)
class BranchTarget:
    """One project and branch that a command used."""

    role: TargetRole
    project_alias: str
    branch_id: int | None  # None = the production endpoint
    branch_name: str | None
    branch_source: BranchSource
    active_id: int | None  # the project's active branch (`branch use`), applied or not
    active_name: str | None
    project_source: ProjectSource | None = None  # merged in by recorded_targets()

    def to_dict(self) -> dict[str, Any]:
        active = None
        if self.active_id is not None:
            active = {"branch_id": self.active_id, "branch_name": self.active_name}
        return {
            "role": self.role,
            "project_alias": self.project_alias,
            "project_source": self.project_source,
            "branch_id": self.branch_id,
            "branch_name": self.branch_name,
            "branch_source": self.branch_source,
            "active_branch": active,
        }


def _keep(targets: list[BranchTarget], target: BranchTarget) -> bool:
    """Add ``target`` unless the command already recorded it; True when added.

    A command often resolves the branch and passes the ID to a service, which
    resolves it again as ``explicit``: the first record wins. Two production
    records are one target, and the one with the looked-up ID wins. A different
    branch for the same project is a second record.
    """
    for index, seen in enumerate(targets):
        if (seen.role, seen.project_alias) != (target.role, target.project_alias):
            continue
        if seen.branch_id == target.branch_id:
            return False
        if seen.branch_source == target.branch_source == "production":
            if seen.branch_id is None:
                targets[index] = target
            return False
    targets.append(target)
    return True


class _Record:
    """The targets of one command or one REST request. Fan-out workers add to it concurrently."""

    def __init__(self, on_record: Callable[[BranchTarget], None] | None) -> None:
        self._lock = threading.Lock()
        self._targets: list[BranchTarget] = []
        # alias -> (project source, active branch ID, active branch name)
        self._projects: dict[str, tuple[ProjectSource, int | None, str | None]] = {}
        self._on_record = on_record

    def add_project(
        self, alias: str, source: ProjectSource, active_id: int | None, active_name: str | None
    ) -> None:
        """Keep the first source recorded for ``alias``: the most specific resolver runs first."""
        with self._lock:
            self._projects.setdefault(alias, (source, active_id, active_name))

    def add(self, target: BranchTarget) -> None:
        with self._lock:
            if not _keep(self._targets, target):
                return
        if self._on_record is not None:
            self._on_record(target)

    def targets(self) -> list[BranchTarget]:
        with self._lock:
            branches = list(self._targets)
            projects = dict(self._projects)
        found = [
            replace(t, project_source=projects[t.project_alias][0])
            if t.project_alias in projects
            else t
            for t in branches
        ]
        with_branch = {t.project_alias for t in branches}
        for alias, (source, active_id, active_name) in projects.items():
            if alias not in with_branch:
                found.append(
                    BranchTarget(
                        role="target",
                        project_alias=alias,
                        branch_id=None,
                        branch_name=None,
                        branch_source="none",
                        active_id=active_id,
                        active_name=active_name,
                        project_source=source,
                    )
                )
        return sorted(found, key=lambda t: (t.role != "source", t.project_alias, t.branch_id or 0))


# The record of the running CLI command: one per process, so every thread of
# the command adds to it.
_COMMAND_LOCK = threading.Lock()
_command_record: _Record | None = None
# The record of one `kbagent serve` request (spec V3). A request runs in its own
# context, and the threads that run its route get a copy of that context, so
# they add to the record of their own request. It wins over the command record.
_REQUEST_RECORD: ContextVar[_Record | None] = ContextVar("kbagent_request_targets", default=None)


def _open_record() -> _Record | None:
    return _REQUEST_RECORD.get() or _command_record


@contextmanager
def record_targets(on_record: Callable[[BranchTarget], None]) -> Iterator[None]:
    """Record the targets of one CLI command; ``on_record`` gets each new one."""
    global _command_record
    with _COMMAND_LOCK:
        previous = _command_record
        _command_record = _Record(on_record)
    try:
        yield
    finally:
        with _COMMAND_LOCK:
            _command_record = previous


def open_request_record() -> None:
    """Start the record of one REST request in the current context (``kbagent serve``).

    The serve middleware calls it before the route runs. A thread that the
    route starts records into it only when it runs in a copy of the context
    (``BaseService._run_parallel`` does so).
    """
    _REQUEST_RECORD.set(_Record(None))


def recorded_targets() -> list[BranchTarget]:
    """The targets of the running command or request, sorted; empty when nothing records.

    Each record carries the project source of its alias. A recorded project
    without a branch record is one record with ``branch_source: "none"``.
    """
    record = _open_record()
    return record.targets() if record is not None else []


# The sources of `resolve_implicit_alias`.
_PROJECT_SOURCES: dict[str, ProjectSource] = {
    "explicit": "explicit",
    "env": "env",
    "active_project": "active_project",
    "sole": "sole",
    "all": "all",
}


def record_project(alias: str, source: str, project: "ProjectConfig") -> None:
    """Record where the registered project ``alias`` came from (spec T6); the first source wins.

    ``source`` is a :data:`ProjectSource`.
    """
    record = _open_record()
    if record is not None:
        record.add_project(
            alias, _PROJECT_SOURCES[source], project.active_branch_id, project.active_branch_name
        )


def record_branch(
    config_store: "ConfigStore",
    alias: str,
    branch_id: int | None,
    source: BranchSource,
    *,
    branch_name: str | None = None,
    fixed: bool = False,
    role: TargetRole = "target",
) -> int | None:
    """Record a branch that was chosen without :func:`resolve_branch`; return ``branch_id``.

    An unknown project records nothing: the command fails on the alias. 0 is the
    production endpoint (the API clients treat it so) and is returned as None.
    ``fixed``: the command always uses this branch, so the record names no
    active branch (and the human line gives no `--branch` hint).
    """
    branch_id = branch_id or None
    project = config_store.get_project(alias)
    if project is None:
        return branch_id
    active_id, active_name = project.active_branch_id, project.active_branch_name
    if fixed:
        active_id = active_name = None
    if branch_name is None and branch_id is not None and branch_id == active_id:
        branch_name = active_name
    record = _open_record()
    if record is None:
        return branch_id
    record.add(
        BranchTarget(
            role=role,
            project_alias=alias,
            branch_id=branch_id,
            branch_name=branch_name,
            branch_source=source if branch_id is not None else "production",
            active_id=active_id,
            active_name=active_name,
        )
    )
    return branch_id


def resolve_branch(
    config_store: "ConfigStore",
    alias: str,
    branch: int | None,
    *,
    ignore_active_branch: bool = False,
    manifest_branch_id: int | None = None,
    required: bool = False,
    role: TargetRole = "target",
) -> int | None:
    """Return the branch ID a command uses on project ``alias``, and record it.

    ``branch`` (``--branch``) wins, then the active branch from ``kbagent branch
    use`` unless ``ignore_active_branch``, then ``manifest_branch_id`` (the first
    branch of a synced tree). ``None`` means the production endpoint.
    ``--branch 0`` is production too: it is recorded so and returned as 0, which
    the API clients send to the production endpoint and a second call here
    keeps as production. ``required``: the command refuses to run without a
    branch, so a missing one is not recorded. A caller that then reads the
    numeric ID of the default branch from the API records it with
    :func:`record_branch` as ``production``.
    """
    if branch == 0:
        if required:
            return None
        record_branch(config_store, alias, None, "production", role=role)
        return 0
    if branch is not None:
        return record_branch(config_store, alias, branch, "explicit", role=role)
    project = config_store.get_project(alias)
    active_id = project.active_branch_id if project is not None else None
    if active_id is not None and not ignore_active_branch:
        return record_branch(config_store, alias, active_id, "active_branch", role=role)
    if manifest_branch_id is not None:
        return record_branch(config_store, alias, manifest_branch_id, "manifest", role=role)
    if required:
        return None
    return record_branch(config_store, alias, None, "production", role=role)


def report_branch_ref(config_store: "ConfigStore", alias: str, ref: int | str | None) -> None:
    """Record a branch given as an ID or ``"default"`` by a command that never applies
    the active branch (Data Streams, branch metadata)."""
    branch = int(ref) if ref is not None and str(ref).isdecimal() else None
    resolve_branch(config_store, alias, branch, ignore_active_branch=True)
