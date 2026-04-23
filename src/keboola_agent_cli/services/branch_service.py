"""Branch service - business logic for branch lifecycle management.

Orchestrates multi-project branch retrieval in parallel, annotates with
project alias, and aggregates results. Provides create, activate, reset,
delete, and merge URL generation for development branches. Also provides
branch metadata CRUD and a "project description" convenience wrapper over
the ``KBC.projectDescription`` metadata key on the default branch.
"""

from typing import Any

from ..constants import METADATA_NOT_FOUND
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..models import ProjectConfig
from .base import BaseService

PROJECT_DESCRIPTION_KEY = "KBC.projectDescription"


class BranchService(BaseService):
    """Business logic for managing Keboola development branches.

    Supports multi-project aggregation: queries multiple projects in parallel
    using ThreadPoolExecutor, collects results, and reports per-project errors
    without stopping others.

    Provides branch lifecycle operations: create, activate (use), reset,
    delete, and merge URL generation.

    Uses dependency injection for config_store and client_factory.
    """

    def _fetch_project_branches(
        self,
        alias: str,
        project: ProjectConfig,
    ) -> tuple[str, list[dict[str, Any]], bool] | tuple[str, dict[str, str]]:
        """Fetch development branches for a single project (runs in a worker thread).

        Returns either (alias, branches_list, True) on success
        or (alias, error_dict) on failure.
        """
        client = self._client_factory(project.stack_url, project.token)
        try:
            raw_branches = client.list_dev_branches()
            branches: list[dict[str, Any]] = []
            for branch in raw_branches:
                branches.append(
                    {
                        "project_alias": alias,
                        "id": branch.get("id"),
                        "name": branch.get("name", ""),
                        "isDefault": branch.get("isDefault", False),
                        "created": branch.get("created", ""),
                        "description": branch.get("description", ""),
                    }
                )
            return (alias, branches, True)
        except KeboolaApiError as exc:
            return (
                alias,
                {
                    "project_alias": alias,
                    "error_code": exc.error_code,
                    "message": exc.message,
                },
            )
        except Exception as exc:
            return (
                alias,
                {
                    "project_alias": alias,
                    "error_code": "UNEXPECTED_ERROR",
                    "message": str(exc),
                },
            )
        finally:
            client.close()

    def list_branches(
        self,
        aliases: list[str] | None = None,
    ) -> dict[str, Any]:
        """List development branches across one or multiple projects.

        Queries each resolved project for development branches in parallel,
        flattens them into a unified list. Per-project errors are collected
        but do not stop other projects from being queried.

        Includes active_branches dict mapping alias -> active_branch_id for
        display purposes.

        Args:
            aliases: Project aliases to query. None means all projects.

        Returns:
            Dict with keys:
                - "branches": list of branch dicts with project_alias,
                  id, name, isDefault, created, description
                - "errors": list of error dicts with project_alias,
                  error_code, message
                - "active_branches": dict mapping alias -> active_branch_id

        Raises:
            ConfigError: If a specified alias is not found (before querying).
        """
        projects = self.resolve_projects(aliases)

        # Collect active branch IDs for display
        active_branches: dict[str, int | None] = {}
        for alias, project in projects.items():
            active_branches[alias] = project.active_branch_id

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            return self._fetch_project_branches(alias, project)

        successes, errors = self._run_parallel(projects, worker)

        # Flatten branches from all successful projects
        all_branches: list[dict[str, Any]] = []
        for _alias, branches, _ok in successes:
            all_branches.extend(branches)

        # Sort for deterministic output
        all_branches.sort(key=lambda b: (b["project_alias"], b.get("id", 0)))
        errors.sort(key=lambda e: e.get("project_alias", ""))

        return {
            "branches": all_branches,
            "errors": errors,
            "active_branches": active_branches,
        }

    def create_branch(
        self,
        alias: str,
        name: str,
        description: str = "",
    ) -> dict[str, Any]:
        """Create a new development branch and auto-activate it.

        Args:
            alias: Project alias.
            name: Branch name.
            description: Optional branch description.

        Returns:
            Dict with branch details and activation info.

        Raises:
            ConfigError: If the project alias is not found.
            KeboolaApiError: If the API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            # create_dev_branch waits for the async job and returns branch data
            # from job.results (contains the real branch ID)
            branch_data = client.create_dev_branch(name=name, description=description)
        finally:
            client.close()

        branch_id = int(branch_data["id"])

        # Auto-activate the created branch
        self._config_store.set_project_branch(alias, branch_id)

        return {
            "project_alias": alias,
            "branch_id": branch_id,
            "branch_name": branch_data.get("name", name),
            "description": branch_data.get("description", description),
            "created": branch_data.get("created", ""),
            "activated": True,
            "message": (
                f"Branch '{name}' (ID: {branch_id}) created and activated for project '{alias}'."
            ),
        }

    def set_active_branch(self, alias: str, branch_id: int) -> dict[str, Any]:
        """Validate and set an existing branch as active.

        Calls the API to verify the branch exists before setting it.

        Args:
            alias: Project alias.
            branch_id: Branch ID to activate.

        Returns:
            Dict with activation details.

        Raises:
            ConfigError: If the project alias is not found or branch does not exist.
            KeboolaApiError: If the API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            raw_branches = client.list_dev_branches()
        finally:
            client.close()

        # Find the branch by ID
        target_branch = None
        for branch in raw_branches:
            if branch.get("id") == branch_id:
                target_branch = branch
                break

        if target_branch is None:
            raise ConfigError(
                f"Branch ID {branch_id} not found in project '{alias}'. "
                f"Use 'kbagent branch list --project {alias}' to see available branches."
            )

        self._config_store.set_project_branch(alias, branch_id)

        branch_name = target_branch.get("name", "")
        return {
            "project_alias": alias,
            "branch_id": branch_id,
            "branch_name": branch_name,
            "message": (
                f"Active branch set to '{branch_name}' (ID: {branch_id}) for project '{alias}'."
            ),
        }

    def reset_branch(self, alias: str) -> dict[str, Any]:
        """Clear the active branch, reverting to the main/production branch.

        Args:
            alias: Project alias.

        Returns:
            Dict confirming the reset.

        Raises:
            ConfigError: If the project alias is not found.
        """
        projects = self.resolve_projects([alias])
        previous_branch = projects[alias].active_branch_id

        self._config_store.set_project_branch(alias, None)

        return {
            "project_alias": alias,
            "previous_branch_id": previous_branch,
            "message": (f"Active branch reset to main for project '{alias}'."),
        }

    def delete_branch(self, alias: str, branch_id: int) -> dict[str, Any]:
        """Delete a development branch via API. Auto-resets if it was active.

        Args:
            alias: Project alias.
            branch_id: Branch ID to delete.

        Returns:
            Dict confirming the deletion.

        Raises:
            ConfigError: If the project alias is not found.
            KeboolaApiError: If the API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            client.delete_dev_branch(branch_id)
        finally:
            client.close()

        # Auto-reset if the deleted branch was active
        was_active = project.active_branch_id == branch_id
        if was_active:
            self._config_store.set_project_branch(alias, None)

        return {
            "project_alias": alias,
            "branch_id": branch_id,
            "was_active": was_active,
            "message": (
                f"Branch ID {branch_id} deleted from project '{alias}'."
                + (" Active branch reset to main." if was_active else "")
            ),
        }

    def get_merge_url(self, alias: str, branch_id: int | None = None) -> dict[str, Any]:
        """Generate KBC UI merge URL for a development branch.

        Does not call any API. Constructs the URL from stored project config.
        If no branch_id is provided, uses the active branch from config.
        After generating the URL, resets the active branch to main.

        Args:
            alias: Project alias.
            branch_id: Branch ID. If None, uses active_branch_id from config.

        Returns:
            Dict with merge URL and instructions.

        Raises:
            ConfigError: If project not found or no branch ID available.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        effective_branch_id = branch_id if branch_id is not None else project.active_branch_id
        if effective_branch_id is None:
            raise ConfigError(
                f"No branch specified and no active branch set for project '{alias}'. "
                f"Use --branch ID or set an active branch with 'kbagent branch use'."
            )

        if project.project_id is None:
            raise ConfigError(
                f"Project '{alias}' has no project_id stored. "
                "Re-add the project with 'kbagent project edit' to populate it."
            )

        stack_url = project.stack_url.rstrip("/")
        merge_url = (
            f"{stack_url}/admin/projects/{project.project_id}"
            f"/branch/{effective_branch_id}/development-overview"
        )

        # Reset active branch to main after generating merge URL
        self._config_store.set_project_branch(alias, None)

        return {
            "project_alias": alias,
            "branch_id": effective_branch_id,
            "url": merge_url,
            "message": (
                f"Open this URL to review and merge branch {effective_branch_id} "
                f"in project '{alias}'. Active branch has been reset to main."
            ),
        }

    # ── Branch metadata ────────────────────────────────────────────────

    def list_branch_metadata(
        self,
        alias: str,
        branch_id: int | str = "default",
    ) -> dict[str, Any]:
        """List all metadata entries on a branch for a single project.

        Args:
            alias: Project alias.
            branch_id: Branch ID or "default" for the main branch.

        Returns:
            Dict with project_alias, branch_id, and a key-sorted metadata list.

        Raises:
            ConfigError: If the project alias is not found.
            KeboolaApiError: If the API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            entries = client.list_branch_metadata(branch_id=branch_id)
        finally:
            client.close()
        return {
            "project_alias": alias,
            "branch_id": branch_id,
            "metadata": sorted(entries, key=lambda e: e.get("key", "")),
        }

    def get_branch_metadata(
        self,
        alias: str,
        key: str,
        branch_id: int | str = "default",
    ) -> dict[str, Any]:
        """Get a single metadata value by key.

        Args:
            alias: Project alias.
            key: Metadata key (e.g. "KBC.projectDescription").
            branch_id: Branch ID or "default" for the main branch.

        Returns:
            Dict with project_alias, branch_id, key, value.

        Raises:
            ConfigError: If the project alias is not found.
            KeboolaApiError: If the key is not present or the API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            value = client.get_branch_metadata_value(key=key, branch_id=branch_id)
        finally:
            client.close()
        if value is METADATA_NOT_FOUND:
            raise KeboolaApiError(
                message=(
                    f"Metadata key '{key}' not found on branch '{branch_id}' of project '{alias}'."
                ),
                status_code=404,
                error_code=ErrorCode.NOT_FOUND,
                retryable=False,
            )
        return {
            "project_alias": alias,
            "branch_id": branch_id,
            "key": key,
            "value": value,
        }

    def set_branch_metadata(
        self,
        alias: str,
        key: str,
        value: str,
        branch_id: int | str = "default",
    ) -> dict[str, Any]:
        """Set a single metadata key/value on a branch.

        Args:
            alias: Project alias.
            key: Metadata key to set.
            value: Metadata value (plain string; e.g. markdown).
            branch_id: Branch ID or "default" for the main branch.

        Returns:
            Dict with project_alias, branch_id, key, value, result, message.

        Raises:
            ConfigError: If the project alias is not found.
            KeboolaApiError: If the API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            result = client.set_branch_metadata(entries=[(key, value)], branch_id=branch_id)
        finally:
            client.close()
        return {
            "project_alias": alias,
            "branch_id": branch_id,
            "key": key,
            "value": value,
            "result": result,
            "message": (f"Metadata '{key}' set on branch '{branch_id}' of project '{alias}'."),
        }

    def delete_branch_metadata(
        self,
        alias: str,
        metadata_id: int | str,
        branch_id: int | str = "default",
    ) -> dict[str, Any]:
        """Delete a metadata entry by its numeric ID.

        Args:
            alias: Project alias.
            metadata_id: Metadata entry ID (from ``list_branch_metadata``).
            branch_id: Branch ID or "default" for the main branch.

        Returns:
            Dict confirming the deletion.

        Raises:
            ConfigError: If the project alias is not found.
            KeboolaApiError: If the API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            client.delete_branch_metadata(metadata_id=metadata_id, branch_id=branch_id)
        finally:
            client.close()
        return {
            "project_alias": alias,
            "branch_id": branch_id,
            "metadata_id": metadata_id,
            "message": (
                f"Metadata ID {metadata_id} deleted from branch '{branch_id}' of project '{alias}'."
            ),
        }

    # ── Project description (convenience wrappers) ─────────────────────

    def get_project_description(self, alias: str) -> dict[str, Any]:
        """Get the dashboard project description for a project.

        Reads the ``KBC.projectDescription`` metadata value from the default
        branch -- this is what the Keboola UI displays on the project
        dashboard.

        Returns an empty description (not an error) if the key is unset, so
        callers don't need to special-case freshly-provisioned projects.

        Args:
            alias: Project alias.

        Returns:
            Dict with project_alias, key, description (str, possibly empty).

        Raises:
            ConfigError: If the project alias is not found.
            KeboolaApiError: If the API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            value = client.get_branch_metadata_value(
                key=PROJECT_DESCRIPTION_KEY, branch_id="default"
            )
        finally:
            client.close()
        return {
            "project_alias": alias,
            "key": PROJECT_DESCRIPTION_KEY,
            "description": "" if value is METADATA_NOT_FOUND else (value or ""),
        }

    def set_project_description(self, alias: str, description: str) -> dict[str, Any]:
        """Set the dashboard project description for a project.

        Writes to ``KBC.projectDescription`` on the default branch, which is
        what the Keboola UI reads for the project dashboard description.

        Args:
            alias: Project alias.
            description: Markdown content for the description.

        Returns:
            Dict with project_alias, key, description, result, message.

        Raises:
            ConfigError: If the project alias is not found.
            KeboolaApiError: If the API call fails.
        """
        raw = self.set_branch_metadata(
            alias=alias,
            key=PROJECT_DESCRIPTION_KEY,
            value=description,
            branch_id="default",
        )
        return {
            "project_alias": alias,
            "key": PROJECT_DESCRIPTION_KEY,
            "description": description,
            "result": raw["result"],
            "message": f"Project description updated for '{alias}' ({len(description)} chars).",
        }
