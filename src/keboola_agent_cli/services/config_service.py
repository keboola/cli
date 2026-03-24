"""Configuration listing service - business logic for listing and detailing configs.

Orchestrates multi-project configuration retrieval in parallel, filtering,
aggregation, and full-text search without knowing about CLI or HTTP details.
"""

import re
from typing import Any

from ..errors import KeboolaApiError
from ..models import ProjectConfig
from .base import BaseService


def _find_matches_in_json(
    obj: Any,
    match_fn: Any,
    path: str = "",
) -> list[str]:
    """Recursively walk a JSON-like object and return paths where match_fn(str_value) is True."""
    paths: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child_path = f"{path}.{key}" if path else key
            paths.extend(_find_matches_in_json(value, match_fn, child_path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            child_path = f"{path}[{i}]"
            paths.extend(_find_matches_in_json(item, match_fn, child_path))
    elif isinstance(obj, str):
        if match_fn(obj):
            paths.append(path)
    else:
        # Numbers, booleans -- convert to string for matching
        if obj is not None and match_fn(str(obj)):
            paths.append(path)
    return paths


class ConfigService(BaseService):
    """Business logic for listing and inspecting Keboola configurations.

    Supports multi-project aggregation: queries multiple projects in parallel
    using ThreadPoolExecutor, collects results, and reports per-project errors
    without stopping others.

    Uses dependency injection for config_store and client_factory.
    """

    def _fetch_project_configs(
        self,
        alias: str,
        project: ProjectConfig,
        component_type: str | None = None,
        component_id: str | None = None,
    ) -> tuple[str, list[dict[str, Any]], bool] | tuple[str, dict[str, str]]:
        """Fetch configurations for a single project (runs in a worker thread).

        Creates its own KeboolaClient, fetches components and configs, then
        closes the client. Returns either (alias, configs_list, True) on success
        or (alias, error_dict) on failure. The 3-tuple convention is required
        by _run_parallel() which uses tuple length to distinguish success/error.
        """
        client = self._client_factory(project.stack_url, project.token)
        try:
            components = client.list_components(component_type=component_type)

            # Fetch folder metadata (requires branch ID — search endpoint is branch-only)
            folder_map: dict[str, str] = {}
            try:
                # Use active branch or find the default branch ID
                branch_id = project.active_branch_id
                if not branch_id:
                    # Fetch default branch ID from dev-branches endpoint
                    branches = client.list_dev_branches()
                    default = next((b for b in branches if b.get("isDefault")), None)
                    if default:
                        branch_id = default["id"]
                if branch_id:
                    result = client.list_config_folder_metadata(branch_id=branch_id)
                    folder_map = result if isinstance(result, dict) else {}
            except Exception:
                pass  # graceful fallback if search endpoint unavailable

            configs: list[dict[str, Any]] = []
            for component in components:
                comp_id = component.get("id", "")
                comp_name = component.get("name", "")
                comp_type = component.get("type", "")

                # Apply component_id filter if specified
                if component_id and comp_id != component_id:
                    continue

                configurations = component.get("configurations", [])
                for cfg in configurations:
                    # Extract last-modified info from currentVersion
                    current_version = cfg.get("currentVersion", {})
                    creator_token = current_version.get("creatorToken", {})
                    cfg_id = str(cfg.get("id", ""))

                    configs.append(
                        {
                            "project_alias": alias,
                            "component_id": comp_id,
                            "component_name": comp_name,
                            "component_type": comp_type,
                            "config_id": cfg_id,
                            "config_name": cfg.get("name", ""),
                            "config_description": cfg.get("description", ""),
                            "last_modified": current_version.get("created", ""),
                            "last_modified_by": creator_token.get("description", ""),
                            "last_change_description": current_version.get("changeDescription", ""),
                            "folder": folder_map.get(f"{comp_id}/{cfg_id}", ""),
                        }
                    )
            return (alias, configs, True)
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

    def list_configs(
        self,
        aliases: list[str] | None = None,
        component_type: str | None = None,
        component_id: str | None = None,
    ) -> dict[str, Any]:
        """List configurations across one or multiple projects.

        Queries each resolved project for components and their configurations
        in parallel, flattens them into a unified list. Per-project errors are
        collected but do not stop other projects from being queried.

        Args:
            aliases: Project aliases to query. None means all projects.
            component_type: Optional filter by component type
                (extractor, writer, transformation, application).
            component_id: Optional filter by specific component ID
                (e.g. keboola.ex-db-snowflake).

        Returns:
            Dict with keys:
                - "configs": list of config dicts with project_alias,
                  component_id, component_name, component_type,
                  config_id, config_name, config_description
                - "errors": list of error dicts with project_alias,
                  error_code, message

        Raises:
            ConfigError: If a specified alias is not found (before querying).
        """
        projects = self.resolve_projects(aliases)

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            return self._fetch_project_configs(alias, project, component_type, component_id)

        successes, errors = self._run_parallel(projects, worker)

        # Flatten configs from all successful projects
        all_configs: list[dict[str, Any]] = []
        for _alias, configs, _ok in successes:
            all_configs.extend(configs)

        # Sort for deterministic output
        all_configs.sort(key=lambda c: (c["project_alias"], c["component_id"], c["config_id"]))
        errors.sort(key=lambda e: e.get("project_alias", ""))

        return {"configs": all_configs, "errors": errors}

    def get_config_detail(
        self,
        alias: str,
        component_id: str,
        config_id: str,
    ) -> dict[str, Any]:
        """Get detailed information about a specific configuration.

        Args:
            alias: Project alias to query.
            component_id: The component ID (e.g. keboola.ex-db-snowflake).
            config_id: The configuration ID.

        Returns:
            Dict with the full configuration detail from the API,
            plus a "project_alias" key.

        Raises:
            ConfigError: If the alias is not found.
            KeboolaApiError: If the API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            detail = client.get_config_detail(component_id, config_id)
        finally:
            client.close()

        detail["project_alias"] = alias
        return detail

    def update_config(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        name: str | None = None,
        description: str | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Update a configuration's name and/or description.

        Args:
            alias: Project alias.
            component_id: The component ID.
            config_id: The configuration ID to update.
            name: New name (if None, not changed).
            description: New description (if None, not changed).
            branch_id: If set, update in a specific dev branch.
                       If None, uses the project's active branch (if any).

        Returns:
            Dict with the updated configuration from the API.

        Raises:
            ConfigError: If the alias is not found.
            KeboolaApiError: If the API call fails.
        """
        if name is None and description is None:
            raise KeboolaApiError(
                status_code=400,
                error_code="VALIDATION_ERROR",
                message="At least one of --name or --description must be provided.",
            )

        projects = self.resolve_projects([alias])
        project = projects[alias]

        effective_branch_id = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            result = client.update_config(
                component_id=component_id,
                config_id=config_id,
                name=name,
                description=description,
                change_description="Updated via kbagent config update",
                branch_id=effective_branch_id,
            )
        finally:
            client.close()

        result["project_alias"] = alias
        result["branch_id"] = effective_branch_id
        return result

    def delete_config(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Delete a configuration from a project.

        Args:
            alias: Project alias.
            component_id: The component ID (e.g. keboola.python-transformation-v2).
            config_id: The configuration ID to delete.
            branch_id: If set, delete from a specific dev branch.
                       If None, uses the project's active branch (if any).

        Returns:
            Dict with deletion confirmation details.

        Raises:
            ConfigError: If the alias is not found.
            KeboolaApiError: If the API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        # Use active branch if no explicit branch_id given
        effective_branch_id = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            client.delete_config(
                component_id=component_id,
                config_id=config_id,
                branch_id=effective_branch_id,
            )
        finally:
            client.close()

        return {
            "status": "deleted",
            "project_alias": alias,
            "component_id": component_id,
            "config_id": config_id,
            "branch_id": effective_branch_id,
        }

    def search_configs(
        self,
        query: str,
        aliases: list[str] | None = None,
        component_type: str | None = None,
        component_id: str | None = None,
        ignore_case: bool = False,
        use_regex: bool = False,
    ) -> dict[str, Any]:
        """Search through configuration bodies across projects.

        Fetches all configurations (including the full JSON body) and searches
        for a query string. Reports which configs match and WHERE in the JSON
        tree the match was found.

        Args:
            query: Search string (plain substring or regex).
            aliases: Project aliases to query. None means all projects.
            component_type: Optional filter by component type.
            component_id: Optional filter by specific component ID.
            ignore_case: If True, match case-insensitively.
            use_regex: If True, interpret query as a regular expression.

        Returns:
            Dict with "matches", "errors", and "stats" keys.
        """
        # Compile the match function once
        if use_regex:
            flags = re.IGNORECASE if ignore_case else 0
            pattern = re.compile(query, flags)
            match_fn = lambda s: pattern.search(s) is not None  # noqa: E731
        elif ignore_case:
            query_lower = query.lower()
            match_fn = lambda s: query_lower in s.lower()  # noqa: E731
        else:
            match_fn = lambda s: query in s  # noqa: E731

        projects = self.resolve_projects(aliases)

        def worker(
            alias: str, project: ProjectConfig
        ) -> tuple[str, dict[str, Any], bool] | tuple[str, dict[str, str]]:
            return self._search_project_configs(
                alias, project, match_fn, component_type, component_id
            )

        successes, errors = self._run_parallel(projects, worker)

        all_matches: list[dict[str, Any]] = []
        total_configs = 0
        for _alias, result, _ok in successes:
            all_matches.extend(result["matches"])
            total_configs += result["configs_searched"]

        all_matches.sort(key=lambda m: (m["project_alias"], m["component_id"], m["config_id"]))
        errors.sort(key=lambda e: e.get("project_alias", ""))

        return {
            "matches": all_matches,
            "errors": errors,
            "stats": {
                "projects_searched": len(successes),
                "configs_searched": total_configs,
                "matches_found": len(all_matches),
            },
        }

    def _search_project_configs(
        self,
        alias: str,
        project: ProjectConfig,
        match_fn: Any,
        component_type: str | None = None,
        component_id: str | None = None,
    ) -> tuple[str, dict[str, Any], bool] | tuple[str, dict[str, str]]:
        """Search configs in a single project (worker thread)."""
        client = self._client_factory(project.stack_url, project.token)
        try:
            components = client.list_components(component_type=component_type)
            matches: list[dict[str, Any]] = []
            configs_searched = 0

            for component in components:
                comp_id = component.get("id", "")
                comp_name = component.get("name", "")
                comp_type = component.get("type", "")

                if component_id and comp_id != component_id:
                    continue

                for cfg in component.get("configurations", []):
                    configs_searched += 1
                    match_locations = _find_matches_in_json(cfg, match_fn)

                    if match_locations:
                        matches.append(
                            {
                                "project_alias": alias,
                                "component_id": comp_id,
                                "component_name": comp_name,
                                "component_type": comp_type,
                                "config_id": str(cfg.get("id", "")),
                                "config_name": cfg.get("name", ""),
                                "config_description": cfg.get("description", ""),
                                "match_locations": match_locations,
                                "match_count": len(match_locations),
                            }
                        )

            return (alias, {"matches": matches, "configs_searched": configs_searched}, True)
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
