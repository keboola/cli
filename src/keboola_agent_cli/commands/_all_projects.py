"""Project selection and exit code of the 18 fan-out reads (#791, CLI-36, CLI-45).

An omitted ``--project`` on these reads uses the active project, like every
other command (spec T1, T2): ``KBAGENT_PROJECT``, then the ``project use``
choice, then the sole registered project, else ``CONFIG_ERROR`` (exit 5).
``--all-projects`` reads every registered project. A read in which a project
failed exits 1 after its output. With no registered project it exits 5 (E4).

``workspace gc`` and ``sync pull/push/diff`` declare their own
``--all-projects`` with their own meaning and are not covered here.
"""

from typing import Annotated, Any

import typer

from ..errors import ConfigError, ErrorCode
from ..output import OutputFormatter
from ..services.base import resolve_fanout_aliases
from ._helpers import item_failure_exit_code

ALL_PROJECTS_HELP = "Read all registered projects. Cannot be used with --project."

# T11: the --project help of a fan-out read states the omitted default.
_OMITTED_PROJECT = (
    "Omitted: the active project (KBAGENT_PROJECT, then 'kbagent project use', "
    "then the only registered project). Use --all-projects for all projects."
)
FANOUT_PROJECT_HELP = f"Project alias or ID, repeatable. {_OMITTED_PROJECT}"
FANOUT_ONE_PROJECT_HELP = f"Project alias or ID. {_OMITTED_PROJECT}"

AllProjectsOption = Annotated[bool, typer.Option("--all-projects", help=ALL_PROJECTS_HELP)]

# E4: a per-project answer that is not a failure (a project without PAYG).
NOT_FAILED_CODES = frozenset({str(ErrorCode.PAYG_NOT_AVAILABLE)})


def reject_project_with_all_projects(
    formatter: OutputFormatter, project: list[str] | str | None, all_projects: bool
) -> None:
    """Exit 2 (``USAGE_ERROR``) when ``--project`` and ``--all-projects`` are both given.

    The same message and exit code as ``workspace gc``.

    Raises:
        typer.Exit: With code 2 when both options are given.
    """
    if all_projects and project:
        formatter.error(
            message="Cannot use --project with --all-projects",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2)


def fanout_projects(
    ctx: typer.Context,
    formatter: OutputFormatter,
    project: list[str] | None,
    all_projects: bool,
) -> list[str] | None:
    """The ``aliases`` argument of a fan-out read's service call (T1, T2).

    ``--project`` values are returned as they are (the option callback
    records them as ``explicit``). ``--all-projects`` returns None, which the
    services read as "every registered project", and records each one as
    ``all``. Without either, the T1 chain picks one project and records its
    source (T6).

    Raises:
        typer.Exit: 2 for ``--project`` with ``--all-projects``. 5 when no
            project resolves, or when ``--all-projects`` finds none registered.
    """
    reject_project_with_all_projects(formatter, project, all_projects)
    try:
        return resolve_fanout_aliases(ctx.obj["config_store"], project, all_projects)
    except ConfigError as exc:
        formatter.error(
            message=exc.message, error_code=getattr(exc, "error_code", ErrorCode.CONFIG_ERROR)
        )
        raise typer.Exit(code=5) from None


def fanout_exit_code(failures: list[dict[str, Any]]) -> int:
    """The exit code of a fan-out read: 1 when a project failed, else 0 (E4).

    ``failures`` is the result's per-project error list. An entry whose
    ``error_code`` is in :data:`NOT_FAILED_CODES` is an answer, not a failure.
    The caller passes the code to ``formatter.output(..., exit_code=)`` and
    raises ``typer.Exit`` after the output. If another helper takes over the
    per-item exit code, :data:`NOT_FAILED_CODES` moves with it.
    """
    failed = [f for f in failures if str(f.get("error_code", "")) not in NOT_FAILED_CODES]
    return item_failure_exit_code(len(failed)) or 0
