"""Configuration listing service - business logic for listing and detailing configs.

Orchestrates multi-project configuration retrieval in parallel, filtering,
aggregation, and full-text search without knowing about CLI or HTTP details.
"""

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..json_utils import compute_diff, deep_merge, set_nested_value
from ..models import ProjectConfig
from ..sync.manifest import Manifest, load_manifest, save_manifest
from ..sync.naming import sanitize_name
from .base import BaseService

logger = logging.getLogger(__name__)


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
        branch_id: int | None = None,
    ) -> tuple[str, list[dict[str, Any]], bool] | tuple[str, dict[str, str]]:
        """Fetch configurations for a single project (runs in a worker thread).

        Creates its own KeboolaClient, fetches components and configs, then
        closes the client. Returns either (alias, configs_list, True) on success
        or (alias, error_dict) on failure. The 3-tuple convention is required
        by _run_parallel() which uses tuple length to distinguish success/error.
        """
        client = self._client_factory(project.stack_url, project.token)
        try:
            effective_branch_id = branch_id or project.active_branch_id
            components = client.list_components(
                component_type=component_type,
                branch_id=effective_branch_id,
            )

            # Fetch folder metadata (requires branch ID — search endpoint is branch-only)
            folder_map: dict[str, str] = {}
            try:
                # Use effective branch, active branch, or find the default branch ID
                folder_branch_id = effective_branch_id
                if not folder_branch_id:
                    # Fetch default branch ID from dev-branches endpoint
                    branches = client.list_dev_branches()
                    default = next((b for b in branches if b.get("isDefault")), None)
                    if default:
                        folder_branch_id = default["id"]
                if folder_branch_id:
                    result = client.list_config_folder_metadata(branch_id=folder_branch_id)
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
        branch_id: int | None = None,
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
            branch_id: If set, list configs from a specific dev branch.
                       If None, uses each project's active branch (if any).

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
            return self._fetch_project_configs(
                alias, project, component_type, component_id, branch_id=branch_id
            )

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
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Get detailed information about a specific configuration.

        Args:
            alias: Project alias to query.
            component_id: The component ID (e.g. keboola.ex-db-snowflake).
            config_id: The configuration ID.
            branch_id: If set, get detail from a specific dev branch.
                       If None, uses the project's active branch (if any).

        Returns:
            Dict with the full configuration detail from the API,
            plus a "project_alias" key.

        Raises:
            ConfigError: If the alias is not found.
            KeboolaApiError: If the API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        effective_branch_id = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            detail = client.get_config_detail(
                component_id, config_id, branch_id=effective_branch_id
            )
        finally:
            client.close()

        detail["project_alias"] = alias
        detail["branch_id"] = effective_branch_id
        return detail

    def update_config(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        name: str | None = None,
        description: str | None = None,
        configuration: dict[str, Any] | None = None,
        set_paths: list[tuple[str, Any]] | None = None,
        merge: bool = False,
        dry_run: bool = False,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Update a configuration's metadata and/or content.

        Args:
            alias: Project alias.
            component_id: The component ID.
            config_id: The configuration ID to update.
            name: New name (if None, not changed).
            description: New description (if None, not changed).
            configuration: Full configuration dict to set/merge.
            set_paths: List of (path, value) tuples for targeted updates
                       (e.g. ``[("parameters.tables", {...})]``).
            merge: If True, deep-merge *configuration* or *set_paths* into
                   the existing config instead of replacing.  When using
                   *set_paths* merge is always implied.
            dry_run: If True, compute and return the diff without applying.
            branch_id: If set, update in a specific dev branch.
                       If None, uses the project's active branch (if any).

        Returns:
            Dict with the updated configuration from the API.
            When *dry_run* is True the dict contains ``"dry_run": True``
            and a ``"changes"`` list instead of the API response.

        Raises:
            ConfigError: If the alias is not found.
            KeboolaApiError: If the API call fails.
        """
        has_content = configuration is not None or bool(set_paths)
        has_metadata = name is not None or description is not None

        if not has_content and not has_metadata:
            raise KeboolaApiError(
                status_code=400,
                error_code=ErrorCode.VALIDATION_ERROR,
                message=(
                    "At least one of --name, --description, --configuration, "
                    "--configuration-file, or --set must be provided."
                ),
            )

        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch_id = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            final_config: dict[str, Any] | None = None

            if has_content:
                final_config = self._resolve_configuration(
                    client=client,
                    component_id=component_id,
                    config_id=config_id,
                    configuration=configuration,
                    set_paths=set_paths,
                    merge=merge,
                    branch_id=effective_branch_id,
                )

            if dry_run:
                current = client.get_config_detail(
                    component_id, config_id, branch_id=effective_branch_id
                )
                old_cfg = current.get("configuration", {})
                new_cfg = final_config if final_config is not None else old_cfg
                changes = compute_diff(old_cfg, new_cfg)
                return {
                    "dry_run": True,
                    "project_alias": alias,
                    "component_id": component_id,
                    "config_id": config_id,
                    "branch_id": effective_branch_id,
                    "changes": changes,
                    "old_configuration": old_cfg,
                    "new_configuration": new_cfg,
                }

            change_parts = []
            if has_metadata:
                change_parts.append("metadata")
            if has_content:
                change_parts.append("configuration")
            change_desc = f"Updated {' + '.join(change_parts)} via kbagent config update"

            result = client.update_config(
                component_id=component_id,
                config_id=config_id,
                name=name,
                description=description,
                configuration=final_config,
                change_description=change_desc,
                branch_id=effective_branch_id,
            )
        finally:
            client.close()

        result["project_alias"] = alias
        result["branch_id"] = effective_branch_id
        return result

    def _resolve_configuration(
        self,
        client: Any,
        component_id: str,
        config_id: str,
        configuration: dict[str, Any] | None,
        set_paths: list[tuple[str, Any]] | None,
        merge: bool,
        branch_id: int | None,
    ) -> dict[str, Any]:
        """Build the final configuration dict by merging/setting paths.

        When *merge* is True or *set_paths* are given, the current
        configuration is fetched from the API and changes are applied
        on top of it (deep-merge for dicts, replace for scalars/lists).
        """
        needs_current = merge or bool(set_paths)

        if needs_current:
            current_detail = client.get_config_detail(component_id, config_id, branch_id=branch_id)
            current_cfg: dict[str, Any] = current_detail.get("configuration", {})
            if isinstance(current_cfg, str):
                current_cfg = json.loads(current_cfg)
        else:
            current_cfg = {}

        if set_paths:
            result = current_cfg
            for path, value in set_paths:
                result = set_nested_value(result, path, value)
            if configuration:
                result = deep_merge(result, configuration)
            return result

        if merge and configuration:
            return deep_merge(current_cfg, configuration)

        # Full replace (no merge, no set_paths)
        return configuration if configuration is not None else current_cfg

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

    def rename_config(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        name: str,
        branch_id: int | None = None,
        directory: Path | None = None,
    ) -> dict[str, Any]:
        """Rename a configuration (update name via API + rename local sync dir).

        Args:
            alias: Project alias.
            component_id: The component ID.
            config_id: The configuration ID to rename.
            name: The new configuration name.
            branch_id: If set, rename in a specific dev branch.
                       If None, uses the project's active branch (if any).
            directory: Optional sync working directory. If a manifest exists
                       here and tracks this config, the local directory is
                       renamed and the manifest path is updated.

        Returns:
            Dict with old name, new name, and optional sync rename details.

        Raises:
            ConfigError: If the alias is not found.
            KeboolaApiError: If the API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch_id = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            # Fetch current state to get old name
            current = client.get_config_detail(
                component_id, config_id, branch_id=effective_branch_id
            )
            old_name = current.get("name", "")

            # Update name via API
            client.update_config(
                component_id=component_id,
                config_id=config_id,
                name=name,
                change_description=f"Renamed via kbagent config rename: {old_name} -> {name}",
                branch_id=effective_branch_id,
            )
        finally:
            client.close()

        result: dict[str, Any] = {
            "status": "renamed",
            "project_alias": alias,
            "component_id": component_id,
            "config_id": config_id,
            "old_name": old_name,
            "new_name": name,
            "branch_id": effective_branch_id,
        }

        # Attempt local sync directory rename if applicable
        sync_result = self._rename_sync_directory(
            directory=directory,
            component_id=component_id,
            config_id=config_id,
            new_name=name,
        )
        if sync_result:
            result["sync"] = sync_result

        return result

    def _rename_sync_directory(
        self,
        directory: Path | None,
        component_id: str,
        config_id: str,
        new_name: str,
    ) -> dict[str, str] | None:
        """Rename the local sync directory for a config if a manifest tracks it.

        Returns a dict with old_path/new_path on success, or None if no
        sync directory was found or rename was not needed.
        """
        if directory is None:
            return None

        from ..constants import KEBOOLA_DIR_NAME, MANIFEST_FILENAME

        manifest_path = directory / KEBOOLA_DIR_NAME / MANIFEST_FILENAME
        if not manifest_path.exists():
            return None

        try:
            manifest = load_manifest(directory)
        except (FileNotFoundError, ValueError):
            return None

        # Find the config entry in the manifest
        target_cfg = None
        for cfg in manifest.configurations:
            if cfg.component_id == component_id and cfg.id == config_id:
                target_cfg = cfg
                break

        if target_cfg is None:
            return None

        # Compute new path using the naming template
        old_path = target_cfg.path
        old_basename = old_path.rsplit("/", 1)[-1] if "/" in old_path else old_path
        new_basename = sanitize_name(new_name)

        if old_basename == new_basename:
            return None  # No rename needed

        # Build new path: replace only the last segment (config name)
        if "/" in old_path:
            parent = old_path.rsplit("/", 1)[0]
            new_path = f"{parent}/{new_basename}"
        else:
            new_path = new_basename

        # Collision detection: if target already exists, append numeric suffix
        branch_dir = self._find_sync_branch_dir(manifest, directory)
        if branch_dir is None:
            return None

        target_dir = branch_dir / new_path
        if target_dir.exists():
            counter = 2
            while (branch_dir / f"{new_path}-{counter}").exists():
                counter += 1
            new_path = f"{new_path}-{counter}"
            target_dir = branch_dir / new_path

        # Perform the rename
        source_dir = branch_dir / old_path
        if not source_dir.exists():
            # Directory doesn't exist locally, just update manifest
            target_cfg.path = new_path
            target_cfg.metadata.pop("pull_hash", None)
            target_cfg.metadata.pop("pull_config_hash", None)
            save_manifest(directory, manifest)
            return {"old_path": old_path, "new_path": new_path, "method": "manifest_only"}

        # Try git mv first for cleaner history, fall back to shutil.move
        method = self._move_directory(source_dir, target_dir)

        # Update manifest
        target_cfg.path = new_path
        target_cfg.metadata.pop("pull_hash", None)
        target_cfg.metadata.pop("pull_config_hash", None)
        save_manifest(directory, manifest)

        # Clean up empty parent directories
        parent_dir = source_dir.parent
        while parent_dir != branch_dir and parent_dir.exists():
            if not any(parent_dir.iterdir()):
                parent_dir.rmdir()
                parent_dir = parent_dir.parent
            else:
                break

        return {"old_path": old_path, "new_path": new_path, "method": method}

    @staticmethod
    def _move_directory(source: Path, target: Path) -> str:
        """Move a directory, using git mv if in a git repo, else shutil.move."""
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                ["git", "mv", str(source), str(target)],
                cwd=source.parent,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                return "git_mv"
        except FileNotFoundError:
            pass  # git not installed
        shutil.move(str(source), str(target))
        return "shutil_move"

    @staticmethod
    def _find_sync_branch_dir(manifest: Manifest, project_root: Path) -> Path | None:
        """Find the branch directory within a sync project root."""
        if not manifest.branches:
            return None
        # Use the first branch (typically "main")
        branch_path = manifest.branches[0].path
        branch_dir = project_root / branch_path
        return branch_dir if branch_dir.exists() else None

    def _resolve_metadata_branch_id(
        self, project: ProjectConfig, client: Any, branch_id: int | None
    ) -> int:
        """Resolve the branch ID required by the config metadata API.

        Config metadata endpoints only support the branch-aware route
        (/v2/storage/branch/{id}/...). This method resolves the effective
        branch: explicit arg → active branch → default branch from API.

        Raises ConfigError if no default branch can be found.
        """
        effective = branch_id or project.active_branch_id
        if effective:
            return int(effective)
        try:
            branches = client.list_dev_branches()
        except KeboolaApiError as exc:
            raise ConfigError(
                f"Could not list branches to resolve metadata branch: {exc.message}. "
                "Pass --branch explicitly."
            ) from exc
        except Exception as exc:
            raise ConfigError(
                f"Unexpected error listing branches for metadata route: {exc}. "
                "Pass --branch explicitly."
            ) from exc
        default = next((b for b in branches if b.get("isDefault")), None)
        if default:
            return int(default["id"])
        raise ConfigError(
            "Could not determine a branch for config metadata. "
            "Set an active branch with 'kbagent branch use' or pass --branch."
        )

    def list_config_metadata(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """List all metadata entries on a configuration.

        Returns:
            Dict with project_alias, component_id, config_id, branch_id,
            and a key-sorted metadata list.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            effective_branch_id = self._resolve_metadata_branch_id(project, client, branch_id)
            entries = client.list_config_metadata(
                component_id, config_id, branch_id=effective_branch_id
            )
            return {
                "project_alias": alias,
                "component_id": component_id,
                "config_id": config_id,
                "branch_id": effective_branch_id,
                "metadata": sorted(entries, key=lambda e: e.get("key", "")),
            }
        finally:
            client.close()

    def get_config_metadata_value(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        key: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Get a single metadata value by key.

        Raises KeboolaApiError(NOT_FOUND) if the key is absent.
        """
        result = self.list_config_metadata(alias, component_id, config_id, branch_id=branch_id)
        for entry in result["metadata"]:
            if entry.get("key") == key:
                return {
                    "project_alias": alias,
                    "component_id": component_id,
                    "config_id": config_id,
                    "branch_id": result["branch_id"],
                    "key": key,
                    "value": entry.get("value"),
                    "metadata_id": entry.get("id"),
                }
        raise KeboolaApiError(
            message=f"Metadata key '{key}' not found on config '{component_id}/{config_id}'.",
            status_code=404,
            error_code=ErrorCode.NOT_FOUND,
            retryable=False,
        )

    def set_config_metadata(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        key: str,
        value: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Set a single metadata key/value on a configuration (upsert)."""
        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            effective_branch_id = self._resolve_metadata_branch_id(project, client, branch_id)
            result = client.set_config_metadata(
                component_id, config_id, entries=[(key, value)], branch_id=effective_branch_id
            )
            return {
                "project_alias": alias,
                "component_id": component_id,
                "config_id": config_id,
                "branch_id": effective_branch_id,
                "key": key,
                "value": value,
                "result": result,
                "message": (
                    f"Metadata '{key}' set on config '{component_id}/{config_id}' in project '{alias}'."
                ),
            }
        finally:
            client.close()

    def delete_config_metadata(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        metadata_id: int | str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Delete a metadata entry by its numeric ID."""
        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            effective_branch_id = self._resolve_metadata_branch_id(project, client, branch_id)
            client.delete_config_metadata(
                component_id, config_id, metadata_id, branch_id=effective_branch_id
            )
            return {
                "project_alias": alias,
                "component_id": component_id,
                "config_id": config_id,
                "branch_id": effective_branch_id,
                "metadata_id": metadata_id,
                "message": (
                    f"Metadata ID {metadata_id} deleted from config "
                    f"'{component_id}/{config_id}' in project '{alias}'."
                ),
            }
        finally:
            client.close()

    def set_config_folder(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        folder_name: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Set the folder name on a configuration (KBC.configuration.folderName)."""
        result = self.set_config_metadata(
            alias,
            component_id,
            config_id,
            key="KBC.configuration.folderName",
            value=folder_name,
            branch_id=branch_id,
        )
        result["folder"] = folder_name
        result["message"] = (
            f"Folder '{folder_name}' set on config '{component_id}/{config_id}' in project '{alias}'."
        )
        return result

    def search_configs(
        self,
        query: str,
        aliases: list[str] | None = None,
        component_type: str | None = None,
        component_id: str | None = None,
        ignore_case: bool = False,
        use_regex: bool = False,
        branch_id: int | None = None,
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
            branch_id: If set, search configs from a specific dev branch.
                       If None, uses each project's active branch (if any).

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
                alias, project, match_fn, component_type, component_id, branch_id=branch_id
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
        branch_id: int | None = None,
    ) -> tuple[str, dict[str, Any], bool] | tuple[str, dict[str, str]]:
        """Search configs in a single project (worker thread).

        Uses ``list_components_with_configs`` (``include=configuration,rows``)
        so that row-level configuration (Snowflake writer rows, DB extractor
        tables, Google Sheets sheets, etc.) is included in the search tree.
        Without ``rows``, the API returns only the top-level configuration
        body, and searches for row-only properties always miss (see #196).
        """
        client = self._client_factory(project.stack_url, project.token)
        try:
            effective_branch_id = branch_id or project.active_branch_id
            components = client.list_components_with_configs(
                branch_id=effective_branch_id,
                component_type=component_type,
            )
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
