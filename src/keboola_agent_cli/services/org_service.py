"""Organization setup service - bulk onboarding of all projects in a Keboola org.

Orchestrates the Manage API (list projects, create tokens) and the Storage API
(verify tokens) to register all organization projects in one shot.
"""

import logging
import re
from collections.abc import Callable
from enum import Enum
from typing import Any

from ..auth.sentinel import is_session_token
from ..client import KeboolaClient
from ..config_store import ConfigStore
from ..constants import DEFAULT_TOKEN_DESCRIPTION
from ..errors import ConfigError, ErrorCode, KeboolaApiError, mask_token
from ..manage_client import ManageClient
from ..models import ProjectConfig
from ._refresh_from_global import (
    PlannedAdoption,
    ensure_unchanged,
    load_global_projects,
    pick_global_candidate,
    verify_global_candidate,
    write_adoption,
)
from .base import default_client_factory, item_failure, make_client_factory

logger = logging.getLogger(__name__)

ManageClientFactory = Callable[[str, str], ManageClient]
StorageClientFactory = Callable[[str, str], KeboolaClient]


def default_manage_client_factory(stack_url: str, manage_token: str) -> ManageClient:
    """Create a ManageClient with the given stack URL and manage token."""
    return ManageClient(stack_url=stack_url, manage_token=manage_token)


def default_storage_client_factory(stack_url: str, token: str) -> KeboolaClient:
    """Create a KeboolaClient with the given stack URL and storage token.

    Delegates to :func:`services.base.default_client_factory` so this stays
    a static-token-only builder (fails fast on a session sentinel) while
    keeping its own name for callers/tests that inject it explicitly. Real
    usage goes through :class:`OrgService`'s bearer-aware default
    (``make_client_factory``) instead.
    """
    return default_client_factory(stack_url, token)


class _TokenState(Enum):
    """Outcome of checking a registered project's Storage token."""

    VALID = "valid"
    INVALID = "invalid"  # the Storage API answered INVALID_TOKEN
    UNKNOWN = "unknown"  # any other API error: says nothing about the token


def _refresh_skip_reason(project: ProjectConfig) -> str | None:
    """Why `project refresh` leaves a project alone, or None to check it.

    A browser-login session project has no static token to replace: its
    credential lives in auth.json and the access token rotates on its own.
    Skipping before the validity check keeps ``force=True`` from converting
    it. A project without a project_id cannot get a token from the Manage API.
    """
    if is_session_token(project.token):
        return (
            "Browser-login (session) project -- nothing to refresh; "
            "session access tokens rotate automatically."
        )
    if project.project_id is None:
        return "Cannot refresh: project_id is missing"
    return None


def slugify(name: str) -> str:
    """Convert a project name to a URL-safe alias.

    Examples:
        "My Project (v2)" -> "my-project-v2"
        "  Hello   World  " -> "hello-world"
        "Prod / Main" -> "prod-main"
        "---test---" -> "test"
        "" -> "project"
    """
    # Lowercase
    slug = name.lower()
    # Replace non-alphanumeric characters with hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    # Strip leading/trailing hyphens
    slug = slug.strip("-")
    # Fallback for empty result
    return slug or "project"


class OrgService:
    """Business logic for organization-level operations.

    Uses dependency injection for config_store and client factories
    to enable easy testing with mocks.
    """

    def __init__(
        self,
        config_store: ConfigStore,
        manage_client_factory: ManageClientFactory | None = None,
        storage_client_factory: StorageClientFactory | None = None,
    ) -> None:
        self._config_store = config_store
        self._manage_client_factory = manage_client_factory or default_manage_client_factory
        self._storage_client_factory = storage_client_factory or make_client_factory(config_store)

    def setup_organization(
        self,
        stack_url: str,
        manage_token: str,
        org_id: int | None = None,
        token_description: str = DEFAULT_TOKEN_DESCRIPTION,
        dry_run: bool = False,
        token_expires_in: int | None = None,
        project_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """Set up projects and register them in the config store.

        Two modes of operation:
        - **Org admin mode** (org_id): lists all projects in the organization
          via Manage API. Requires an org-admin manage token.
        - **Project member mode** (project_ids): fetches each project
          individually. Works with a Personal Access Token (PAT) for any
          project the user is a member of.

        Args:
            stack_url: Keboola stack URL.
            manage_token: Manage API token or Personal Access Token.
            org_id: Organization ID (org admin mode).
            token_description: Description prefix for created tokens.
            dry_run: If True, only preview what would happen without making changes.
            token_expires_in: Token lifetime in seconds. None means no expiration.
            project_ids: Explicit project IDs (project member mode).

        Returns:
            Dict with setup results including added, skipped, and failed projects.

        Raises:
            ValueError: If neither org_id nor project_ids is provided.
        """
        if not org_id and not project_ids:
            msg = "Either org_id or project_ids must be provided"
            raise ValueError(msg)

        manage_client = self._manage_client_factory(stack_url, manage_token)
        org_name: str | None = None
        try:
            if project_ids:
                projects, fetch_failed = self._fetch_projects_by_ids(manage_client, project_ids)
                # Derive org_id from the first successfully fetched project
                if not org_id and projects:
                    org_id = projects[0].get("organization", {}).get("id")
            else:
                # org_id is non-None here: the guard above raises if both are None
                assert org_id is not None
                projects = manage_client.list_organization_projects(org_id)
                fetch_failed = []

            # Derive org_name: prefer per-project payload, otherwise call Manage API once.
            for p in projects:
                candidate = (p.get("organization") or {}).get("name")
                if isinstance(candidate, str) and candidate:
                    org_name = candidate
                    break
            if not org_name and org_id:
                try:
                    fetched = manage_client.get_organization(org_id).get("name")
                    if isinstance(fetched, str) and fetched:
                        org_name = fetched
                except Exception:
                    # Best-effort: org name resolution is optional, but the
                    # specific exception type matters when something IS wrong
                    # (403 vs 404 vs network), so log with traceback.
                    logger.debug(
                        "Could not resolve organization name for org_id=%s",
                        org_id,
                        exc_info=True,
                    )

            # Resolve token owner identity for unique token naming
            owner_name = ""
            try:
                token_info = manage_client.verify_token()
                user_info = token_info.get("user", {})
                owner_name = user_info.get("email") or user_info.get("name", "")
            except Exception:
                logger.debug("Could not resolve manage token owner identity")
        finally:
            manage_client.close()

        # Load existing config to check for already-registered projects
        config = self._config_store.load()
        existing_project_ids = {
            p.project_id for p in config.projects.values() if p.project_id is not None
        }

        # Track used aliases for uniqueness
        used_aliases = set(config.projects.keys())

        added: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = list(fetch_failed)
        active_project_set: str | None = None

        for project in projects:
            project_id = project.get("id", 0)
            project_name = project.get("name", f"project-{project_id}")

            # Skip already-registered projects (idempotency)
            if project_id in existing_project_ids:
                skipped.append(
                    {
                        "project_id": project_id,
                        "project_name": project_name,
                        "reason": "Already registered in config",
                    }
                )
                continue

            # Generate unique alias
            alias = self._unique_alias(slugify(project_name), used_aliases)
            used_aliases.add(alias)

            if dry_run:
                added.append(
                    {
                        "project_id": project_id,
                        "project_name": project_name,
                        "alias": alias,
                        "action": "would_add",
                    }
                )
                continue

            # Create token and register project
            try:
                became_active = self._setup_single_project(
                    stack_url=stack_url,
                    manage_token=manage_token,
                    project_id=project_id,
                    project_name=project_name,
                    alias=alias,
                    token_description=token_description,
                    owner_name=owner_name,
                    token_expires_in=token_expires_in,
                    org_id=org_id,
                    org_name=org_name,
                )
                if became_active:
                    active_project_set = alias
                # Re-read to get masked token
                registered = self._config_store.get_project(alias)
                added.append(
                    {
                        "project_id": project_id,
                        "project_name": project_name,
                        "alias": alias,
                        "token": mask_token(registered.token) if registered else "***",
                        "action": "added",
                    }
                )
            except KeboolaApiError as exc:
                failed.append(
                    item_failure(
                        exc,
                        project_id=project_id,
                        project_name=project_name,
                        alias=alias,
                    )
                )
            except Exception as exc:
                failed.append(
                    item_failure(
                        exc,
                        project_id=project_id,
                        project_name=project_name,
                        alias=alias,
                    )
                )

        return {
            "organization_id": org_id,
            "stack_url": stack_url,
            "projects_found": len(projects),
            "projects_added": added,
            "projects_skipped": skipped,
            "projects_failed": failed,
            "dry_run": dry_run,
            "token_expires_in": token_expires_in,
            "active_project_set": active_project_set,
        }

    def refresh_tokens(
        self,
        manage_token: str | None = None,
        aliases: list[str] | None = None,
        token_description: str = DEFAULT_TOKEN_DESCRIPTION,
        dry_run: bool = False,
        token_expires_in: int | None = None,
        force: bool = False,
        *,
        manage_token_provider: Callable[[], str] | None = None,
        global_store: ConfigStore | None = None,
    ) -> dict[str, Any]:
        """Refresh storage API tokens for registered projects.

        Runs in two phases. Phase 1 checks every token and plans, writing
        nothing: a valid token stays (unless ``force``); a token the Storage
        API rejects as ``INVALID_TOKEN`` takes the global config's token for
        the same project when ``global_store`` is given and exactly one such
        token verifies (#955); every other token needs a new one minted
        through the Manage API. Phase 2 resolves the Manage token once --
        only when something must be minted and not in a dry run -- and then
        writes the adoptions and mints.

        Browser-login (session) projects are reported under
        ``projects_skipped`` and never touched, ``force`` included: they hold a
        ``kbc-session://`` sentinel instead of a static token, and their real
        credential rotates on its own.

        Args:
            manage_token: Manage API token (for creating new storage tokens).
            aliases: Optional list of project aliases to refresh. If None, all
                projects are checked.
            token_description: Description prefix for newly created tokens.
            dry_run: If True, only preview what would happen without making changes.
            token_expires_in: Token lifetime in seconds. None means no expiration.
            force: If True, refresh all tokens even if they are still valid.
                Never takes a token from the global config.
            manage_token_provider: Called once, in phase 2, when ``manage_token``
                is None and a token must be minted. Its exceptions (the CLI's
                ``typer.Exit(2)``) propagate untouched; nothing is written
                before it is called.
            global_store: The global config, read-only, consulted when the
                refreshed config is a local one. None disables adoption.

        Returns:
            Dict with refresh results including adopted, refreshed, valid,
            skipped, and failed projects.

        Raises:
            ValueError: A token must be minted and neither ``manage_token`` nor
                ``manage_token_provider`` was given.
        """
        config = self._config_store.load()

        # Determine which projects to check
        projects_to_check: list[tuple[str, ProjectConfig]] = []
        failed: list[dict[str, Any]] = []

        if aliases:
            for alias in aliases:
                if alias not in config.projects:
                    failed.append(
                        {
                            "alias": alias,
                            "project_name": "",
                            "message": f"Project '{alias}' not found in config",
                        }
                    )
                else:
                    projects_to_check.append((alias, config.projects[alias]))
        else:
            projects_to_check = list(config.projects.items())

        projects_valid: list[dict[str, Any]] = []
        projects_skipped: list[dict[str, Any]] = []
        adoptions: list[PlannedAdoption] = []
        to_mint: list[tuple[str, ProjectConfig]] = []
        # Loaded on first need: only an INVALID_TOKEN project reads the global config.
        global_projects: dict[str, ProjectConfig] | None = None

        # Phase 1: check and plan. Nothing is written here.
        for alias, project in projects_to_check:
            skip_reason = _refresh_skip_reason(project)
            if skip_reason:
                projects_skipped.append(
                    {"alias": alias, "project_name": project.project_name, "reason": skip_reason}
                )
                continue

            try:
                state = self._check_token_state(project)
            except KeboolaApiError:
                # The client could not even be built: unknown, as before.
                state = _TokenState.UNKNOWN
            except Exception as exc:
                failed.append(item_failure(exc, alias=alias, project_name=project.project_name))
                continue

            if state is _TokenState.VALID and not force:
                projects_valid.append(
                    {
                        "alias": alias,
                        "project_id": project.project_id,
                        "project_name": project.project_name,
                    }
                )
                continue

            # Only a definitively invalid token may be replaced from the global
            # config: a transient check error says nothing about the token (R1).
            # An ephemeral (env-synthesized `__env__`) local project is never
            # persisted, so it cannot take a token either: today's path.
            if (
                state is _TokenState.INVALID
                and not force
                and global_store is not None
                and not project.ephemeral
            ):
                if global_projects is None:
                    global_projects = load_global_projects(global_store)
                candidate = pick_global_candidate(global_projects, project)
                plan = (
                    verify_global_candidate(self._storage_client_factory, alias, project, candidate)
                    if candidate
                    else None
                )
                if plan is not None:
                    adoptions.append(plan)
                    continue

            to_mint.append((alias, project))

        result: dict[str, Any] = {
            "dry_run": dry_run,
            "projects_checked": len(projects_to_check),
            "projects_adopted": [],
            "projects_refreshed": [],
            "projects_valid": projects_valid,
            "projects_skipped": projects_skipped,
            "projects_failed": failed,
            "token_expires_in": token_expires_in,
        }

        if dry_run:
            result["projects_adopted"] = [plan.report(dry_run=True) for plan in adoptions]
            result["projects_refreshed"] = [
                {
                    "alias": alias,
                    "project_id": project.project_id,
                    "project_name": project.project_name,
                    "token": "***",
                    "action": "would_refresh",
                }
                for alias, project in to_mint
            ]
            return result

        # Phase 2. The Manage token is resolved before the first write and
        # outside any per-project handler, so a provider that cannot supply
        # one (typer.Exit(2)) ends the run with nothing written (R2).
        if to_mint and manage_token is None:
            if manage_token_provider is None:
                raise ValueError("refresh_tokens needs manage_token or manage_token_provider")
            manage_token = manage_token_provider()

        # The owner lookup also runs before the first write, and never raises:
        # a run either stops before writing anything or returns its full result.
        owner_name = (
            self._manage_token_owner(to_mint[0][1].stack_url, manage_token)
            if to_mint and manage_token is not None
            else ""
        )

        for plan in adoptions:
            try:
                write_adoption(self._config_store, plan)
                result["projects_adopted"].append(plan.report(dry_run=False))
            except Exception as exc:
                failed.append(item_failure(exc, alias=plan.alias, project_name=plan.project_name))

        if to_mint and manage_token is not None:
            for alias, project in to_mint:
                try:
                    new_token = self._refresh_single_project(
                        manage_token=manage_token,
                        alias=alias,
                        project=project,
                        token_description=token_description,
                        owner_name=owner_name,
                        token_expires_in=token_expires_in,
                    )
                    result["projects_refreshed"].append(
                        {
                            "alias": alias,
                            "project_id": project.project_id,
                            "project_name": project.project_name,
                            "token": mask_token(new_token),
                            "action": "refreshed",
                        }
                    )
                except Exception as exc:
                    failed.append(item_failure(exc, alias=alias, project_name=project.project_name))

        return result

    def _check_token_state(self, project: ProjectConfig) -> _TokenState:
        """Verify a project's token: valid, definitively invalid, or unknown.

        ``UNKNOWN`` (a transient or any other API error) keeps the old
        behaviour -- a new token is minted -- but never allows taking a token
        from the global config. A non-API exception propagates.
        """
        client = self._storage_client_factory(project.stack_url, project.token)
        try:
            client.verify_token()
            return _TokenState.VALID
        except KeboolaApiError as exc:
            if exc.error_code == ErrorCode.INVALID_TOKEN:
                return _TokenState.INVALID
            return _TokenState.UNKNOWN
        finally:
            client.close()

    def _manage_token_owner(self, stack_url: str, manage_token: str) -> str:
        """Email or name of the Manage token's owner, for unique token naming.

        Never raises: a failure anywhere -- building the client included --
        only means the description carries no owner.
        """
        try:
            manage_client = self._manage_client_factory(stack_url, manage_token)
            try:
                token_info = manage_client.verify_token()
            finally:
                manage_client.close()
            user_info = token_info.get("user", {})
            return user_info.get("email") or user_info.get("name", "")
        except Exception as exc:
            logger.debug("Could not resolve manage token owner identity (%s)", type(exc).__name__)
            return ""

    def _refresh_single_project(
        self,
        manage_token: str,
        alias: str,
        project: ProjectConfig,
        token_description: str,
        owner_name: str = "",
        token_expires_in: int | None = None,
    ) -> str:
        """Create a new token for an existing project and update the config.

        Args:
            manage_token: Manage API token (for creating the storage token).
            alias: The project alias in the config store.
            project: The existing project configuration.
            token_description: Description prefix for the created token.
            owner_name: Email/name of the manage token owner (for unique identification).
            token_expires_in: Token lifetime in seconds. None means no expiration.

        Returns:
            The new storage API token string.
        """
        description = f"{token_description} [{owner_name}]" if owner_name else token_description

        logger.info(
            "Refreshing token for project %d (%s) alias '%s' with description '%s'",
            project.project_id,
            project.project_name,
            alias,
            description,
        )

        # Create a new Storage API token via Manage API
        # project_id is always set on registered projects (populated by verify_token at add time)
        assert project.project_id is not None
        # The Manage-token prompt may have waited for a human since the plan:
        # check the alias before creating a token, so a concurrent edit costs
        # no orphaned token, and again inside the write transaction below.
        with self._config_store.transaction():
            ensure_unchanged(self._config_store, alias, project)
        manage_client = self._manage_client_factory(project.stack_url, manage_token)
        try:
            token_data = manage_client.create_project_token(
                project_id=project.project_id,
                description=description,
                expires_in=token_expires_in,
            )
        finally:
            manage_client.close()

        storage_token = token_data["token"]

        # Verify the new token to get project info
        storage_client = self._storage_client_factory(project.stack_url, storage_token)
        try:
            token_info = storage_client.verify_token()
        finally:
            storage_client.close()

        # Update the project in config, only if nobody changed it meanwhile.
        with self._config_store.transaction():
            try:
                ensure_unchanged(self._config_store, alias, project)
            except ConfigError as exc:
                raise ConfigError(
                    f"{exc.message} A new Storage token (ID {token_data.get('id', '?')}) "
                    f"was created in project {project.project_id} on "
                    f"{project.stack_url} but not saved; delete it in the Keboola UI "
                    "or with `kbagent token delete --token-id ID` from a config "
                    "that holds that project."
                ) from None
            self._config_store.edit_project(
                alias,
                token=storage_token,
                project_name=token_info.project_name,
                project_id=token_info.project_id,
            )

        return storage_token

    @staticmethod
    def _fetch_projects_by_ids(
        manage_client: ManageClient,
        project_ids: list[int],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Fetch project details for explicit project IDs.

        Uses GET /manage/projects/{id} which works with Personal Access
        Tokens for projects where the token owner is a member.

        Returns:
            Tuple of (projects, failed) where projects is a list of project
            dicts and failed is a list of error dicts for inaccessible projects.
        """
        projects: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for pid in project_ids:
            try:
                project = manage_client.get_project(pid)
                projects.append(project)
            except KeboolaApiError as exc:
                failed.append(
                    item_failure(
                        exc,
                        project_id=pid,
                        project_name=f"project-{pid}",
                        alias=f"project-{pid}",
                    )
                )
            except Exception as exc:
                failed.append(
                    item_failure(
                        exc,
                        project_id=pid,
                        project_name=f"project-{pid}",
                        alias=f"project-{pid}",
                    )
                )
        return projects, failed

    def _setup_single_project(
        self,
        stack_url: str,
        manage_token: str,
        project_id: int,
        project_name: str,
        alias: str,
        token_description: str,
        owner_name: str = "",
        token_expires_in: int | None = None,
        org_id: int | None = None,
        org_name: str | None = None,
    ) -> bool:
        """Create a token for a single project, verify it, and register it.

        Returns True when the registered project became the active project.

        Args:
            stack_url: Keboola stack URL.
            manage_token: Manage API token (for creating the storage token).
            project_id: The project ID.
            project_name: The project name (from Manage API).
            alias: The alias to register the project under.
            token_description: Description for the created token.
            owner_name: Email/name of the manage token owner (for unique identification).
            token_expires_in: Token lifetime in seconds. None means no expiration.
        """
        description = f"{token_description} [{owner_name}]" if owner_name else token_description

        logger.info(
            "Creating token for project %d (%s) with description '%s'",
            project_id,
            project_name,
            description,
        )

        # Create a Storage API token via Manage API
        manage_client = self._manage_client_factory(stack_url, manage_token)
        try:
            token_data = manage_client.create_project_token(
                project_id=project_id,
                description=description,
                expires_in=token_expires_in,
            )
        finally:
            manage_client.close()

        storage_token = token_data["token"]

        # Verify the new token to get project info
        storage_client = self._storage_client_factory(stack_url, storage_token)
        try:
            token_info = storage_client.verify_token()
        finally:
            storage_client.close()

        # Register the project in config. Org info from token verify wins
        # (it's the authoritative source); fall back to the values derived
        # during org setup.
        project_config = ProjectConfig(
            stack_url=stack_url,
            token=storage_token,
            project_name=token_info.project_name,
            project_id=token_info.project_id,
            org_id=token_info.org_id or org_id,
            org_name=token_info.org_name or org_name,
        )
        return self._config_store.add_project(alias, project_config)

    @staticmethod
    def _unique_alias(base: str, used: set[str]) -> str:
        """Generate a unique alias by appending a counter if needed.

        Examples:
            _unique_alias("prod", set()) -> "prod"
            _unique_alias("prod", {"prod"}) -> "prod-2"
            _unique_alias("prod", {"prod", "prod-2"}) -> "prod-3"
        """
        if base not in used:
            return base
        counter = 2
        while f"{base}-{counter}" in used:
            counter += 1
        return f"{base}-{counter}"
