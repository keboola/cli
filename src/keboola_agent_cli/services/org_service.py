"""Organization setup service - bulk onboarding of all projects in a Keboola org.

Orchestrates the Manage API (list projects, create tokens) and the Storage API
(verify tokens) to register all organization projects in one shot.
"""

import logging
import re
from collections.abc import Callable
from typing import Any

from ..client import KeboolaClient
from ..config_store import ConfigStore
from ..constants import DEFAULT_TOKEN_DESCRIPTION
from ..errors import KeboolaApiError, mask_token
from ..manage_client import ManageClient
from ..models import ProjectConfig
from .base import default_client_factory, make_client_factory

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
                self._setup_single_project(
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
                    {
                        "project_id": project_id,
                        "project_name": project_name,
                        "alias": alias,
                        "error": str(exc),
                    }
                )
            except Exception as exc:
                failed.append(
                    {
                        "project_id": project_id,
                        "project_name": project_name,
                        "alias": alias,
                        "error": str(exc),
                    }
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
        }

    def refresh_tokens(
        self,
        manage_token: str,
        aliases: list[str] | None = None,
        token_description: str = DEFAULT_TOKEN_DESCRIPTION,
        dry_run: bool = False,
        token_expires_in: int | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Refresh storage API tokens for registered projects.

        Checks each project's token validity and creates new tokens for
        projects with expired or invalid tokens. Already-valid tokens are
        skipped unless ``force=True``.

        Args:
            manage_token: Manage API token (for creating new storage tokens).
            aliases: Optional list of project aliases to refresh. If None, all
                projects are checked.
            token_description: Description prefix for newly created tokens.
            dry_run: If True, only preview what would happen without making changes.
            token_expires_in: Token lifetime in seconds. None means no expiration.
            force: If True, refresh all tokens even if they are still valid.

        Returns:
            Dict with refresh results including refreshed, valid, skipped,
            and failed projects.
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
                            "error": f"Project '{alias}' not found in config",
                        }
                    )
                else:
                    projects_to_check.append((alias, config.projects[alias]))
        else:
            projects_to_check = list(config.projects.items())

        if not projects_to_check:
            return {
                "dry_run": dry_run,
                "projects_checked": 0,
                "projects_refreshed": [],
                "projects_valid": [],
                "projects_skipped": [],
                "projects_failed": failed,
                "token_expires_in": token_expires_in,
            }

        # Resolve manage token owner identity for unique token naming
        owner_name = ""
        manage_client = self._manage_client_factory(
            projects_to_check[0][1].stack_url,
            manage_token,
        )
        try:
            token_info = manage_client.verify_token()
            user_info = token_info.get("user", {})
            owner_name = user_info.get("email") or user_info.get("name", "")
        except Exception:
            logger.debug("Could not resolve manage token owner identity")
        finally:
            manage_client.close()

        projects_refreshed: list[dict[str, Any]] = []
        projects_valid: list[dict[str, Any]] = []
        projects_skipped: list[dict[str, Any]] = []

        for alias, project in projects_to_check:
            # Skip projects without project_id (cannot create tokens via Manage API)
            if project.project_id is None:
                projects_skipped.append(
                    {
                        "alias": alias,
                        "project_name": project.project_name,
                        "reason": "Cannot refresh: project_id is missing",
                    }
                )
                continue

            # Check current token validity
            token_valid = False
            try:
                client = self._storage_client_factory(project.stack_url, project.token)
                try:
                    client.verify_token()
                    token_valid = True
                except KeboolaApiError as exc:
                    if exc.error_code == "INVALID_TOKEN":
                        token_valid = False
                    else:
                        raise
                finally:
                    client.close()
            except KeboolaApiError:
                token_valid = False
            except Exception as exc:
                failed.append(
                    {
                        "alias": alias,
                        "project_name": project.project_name,
                        "error": f"Error checking token: {exc}",
                    }
                )
                continue

            # Valid token and not forcing refresh
            if token_valid and not force:
                projects_valid.append(
                    {
                        "alias": alias,
                        "project_id": project.project_id,
                        "project_name": project.project_name,
                    }
                )
                continue

            # Token needs refresh (or force=True)
            if dry_run:
                projects_refreshed.append(
                    {
                        "alias": alias,
                        "project_id": project.project_id,
                        "project_name": project.project_name,
                        "token": "***",
                        "action": "would_refresh",
                    }
                )
                continue

            try:
                new_token = self._refresh_single_project(
                    manage_token=manage_token,
                    alias=alias,
                    project=project,
                    token_description=token_description,
                    owner_name=owner_name,
                    token_expires_in=token_expires_in,
                )
                projects_refreshed.append(
                    {
                        "alias": alias,
                        "project_id": project.project_id,
                        "project_name": project.project_name,
                        "token": mask_token(new_token),
                        "action": "refreshed",
                    }
                )
            except Exception as exc:
                failed.append(
                    {
                        "alias": alias,
                        "project_name": project.project_name,
                        "error": str(exc),
                    }
                )

        total_count = len(projects_to_check)

        return {
            "dry_run": dry_run,
            "projects_checked": total_count,
            "projects_refreshed": projects_refreshed,
            "projects_valid": projects_valid,
            "projects_skipped": projects_skipped,
            "projects_failed": failed,
            "token_expires_in": token_expires_in,
        }

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

        # Update the project in config
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
                    {
                        "project_id": pid,
                        "project_name": f"project-{pid}",
                        "alias": f"project-{pid}",
                        "error": str(exc),
                    }
                )
            except Exception as exc:
                failed.append(
                    {
                        "project_id": pid,
                        "project_name": f"project-{pid}",
                        "alias": f"project-{pid}",
                        "error": str(exc),
                    }
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
    ) -> None:
        """Create a token for a single project, verify it, and register it.

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
        self._config_store.add_project(alias, project_config)

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
