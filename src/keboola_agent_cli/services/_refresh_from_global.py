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
from ..errors import ConfigError, ErrorCode
from ..models import ProjectConfig, normalize_stack_url

logger = logging.getLogger(__name__)

StorageClientFactory = Callable[[str, str], KeboolaClient]

ACTION_ADOPTED = "adopted_from_global"
ACTION_WOULD_ADOPT = "would_adopt_from_global"

# The `reason` of `global_token_not_used`: the global token has fewer rights
# than a token kbagent would mint (D5).
REASON_SCOPED = "scoped"

# The rights `manage_client.create_project_token` gives a minted token by
# default. A global token is adopted only when it has all of them (or is a
# master token): the dead local token's rights cannot be read any more, so
# what a mint would produce is the only safe baseline. `canManageBuckets`
# already covers every bucket and component, so bucketPermissions and
# componentAccess need no check.
MINTED_TOKEN_RIGHTS = (
    "canManageBuckets",
    "canReadAllFileUploads",
    "canReadAllProjectEvents",
    "canManageDevBranches",
    "canManageTokens",
)


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


@dataclass(frozen=True)
class GlobalTokenNotUsed:
    """A global token that verified for the project but was not adopted."""

    global_alias: str
    reason: str

    def report(self) -> dict[str, str]:
        """The `global_token_not_used` value. It never carries the token."""
        return {"global_alias": self.global_alias, "reason": self.reason}


class ManageTokenRequiredError(Exception):
    """A project needs a minted token and no Manage API token is available (#955).

    Reported as that project's ``projects_failed`` entry; the run goes on.
    """

    error_code = ErrorCode.MANAGE_TOKEN_REQUIRED
    retryable = False

    def __init__(self, alias: str) -> None:
        self.message = (
            f"Project '{alias}' needs a new Storage token and no Manage token is available "
            "(no terminal, and KBC_MANAGE_API_TOKEN is read only with "
            "--allow-env-manage-token). Run `kbagent project refresh --project "
            f"{alias}` in a terminal, or set KBC_MANAGE_API_TOKEN and pass "
            "--allow-env-manage-token."
        )
        super().__init__(self.message)


def has_minted_token_rights(info: dict[str, Any]) -> bool:
    """True when a raw token-verify response is at least as strong as a minted token.

    A master token, or every flag of :data:`MINTED_TOKEN_RIGHTS` explicitly
    ``True``. A missing or non-boolean flag counts as not granted.
    """
    if info.get("isMasterToken") is True:
        return True
    return all(info.get(flag) is True for flag in MINTED_TOKEN_RIGHTS)


def load_global_projects(global_store: ConfigStore | None) -> dict[str, ProjectConfig]:
    """The projects of the global config, or none when it cannot be read.

    A missing, unreadable or corrupt global config is not an error of
    `project refresh`: it only means there is nothing to take from it.
    """
    if global_store is None:
        return {}
    # Any failure -- an unreadable directory, a corrupt file, a config that
    # fails validation in any way -- means "no candidates", never an error.
    # Only the exception class is logged: a validation error can quote a token.
    try:
        if not global_store.config_path.is_file():
            return {}
        return dict(global_store.load().projects)
    except Exception as exc:
        logger.debug(
            "Global config unreadable (%s); no global token candidates", type(exc).__name__
        )
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


def _owner_id(owner: dict[str, Any]) -> int | None:
    """The project ID of a raw verify response's ``owner`` block, or None."""
    try:
        return int(owner["id"])
    except (KeyError, TypeError, ValueError):
        return None


def verify_global_candidate(
    storage_client_factory: StorageClientFactory,
    alias: str,
    project: ProjectConfig,
    candidate: GlobalCandidate,
) -> PlannedAdoption | GlobalTokenNotUsed | None:
    """Verify the global token; adopt it only when it works for the same project
    and has at least the rights of a minted token.

    Returns:
        A :class:`PlannedAdoption`; a :class:`GlobalTokenNotUsed` (reason
        ``scoped``) when the token verified for this project but has fewer
        rights; or None when it did not verify or belongs to another project.
        In both non-adoption cases the caller falls back to minting.
    """
    try:
        with storage_client_factory(project.stack_url, candidate.token) as client:
            info = client.get_project_info()
    except Exception as exc:
        logger.debug("Global token for '%s' did not verify (%s)", alias, type(exc).__name__)
        return None
    owner = info.get("owner") if isinstance(info, dict) else None
    if not isinstance(owner, dict) or _owner_id(owner) != project.project_id:
        logger.debug("Global token for '%s' belongs to another project", alias)
        return None
    if not has_minted_token_rights(info):
        logger.debug("Global token for '%s' has fewer rights than a minted token", alias)
        return GlobalTokenNotUsed(global_alias=candidate.global_alias, reason=REASON_SCOPED)
    return PlannedAdoption(
        alias=alias,
        project=project,
        token=candidate.token,
        global_alias=candidate.global_alias,
        project_name=owner.get("name") or project.project_name,
    )


def ensure_unchanged(config_store: ConfigStore, alias: str, snapshot: ProjectConfig) -> None:
    """Raise unless ``alias`` still holds the token, project and stack of ``snapshot``.

    Call it inside ``config_store.transaction()`` right before a write, so a
    concurrent edit of the alias (another kbagent process, or one that ran
    while the Manage-token prompt waited for a human) is never overwritten.

    Raises:
        ConfigError: The alias was removed or changed since the snapshot.
    """
    current = config_store.load().projects.get(alias)
    unchanged = (
        current is not None
        and not current.ephemeral
        and current.token == snapshot.token
        and current.project_id == snapshot.project_id
        and same_stack(current.stack_url, snapshot.stack_url)
    )
    if not unchanged:
        raise ConfigError(
            f"Project '{alias}' changed concurrently while refreshing; "
            "nothing was written. Run `kbagent project refresh` again."
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
        ensure_unchanged(config_store, plan.alias, plan.project)
        config_store.edit_project(plan.alias, token=plan.token, project_name=plan.project_name)
