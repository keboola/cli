"""Sync service - business logic for project pull/push/status operations.

Handles downloading Keboola project configurations to the local filesystem
in a dev-friendly format (YAML configs), and tracking local changes.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import yaml

from ..constants import (
    BRANCH_MAPPING_FILENAME,
    CONFIG_FILENAME,
    KEBOOLA_DIR_NAME,
    MANIFEST_VERSION,
)
from ..errors import ConfigError
from ..sync.code_extraction import extract_code_files, merge_code_files
from ..sync.config_format import (
    api_config_to_local,
    api_row_to_local,
    classify_component_type,
    local_config_to_api,
)
from ..sync.diff_engine import compute_changeset
from ..sync.git_utils import get_default_branch, is_git_repo
from ..sync.manifest import (
    Manifest,
    ManifestBranch,
    ManifestConfigRow,
    ManifestConfiguration,
    ManifestGitBranching,
    ManifestNaming,
    ManifestProject,
    load_manifest,
    save_manifest,
)
from ..sync.naming import config_path, config_row_path
from .base import BaseService

logger = logging.getLogger(__name__)


class SyncService(BaseService):
    """Business logic for project sync operations (init, pull, status).

    Single-project operations only. Uses dependency injection for
    config_store and client_factory following the BaseService pattern.
    """

    # ------------------------------------------------------------------
    # init
    # ------------------------------------------------------------------

    def init_sync(
        self,
        alias: str,
        project_root: Path,
        git_branching: bool = False,
    ) -> dict[str, Any]:
        """Initialize a sync working directory for a project.

        Creates the ``.keboola/`` directory with ``manifest.json``.
        Fetches project metadata from the API to populate the manifest.

        Args:
            alias: Project alias from config store.
            project_root: Root directory for the sync working tree.
            git_branching: Enable git-branching mode.

        Returns:
            Dict with initialization stats and created file paths.

        Raises:
            ConfigError: If the project alias is not found.
            FileExistsError: If manifest already exists (use pull instead).
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        keboola_dir = project_root / KEBOOLA_DIR_NAME
        manifest_path = keboola_dir / "manifest.json"
        if manifest_path.exists():
            raise FileExistsError(
                f"Manifest already exists at {manifest_path}. "
                "Use 'sync pull' to update, or delete .keboola/ to reinitialize."
            )

        # Fetch project info from API
        client = self._client_factory(project.stack_url, project.token)
        with client:
            token_info = client.verify_token()
            branches = client.list_dev_branches()

        project_id = token_info.project_id
        api_host = project.stack_url.replace("https://", "").rstrip("/")
        default_branch_info = next(
            (b for b in branches if b.get("isDefault")),
            None,
        )
        default_branch_id = default_branch_info["id"] if default_branch_info else None
        default_branch_name = "main"

        # Git branching setup
        git_branching_config = ManifestGitBranching(enabled=False)
        if git_branching:
            if not is_git_repo(project_root):
                raise ConfigError("Git repository not found. Initialize git first: git init")
            default_branch_name = get_default_branch(project_root)
            git_branching_config = ManifestGitBranching(
                enabled=True,
                default_branch=default_branch_name,
            )

        # Build manifest
        manifest = Manifest(
            version=MANIFEST_VERSION,
            project=ManifestProject(id=project_id, api_host=api_host),
            allow_target_env=True,
            git_branching=git_branching_config,
            naming=ManifestNaming(),
            branches=[
                ManifestBranch(
                    id=default_branch_id,
                    path=default_branch_name,
                )
            ]
            if default_branch_id
            else [],
            configurations=[],
        )

        # Save manifest
        save_manifest(project_root, manifest)

        created_files = [str(manifest_path)]

        # Create branch mapping if git-branching mode
        if git_branching:
            mapping = {
                "version": 1,
                "mappings": {
                    default_branch_name: {
                        "id": None,
                        "name": "Main",
                    }
                },
            }
            mapping_path = keboola_dir / BRANCH_MAPPING_FILENAME
            mapping_path.write_text(
                json.dumps(mapping, indent=4, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            created_files.append(str(mapping_path))

        return {
            "status": "initialized",
            "project_id": project_id,
            "project_alias": alias,
            "api_host": api_host,
            "git_branching": git_branching,
            "default_branch": default_branch_name,
            "files_created": created_files,
        }

    # ------------------------------------------------------------------
    # pull
    # ------------------------------------------------------------------

    def pull(
        self,
        alias: str,
        project_root: Path,
        force: bool = False,
    ) -> dict[str, Any]:
        """Download all configurations from Keboola to local filesystem.

        Args:
            alias: Project alias from config store.
            project_root: Root directory of the sync working tree.
            force: If True, overwrite existing local files without checking.

        Returns:
            Dict with pull statistics (configs, rows, files written).
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        # Load or verify manifest exists
        manifest = load_manifest(project_root)

        # Determine branch to pull from
        branch_id = project.active_branch_id
        if not branch_id and manifest.branches:
            branch_id = manifest.branches[0].id

        # Fetch all components with configs from API
        client = self._client_factory(project.stack_url, project.token)
        with client:
            components = client.list_components_with_configs(branch_id=branch_id)

        # Determine branch directory name
        branch_dir_name = "main"
        for mb in manifest.branches:
            if mb.id == branch_id:
                branch_dir_name = mb.path
                break

        branch_dir = project_root / branch_dir_name

        # Track stats
        configs_pulled = 0
        rows_pulled = 0
        files_written = 0
        new_configurations: list[ManifestConfiguration] = []
        used_paths: set[str] = set()  # detect naming collisions

        # Build lookup for existing manifest paths by config ID
        # so renames don't cause path changes (stable paths)
        existing_paths: dict[str, str] = {
            f"{c.component_id}/{c.id}": c.path for c in manifest.configurations
        }

        for component in components:
            component_id = component.get("id", "")
            component_type = classify_component_type(component.get("type", "other"))
            configs = component.get("configurations", [])

            for cfg in configs:
                config_id = str(cfg.get("id", ""))
                config_name = cfg.get("name", "untitled")

                # Reuse existing path if config is already tracked (stable paths)
                lookup_key = f"{component_id}/{config_id}"
                if lookup_key in existing_paths:
                    rel_path = existing_paths[lookup_key]
                else:
                    # Generate new filesystem path with collision detection
                    rel_path = config_path(
                        manifest.naming.config,
                        component_type,
                        component_id,
                        config_name,
                    )
                if rel_path in used_paths:
                    # Append short config ID suffix to resolve collision
                    suffix = config_id[:8] if len(config_id) > 8 else config_id
                    rel_path = f"{rel_path}-{suffix}"
                used_paths.add(rel_path)
                config_dir = branch_dir / rel_path

                # Convert API format to local _config.yml
                local_data = api_config_to_local(component_id, cfg, config_id)

                # Extract code files (SQL, Python) if applicable.
                # This modifies local_data in place (removes blocks/code)
                # and writes separate code files (transform.sql, transform.py, etc.)
                extract_code_files(component_id, local_data, config_dir)

                # Write _config.yml (without extracted code) and capture content hash
                file_hash = self._write_config_file(config_dir, local_data)
                files_written += 1
                configs_pulled += 1

                # Handle rows
                row_manifests: list[ManifestConfigRow] = []
                used_row_paths: set[str] = set()
                for row in cfg.get("rows", []):
                    row_id = str(row.get("id", ""))
                    row_name = row.get("name", "untitled")

                    row_rel_path = config_row_path(
                        manifest.naming.config_row,
                        row_name,
                    )
                    if row_rel_path in used_row_paths:
                        suffix = row_id[:8] if len(row_id) > 8 else row_id
                        row_rel_path = f"{row_rel_path}-{suffix}"
                    used_row_paths.add(row_rel_path)
                    row_dir = config_dir / row_rel_path

                    row_local = api_row_to_local(row, component_id)
                    self._write_config_file(row_dir, row_local)
                    files_written += 1
                    rows_pulled += 1

                    row_manifests.append(ManifestConfigRow(id=row_id, path=row_rel_path))

                # Record in manifest (store file hash for change detection)
                new_configurations.append(
                    ManifestConfiguration(
                        branch_id=branch_id or 0,
                        component_id=component_id,
                        id=config_id,
                        path=rel_path,
                        metadata={"pull_hash": file_hash},
                        rows=row_manifests,
                    )
                )

        # Update manifest with pulled configurations
        manifest.configurations = new_configurations
        save_manifest(project_root, manifest)

        return {
            "status": "pulled",
            "project_alias": alias,
            "branch_id": branch_id,
            "branch_dir": branch_dir_name,
            "configs_pulled": configs_pulled,
            "rows_pulled": rows_pulled,
            "files_written": files_written,
        }

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self, project_root: Path) -> dict[str, Any]:
        """Compare local state against the manifest to detect changes.

        Walks the local filesystem and compares against manifest entries
        to classify configurations as modified, added, deleted, or unchanged.

        Args:
            project_root: Root directory of the sync working tree.

        Returns:
            Dict with lists of modified/added/deleted configs and count of unchanged.
        """
        manifest = load_manifest(project_root)

        modified: list[dict[str, str]] = []
        deleted: list[dict[str, str]] = []
        unchanged = 0

        # Check each manifest entry against local files
        for cfg in manifest.configurations:
            branch_path = self._find_branch_path(manifest, cfg.branch_id)
            config_dir = project_root / branch_path / cfg.path
            config_file = config_dir / CONFIG_FILENAME

            if not config_file.exists():
                deleted.append(
                    {
                        "component_id": cfg.component_id,
                        "config_id": cfg.id,
                        "path": str(cfg.path),
                    }
                )
                continue

            # Compare file hash against the hash stored at pull time
            current_hash = self._file_hash(config_file)
            pull_hash = cfg.metadata.get("pull_hash", "")

            if pull_hash and current_hash == pull_hash:
                unchanged += 1
            else:
                modified.append(
                    {
                        "component_id": cfg.component_id,
                        "config_id": cfg.id,
                        "path": str(cfg.path),
                    }
                )

        # Scan for added configs (local files without manifest entry)
        added = self._find_untracked_configs(project_root, manifest)

        return {
            "modified": modified,
            "added": added,
            "deleted": deleted,
            "unchanged": unchanged,
            "total_tracked": len(manifest.configurations),
        }

    # ------------------------------------------------------------------
    # diff
    # ------------------------------------------------------------------

    def diff(
        self,
        alias: str,
        project_root: Path,
    ) -> dict[str, Any]:
        """Compare local configs against the remote API state.

        Fetches current state from API, reads local _config.yml files,
        and runs the diff engine to produce a detailed changeset.

        Returns:
            Dict with 'changes' list and summary counts.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        manifest = load_manifest(project_root)

        branch_id = project.active_branch_id
        if not branch_id and manifest.branches:
            branch_id = manifest.branches[0].id

        # Fetch remote state
        client = self._client_factory(project.stack_url, project.token)
        with client:
            components = client.list_components_with_configs(branch_id=branch_id)

        # Build remote configs lookup: "{component_id}/{config_id}" -> API data
        remote_configs: dict[str, dict[str, Any]] = {}
        for component in components:
            component_id = component.get("id", "")
            for cfg in component.get("configurations", []):
                config_id = str(cfg.get("id", ""))
                key = f"{component_id}/{config_id}"
                # Convert remote to local format for apples-to-apples comparison
                remote_configs[key] = api_config_to_local(component_id, cfg, config_id)

        # Build local configs list from manifest
        # Merge code files (transform.sql, code.py, etc.) back into config
        # data so comparison with remote is apples-to-apples
        local_configs: list[dict[str, Any]] = []
        for cfg in manifest.configurations:
            branch_path = self._find_branch_path(manifest, cfg.branch_id)
            config_dir = project_root / branch_path / cfg.path
            local_data = self._read_config_file(config_dir)
            if local_data is None:
                continue
            merge_code_files(cfg.component_id, local_data, config_dir)
            local_configs.append(
                {
                    "component_id": cfg.component_id,
                    "config_id": cfg.id,
                    "config_name": local_data.get("name", ""),
                    "path": cfg.path,
                    "data": local_data,
                }
            )

        # Also add untracked local configs (new files)
        for added_cfg in self._find_untracked_configs(project_root, manifest):
            branch_path = manifest.branches[0].path if manifest.branches else "main"
            config_dir = project_root / branch_path / added_cfg["path"]
            local_data = self._read_config_file(config_dir)
            if local_data is None:
                continue
            local_configs.append(
                {
                    "component_id": added_cfg.get("component_id", "unknown"),
                    "config_id": "",  # new config, no ID yet
                    "config_name": local_data.get("name", ""),
                    "path": added_cfg["path"],
                    "data": local_data,
                }
            )

        changeset = compute_changeset(local_configs, remote_configs)

        added = [c for c in changeset if c.change_type == "added"]
        modified = [c for c in changeset if c.change_type == "modified"]
        deleted = [c for c in changeset if c.change_type == "deleted"]

        return {
            "changes": [c.to_dict() for c in changeset],
            "summary": {
                "added": len(added),
                "modified": len(modified),
                "deleted": len(deleted),
                "unchanged": len(local_configs) - len(added) - len(modified),
            },
        }

    # ------------------------------------------------------------------
    # push
    # ------------------------------------------------------------------

    def push(
        self,
        alias: str,
        project_root: Path,
        dry_run: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Push local changes to Keboola.

        Computes diff, then creates/updates/deletes configs via API.
        New configs get IDs assigned by the API; the manifest is updated.

        Args:
            alias: Project alias from config store.
            project_root: Root directory of the sync working tree.
            dry_run: If True, compute changes but don't execute them.
            force: If True, allow deletions without extra confirmation.

        Returns:
            Dict with push results (created, updated, deleted, errors).
        """
        diff_result = self.diff(alias, project_root)
        changes = diff_result["changes"]

        if not changes:
            return {
                "status": "no_changes",
                "created": 0,
                "updated": 0,
                "deleted": 0,
                "errors": [],
            }

        if dry_run:
            return {
                "status": "dry_run",
                "changes": changes,
                "summary": diff_result["summary"],
            }

        projects = self.resolve_projects([alias])
        project = projects[alias]
        manifest = load_manifest(project_root)

        branch_id = project.active_branch_id
        if not branch_id and manifest.branches:
            branch_id = manifest.branches[0].id

        client = self._client_factory(project.stack_url, project.token)
        created = 0
        updated = 0
        deleted = 0
        errors: list[dict[str, str]] = []

        with client:
            for change in changes:
                change_type = change["change_type"]
                component_id = change["component_id"]
                config_id = change["config_id"]
                config_path_str = change.get("path", "")

                try:
                    if change_type == "added":
                        result = self._push_create(
                            client,
                            component_id,
                            config_path_str,
                            project_root,
                            manifest,
                            branch_id,
                        )
                        if result:
                            created += 1

                    elif change_type == "modified":
                        self._push_update(
                            client,
                            component_id,
                            config_id,
                            config_path_str,
                            project_root,
                            manifest,
                            branch_id,
                        )
                        updated += 1

                    elif change_type == "deleted" and force:
                        client.delete_config(
                            component_id=component_id,
                            config_id=config_id,
                            branch_id=branch_id,
                        )
                        deleted += 1

                except Exception as exc:
                    logger.warning(
                        "Failed to push %s %s/%s: %s",
                        change_type,
                        component_id,
                        config_id,
                        exc,
                    )
                    errors.append(
                        {
                            "change_type": change_type,
                            "component_id": component_id,
                            "config_id": config_id,
                            "message": str(exc),
                        }
                    )

        # Re-pull to update manifest with new IDs and sync state
        if created > 0 or updated > 0 or deleted > 0:
            self.pull(alias, project_root, force=True)

        return {
            "status": "pushed",
            "created": created,
            "updated": updated,
            "deleted": deleted,
            "errors": errors,
        }

    def _push_create(
        self,
        client: Any,
        component_id: str,
        config_path_str: str,
        project_root: Path,
        manifest: Manifest,
        branch_id: int | None,
    ) -> dict[str, Any] | None:
        """Create a new config from a local _config.yml file."""
        branch_path = manifest.branches[0].path if manifest.branches else "main"
        config_dir = project_root / branch_path / config_path_str
        local_data = self._read_config_file(config_dir)
        if local_data is None:
            return None

        # Merge code files (transform.sql, transform.py, code.py) back into config
        merge_code_files(component_id, local_data, config_dir)

        name, description, configuration = local_config_to_api(local_data)
        result = client.create_config(
            component_id=component_id,
            name=name,
            configuration=configuration,
            description=description,
            branch_id=branch_id,
        )
        logger.info(
            "Created config %s/%s (ID: %s)",
            component_id,
            name,
            result.get("id"),
        )
        return result

    def _push_update(
        self,
        client: Any,
        component_id: str,
        config_id: str,
        config_path_str: str,
        project_root: Path,
        manifest: Manifest,
        branch_id: int | None,
    ) -> None:
        """Update an existing config from a local _config.yml file."""
        branch_path = manifest.branches[0].path if manifest.branches else "main"
        config_dir = project_root / branch_path / config_path_str
        local_data = self._read_config_file(config_dir)
        if local_data is None:
            return

        # Merge code files (transform.sql, transform.py, code.py) back into config
        merge_code_files(component_id, local_data, config_dir)

        name, description, configuration = local_config_to_api(local_data)
        client.update_config(
            component_id=component_id,
            config_id=config_id,
            name=name,
            configuration=configuration,
            description=description,
            change_description="Updated via kbagent sync push",
            branch_id=branch_id,
        )
        logger.info("Updated config %s/%s", component_id, config_id)

    # ------------------------------------------------------------------
    # branch mapping
    # ------------------------------------------------------------------

    def branch_link(
        self,
        alias: str,
        project_root: Path,
        branch_id: int | None = None,
        branch_name: str | None = None,
    ) -> dict[str, Any]:
        """Link the current git branch to a Keboola development branch.

        If no branch_id or branch_name is given:
        1. Get current git branch name
        2. Search for existing Keboola branch with same name
        3. If not found: create a new dev branch
        4. Save mapping to branch-mapping.json

        Args:
            alias: Project alias.
            project_root: Root directory of the sync working tree.
            branch_id: Link to a specific existing Keboola branch.
            branch_name: Create/find a branch with this name.

        Returns:
            Dict with link result including git branch, Keboola branch ID, name.
        """
        from ..sync.branch_mapping import load_branch_mapping, save_branch_mapping
        from ..sync.git_utils import get_current_branch

        manifest = load_manifest(project_root)
        if not manifest.git_branching.enabled:
            raise ConfigError(
                "Git-branching mode is not enabled. Run 'sync init --git-branching' first."
            )

        git_branch = get_current_branch(project_root)
        if git_branch is None:
            raise ConfigError("Cannot determine current git branch.")

        default_branch = manifest.git_branching.default_branch
        if git_branch == default_branch:
            raise ConfigError(
                f"Cannot link the default branch '{default_branch}'. "
                "It is automatically linked to Keboola production."
            )

        # Load existing mapping
        try:
            mapping = load_branch_mapping(project_root)
        except FileNotFoundError:
            from ..sync.branch_mapping import BranchMapping

            mapping = BranchMapping()
            mapping.set(default_branch, None, "Main")

        # Check if already linked
        existing = mapping.get(git_branch)
        if existing is not None:
            return {
                "status": "already_linked",
                "git_branch": git_branch,
                "keboola_branch_id": existing.keboola_id,
                "keboola_branch_name": existing.name,
            }

        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)

        with client:
            if branch_id:
                # Link to existing branch by ID
                branches = client.list_dev_branches()
                branch_info = next(
                    (b for b in branches if b["id"] == branch_id),
                    None,
                )
                if branch_info is None:
                    raise ConfigError(f"Keboola branch {branch_id} not found.")
                kbc_branch_id = str(branch_info["id"])
                kbc_branch_name = branch_info.get("name", "")
            elif branch_name:
                # Search by name or create
                branches = client.list_dev_branches()
                branch_info = next(
                    (b for b in branches if b.get("name") == branch_name),
                    None,
                )
                if branch_info:
                    kbc_branch_id = str(branch_info["id"])
                    kbc_branch_name = branch_info.get("name", "")
                else:
                    result = client.create_dev_branch(name=branch_name)
                    kbc_branch_id = str(result["id"])
                    kbc_branch_name = branch_name
            else:
                # Default: use git branch name to search/create
                branches = client.list_dev_branches()
                branch_info = next(
                    (b for b in branches if b.get("name") == git_branch),
                    None,
                )
                if branch_info:
                    kbc_branch_id = str(branch_info["id"])
                    kbc_branch_name = branch_info.get("name", "")
                else:
                    result = client.create_dev_branch(name=git_branch)
                    kbc_branch_id = str(result["id"])
                    kbc_branch_name = git_branch

        mapping.set(git_branch, kbc_branch_id, kbc_branch_name)
        save_branch_mapping(project_root, mapping)

        return {
            "status": "linked",
            "git_branch": git_branch,
            "keboola_branch_id": kbc_branch_id,
            "keboola_branch_name": kbc_branch_name,
        }

    def branch_unlink(
        self,
        project_root: Path,
    ) -> dict[str, Any]:
        """Remove the branch mapping for the current git branch."""
        from ..sync.branch_mapping import load_branch_mapping, save_branch_mapping
        from ..sync.git_utils import get_current_branch

        manifest = load_manifest(project_root)
        if not manifest.git_branching.enabled:
            raise ConfigError("Git-branching mode is not enabled.")

        git_branch = get_current_branch(project_root)
        if git_branch is None:
            raise ConfigError("Cannot determine current git branch.")

        default_branch = manifest.git_branching.default_branch
        if git_branch == default_branch:
            raise ConfigError(
                f"Cannot unlink the default branch '{default_branch}'. "
                "It is permanently linked to Keboola production."
            )

        mapping = load_branch_mapping(project_root)
        existing = mapping.get(git_branch)
        if existing is None:
            return {
                "status": "not_linked",
                "git_branch": git_branch,
            }

        kbc_id = existing.keboola_id
        kbc_name = existing.name
        mapping.remove(git_branch)
        save_branch_mapping(project_root, mapping)

        return {
            "status": "unlinked",
            "git_branch": git_branch,
            "keboola_branch_id": kbc_id,
            "keboola_branch_name": kbc_name,
        }

    def branch_status(
        self,
        project_root: Path,
    ) -> dict[str, Any]:
        """Show the branch mapping status for the current git branch."""
        from ..sync.branch_mapping import load_branch_mapping
        from ..sync.git_utils import get_current_branch

        manifest = load_manifest(project_root)
        if not manifest.git_branching.enabled:
            return {"git_branching": False}

        git_branch = get_current_branch(project_root)
        try:
            mapping = load_branch_mapping(project_root)
        except FileNotFoundError:
            return {
                "git_branching": True,
                "git_branch": git_branch,
                "linked": False,
            }

        entry = mapping.get(git_branch) if git_branch else None
        if entry is None:
            return {
                "git_branching": True,
                "git_branch": git_branch,
                "linked": False,
            }

        return {
            "git_branching": True,
            "git_branch": git_branch,
            "linked": True,
            "keboola_branch_id": entry.keboola_id,
            "keboola_branch_name": entry.name,
            "is_production": entry.is_production(),
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _write_config_file(self, config_dir: Path, config_data: dict[str, Any]) -> str:
        """Write a ``_config.yml`` file and return its SHA256 hash."""
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / CONFIG_FILENAME
        content = yaml.dump(
            config_data,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            width=120,
        )
        config_file.write_text(content, encoding="utf-8")
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _file_hash(self, file_path: Path) -> str:
        """Return the SHA256 hex digest of a file's contents."""
        content = file_path.read_bytes()
        return hashlib.sha256(content).hexdigest()

    def _read_config_file(self, config_dir: Path) -> dict[str, Any] | None:
        """Read and parse a ``_config.yml`` file, returning None if missing."""
        config_file = config_dir / CONFIG_FILENAME
        if not config_file.exists():
            return None
        try:
            return yaml.safe_load(config_file.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            logger.warning("Failed to parse %s", config_file)
            return None

    def _find_branch_path(self, manifest: Manifest, branch_id: int) -> str:
        """Find the branch directory name for a given branch ID."""
        for branch in manifest.branches:
            if branch.id == branch_id:
                return branch.path
        return "main"

    def _find_untracked_configs(
        self, project_root: Path, manifest: Manifest
    ) -> list[dict[str, str]]:
        """Scan for _config.yml files that are not tracked in the manifest."""
        tracked_paths: set[str] = set()
        for cfg in manifest.configurations:
            branch_path = self._find_branch_path(manifest, cfg.branch_id)
            tracked_paths.add(str(project_root / branch_path / cfg.path))

        added: list[dict[str, str]] = []
        for branch in manifest.branches:
            branch_dir = project_root / branch.path
            if not branch_dir.exists():
                continue
            for config_file in branch_dir.rglob(CONFIG_FILENAME):
                config_dir = config_file.parent
                # Skip row-level configs (they're under rows/ subdirectory)
                if "rows" in config_dir.parts:
                    continue
                # Skip branch-level _config.yml
                if config_dir == branch_dir:
                    continue
                if str(config_dir) not in tracked_paths:
                    local_data = self._read_config_file(config_dir)
                    keboola_meta = local_data.get("_keboola", {}) if local_data else {}
                    added.append(
                        {
                            "component_id": keboola_meta.get("component_id", "unknown"),
                            "config_id": keboola_meta.get("config_id", ""),
                            "path": str(config_dir.relative_to(project_root / branch.path)),
                        }
                    )

        return added
