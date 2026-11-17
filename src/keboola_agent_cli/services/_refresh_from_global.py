"""Take a working Storage token from the global config during `project refresh` (#955).

`kbagent init --from-global` copies the projects of the global config, tokens
included, into a local `.kbagent/config.json` once. When the tokens are later
rotated in the global config, the local copies stay dead. `project refresh`
run against a local config therefore looks into the global config first: a
project whose local token is definitively invalid takes the global token of
the same project when exactly one such token exists and it verifies. Only
when that is not possible does refresh mint a new token through the Manage
API, as before.

The global config is only ever read here. The write of an adopted token goes
to the local config, inside its transaction, and only when the alias still
holds what the plan saw.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..auth.sentinel import is_session_token
from ..client import KeboolaClient
from ..config_store import ConfigStore
from ..errors import ConfigError
from ..models import ProjectConfig, normalize_stack_url

logger = logging.getLogger(__name__)

StorageClientFactory = Callable[[str, str], KeboolaClient]

ACTION_ADOPTED = "adopted_from_global"
ACTION_WOULD_ADOPT = "would_adopt_from_global"


@dataclass(frozen=True)
class GlobalCandidate:
    """The one token the global config holds for a project."""

    token: str
    global_alias: str


@dataclass(frozen=True)
class PlannedAdoption:
    """A local alias whose dead token is replaced by a verified global token."""

    alias: str
    project: ProjectConfig
    token: str
    global_alias: str
    project_name: str

    def report(self, *, dry_run: bool) -> dict[str, Any]:
        """The `projects_adopted` entry. It never carries the token, not even masked."""
        return {
            "alias": self.alias,
            "project_id": self.project.project_id,
            "project_name": self.project_name,
            "global_alias": self.global_alias,
            "action": ACTION_WOULD_ADOPT if dry_run else ACTION_ADOPTED,
        }


def load_global_projects(global_store: ConfigStore | None) -> dict[str, ProjectConfig]:
    """The projects of the global config, or none when it cannot be read.

    A missing, unreadable or corrupt global config is not an error of
    `project refresh`: it only means there is nothing to take from it.
    """
    if global_store is None or not global_store.config_path.is_file():
        return {}
    try:
        return dict(global_store.load().projects)
    except (ConfigError, OSError):
        logger.debug("Global config unreadable; no global token candidates", exc_info=True)
        return {}


def _stack_key(stack_url: str) -> str | None:
    """The canonical stack URL (ports kept), or None when it does not parse."""
    try:
        return normalize_stack_url(stack_url)
    except ValueError:
        return None


def same_stack(first: str, second: str) -> bool:
    """True when both URLs name one stack (`models.normalize_stack_url`)."""
    key = _stack_key(first)
    return key is not None and key == _stack_key(second)


def pick_global_candidate(
    global_projects: dict[str, ProjectConfig], project: ProjectConfig
) -> GlobalCandidate | None:
    """The single distinct global token for ``project``, or None.

    A candidate has the same project ID and stack, is a static token (not a
    session sentinel), is persisted (not the env-synthesized ``__env__``
    project) and differs from the local token. Zero or several distinct
    tokens mean no candidate: kbagent cannot tell which one is right.
    """
    found: dict[str, str] = {}
    for global_alias, entry in global_projects.items():
        if entry.ephemeral or is_session_token(entry.token):
            continue
        if entry.project_id is None or entry.project_id != project.project_id:
            continue
        if not same_stack(entry.stack_url, project.stack_url):
            continue
        if entry.token == project.token:
            continue
        found.setdefault(entry.token, global_alias)
    if len(found) != 1:
        return None
    token, global_alias = next(iter(found.items()))
    return GlobalCandidate(token=token, global_alias=global_alias)


def verify_global_candidate(
    storage_client_factory: StorageClientFactory,
    alias: str,
    project: ProjectConfig,
    candidate: GlobalCandidate,
) -> PlannedAdoption | None:
    """Verify the global token; adopt it only when it works for the same project.

    Any verification error means no adoption: the caller falls back to minting.
    """
    try:
        client = storage_client_factory(project.stack_url, candidate.token)
        try:
            info = client.verify_token()
        finally:
            client.close()
    except Exception:
        logger.debug("Global token for '%s' did not verify", alias, exc_info=True)
        return None
    if info.project_id != project.project_id:
        logger.debug("Global token for '%s' belongs to another project", alias)
        return None
    return PlannedAdoption(
        alias=alias,
        project=project,
        token=candidate.token,
        global_alias=candidate.global_alias,
        project_name=info.project_name or project.project_name,
    )


def write_adoption(config_store: ConfigStore, plan: PlannedAdoption) -> None:
    """Write the adopted token, only if the alias is unchanged since the plan.

    Raises:
        ConfigError: The alias was removed or changed (token, project or
            stack) by another process since the plan. Nothing is written.
        SessionAuthUnsupportedError: Through ``edit_project``, should the
            alias now hold a session sentinel.
    """
    with config_store.transaction():
        current = config_store.load().projects.get(plan.alias)
        unchanged = (
            current is not None
            and not current.ephemeral
            and current.token == plan.project.token
            and current.project_id == plan.project.project_id
            and same_stack(current.stack_url, plan.project.stack_url)
        )
        if not unchanged:
            raise ConfigError(
                f"Project '{plan.alias}' changed concurrently while refreshing; "
                "nothing was written. Run `kbagent project refresh` again."
            )
        config_store.edit_project(plan.alias, token=plan.token, project_name=plan.project_name)
