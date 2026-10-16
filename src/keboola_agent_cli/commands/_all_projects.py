"""The shared ``--all-projects`` option of the fan-out read commands (#791, CLI-36).

In 1.0.0 an omitted ``--project`` on these reads uses the default project
(``KBAGENT_PROJECT``, then the ``project use`` choice, then the sole project),
and reading every registered project needs ``--all-projects``. The 0.x line
accepts ``--all-projects`` already, so that one script works on 0.x and on
1.0.0. Until 1.0.0 it does the same as an omitted ``--project``: the command
gets no project list and reads all registered projects.

``workspace gc`` and ``sync pull/push/diff`` declare their own
``--all-projects`` with their own meaning and are not covered here.
"""

from typing import Annotated

import typer

from ..errors import ErrorCode
from ..output import OutputFormatter

ALL_PROJECTS_HELP = (
    "Read all registered projects. Until 1.0.0 the same as omitting --project; "
    "from 1.0.0 an omitted --project reads only the default project. "
    "Cannot be used with --project."
)

AllProjectsOption = Annotated[bool, typer.Option("--all-projects", help=ALL_PROJECTS_HELP)]


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
