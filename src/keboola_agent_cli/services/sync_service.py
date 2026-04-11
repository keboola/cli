"""Sync service - business logic for project pull/push/status operations.

Handles downloading Keboola project configurations to the local filesystem
in a dev-friendly format (YAML configs), and tracking local changes.
"""

import copy
import hashlib
import json
import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import yaml

from ..constants import (
    ALWAYS_IGNORED_COMPONENTS,
    BRANCH_MAPPING_FILENAME,
    CONFIG_FILENAME,
    DEFAULT_JOBS_PER_CONFIG,
    DEFAULT_MAX_SAMPLES,
    DEFAULT_SAMPLE_LIMIT,
    ENCRYPTED_COLUMN_MASK,
    ENCRYPTED_COLUMN_PREFIX,
    JOBS_FILENAME,
    KEBOOLA_DIR_NAME,
    MANIFEST_VERSION,
    STORAGE_BUCKETS_FILENAME,
    STORAGE_DIR_NAME,
    STORAGE_SAMPLES_DIR_NAME,
)
from ..errors import ConfigError, KeboolaApiError
from ..sync.code_extraction import extract_code_files, merge_code_files
from ..sync.config_format import (
    api_config_to_local,
    api_row_to_local,
    classify_component_type,
    local_config_to_api,
)
from ..sync.diff_engine import compute_changeset, config_hash
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
        dry_run: bool = False,
        job_limit: int = DEFAULT_JOBS_PER_CONFIG,
        no_storage: bool = False,
        no_jobs: bool = False,
        with_samples: bool = False,
        sample_limit: int = DEFAULT_SAMPLE_LIMIT,
        max_samples: int = DEFAULT_MAX_SAMPLES,
    ) -> dict[str, Any]:
        """Download all configurations from Keboola to local filesystem.

        Args:
            alias: Project alias from config store.
            project_root: Root directory of the sync working tree.
            force: If True, overwrite existing local files without checking.
            dry_run: If True, compute what would be pulled but don't write.
            job_limit: Max jobs per config to pull (default 5).
            no_storage: Skip storage metadata download.
            no_jobs: Skip per-config jobs download.
            with_samples: Download table data samples (opt-in).
            sample_limit: Max rows per sample (default 100).
            max_samples: Max number of tables to sample (default 50).

        Returns:
            Dict with pull statistics (configs, rows, files written).
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        # Load or verify manifest exists
        manifest = load_manifest(project_root)

        # Determine branch to pull from (git-branching aware)
        branch_id = self._resolve_branch_id(project, manifest, project_root)

        # Fetch all components with configs from API (+ storage metadata + jobs)
        client = self._client_factory(project.stack_url, project.token)
        buckets_data: list[dict[str, Any]] = []
        tables_data: list[dict[str, Any]] = []
        jobs_grouped: list[dict[str, Any]] = []
        samples_data: dict[str, str] = {}  # table_id -> CSV string
        with client:
            components = client.list_components_with_configs(branch_id=branch_id)

            if not no_storage:
                try:
                    buckets_data = client.list_buckets_with_metadata()
                    tables_data = client.list_tables_with_metadata()
                except Exception:
                    logger.warning("Failed to fetch storage metadata", exc_info=True)

            if not no_jobs:
                try:
                    # API constraint: jobsPerGroup * limit <= 500
                    group_limit = min(500 // max(job_limit, 1), 500)
                    total_configs = sum(len(comp.get("configurations", [])) for comp in components)

                    if group_limit >= total_configs:
                        # Fast path: one grouped API call covers all configs
                        jobs_grouped = client.list_jobs_grouped(
                            jobs_per_group=job_limit,
                            limit=group_limit,
                        )
                    else:
                        # Slow path: too many configs for grouped-jobs limit,
                        # fetch per-config via /search/jobs in parallel
                        logger.info(
                            "Project has %d configs but grouped-jobs limit is %d "
                            "(job_limit=%d); falling back to per-config fetching",
                            total_configs,
                            group_limit,
                            job_limit,
                        )
                        jobs_grouped = self._fetch_jobs_per_config(client, components, job_limit)
                except Exception:
                    logger.warning("Failed to fetch jobs", exc_info=True)

            if with_samples and tables_data:
                samples_data = self._fetch_samples(client, tables_data, sample_limit, max_samples)

        # Determine branch directory name
        branch_dir_name = self._find_branch_path(manifest, branch_id)

        branch_dir = project_root / branch_dir_name

        # Track stats and change details
        configs_pulled = 0
        rows_pulled = 0
        files_written = 0
        new_configurations: list[ManifestConfiguration] = []
        used_paths: set[str] = set()  # detect naming collisions
        pull_details: list[dict[str, str]] = []  # per-config change info

        # Build lookups for existing manifest state
        existing_paths: dict[str, str] = {
            f"{c.component_id}/{c.id}": c.path for c in manifest.configurations
        }
        existing_keys: set[str] = set(existing_paths.keys())
        existing_config_hashes: dict[str, str] = {
            f"{c.component_id}/{c.id}": c.metadata.get("pull_config_hash", "")
            for c in manifest.configurations
        }
        # Build lookup for file hashes at pull time (to detect local edits)
        existing_file_hashes: dict[str, str] = {
            f"{c.component_id}/{c.id}": c.metadata.get("pull_hash", "")
            for c in manifest.configurations
        }

        for component in components:
            component_id = component.get("id", "")
            if component_id in ALWAYS_IGNORED_COMPONENTS:
                continue
            component_type = classify_component_type(component.get("type", "other"))
            configs = component.get("configurations", [])

            for cfg in configs:
                config_id = str(cfg.get("id", ""))
                config_name = cfg.get("name", "untitled")

                # Reuse existing path if config is already tracked (stable paths)
                lookup_key = f"{component_id}/{config_id}"
                is_new = lookup_key not in existing_keys
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

                # Hash of API-converted data.  Stored as pull_config_hash so
                # diff can compare it directly with fresh remote data without
                # a lossy file roundtrip.
                api_cfg_hash = config_hash(local_data)
                pull_cfg_hash = api_cfg_hash

                # Detect local modifications: if file hash differs from
                # pull_hash stored in manifest, the user edited the file.
                # Skip overwrite unless --force to avoid losing local work.
                locally_modified = False
                if not is_new and not force:
                    old_file_hash = existing_file_hashes.get(lookup_key, "")
                    if old_file_hash:
                        config_file = config_dir / CONFIG_FILENAME
                        if config_file.exists():
                            current_file_hash = self._file_hash(config_file)
                            locally_modified = current_file_hash != old_file_hash

                remote_unchanged = False  # set in else branch; default for locally_modified path
                if locally_modified and not dry_run:
                    # Preserve the existing local file -- don't overwrite.
                    # Still update manifest entry (remote hash changes, but
                    # local file stays as-is).
                    file_hash = self._file_hash(config_dir / CONFIG_FILENAME)
                    pull_details.append(
                        {
                            "action": "skipped",
                            "component_id": component_id,
                            "config_name": config_name,
                            "path": rel_path,
                            "reason": "locally modified",
                        }
                    )
                elif locally_modified and dry_run:
                    file_hash = ""
                    pull_details.append(
                        {
                            "action": "skipped",
                            "component_id": component_id,
                            "config_name": config_name,
                            "path": rel_path,
                            "reason": "locally modified",
                        }
                    )
                else:
                    # Check if remote actually changed since last pull.
                    # If pull_config_hash matches, skip write (idempotent).
                    old_cfg_hash = existing_config_hashes.get(lookup_key, "")
                    remote_unchanged = not is_new and old_cfg_hash and old_cfg_hash == api_cfg_hash

                    if remote_unchanged:
                        # Nothing changed -- reuse existing file hash
                        config_file = config_dir / CONFIG_FILENAME
                        file_hash = self._file_hash(config_file) if config_file.exists() else ""
                    else:
                        # Extract code files (SQL, Python) if applicable.
                        # This modifies local_data in place (removes
                        # blocks/code) and writes separate code files.
                        if not dry_run:
                            extract_code_files(component_id, local_data, config_dir)
                            file_hash = self._write_config_file(config_dir, local_data)
                        else:
                            file_hash = ""

                        configs_pulled += 1
                        files_written += 1

                        if is_new:
                            pull_details.append(
                                {
                                    "action": "new",
                                    "component_id": component_id,
                                    "config_name": config_name,
                                    "path": rel_path,
                                }
                            )
                        else:
                            pull_details.append(
                                {
                                    "action": "updated",
                                    "component_id": component_id,
                                    "config_name": config_name,
                                    "path": rel_path,
                                }
                            )

                # Handle rows -- skip writing if config is unchanged or
                # locally modified (rows inherit the parent config's state).
                skip_rows = locally_modified or remote_unchanged
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

                    if not skip_rows:
                        row_local = api_row_to_local(row, component_id)
                        if not dry_run:
                            self._write_config_file(row_dir, row_local)
                        files_written += 1
                        rows_pulled += 1

                    row_manifests.append(ManifestConfigRow(id=row_id, path=row_rel_path))

                # Record in manifest (store file hash for change detection).
                # For skipped configs: keep existing pull_hash (file untouched)
                # but do NOT update pull_config_hash -- keep the old base so
                # 3-way diff still correctly detects the local modification.
                if locally_modified:
                    old_pull_hash = existing_file_hashes.get(lookup_key, file_hash)
                    old_cfg_hash = existing_config_hashes.get(lookup_key, pull_cfg_hash)
                    cfg_metadata = {
                        "pull_hash": old_pull_hash,
                        "pull_config_hash": old_cfg_hash,
                    }
                else:
                    # Compute hashes for all extracted files
                    extra_hashes: dict[str, str] = {}
                    if not dry_run:
                        for fname in [
                            "_description.md",
                            "transform.sql",
                            "transform.py",
                            "code.py",
                            "pyproject.toml",
                        ]:
                            fpath = config_dir / fname
                            if fpath.exists():
                                extra_hashes[fname] = self._file_hash(fpath)
                    cfg_metadata = {
                        "pull_hash": file_hash,
                        "pull_config_hash": pull_cfg_hash,
                        "pull_extra_hashes": extra_hashes,
                    }
                new_configurations.append(
                    ManifestConfiguration(
                        branch_id=branch_id or 0,
                        component_id=component_id,
                        id=config_id,
                        path=rel_path,
                        metadata=cfg_metadata,
                        rows=row_manifests,
                    )
                )

        # Detect configs removed from remote (in old manifest but not in new)
        new_keys = {f"{c.component_id}/{c.id}" for c in new_configurations}
        for old_cfg in manifest.configurations:
            old_key = f"{old_cfg.component_id}/{old_cfg.id}"
            if old_key not in new_keys:
                pull_details.append(
                    {
                        "action": "removed",
                        "component_id": old_cfg.component_id,
                        "config_name": "",
                        "path": old_cfg.path,
                    }
                )

        # Delete orphaned directories for removed configurations
        if not dry_run:
            for detail in pull_details:
                if detail["action"] == "removed" and detail.get("path"):
                    orphan_dir = branch_dir / detail["path"]
                    if orphan_dir.exists() and orphan_dir.is_dir():
                        shutil.rmtree(orphan_dir)
                        logger.info("Removed orphaned directory: %s", orphan_dir)
                        # Clean up empty parent dirs up to (but not including) branch_dir
                        parent = orphan_dir.parent
                        while parent != branch_dir and parent.exists():
                            if not any(parent.iterdir()):
                                parent.rmdir()
                                logger.info("Removed empty parent directory: %s", parent)
                                parent = parent.parent
                            else:
                                break

        # -- Storage metadata (read-only, not tracked in manifest) --
        storage_stats: dict[str, int] = {"buckets": 0, "tables": 0, "samples": 0}
        if not dry_run and buckets_data:
            storage_stats = self._write_storage_metadata(
                project_root, buckets_data, tables_data, samples_data
            )

        # -- Per-config jobs (JSONL files next to _config.yml) --
        jobs_written = 0
        if not dry_run and jobs_grouped:
            jobs_written = self._write_per_config_jobs(branch_dir, new_configurations, jobs_grouped)

        if not dry_run:
            # Update manifest with pulled configurations
            manifest.configurations = new_configurations
            save_manifest(project_root, manifest)

        return {
            "status": "dry_run" if dry_run else "pulled",
            "project_alias": alias,
            "branch_id": branch_id,
            "branch_dir": branch_dir_name,
            "configs_pulled": configs_pulled,
            "rows_pulled": rows_pulled,
            "files_written": files_written,
            "jobs_written": jobs_written,
            "storage": storage_stats,
            "details": pull_details,
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

        branch_id = self._resolve_branch_id(project, manifest, project_root)

        # Fetch remote state
        client = self._client_factory(project.stack_url, project.token)
        with client:
            components = client.list_components_with_configs(branch_id=branch_id)

        # Build remote configs lookup: "{component_id}/{config_id}" -> API data
        remote_configs: dict[str, dict[str, Any]] = {}
        for component in components:
            component_id = component.get("id", "")
            if component_id in ALWAYS_IGNORED_COMPONENTS:
                continue
            for cfg in component.get("configurations", []):
                config_id = str(cfg.get("id", ""))
                key = f"{component_id}/{config_id}"
                # Convert remote to local format for apples-to-apples comparison
                remote_configs[key] = api_config_to_local(component_id, cfg, config_id)

        # Build local configs list from manifest.
        # For files unchanged since pull, use the stored pull_config_hash
        # directly (avoids lossy code extraction roundtrip).
        # For locally modified files, merge code back for real comparison.
        local_configs: list[dict[str, Any]] = []
        file_unchanged: dict[str, bool] = {}
        local_override_hashes: dict[str, str] = {}
        for cfg in manifest.configurations:
            branch_path = self._find_branch_path(manifest, cfg.branch_id)
            config_dir = project_root / branch_path / cfg.path
            local_data = self._read_config_file(config_dir)
            if local_data is None:
                continue

            key = f"{cfg.component_id}/{cfg.id}"

            # Check if ANY file in this config dir changed since pull.
            # Manifest stores pull_extra_hashes for extracted files.
            pull_hash = cfg.metadata.get("pull_hash", "")
            config_file = config_dir / CONFIG_FILENAME
            current_file_hash = self._file_hash(config_file) if config_file.exists() else ""
            config_unchanged = bool(pull_hash and current_file_hash == pull_hash)

            extras_unchanged = True
            stored_extra = cfg.metadata.get("pull_extra_hashes", {})
            for fname, stored_h in stored_extra.items():
                fpath = config_dir / fname
                if fpath.exists():
                    if self._file_hash(fpath) != stored_h:
                        extras_unchanged = False
                        break
                else:
                    extras_unchanged = False
                    break

            is_unchanged = config_unchanged and extras_unchanged
            file_unchanged[key] = is_unchanged

            if is_unchanged:
                # All files match pull state -- use stored API hash
                stored_cfg_hash = cfg.metadata.get("pull_config_hash", "")
                if stored_cfg_hash:
                    local_override_hashes[key] = stored_cfg_hash
            # Always merge for local_data (needed for deep_diff details)
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
            branch_path = self._find_branch_path(manifest, branch_id)
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

        # Build set of manifest-tracked keys so that compute_changeset only
        # flags configs that were previously pulled (not brand-new remote ones).
        tracked_keys = {f"{cfg.component_id}/{cfg.id}" for cfg in manifest.configurations}

        # Build base hashes for 3-way diff.
        # Preferred: pull_config_hash (normalized hash stored at pull time).
        # Fallback: if file is unchanged since pull, use current config_hash
        # as the base (since local == base when file hasn't been modified).
        base_hashes: dict[str, str] = {}
        for cfg in manifest.configurations:
            key = f"{cfg.component_id}/{cfg.id}"
            pch = cfg.metadata.get("pull_config_hash")
            if pch:
                base_hashes[key] = pch
            elif file_unchanged.get(key):
                # File not modified locally → local data IS the base.
                # Find the matching local_configs entry and hash it.
                for lc in local_configs:
                    if lc.get("config_id") == cfg.id and lc["component_id"] == cfg.component_id:
                        base_hashes[key] = config_hash(lc["data"])
                        break

        changeset = compute_changeset(
            local_configs,
            remote_configs,
            tracked_keys,
            base_hashes or None,
            local_override_hashes or None,
        )

        added = [c for c in changeset if c.change_type == "added"]
        modified = [c for c in changeset if c.change_type == "modified"]
        remote_modified = [c for c in changeset if c.change_type == "remote_modified"]
        conflicts = [c for c in changeset if c.change_type == "conflict"]
        deleted = [c for c in changeset if c.change_type == "deleted"]

        # Detect remote-only configs (new on server, not yet pulled).
        local_keys = {
            f"{e['component_id']}/{e['config_id']}" for e in local_configs if e.get("config_id")
        } | tracked_keys
        remote_only: list[dict[str, str]] = []
        for remote_key, remote_data in remote_configs.items():
            if remote_key not in local_keys:
                parts = remote_key.split("/", 1)
                remote_only.append(
                    {
                        "component_id": parts[0] if parts else "",
                        "config_id": parts[1] if len(parts) > 1 else "",
                        "config_name": remote_data.get("name", ""),
                    }
                )

        return {
            "changes": [c.to_dict() for c in changeset],
            "remote_only": remote_only,
            "summary": {
                "added": len(added),
                "modified": len(modified),
                "remote_modified": len(remote_modified),
                "conflict": len(conflicts),
                "deleted": len(deleted),
                "unchanged": len(local_configs)
                - len(added)
                - len(modified)
                - len(remote_modified)
                - len(conflicts),
                "remote_only": len(remote_only),
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
        allow_plaintext_fallback: bool = False,
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
        all_changes = diff_result["changes"]

        # Only push local-side changes (added, modified, deleted).
        # Skip remote_modified (need pull) and conflict (need resolution).
        pushable_types = {"added", "modified", "deleted"}
        changes = [c for c in all_changes if c["change_type"] in pushable_types]

        # Warn about skipped changes
        skipped = [c for c in all_changes if c["change_type"] not in pushable_types]

        if not changes:
            result: dict[str, Any] = {
                "status": "no_changes",
                "created": 0,
                "updated": 0,
                "deleted": 0,
                "errors": [],
            }
            if skipped:
                result["skipped"] = len(skipped)
                result["skipped_reason"] = "Remote changes detected. Run 'sync pull' first."
            return result

        if dry_run:
            return {
                "status": "dry_run",
                "changes": changes,
                "summary": diff_result["summary"],
            }

        projects = self.resolve_projects([alias])
        project = projects[alias]
        manifest = load_manifest(project_root)

        branch_id = self._resolve_branch_id(project, manifest, project_root)

        client = self._client_factory(project.stack_url, project.token)
        created = 0
        updated = 0
        deleted = 0
        errors: list[dict[str, str]] = []
        pushed_details: list[dict[str, str]] = []
        manifest_dirty = False
        branch_path = self._find_branch_path(manifest, branch_id)

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
                            allow_plaintext_fallback=allow_plaintext_fallback,
                        )
                        if result:
                            new_id = str(result.get("id", ""))
                            # Add to manifest with the API-assigned ID
                            config_dir = project_root / branch_path / config_path_str
                            config_file = config_dir / CONFIG_FILENAME
                            file_hash = self._file_hash(config_file) if config_file.exists() else ""
                            local_data = self._read_config_file(config_dir)
                            if local_data is not None:
                                merge_code_files(component_id, local_data, config_dir)
                                cfg_hash = config_hash(local_data)
                            else:
                                cfg_hash = ""
                            manifest.configurations.append(
                                ManifestConfiguration(
                                    branch_id=branch_id or 0,
                                    component_id=component_id,
                                    id=new_id,
                                    path=config_path_str,
                                    metadata={
                                        "pull_hash": file_hash,
                                        "pull_config_hash": cfg_hash,
                                    },
                                )
                            )
                            manifest_dirty = True
                            created += 1
                            pushed_details.append(change)

                    elif change_type == "modified":
                        self._push_update(
                            client,
                            component_id,
                            config_id,
                            config_path_str,
                            project_root,
                            manifest,
                            branch_id,
                            allow_plaintext_fallback=allow_plaintext_fallback,
                        )
                        # Update both hashes so pull knows local == remote
                        config_dir = project_root / branch_path / config_path_str
                        config_file = config_dir / CONFIG_FILENAME
                        if config_file.exists():
                            new_file_hash = self._file_hash(config_file)
                            local_data = self._read_config_file(config_dir)
                            if local_data is not None:
                                merge_code_files(component_id, local_data, config_dir)
                                new_cfg_hash = config_hash(local_data)
                            else:
                                new_cfg_hash = ""
                            for cfg in manifest.configurations:
                                if cfg.component_id == component_id and cfg.id == config_id:
                                    cfg.metadata["pull_hash"] = new_file_hash
                                    cfg.metadata["pull_config_hash"] = new_cfg_hash
                                    break
                            manifest_dirty = True
                        updated += 1
                        pushed_details.append(change)

                    elif change_type == "deleted":
                        client.delete_config(
                            component_id=component_id,
                            config_id=config_id,
                            branch_id=branch_id,
                        )
                        # Remove from manifest
                        manifest.configurations = [
                            c
                            for c in manifest.configurations
                            if not (c.component_id == component_id and c.id == config_id)
                        ]
                        manifest_dirty = True
                        deleted += 1
                        pushed_details.append(change)

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

        # Save manifest with updated hashes / new IDs / removed entries
        if manifest_dirty:
            save_manifest(project_root, manifest)

        return {
            "status": "pushed",
            "created": created,
            "updated": updated,
            "deleted": deleted,
            "errors": errors,
            "pushed_details": pushed_details,
        }

    @staticmethod
    def _encrypt_secrets_in_config(
        client: Any,
        project_id: int | None,
        component_id: str,
        configuration: dict[str, Any],
        *,
        allow_plaintext_fallback: bool = False,
    ) -> dict[str, Any]:
        """Encrypt #-prefixed secret values in configuration before push.

        Walks the configuration dict recursively, collects all #-prefixed keys
        with unencrypted string values, sends them to the Encryption API,
        and replaces plaintext values with encrypted ones.

        Args:
            client: HTTP client with encrypt_values method.
            project_id: Keboola project ID (encryption skipped if None).
            component_id: Component ID for encryption context.
            configuration: Config dict to encrypt in-place.
            allow_plaintext_fallback: When False (default), encryption failure
                raises KeboolaApiError. When True, logs a warning and continues
                with plaintext values (escape hatch).
        """
        if not project_id:
            return configuration

        # Collect all unencrypted secret values
        secrets: dict[str, str] = {}
        _collect_secrets(configuration, "", secrets)

        if not secrets:
            return configuration

        try:
            encrypted = client.encrypt_values(
                project_id=project_id,
                component_id=component_id,
                data=secrets,
            )
            # Apply encrypted values back into configuration
            _apply_encrypted(configuration, "", encrypted)
            logger.info("Encrypted %d secret value(s) for %s", len(encrypted), component_id)
        except Exception as exc:
            if allow_plaintext_fallback:
                logger.warning(
                    "Failed to encrypt secrets for %s: %s (plaintext fallback allowed)",
                    component_id,
                    exc,
                )
            else:
                raise KeboolaApiError(
                    message=(
                        f"Encryption failed for {component_id}: {exc}. "
                        f"Refusing to push plaintext secrets. "
                        f"Use --allow-plaintext-on-encrypt-failure to override."
                    ),
                    status_code=0,
                    error_code="ENCRYPTION_FAILED",
                ) from exc

        return configuration

    def _push_create(
        self,
        client: Any,
        component_id: str,
        config_path_str: str,
        project_root: Path,
        manifest: Manifest,
        branch_id: int | None,
        *,
        allow_plaintext_fallback: bool = False,
    ) -> dict[str, Any] | None:
        """Create a new config from a local _config.yml file."""
        branch_path = self._find_branch_path(manifest, branch_id)
        config_dir = project_root / branch_path / config_path_str
        local_data = self._read_config_file(config_dir)
        if local_data is None:
            return None

        # Preserve pristine data for writeback (merge_code_files mutates
        # local_data by injecting parameters.blocks which should not end
        # up in _config.yml).
        pristine_data = copy.deepcopy(local_data)

        # Merge code files (transform.sql, transform.py, code.py) back into config
        merge_code_files(component_id, local_data, config_dir)

        name, description, configuration = local_config_to_api(local_data)

        # Encrypt #-prefixed secrets before sending to API
        project_id = manifest.project.id if manifest.project else None
        configuration = self._encrypt_secrets_in_config(
            client,
            project_id,
            component_id,
            configuration,
            allow_plaintext_fallback=allow_plaintext_fallback,
        )

        result = client.create_config(
            component_id=component_id,
            name=name,
            configuration=configuration,
            description=description,
            branch_id=branch_id,
        )
        new_config_id = result.get("id", "")
        logger.info(
            "Created config %s/%s (ID: %s)",
            component_id,
            name,
            new_config_id,
        )

        # Write back: update local file with config_id + encrypted secrets.
        # Use pristine_data so blocks/code stay only in their code files.
        self._writeback_after_push(pristine_data, config_dir, new_config_id, configuration)

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
        *,
        allow_plaintext_fallback: bool = False,
    ) -> None:
        """Update an existing config from a local _config.yml file."""
        branch_path = self._find_branch_path(manifest, branch_id)
        config_dir = project_root / branch_path / config_path_str
        local_data = self._read_config_file(config_dir)
        if local_data is None:
            raise FileNotFoundError(f"Config file not found: {config_dir / CONFIG_FILENAME}")

        # Preserve pristine data for writeback (merge_code_files mutates
        # local_data by injecting parameters.blocks which should not end
        # up in _config.yml).
        pristine_data = copy.deepcopy(local_data)

        # Merge code files (transform.sql, transform.py, code.py) back into config
        merge_code_files(component_id, local_data, config_dir)

        name, description, configuration = local_config_to_api(local_data)

        # Encrypt #-prefixed secrets before sending to API
        project_id = manifest.project.id if manifest.project else None
        configuration = self._encrypt_secrets_in_config(
            client,
            project_id,
            component_id,
            configuration,
            allow_plaintext_fallback=allow_plaintext_fallback,
        )

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

        # Write back: update local file with encrypted secrets.
        # Use pristine_data so blocks/code stay only in their code files.
        self._writeback_after_push(pristine_data, config_dir, config_id, configuration)

    def _writeback_after_push(
        self,
        local_data: dict[str, Any],
        config_dir: Path,
        config_id: str,
        pushed_configuration: dict[str, Any],
    ) -> None:
        """Update local _config.yml after a successful push.

        Writes back:
        - _keboola.config_id (assigned by API on first create)
        - Encrypted secret values (so local matches remote state)
        """
        # Ensure _keboola section exists and has config_id
        keboola_meta = local_data.setdefault("_keboola", {})
        if config_id:
            keboola_meta["config_id"] = config_id

        # Apply encrypted values from the pushed configuration back to local_data
        pushed_params = pushed_configuration.get("parameters", {})
        local_params = local_data.get("parameters", {})
        if pushed_params and local_params:
            _apply_encrypted_to_local(local_params, pushed_params)

        self._write_config_file(config_dir, local_data)
        logger.debug("Updated local config at %s after push", config_dir)

    # ------------------------------------------------------------------
    # bulk operations (all projects)
    # ------------------------------------------------------------------

    def pull_all(
        self,
        base_dir: Path,
        force: bool = False,
        dry_run: bool = False,
        job_limit: int = DEFAULT_JOBS_PER_CONFIG,
        no_storage: bool = False,
        no_jobs: bool = False,
        with_samples: bool = False,
        sample_limit: int = DEFAULT_SAMPLE_LIMIT,
        max_samples: int = DEFAULT_MAX_SAMPLES,
    ) -> dict[str, Any]:
        """Pull all registered projects in parallel.

        For each project, creates ``base_dir/<alias>/`` and initializes
        if no manifest exists yet, then pulls.

        Args:
            base_dir: Parent directory; each project gets a subdirectory.
            force: Overwrite local files without checking.
            dry_run: Compute what would be pulled but don't write.
            job_limit: Max jobs per config to pull.
            no_storage: Skip storage metadata download.
            no_jobs: Skip per-config jobs download.
            with_samples: Download table data samples.
            sample_limit: Max rows per sample.
            max_samples: Max number of tables to sample.

        Returns:
            Dict with per-project results and a summary.
        """
        projects = self.resolve_projects(None)
        results: dict[str, Any] = {}
        success_count = 0
        failed_count = 0

        def _worker(alias: str) -> None:
            nonlocal success_count, failed_count
            project_root = base_dir / alias
            manifest_path = project_root / KEBOOLA_DIR_NAME / "manifest.json"
            try:
                if not manifest_path.exists():
                    self.init_sync(alias, project_root)
                result = self.pull(
                    alias,
                    project_root,
                    force=force,
                    dry_run=dry_run,
                    job_limit=job_limit,
                    no_storage=no_storage,
                    no_jobs=no_jobs,
                    with_samples=with_samples,
                    sample_limit=sample_limit,
                    max_samples=max_samples,
                )
                results[alias] = result
                success_count += 1
            except Exception as exc:
                results[alias] = {"error": str(exc)}
                failed_count += 1

        max_workers = min(len(projects), self._resolve_max_workers()) if projects else 1
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_worker, alias): alias for alias in projects}
            for future in as_completed(futures):
                # Exceptions are captured inside _worker; this catches truly
                # unexpected failures (e.g. threading errors).
                try:
                    future.result()
                except Exception as exc:
                    alias = futures[future]
                    results[alias] = {"error": str(exc)}
                    failed_count += 1

        total = len(projects)
        return {
            "projects": results,
            "summary": {
                "total": total,
                "success": success_count,
                "failed": failed_count,
            },
        }

    def diff_all(self, base_dir: Path) -> dict[str, Any]:
        """Diff all registered projects that have a local manifest.

        Projects without an existing manifest are skipped.

        Args:
            base_dir: Parent directory containing per-project subdirectories.

        Returns:
            Dict with per-project diff results, a summary, and skipped list.
        """
        projects = self.resolve_projects(None)
        results: dict[str, Any] = {}
        skipped: list[str] = []
        success_count = 0
        failed_count = 0

        # Partition into actionable vs skipped
        actionable: list[str] = []
        for alias in projects:
            manifest_path = base_dir / alias / KEBOOLA_DIR_NAME / "manifest.json"
            if manifest_path.exists():
                actionable.append(alias)
            else:
                skipped.append(alias)

        def _worker(alias: str) -> None:
            nonlocal success_count, failed_count
            project_root = base_dir / alias
            try:
                result = self.diff(alias, project_root)
                results[alias] = result
                success_count += 1
            except Exception as exc:
                results[alias] = {"error": str(exc)}
                failed_count += 1

        max_workers = min(len(actionable), self._resolve_max_workers()) if actionable else 1
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_worker, alias): alias for alias in actionable}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    alias = futures[future]
                    results[alias] = {"error": str(exc)}
                    failed_count += 1

        total = len(projects)
        return {
            "projects": results,
            "summary": {
                "total": total,
                "success": success_count,
                "failed": failed_count,
                "skipped": len(skipped),
            },
            "skipped": skipped,
        }

    def push_all(
        self,
        base_dir: Path,
        dry_run: bool = False,
        force: bool = False,
        allow_plaintext_fallback: bool = False,
    ) -> dict[str, Any]:
        """Push all registered projects that have a local manifest.

        Projects without an existing manifest are skipped.

        Args:
            base_dir: Parent directory containing per-project subdirectories.
            dry_run: Compute changes but don't execute them.
            force: Allow deletions without extra confirmation.
            allow_plaintext_fallback: Allow push with plaintext secrets on
                encryption failure.

        Returns:
            Dict with per-project push results, a summary, and skipped list.
        """
        projects = self.resolve_projects(None)
        results: dict[str, Any] = {}
        skipped: list[str] = []
        success_count = 0
        failed_count = 0

        # Partition into actionable vs skipped
        actionable: list[str] = []
        for alias in projects:
            manifest_path = base_dir / alias / KEBOOLA_DIR_NAME / "manifest.json"
            if manifest_path.exists():
                actionable.append(alias)
            else:
                skipped.append(alias)

        def _worker(alias: str) -> None:
            nonlocal success_count, failed_count
            project_root = base_dir / alias
            try:
                result = self.push(
                    alias,
                    project_root,
                    dry_run=dry_run,
                    force=force,
                    allow_plaintext_fallback=allow_plaintext_fallback,
                )
                results[alias] = result
                success_count += 1
            except Exception as exc:
                results[alias] = {"error": str(exc)}
                failed_count += 1

        max_workers = min(len(actionable), self._resolve_max_workers()) if actionable else 1
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_worker, alias): alias for alias in actionable}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    alias = futures[future]
                    results[alias] = {"error": str(exc)}
                    failed_count += 1

        total = len(projects)
        return {
            "projects": results,
            "summary": {
                "total": total,
                "success": success_count,
                "failed": failed_count,
                "skipped": len(skipped),
            },
            "skipped": skipped,
        }

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

    @staticmethod
    def _resolve_branch_id(
        project: Any,
        manifest: "Manifest",
        project_root: Path,
    ) -> int | None:
        """Resolve the Keboola branch ID for sync operations.

        Priority:
        1. Git-branching mode: read branch-mapping.json for current git branch
        2. ``active_branch_id`` from project config (``kbagent branch use``)
        3. First branch in manifest (production fallback)

        Raises ``ConfigError`` if git-branching is enabled but the current
        branch is not linked (prevents accidental production writes).
        """
        from ..sync.branch_mapping import load_branch_mapping
        from ..sync.git_utils import get_current_branch

        if manifest.git_branching.enabled:
            git_branch = get_current_branch(project_root)
            if git_branch:
                try:
                    mapping = load_branch_mapping(project_root)
                    entry = mapping.get(git_branch)
                    if entry is not None:
                        # entry.keboola_id is None for production (default branch)
                        return entry.keboola_id
                except FileNotFoundError:
                    pass
                # Branch not linked -- block operation
                raise ConfigError(
                    f"Git branch '{git_branch}' is not linked to a Keboola branch. "
                    f"Run 'kbagent sync branch-link --project ALIAS' first."
                )

        # Non git-branching: use active_branch_id or manifest fallback
        branch_id = project.active_branch_id
        if not branch_id and manifest.branches:
            branch_id = manifest.branches[0].id
        return branch_id

    # ------------------------------------------------------------------
    # Storage metadata / jobs / samples helpers
    # ------------------------------------------------------------------

    def _write_storage_metadata(
        self,
        project_root: Path,
        buckets: list[dict[str, Any]],
        tables: list[dict[str, Any]],
        samples: dict[str, str],
    ) -> dict[str, int]:
        """Write storage bucket and table metadata to the filesystem.

        Creates:
            storage/buckets.json - list of all buckets
            storage/tables/{bucket_id}/{table_name}.json - per-table metadata
            storage/samples/{bucket}/{table}/sample.csv - data samples (if any)

        Returns:
            Dict with counts: buckets, tables, samples written.
        """
        storage_dir = project_root / STORAGE_DIR_NAME
        storage_dir.mkdir(parents=True, exist_ok=True)

        # Write buckets index
        bucket_summaries = [
            {
                "id": b.get("id", ""),
                "name": b.get("name", ""),
                "stage": b.get("stage", ""),
                "description": b.get("description", ""),
                "tables_count": b.get("tablesCount", 0),
                "data_size_bytes": b.get("dataSizeBytes", 0),
                "metadata": b.get("metadata", []),
            }
            for b in buckets
        ]
        buckets_file = storage_dir / STORAGE_BUCKETS_FILENAME
        buckets_file.write_text(
            json.dumps(bucket_summaries, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Group tables by bucket
        tables_by_bucket: dict[str, list[dict[str, Any]]] = {}
        for t in tables:
            bucket_id = (
                t.get("bucket", {}).get("id", "")
                if isinstance(t.get("bucket"), dict)
                else t.get("bucketId", "")
            )
            if not bucket_id:
                continue
            tables_by_bucket.setdefault(bucket_id, []).append(t)

        tables_written = 0
        tables_dir = storage_dir / "tables"
        for bucket_id, bucket_tables in tables_by_bucket.items():
            # Sanitize bucket_id for filesystem (replace dots with dashes)
            safe_bucket = bucket_id.replace(".", "-")
            bucket_dir = tables_dir / safe_bucket
            bucket_dir.mkdir(parents=True, exist_ok=True)

            for t in bucket_tables:
                table_name = t.get("name", "unknown")
                table_meta = {
                    "id": t.get("id", ""),
                    "name": table_name,
                    "primary_key": t.get("primaryKey", []),
                    "columns": t.get("columns", []),
                    "rows_count": t.get("rowsCount", 0),
                    "data_size_bytes": t.get("dataSizeBytes", 0),
                    "last_import_date": t.get("lastImportDate", ""),
                    "last_change_date": t.get("lastChangeDate", ""),
                    "description": t.get("description", ""),
                    "metadata": t.get("metadata", []),
                    "column_metadata": t.get("columnMetadata", {}),
                }
                table_file = bucket_dir / f"{table_name}.json"
                table_file.write_text(
                    json.dumps(table_meta, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                tables_written += 1

        # Write samples
        samples_written = 0
        if samples:
            samples_dir = storage_dir / STORAGE_SAMPLES_DIR_NAME
            for table_id, csv_data in samples.items():
                # table_id format: "in.c-bucket.table" -> samples/in-c-bucket/table/
                parts = table_id.split(".", 2)
                if len(parts) >= 3:
                    safe_bucket = f"{parts[0]}-{parts[1]}"
                    table_name = parts[2]
                else:
                    safe_bucket = table_id.replace(".", "-")
                    table_name = "data"
                sample_dir = samples_dir / safe_bucket / table_name
                sample_dir.mkdir(parents=True, exist_ok=True)

                # Mask encrypted columns in CSV
                masked_csv = self._mask_encrypted_columns(csv_data)
                (sample_dir / "sample.csv").write_text(masked_csv, encoding="utf-8")
                samples_written += 1

        return {
            "buckets": len(buckets),
            "tables": tables_written,
            "samples": samples_written,
        }

    def _fetch_jobs_per_config(
        self,
        client: Any,
        components: list[dict[str, Any]],
        job_limit: int,
    ) -> list[dict[str, Any]]:
        """Fetch jobs per config via /search/jobs in parallel.

        Used as fallback when the grouped-jobs API cannot return all configs
        in a single call (jobsPerGroup * limit <= 500 constraint).

        Returns data in the same format as list_jobs_grouped() so that
        _write_per_config_jobs() works unchanged.
        """
        config_pairs: list[tuple[str, str]] = []
        for comp in components:
            comp_id = comp.get("id", "")
            for cfg in comp.get("configurations", []):
                cfg_id = str(cfg.get("id", ""))
                if comp_id and cfg_id:
                    config_pairs.append((comp_id, cfg_id))

        if not config_pairs:
            return []

        results: list[dict[str, Any]] = []
        lock = threading.Lock()
        max_workers = min(len(config_pairs), self._resolve_max_workers())

        def _fetch_one(pair: tuple[str, str]) -> None:
            comp_id, cfg_id = pair
            try:
                jobs = client.list_jobs(
                    component_id=comp_id,
                    config_id=cfg_id,
                    limit=job_limit,
                )
                if jobs:
                    with lock:
                        results.append(
                            {
                                "group": {"componentId": comp_id, "configId": cfg_id},
                                "jobs": jobs,
                            }
                        )
            except Exception:
                logger.debug("Failed to fetch jobs for %s/%s", comp_id, cfg_id, exc_info=True)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_fetch_one, pair) for pair in config_pairs]
            for future in as_completed(futures):
                future.result()  # propagate unexpected errors

        return results

    def _write_per_config_jobs(
        self,
        branch_dir: Path,
        configurations: list[ManifestConfiguration],
        jobs_grouped: list[dict[str, Any]],
    ) -> int:
        """Write _jobs.jsonl files next to each configuration.

        Matches grouped jobs to configs by componentId+configId,
        then writes a JSONL file with light job records.

        Returns:
            Number of _jobs.jsonl files written.
        """
        # Build lookup: (component_id, config_id) -> list of jobs
        jobs_by_config: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for group in jobs_grouped:
            group_key = group.get("group", {})
            component_id = group_key.get("componentId", "")
            config_id = group_key.get("configId", "")
            if component_id and config_id:
                jobs_by_config[(component_id, config_id)] = group.get("jobs", [])

        files_written = 0
        for cfg in configurations:
            key = (cfg.component_id, cfg.id)
            jobs = jobs_by_config.get(key)
            if not jobs:
                continue

            config_dir = branch_dir / cfg.path
            config_dir.mkdir(parents=True, exist_ok=True)
            jobs_file = config_dir / JOBS_FILENAME

            lines: list[str] = []
            for job in jobs:
                light_job: dict[str, Any] = {
                    "id": str(job.get("id", "")),
                    "status": job.get("status", ""),
                    "start_time": job.get("startTime", ""),
                    "end_time": job.get("endTime", ""),
                    "duration_seconds": job.get("durationSeconds", 0),
                }
                if job.get("mode") and job["mode"] != "run":
                    light_job["mode"] = job["mode"]
                # Include error message for failed/warning jobs
                status = job.get("status", "")
                if status in ("error", "warning", "terminated", "cancelled"):
                    result = job.get("result", {})
                    if isinstance(result, dict) and result.get("message"):
                        light_job["error_message"] = result["message"]
                lines.append(json.dumps(light_job, ensure_ascii=False))

            jobs_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            files_written += 1

        return files_written

    def _fetch_samples(
        self,
        client: Any,
        tables: list[dict[str, Any]],
        sample_limit: int,
        max_samples: int,
    ) -> dict[str, str]:
        """Fetch CSV data previews for tables, respecting limits.

        Selects tables sorted by rowsCount descending (largest first),
        limited to max_samples tables.

        Returns:
            Dict mapping table_id -> CSV string.
        """
        # Sort by rows count desc, pick top N
        sorted_tables = sorted(
            [t for t in tables if t.get("rowsCount", 0) > 0],
            key=lambda t: t.get("rowsCount", 0),
            reverse=True,
        )[:max_samples]

        # Storage API sync export limit
        max_sync_columns = 30

        samples: dict[str, str] = {}
        for t in sorted_tables:
            table_id = t.get("id", "")
            if not table_id:
                continue
            try:
                # Limit columns to max_sync_columns to avoid API 400 error
                all_columns = t.get("columns", [])
                columns = (
                    all_columns[:max_sync_columns] if len(all_columns) > max_sync_columns else None
                )
                csv_data = client.get_table_data_preview(
                    table_id, limit=sample_limit, columns=columns
                )
                samples[table_id] = csv_data
            except Exception:
                logger.warning("Failed to fetch sample for %s", table_id, exc_info=True)

        return samples

    @staticmethod
    def _mask_encrypted_columns(csv_data: str) -> str:
        """Mask encrypted column values in CSV data.

        Encrypted columns in Keboola start with '#' in the column name.
        Their values are replaced with the masked placeholder.
        """
        if not csv_data:
            return csv_data

        lines = csv_data.split("\n")
        if not lines:
            return csv_data

        # Parse header to find encrypted column indices
        import csv
        import io

        reader = csv.reader(io.StringIO(lines[0]))
        try:
            header = next(reader)
        except StopIteration:
            return csv_data

        encrypted_indices = [
            i for i, col in enumerate(header) if col.startswith(ENCRYPTED_COLUMN_PREFIX)
        ]
        if not encrypted_indices:
            return csv_data

        # Rewrite CSV with masked values
        output = io.StringIO()
        writer = csv.writer(output)
        full_reader = csv.reader(io.StringIO(csv_data))
        for row_idx, row in enumerate(full_reader):
            if row_idx == 0:
                writer.writerow(row)  # header unchanged
            else:
                for idx in encrypted_indices:
                    if idx < len(row):
                        row[idx] = ENCRYPTED_COLUMN_MASK
                writer.writerow(row)

        return output.getvalue()

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

    def _find_branch_path(self, manifest: Manifest, branch_id: int | None) -> str:
        """Find the branch directory name for a given branch ID.

        When ``branch_id`` is ``None`` (production / default branch),
        return the first branch path from the manifest.
        """
        if branch_id is None:
            # Production -- use default branch path
            return manifest.branches[0].path if manifest.branches else "main"
        for branch in manifest.branches:
            if branch.id == branch_id:
                return branch.path
        # Fallback to default branch
        return manifest.branches[0].path if manifest.branches else "main"

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


# ---------------------------------------------------------------------------
# Module-level helpers for secret encryption
# ---------------------------------------------------------------------------

_ENCRYPTED_PREFIX = "KBC::"


def _is_secret_key(key: str) -> bool:
    """Check if a YAML key represents a secret (starts with #)."""
    return isinstance(key, str) and key.startswith("#")


def _is_already_encrypted(value: Any) -> bool:
    """Check if a value is already encrypted (KBC::*Secure::* prefix)."""
    return isinstance(value, str) and value.startswith(_ENCRYPTED_PREFIX)


def _collect_secrets(obj: Any, path_prefix: str, result: dict[str, str]) -> None:
    """Recursively collect unencrypted #-prefixed secret values.

    Builds a flat dict of {#path_key: plaintext_value} suitable for
    the Encryption API. The Encryption API requires all keys to start
    with '#'.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            if _is_secret_key(key) and isinstance(value, str) and not _is_already_encrypted(value):
                # Use the key directly for the encrypt API
                encrypt_key = f"#{path_prefix}{key}" if path_prefix else key
                result[encrypt_key] = value
            elif isinstance(value, (dict, list)):
                child_prefix = f"{path_prefix}{key}." if path_prefix else f"{key}."
                _collect_secrets(value, child_prefix, result)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            child_prefix = f"{path_prefix}[{i}]."
            _collect_secrets(item, child_prefix, result)


def _apply_encrypted(obj: Any, path_prefix: str, encrypted: dict[str, str]) -> None:
    """Recursively apply encrypted values back into the configuration dict."""
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            value = obj[key]
            if _is_secret_key(key) and isinstance(value, str) and not _is_already_encrypted(value):
                encrypt_key = f"#{path_prefix}{key}" if path_prefix else key
                if encrypt_key in encrypted:
                    obj[key] = encrypted[encrypt_key]
            elif isinstance(value, (dict, list)):
                child_prefix = f"{path_prefix}{key}." if path_prefix else f"{key}."
                _apply_encrypted(value, child_prefix, encrypted)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            child_prefix = f"{path_prefix}[{i}]."
            _apply_encrypted(item, child_prefix, encrypted)


def _apply_encrypted_to_local(local: Any, pushed: Any) -> None:
    """Copy encrypted secret values from pushed config back into local data.

    Walks both dicts in parallel. Where pushed has an encrypted value
    for a #-key, replaces the local plaintext with it.
    """
    if isinstance(local, dict) and isinstance(pushed, dict):
        for key in local:
            if key not in pushed:
                continue
            if _is_secret_key(key) and _is_already_encrypted(pushed[key]):
                local[key] = pushed[key]
            elif isinstance(local[key], dict) and isinstance(pushed[key], dict):
                _apply_encrypted_to_local(local[key], pushed[key])
            elif isinstance(local[key], list) and isinstance(pushed[key], list):
                for i in range(min(len(local[key]), len(pushed[key]))):
                    _apply_encrypted_to_local(local[key][i], pushed[key][i])
