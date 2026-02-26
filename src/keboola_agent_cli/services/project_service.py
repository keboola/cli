"""Project management service - business logic for add/remove/edit/list/status.

Orchestrates config persistence and API calls without knowing about CLI or HTTP details.
"""

import time
from collections.abc import Callable
from typing import Any

from ..client import KeboolaClient
from ..config_store import ConfigStore
from ..errors import ConfigError, KeboolaApiError, mask_token
from ..models import ProjectConfig

ClientFactory = Callable[[str, str], KeboolaClient]


def default_client_factory(stack_url: str, token: str) -> KeboolaClient:
    """Create a KeboolaClient with the given stack URL and token."""
    return KeboolaClient(stack_url=stack_url, token=token)


class ProjectService:
    """Business logic for managing Keboola project connections.

    Uses dependency injection for config_store and client_factory to enable
    easy testing with mocks.
    """

    def __init__(
        self,
        config_store: ConfigStore,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._config_store = config_store
        self._client_factory = client_factory or default_client_factory

    def add_project(self, alias: str, stack_url: str, token: str) -> dict[str, Any]:
        """Add a new project connection after verifying the token.

        Calls the Keboola API to verify the token and extract project info,
        then saves the project to the config store.

        Args:
            alias: Human-friendly project name.
            stack_url: Keboola stack URL.
            token: Storage API token.

        Returns:
            Dict with project details (alias, project_name, project_id, stack_url, masked_token).

        Raises:
            KeboolaApiError: If token verification fails.
            ConfigError: If the alias already exists.
        """
        client = self._client_factory(stack_url, token)
        try:
            token_info = client.verify_token()
        finally:
            client.close()

        project = ProjectConfig(
            stack_url=stack_url,
            token=token,
            project_name=token_info.project_name,
            project_id=token_info.project_id,
        )

        self._config_store.add_project(alias, project)

        return {
            "alias": alias,
            "project_name": token_info.project_name,
            "project_id": token_info.project_id,
            "stack_url": stack_url,
            "token": mask_token(token),
        }

    def remove_project(self, alias: str) -> dict[str, str]:
        """Remove a project from the configuration.

        Args:
            alias: The project alias to remove.

        Returns:
            Dict confirming the removal.

        Raises:
            ConfigError: If the alias does not exist.
        """
        self._config_store.remove_project(alias)
        return {"alias": alias, "message": f"Project '{alias}' removed."}

    def edit_project(
        self,
        alias: str,
        stack_url: str | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Edit an existing project's configuration.

        If the token is changed, re-verifies it against the API to update
        project name and ID.

        Args:
            alias: The project alias to edit.
            stack_url: New stack URL (if changing).
            token: New token (if changing).

        Returns:
            Dict with updated project details.

        Raises:
            KeboolaApiError: If token re-verification fails.
            ConfigError: If the alias does not exist or no changes provided.
        """
        existing = self._config_store.get_project(alias)
        if existing is None:
            raise ConfigError(f"Project '{alias}' not found.")

        if stack_url is None and token is None:
            raise ConfigError("No changes specified. Provide --url and/or --token.")

        updates: dict[str, str | int] = {}

        if stack_url is not None:
            updates["stack_url"] = stack_url

        if token is not None:
            effective_url = stack_url if stack_url is not None else existing.stack_url
            client = self._client_factory(effective_url, token)
            try:
                token_info = client.verify_token()
            finally:
                client.close()
            updates["token"] = token
            updates["project_name"] = token_info.project_name
            updates["project_id"] = token_info.project_id

        self._config_store.edit_project(alias, **updates)

        updated = self._config_store.get_project(alias)
        assert updated is not None  # we just edited it

        return {
            "alias": alias,
            "project_name": updated.project_name,
            "project_id": updated.project_id,
            "stack_url": updated.stack_url,
            "token": mask_token(updated.token),
        }

    def list_projects(self) -> list[dict[str, Any]]:
        """List all configured projects.

        Returns:
            List of dicts with project details (token masked).
        """
        config = self._config_store.load()
        result = []
        for alias, project in config.projects.items():
            result.append(
                {
                    "alias": alias,
                    "project_name": project.project_name,
                    "project_id": project.project_id,
                    "stack_url": project.stack_url,
                    "token": mask_token(project.token),
                    "is_default": alias == config.default_project,
                }
            )
        return result

    def get_status(self, aliases: list[str] | None = None) -> list[dict[str, Any]]:
        """Check connectivity status for one or more projects.

        For each project, verifies the token against the API and measures
        response time.

        Args:
            aliases: Specific project aliases to check (None = all projects).

        Returns:
            List of dicts with status, response time, and project details.

        Raises:
            ConfigError: If a specified alias does not exist.
        """
        config = self._config_store.load()

        if aliases:
            projects_to_check = {}
            for alias in aliases:
                if alias not in config.projects:
                    raise ConfigError(f"Project '{alias}' not found.")
                projects_to_check[alias] = config.projects[alias]
        else:
            projects_to_check = config.projects

        results = []
        for alias, project in projects_to_check.items():
            status_entry: dict[str, Any] = {
                "alias": alias,
                "stack_url": project.stack_url,
                "token": mask_token(project.token),
            }

            client = self._client_factory(project.stack_url, project.token)
            start_time = time.monotonic()
            try:
                token_info = client.verify_token()
                elapsed = time.monotonic() - start_time
                status_entry["status"] = "ok"
                status_entry["response_time_ms"] = round(elapsed * 1000)
                status_entry["project_name"] = token_info.project_name
                status_entry["project_id"] = token_info.project_id
            except KeboolaApiError as exc:
                elapsed = time.monotonic() - start_time
                status_entry["status"] = "error"
                status_entry["response_time_ms"] = round(elapsed * 1000)
                status_entry["error"] = exc.message
                status_entry["error_code"] = exc.error_code
            finally:
                client.close()

            results.append(status_entry)

        return results
