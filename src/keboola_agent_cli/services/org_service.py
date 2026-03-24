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

logger = logging.getLogger(__name__)

ManageClientFactory = Callable[[str, str], ManageClient]
StorageClientFactory = Callable[[str, str], KeboolaClient]


def default_manage_client_factory(stack_url: str, manage_token: str) -> ManageClient:
    """Create a ManageClient with the given stack URL and manage token."""
    return ManageClient(stack_url=stack_url, manage_token=manage_token)


def default_storage_client_factory(stack_url: str, token: str) -> KeboolaClient:
    """Create a KeboolaClient with the given stack URL and storage token."""
    return KeboolaClient(stack_url=stack_url, token=token)


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
        self._storage_client_factory = storage_client_factory or default_storage_client_factory

    def setup_organization(
        self,
        stack_url: str,
        manage_token: str,
        org_id: int,
        token_description: str = DEFAULT_TOKEN_DESCRIPTION,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Set up all projects from a Keboola organization.

        Lists all projects in the organization, creates Storage API tokens
        for new ones, verifies them, and registers them in the config store.

        Args:
            stack_url: Keboola stack URL.
            manage_token: Manage API token.
            org_id: Organization ID.
            token_description: Description prefix for created tokens.
            dry_run: If True, only preview what would happen without making changes.

        Returns:
            Dict with setup results including added, skipped, and failed projects.
        """
        manage_client = self._manage_client_factory(stack_url, manage_token)
        try:
            projects = manage_client.list_organization_projects(org_id)

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
        failed: list[dict[str, Any]] = []

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
        }

    def _setup_single_project(
        self,
        stack_url: str,
        manage_token: str,
        project_id: int,
        project_name: str,
        alias: str,
        token_description: str,
        owner_name: str = "",
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

        # Register the project in config
        project_config = ProjectConfig(
            stack_url=stack_url,
            token=storage_token,
            project_name=token_info.project_name,
            project_id=token_info.project_id,
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
