"""Translate a project ID to the registered alias it names (CLI-22).

``--project``, the other CLI options that name a registered alias,
``KBAGENT_PROJECT``, ``project use`` and the ``kbagent serve`` path and query
project parameters take a registered alias. They also take a Keboola project
ID: :func:`resolve_project_ref` turns it into the alias of the one registered
project with that ``ProjectConfig.project_id``.

The translation runs once, before a command body or route handler runs
(``commands/_project_ref.py`` for the CLI, ``server/dependencies.py`` for
``kbagent serve``). Services use the value as a dict key after resolution
(``projects[alias]``), so they must only ever receive aliases.

Rules:

- An alias wins. A registered alias is returned unchanged, even when it is
  also the ID of another project.
- A project ID is unique only per stack, and one project can be registered
  under two aliases (two tokens). When several registered projects have the
  ID, the one session-token alias wins only if all of them are on the same
  stack. Every other multiple match is an error: nothing picks one silently.
- No match returns the value unchanged, so the caller's own "not found" error
  applies. A registered project without a stored ``project_id`` (an old entry)
  never matches by ID.
"""

import re
from collections.abc import Mapping

from .auth.sentinel import is_session_token
from .errors import ConfigError
from .models import ProjectConfig

# Up to 18 digits: every real project ID fits, and int() of a longer string can
# raise (Python caps int() at 4300 digits) or produce an ID no project has.
_PROJECT_ID_RE = re.compile(r"[0-9]{1,18}")


def is_project_id(value: str) -> bool:
    """True when ``value`` has the shape of a Keboola project ID (1-18 ASCII digits)."""
    return bool(_PROJECT_ID_RE.fullmatch(value))


def _aliases_with_id(projects: Mapping[str, ProjectConfig], project_id: int) -> list[str]:
    return sorted(alias for alias, project in projects.items() if project.project_id == project_id)


def resolve_project_ref(projects: Mapping[str, ProjectConfig], ref: str) -> str:
    """Return the registered alias that ``ref`` names, or ``ref`` when nothing matches.

    Raises:
        ConfigError: ``ref`` is the project ID of several registered projects
            and none of them wins (see the module docstring).
    """
    if ref in projects or not is_project_id(ref):
        return ref
    return resolve_project_id(projects, int(ref)) or ref


def resolve_project_id(projects: Mapping[str, ProjectConfig], project_id: int) -> str | None:
    """Return the alias of the registered project with ``project_id``, or None.

    The ID half of :func:`resolve_project_ref`, for a value that is only ever
    an ID (never looked up as an alias).

    Raises:
        ConfigError: several registered projects have that ID and none of
            them wins (see the module docstring).
    """
    matches = _aliases_with_id(projects, project_id)
    if len(matches) <= 1:
        return matches[0] if matches else None
    session_aliases = [alias for alias in matches if is_session_token(projects[alias].token)]
    stacks = {projects[alias].stack_url for alias in matches}
    if len(stacks) == 1 and len(session_aliases) == 1:
        return session_aliases[0]
    listed = ", ".join(f"'{alias}' ({projects[alias].stack_url})" for alias in matches)
    raise ConfigError(
        f"Project ID {project_id} matches more than one registered project: {listed}. "
        "Use one of these aliases instead."
    )


def alias_shadow_notice(projects: Mapping[str, ProjectConfig], ref: str) -> str | None:
    """Explain an alias that hides another project's ID, or None when nothing is hidden.

    ``ref`` resolves to the alias ``ref`` (an alias wins), but when it is also
    the project ID of a DIFFERENT registered project, the caller may have meant
    that project. Aliases of the same project (same ID and stack) hide nothing.
    """
    if ref not in projects or not is_project_id(ref):
        return None
    own = projects[ref]
    hidden = [
        alias
        for alias in _aliases_with_id(projects, int(ref))
        if (projects[alias].project_id, projects[alias].stack_url)
        != (own.project_id, own.stack_url)
    ]
    if not hidden:
        return None
    owner = f"project {own.project_id}" if own.project_id is not None else "a project"
    listed = ", ".join(f"'{alias}'" for alias in hidden)
    remedy = f"Pass {listed}" if len(hidden) == 1 else "Pass one of those aliases"
    return (
        f"'{ref}' is an alias of {owner}; project ID {ref} is registered as {listed}. "
        f"{remedy} to use that project."
    )
