"""Sync service - business logic for project pull/push/status operations.

Handles downloading Keboola project configurations to the local filesystem
in a dev-friendly format (YAML configs), and tracking local changes.
"""

import copy
import hashlib
import json
import logging
import shutil
import subprocess
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
    KEBOOLA_DIR_NAME,
    MANIFEST_VERSION,
)
from ..errors import ConfigError, ErrorCode, KeboolaApiError, SyncConflictError
from ..sync.code_extraction import extract_code_files, merge_code_files
from ..sync.config_format import (
    api_config_to_local,
    api_row_to_local,
    classify_component_type,
    local_config_to_api,
    local_row_to_api,
)
from ..sync.diff_engine import compute_changeset, compute_row_changeset, config_hash
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
from ..sync.naming import config_path, config_row_path, sanitize_name
from ._encryption import (
    encrypt_secrets_in_config,
    find_plaintext_secret_keys,
)
from ._sync_bindings import resolve_flow_task_bindings, resolve_variable_bindings
from ._sync_branch import (
    branch_link as _branch_link,
)
from ._sync_branch import (
    branch_status as _branch_status,
)
from ._sync_branch import (
    branch_unlink as _branch_unlink,
)
from ._sync_bulk import (
    diff_all as _bulk_diff_all,
)
from ._sync_bulk import (
    pull_all as _bulk_pull_all,
)
from ._sync_bulk import (
    push_all as _bulk_push_all,
)
from ._sync_clone import clone_project as _clone_project_impl
from ._sync_models import CreatedConfig, LocalConfigHashes
from ._sync_storage import (
    fetch_jobs_per_config,
    fetch_samples,
    write_per_config_jobs,
    write_storage_metadata,
)
from ._sync_writeback import (
    propagate_kbc_metadata,
    writeback_after_push,
    writeback_create_config_in_manifest,
    writeback_create_row_in_manifest,
)
from .base import BaseService

logger = logging.getLogger(__name__)

# Companion files extracted alongside a config's ``_config.yml`` whose hashes
# are tracked so ``sync diff`` notices edits to code/description files.
_EXTRA_HASH_FILENAMES: tuple[str, ...] = (
    "_description.md",
    "transform.sql",
    "transform.py",
    "code.py",
    "pyproject.toml",
)


def _ensure_within_branch(
    branch_dir: Path,
    config_dir: Path,
    component_id: str,
    config_id: str,
) -> None:
    """Reject paths that escape the branch directory (issue #269 sec-01).

    Resolves both paths and checks that *config_dir* is contained in
    *branch_dir*. Raises ConfigError if not. Defense-in-depth on top of
    ``naming.sanitize_path_segment()`` so a regression in either layer
    cannot turn into a path-traversal write.
    """
    try:
        branch_resolved = branch_dir.resolve()
        config_resolved = config_dir.resolve()
    except OSError as exc:
        raise ConfigError(f"Cannot resolve sync path: {exc}") from exc
    if not config_resolved.is_relative_to(branch_resolved):
        raise ConfigError(
            f"Config path escapes sync workspace (component='{component_id}', "
            f"config_id='{config_id}'). Refusing to write outside "
            f"'{branch_resolved}'. This indicates a malformed API response "
            f"or a regression in path sanitization."
        )


def scan_synced_plaintext_secrets(
    project_root: Path, manifest: Manifest | None = None
) -> list[dict[str, Any]]:
    """Find in-sync configs/rows whose local files still hold plaintext #-secrets.

    Regression guard for issue #378. A config that is *in sync* with the remote
    (local file hash == manifest ``pull_hash``) but whose ``#``-prefixed values
    are NOT ``KBC::``-encrypted means the remote is holding that secret in
    plaintext -- the value passed through the sync baseline unencrypted (written
    by a pre-0.54.0 ``config``/``sync`` path, or pulled from an already-leaked
    remote). Pending local edits (hash != ``pull_hash``) are deliberately
    skipped: there a ``sync push`` on >=0.54.0 encrypts on write, so flagging
    them would be noise.

    Read-only -- filesystem + manifest only, no API client. Returns one entry
    per affected config/row with the secret *key paths* (never the values).

    Args:
        project_root: Root of the sync working tree.
        manifest: Already-loaded manifest to reuse (callers like
            :meth:`SyncService.status` load it themselves). Loaded from
            *project_root* when ``None``.

    Raises:
        FileNotFoundError: if *manifest* is ``None`` and *project_root* has no
            ``.keboola/manifest.json``.
    """
    if manifest is None:
        manifest = load_manifest(project_root)
    warnings: list[dict[str, Any]] = []

    def _branch_path(branch_id: int | None) -> str:
        if branch_id is not None:
            for b in manifest.branches:
                if b.id == branch_id:
                    return b.path
        return manifest.branches[0].path if manifest.branches else "main"

    def _read_yaml(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _in_sync(path: Path, pull_hash: str) -> bool:
        if not pull_hash or not path.exists():
            return False
        return hashlib.sha256(path.read_bytes()).hexdigest() == pull_hash

    for cfg in manifest.configurations:
        config_dir = project_root / _branch_path(cfg.branch_id) / cfg.path
        config_file = config_dir / CONFIG_FILENAME
        data = _read_yaml(config_file)
        if data is not None and _in_sync(config_file, cfg.metadata.get("pull_hash", "")):
            _name, _desc, configuration = local_config_to_api(data)
            keys = find_plaintext_secret_keys(configuration)
            if keys:
                warnings.append(
                    {
                        "component_id": cfg.component_id,
                        "config_id": cfg.id,
                        "path": str(cfg.path),
                        "scope": "config",
                        "secret_keys": keys,
                    }
                )

        for row in cfg.rows:
            row_file = config_dir / row.path / CONFIG_FILENAME
            row_data = _read_yaml(row_file)
            if row_data is not None and _in_sync(row_file, row.metadata.get("pull_hash", "")):
                _name, _desc, row_cfg = local_row_to_api(row_data, cfg.component_id)
                row_keys = find_plaintext_secret_keys(row_cfg)
                if row_keys:
                    warnings.append(
                        {
                            "component_id": cfg.component_id,
                            "config_id": cfg.id,
                            "row_id": row.id,
                            "path": f"{cfg.path}/{row.path}",
                            "scope": "row",
                            "secret_keys": row_keys,
                        }
                    )

    return warnings


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
        adopt_existing: bool = False,
    ) -> dict[str, Any]:
        """Initialize a sync working directory for a project.

        Creates the ``.keboola/`` directory with ``manifest.json``.
        Fetches project metadata from the API to populate the manifest.

        Args:
            alias: Project alias from config store.
            project_root: Root directory for the sync working tree.
            git_branching: Enable git-branching mode.
            adopt_existing: If True and a manifest already exists, validate it
                against the alias's project_id and normalise it (idempotent
                upgrade of a ``kbc``-written manifest) instead of refusing.

        Returns:
            Dict with initialization stats and created file paths.

        Raises:
            ConfigError: If the project alias is not found, or if
                ``adopt_existing`` is True but the manifest's project_id does
                not match the alias's project.
            FileExistsError: If manifest already exists and adopt_existing is
                False (use ``sync pull`` to update).
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        keboola_dir = project_root / KEBOOLA_DIR_NAME
        manifest_path = keboola_dir / "manifest.json"
        if manifest_path.exists():
            if adopt_existing:
                return self._adopt_existing_manifest(alias, project_root, project)
            raise FileExistsError(
                f"Manifest already exists at {manifest_path}. "
                "Use 'sync pull' to update, 'sync init --adopt-existing' to adopt a "
                "kbc-written manifest, or delete .keboola/ to reinitialize."
            )

        # Fetch project info from API
        client = self._client_factory(project.stack_url, project.token)
        with client:
            token_info = client.verify_token()
            branches = client.list_dev_branches()

        project_id = token_info.project_id
        if project_id is None:
            raise ConfigError("Token verification returned no project ID; cannot build manifest.")
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
                defaultBranch=default_branch_name,
            )

        # Build manifest
        manifest = Manifest(
            version=MANIFEST_VERSION,
            project=ManifestProject(id=project_id, apiHost=api_host),
            allowTargetEnv=True,
            gitBranching=git_branching_config,
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

    def _adopt_existing_manifest(
        self,
        alias: str,
        project_root: Path,
        project: Any,
    ) -> dict[str, Any]:
        """Validate and normalise an existing manifest written by kbc or kbagent.

        Idempotent: loads the manifest, confirms project_id matches the alias,
        then saves it back through kbagent's serialiser (fills missing optional
        fields with defaults, normalises camelCase keys, preserves all
        existing content).

        Raises:
            ConfigError: If the manifest's project_id doesn't match the alias.
        """
        existing = load_manifest(project_root)

        client = self._client_factory(project.stack_url, project.token)
        with client:
            token_info = client.verify_token()

        if existing.project.id != token_info.project_id:
            raise ConfigError(
                f"Manifest project_id={existing.project.id} does not match alias "
                f"'{alias}' project_id={token_info.project_id}; refusing to overwrite. "
                "Check that --project points to the correct alias."
            )

        api_host = project.stack_url.replace("https://", "").rstrip("/")
        save_manifest(project_root, existing)

        return {
            "status": "adopted",
            "project_id": token_info.project_id,
            "project_alias": alias,
            "api_host": api_host,
            "git_branching": existing.git_branching.enabled,
            "default_branch": existing.git_branching.default_branch,
            "files_created": [],
        }

    # ------------------------------------------------------------------
    # pull
    # ------------------------------------------------------------------

    def _is_conflict(
        self,
        config_file: Path,
        old_pull_hash: str,
        old_cfg_hash: str,
        api_cfg_hash: str,
    ) -> bool:
        """True iff the file is locally modified AND the remote also changed.

        A 3-way conflict needs both a stored ``pull_hash`` (the synced file
        state) and a stored ``pull_config_hash`` (the synced remote state);
        without either we cannot prove a conflict, so return False -- be
        conservative, ``--force`` must not abort on incomplete bookkeeping.
        A missing local file is not a content conflict (nothing to lose).
        """
        if not old_pull_hash or not old_cfg_hash:
            return False
        if not config_file.exists():
            return False
        locally_modified = self._file_hash(config_file) != old_pull_hash
        remote_changed = api_cfg_hash != old_cfg_hash
        return locally_modified and remote_changed

    def _detect_force_pull_conflicts(
        self,
        components: list[dict[str, Any]],
        branch_dir: Path,
        *,
        existing_keys: set[str],
        existing_paths: dict[str, str],
        existing_file_hashes: dict[str, str],
        existing_config_hashes: dict[str, str],
        existing_rows: dict[str, dict[str, str]],
    ) -> list[dict[str, str]]:
        """Return configs/rows a ``--force`` pull would clobber as conflicts.

        A *conflict* is a config (or row) that is BOTH locally modified (its
        on-disk ``_config.yml`` hash differs from the manifest ``pull_hash``)
        AND changed on the remote since the last pull (the freshly fetched
        config hash differs from ``pull_config_hash``).  That is the only case
        where ``--force`` must stop: local and remote have diverged, so neither
        "take remote" nor "keep local" is safe without the user deciding.

        Configs only locally modified (remote unchanged) are NOT conflicts --
        ``--force`` preserves them so their pending delta stays pushable.
        Brand-new remote configs and configs whose local file is missing are
        skipped (nothing local to lose).  Read-only: hashes but writes nothing.
        """
        conflicts: list[dict[str, str]] = []
        for component in components:
            component_id = component.get("id", "")
            if component_id in ALWAYS_IGNORED_COMPONENTS:
                continue
            for cfg in component.get("configurations", []):
                config_id = str(cfg.get("id", ""))
                lookup_key = f"{component_id}/{config_id}"
                if lookup_key not in existing_keys:
                    continue  # brand-new remote config -- nothing local to lose

                rel_path = existing_paths.get(lookup_key, "")
                api_cfg_hash = config_hash(api_config_to_local(component_id, cfg, config_id))
                if self._is_conflict(
                    branch_dir / rel_path / CONFIG_FILENAME,
                    existing_file_hashes.get(lookup_key, ""),
                    existing_config_hashes.get(lookup_key, ""),
                    api_cfg_hash,
                ):
                    conflicts.append(
                        {
                            "scope": "config",
                            "component_id": component_id,
                            "config_id": config_id,
                            "config_name": str(cfg.get("name", "untitled")),
                            "path": rel_path,
                        }
                    )

                # Row-level conflicts (same 3-way rule, per row).
                config_dir = branch_dir / rel_path
                for row in cfg.get("rows", []):
                    row_id = str(row.get("id", ""))
                    existing_row = existing_rows.get(f"{component_id}/{config_id}/{row_id}")
                    if not existing_row:
                        continue
                    row_rel_path = existing_row.get("path", "")
                    if self._is_conflict(
                        config_dir / row_rel_path / CONFIG_FILENAME,
                        existing_row.get("pull_hash", ""),
                        existing_row.get("pull_config_hash", ""),
                        config_hash(api_row_to_local(row, component_id)),
                    ):
                        conflicts.append(
                            {
                                "scope": "row",
                                "component_id": component_id,
                                "config_id": config_id,
                                "config_name": (
                                    f"{cfg.get('name', 'untitled')}/{row.get('name', 'untitled')}"
                                ),
                                "path": f"{rel_path}/{row_rel_path}",
                                "row_id": row_id,
                            }
                        )
        return conflicts

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
        branch_override: int | None = None,
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
            branch_override: If set, pull from this branch ID rather than
                the resolved active/manifest branch (CLI ``--branch``).

        Returns:
            Dict with pull statistics (configs, rows, files written).
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        # Load or verify manifest exists
        manifest = load_manifest(project_root)

        # Determine branch to pull from (git-branching aware)
        branch_id = self._resolve_branch_id(
            project, manifest, project_root, branch_override=branch_override
        )

        # Fetch all components with configs from API (+ storage metadata + jobs)
        client = self._client_factory(project.stack_url, project.token)
        buckets_data: list[dict[str, Any]] = []
        tables_data: list[dict[str, Any]] = []
        jobs_grouped: list[dict[str, Any]] = []
        samples_data: dict[str, str] = {}  # table_id -> CSV string
        with client:
            components = client.list_components_with_configs(branch_id=branch_id)
            self._ensure_branch_registered(manifest, branch_id, client)

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
                        jobs_grouped = fetch_jobs_per_config(self, client, components, job_limit)
                except Exception:
                    logger.warning("Failed to fetch jobs", exc_info=True)

            if with_samples and tables_data:
                samples_data = fetch_samples(client, tables_data, sample_limit, max_samples)

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
        # Track branch_id per config to detect branch switches
        existing_branch_ids: dict[str, int] = {
            f"{c.component_id}/{c.id}": c.branch_id for c in manifest.configurations
        }
        # Row-level state lookup, keyed by "{component_id}/{config_id}/{row_id}".
        # Values: dict with path, pull_hash, pull_config_hash (all optional).
        existing_rows: dict[str, dict[str, str]] = {}
        for c in manifest.configurations:
            for r in c.rows:
                row_key = f"{c.component_id}/{c.id}/{r.id}"
                existing_rows[row_key] = {
                    "path": r.path,
                    "pull_hash": r.metadata.get("pull_hash", ""),
                    "pull_config_hash": r.metadata.get("pull_config_hash", ""),
                }

        # Force-pull conflict guard (force-pull baseline corruption fix).
        # ``--force`` bypasses the "preserve locally-modified files" guard
        # below, so a config edited locally AND changed on the remote since the
        # last pull (a true 3-way conflict) would be silently overwritten -- and
        # when the remote is unchanged, its baseline would instead be re-stamped
        # from the edited file, stranding the un-pushed edits.  Detect such
        # conflicts up front and abort BEFORE writing anything (the read-only
        # API fetch above has already happened; nothing is on disk yet), so the
        # user can resolve them.  Non-force pull preserves locally-modified
        # files and surfaces conflicts via ``sync diff``, so it needs no abort.
        if force:
            conflicts = self._detect_force_pull_conflicts(
                components,
                branch_dir,
                existing_keys=existing_keys,
                existing_paths=existing_paths,
                existing_file_hashes=existing_file_hashes,
                existing_config_hashes=existing_config_hashes,
                existing_rows=existing_rows,
            )
            if conflicts:
                raise SyncConflictError(conflicts)

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

                    # Auto-rename: detect when remote name changed
                    expected_path = config_path(
                        manifest.naming.config,
                        component_type,
                        component_id,
                        config_name,
                    )
                    if rel_path != expected_path and not dry_run:
                        rename_target = expected_path
                        # Collision: if target already used, add suffix
                        if rename_target in used_paths:
                            suffix = config_id[:8] if len(config_id) > 8 else config_id
                            rename_target = f"{rename_target}-{suffix}"

                        old_dir = branch_dir / rel_path
                        new_dir = branch_dir / rename_target
                        if old_dir.exists() and not new_dir.exists():
                            self._rename_directory(old_dir, new_dir)
                            pull_details.append(
                                {
                                    "action": "renamed",
                                    "component_id": component_id,
                                    "config_name": config_name,
                                    "path": rename_target,
                                    "old_path": rel_path,
                                }
                            )
                            rel_path = rename_target
                            logger.info(
                                "Renamed config dir: %s -> %s",
                                rel_path,
                                rename_target,
                            )
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
                # Defense-in-depth: refuse to write outside the branch dir
                # (issue #269 sec-01). naming.sanitize_path_segment() should
                # already neutralize traversal; this check guards against any
                # future regression in the sanitizer or template parsing.
                _ensure_within_branch(branch_dir, config_dir, component_id, config_id)

                # Convert API format to local _config.yml
                local_data = api_config_to_local(component_id, cfg, config_id)

                # Hash of API-converted data.  Stored as pull_config_hash so
                # diff can compare it directly with fresh remote data without
                # a lossy file roundtrip.
                api_cfg_hash = config_hash(local_data)
                pull_cfg_hash = api_cfg_hash

                # Detect local modifications: if the file hash differs from the
                # pull_hash stored in manifest, the user edited the file -- so
                # preserve it instead of overwriting.  This now runs even under
                # ``--force``: a force-pull that reaches here for a modified file
                # has already passed the conflict guard above (so the remote is
                # unchanged), and re-stamping the baseline from the edited file
                # would silently strand the un-pushed edits.  Preserving keeps
                # the pending delta visible to ``sync push``.
                locally_modified = False
                if not is_new:
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
                    # If pull_config_hash matches AND branch hasn't changed,
                    # skip write (idempotent).  A branch switch means files
                    # live in a different directory, so we must re-write.
                    old_cfg_hash = existing_config_hashes.get(lookup_key, "")
                    branch_switched = existing_branch_ids.get(lookup_key, branch_id or 0) != (
                        branch_id or 0
                    )
                    remote_unchanged = (
                        not is_new
                        and not branch_switched
                        and old_cfg_hash
                        and old_cfg_hash == api_cfg_hash
                    )

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

                # Row-level pull: each row gets its own 3-way diff state
                # (pull_hash, pull_config_hash) so ``sync push`` can detect
                # per-row local modifications and push only changed rows.
                row_manifests: list[ManifestConfigRow] = []
                used_row_paths: set[str] = set()
                for row in cfg.get("rows", []):
                    row_id = str(row.get("id", ""))
                    row_name = row.get("name", "untitled")

                    # Reuse existing row path (stable) if already tracked.
                    row_lookup_key = f"{component_id}/{config_id}/{row_id}"
                    existing_row = existing_rows.get(row_lookup_key)
                    if existing_row:
                        row_rel_path = existing_row["path"]
                    else:
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
                    row_api_cfg_hash = config_hash(row_local)

                    row_file = row_dir / CONFIG_FILENAME
                    old_row_file_hash = existing_row["pull_hash"] if existing_row else ""
                    old_row_cfg_hash = existing_row["pull_config_hash"] if existing_row else ""
                    # Runs even under ``--force``: a force-pull reaching a
                    # modified row has passed the conflict guard (remote
                    # unchanged), so preserve the row rather than re-stamp its
                    # baseline from the edited file and strand the edits.
                    row_locally_modified = False
                    if existing_row and old_row_file_hash and row_file.exists():
                        row_locally_modified = self._file_hash(row_file) != old_row_file_hash

                    if row_locally_modified:
                        # Preserve local edits; keep old hashes as the 3-way base.
                        row_file_hash = old_row_file_hash
                        row_pull_cfg_hash = old_row_cfg_hash
                        pull_details.append(
                            {
                                "action": "skipped",
                                "component_id": component_id,
                                "config_name": f"{config_name}/{row_name}",
                                "path": f"{rel_path}/{row_rel_path}",
                                "reason": "row locally modified",
                            }
                        )
                    elif (
                        existing_row
                        and old_row_cfg_hash
                        and old_row_cfg_hash == row_api_cfg_hash
                        and row_file.exists()
                    ):
                        # Idempotent: remote unchanged since last pull, file untouched.
                        # Guard: row_file.exists() ensures we don't skip writing when
                        # the directory is new (e.g. first pull of a dev branch that
                        # clones main -- same hash but no file on disk yet).
                        row_file_hash = (
                            self._file_hash(row_file) if row_file.exists() else old_row_file_hash
                        )
                        row_pull_cfg_hash = row_api_cfg_hash
                    else:
                        # New or remote-changed row: write the file.
                        if not dry_run:
                            self._write_config_file(row_dir, row_local)
                            row_file_hash = self._file_hash(row_file) if row_file.exists() else ""
                        else:
                            row_file_hash = ""
                        row_pull_cfg_hash = row_api_cfg_hash
                        files_written += 1
                        rows_pulled += 1

                    row_manifests.append(
                        ManifestConfigRow(
                            id=row_id,
                            path=row_rel_path,
                            metadata={
                                "pull_hash": row_file_hash,
                                "pull_config_hash": row_pull_cfg_hash,
                            },
                        )
                    )

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
                        branchId=branch_id or 0,
                        componentId=component_id,
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
            storage_stats = write_storage_metadata(
                project_root, buckets_data, tables_data, samples_data
            )

        # -- Per-config jobs (JSONL files next to _config.yml) --
        jobs_written = 0
        if not dry_run and jobs_grouped:
            jobs_written = write_per_config_jobs(branch_dir, new_configurations, jobs_grouped)

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
            Dict with lists of modified/added/deleted configs, count of
            unchanged, and ``plaintext_secret_warnings`` -- in-sync configs/rows
            whose ``#``-secrets are still plaintext on the remote (issue #378).
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

            # Compare file hash against the hash stored at pull time.
            # Check both _config.yml AND extracted code files (transform.sql etc.)
            # to match the same logic used by diff().
            current_hash = self._file_hash(config_file)
            pull_hash = cfg.metadata.get("pull_hash", "")
            config_unchanged = bool(pull_hash and current_hash == pull_hash)

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

            if config_unchanged and extras_unchanged:
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

        # Flag in-sync configs whose secrets are still plaintext (issue #378).
        # Best-effort: a file race during the scan must not crash `sync status`
        # (mirrors the doctor sync_secrets check's defensive guard).
        try:
            plaintext_secret_warnings = scan_synced_plaintext_secrets(project_root, manifest)
        except Exception:
            logger.warning("Plaintext-secret scan failed; skipping", exc_info=True)
            plaintext_secret_warnings = []

        return {
            "modified": modified,
            "added": added,
            "deleted": deleted,
            "unchanged": unchanged,
            "total_tracked": len(manifest.configurations),
            "plaintext_secret_warnings": plaintext_secret_warnings,
        }

    # ------------------------------------------------------------------
    # diff
    # ------------------------------------------------------------------

    def diff(
        self,
        alias: str,
        project_root: Path,
        branch_override: int | None = None,
    ) -> dict[str, Any]:
        """Compare local configs against the remote API state.

        Fetches current state from API, reads local _config.yml files,
        and runs the diff engine to produce a detailed changeset.

        Args:
            alias: Project alias.
            project_root: Sync working-tree root.
            branch_override: If set, diff against this branch ID rather
                than the resolved active/manifest branch.

        Returns:
            Dict with 'changes' list and summary counts.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        manifest = load_manifest(project_root)

        branch_id = self._resolve_branch_id(
            project, manifest, project_root, branch_override=branch_override
        )

        # Fetch remote state
        client = self._client_factory(project.stack_url, project.token)
        with client:
            components = client.list_components_with_configs(branch_id=branch_id)
            self._ensure_branch_registered(manifest, branch_id, client)

        # Build remote lookups:
        #   remote_configs: "{component_id}/{config_id}" -> parent config data
        #   remote_rows:    "{component_id}/{parent_config_id}/rows/{row_id}" -> row data
        remote_configs: dict[str, dict[str, Any]] = {}
        remote_rows: dict[str, dict[str, Any]] = {}
        for component in components:
            component_id = component.get("id", "")
            if component_id in ALWAYS_IGNORED_COMPONENTS:
                continue
            for cfg in component.get("configurations", []):
                config_id = str(cfg.get("id", ""))
                key = f"{component_id}/{config_id}"
                remote_configs[key] = api_config_to_local(component_id, cfg, config_id)
                for row in cfg.get("rows", []):
                    row_id = str(row.get("id", ""))
                    row_key = f"{component_id}/{config_id}/rows/{row_id}"
                    remote_rows[row_key] = api_row_to_local(row, component_id)

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
        for added_cfg in self._find_untracked_configs(project_root, manifest, branch_id):
            branch_path = self._resolve_source_branch_path(manifest, project_root, branch_id)
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

        # Row-level diff: walk manifest rows, load local YAML, feed into
        # compute_row_changeset alongside remote_rows built above.
        local_rows: list[dict[str, Any]] = []
        tracked_row_keys: set[str] = set()
        row_base_hashes: dict[str, str] = {}
        for cfg in manifest.configurations:
            branch_path = self._find_branch_path(manifest, cfg.branch_id)
            parent_dir = project_root / branch_path / cfg.path
            for row in cfg.rows:
                row_key = f"{cfg.component_id}/{cfg.id}/rows/{row.id}"
                tracked_row_keys.add(row_key)
                row_dir = parent_dir / row.path
                row_local = self._read_config_file(row_dir)
                if row_local is None:
                    # File missing -> "deleted" detected via tracked_row_keys.
                    continue
                local_rows.append(
                    {
                        "component_id": cfg.component_id,
                        "parent_config_id": cfg.id,
                        "row_id": row.id,
                        "row_name": row_local.get("name", ""),
                        "path": row.path,
                        "data": row_local,
                    }
                )
                pch = row.metadata.get("pull_config_hash") if row.metadata else ""
                if pch:
                    row_base_hashes[row_key] = pch

        # Also add untracked local rows (new row dirs dropped under a tracked
        # config). They have no row_id yet so compute_row_changeset flags
        # them as "added" and push dispatches them via create_config_row.
        for untracked in self._find_untracked_rows(project_root, manifest):
            local_rows.append(
                {
                    "component_id": untracked["component_id"],
                    "parent_config_id": untracked["parent_config_id"],
                    "row_id": "",
                    "row_name": untracked["row_name"],
                    "path": untracked["path"],
                    "data": untracked["data"],
                }
            )

        row_changeset = compute_row_changeset(
            local_rows,
            remote_rows,
            tracked_row_keys,
            row_base_hashes or None,
        )
        changeset.extend(row_changeset)

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
        branch_override: int | None = None,
        no_name_drift_warnings: bool = False,
    ) -> dict[str, Any]:
        """Push local changes to Keboola.

        Computes diff, then creates/updates/deletes configs via API.
        New configs get IDs assigned by the API; the manifest is updated.

        Args:
            alias: Project alias from config store.
            project_root: Root directory of the sync working tree.
            dry_run: If True, compute changes but don't execute them.
            force: If True, allow deletions without extra confirmation.
            allow_plaintext_fallback: If True, allow push when secret
                encryption fails (DANGEROUS).
            branch_override: If set, target this dev-branch ID for the push.
                Wins over ``active_branch_id`` / ``manifest.branches[0]`` /
                git-branching mapping. Used by the CLI ``--branch`` flag. When
                no ``<branch_name>/`` subtree exists on disk for the target,
                the default tree (``main/``) is read as the source and promoted
                to the target branch (KFR-07); API writes still target the
                branch id.
            no_name_drift_warnings: If True, omit the ``name_drift_warnings``
                array from the result envelope (the underlying detection
                still runs; only the report is suppressed). Used by the CLI
                ``--no-name-drift-warnings`` flag.

        Returns:
            Dict with push results (created, updated, deleted, errors).
        """
        diff_result = self.diff(alias, project_root, branch_override=branch_override)
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

        branch_id = self._resolve_branch_id(
            project, manifest, project_root, branch_override=branch_override
        )

        # Detect name drift: local dir name doesn't match config name
        name_drift_warnings = self._detect_name_drift(manifest, project_root)

        client = self._client_factory(project.stack_url, project.token)
        created = 0
        updated = 0
        deleted = 0
        errors: list[dict[str, str]] = []
        pushed_details: list[dict[str, str]] = []
        manifest_dirty = False

        with client:
            self._ensure_branch_registered(manifest, branch_id, client)
            branch_path = self._resolve_source_branch_path(manifest, project_root, branch_id)

            # Process configs before rows, and rebind variable links last.
            # A freshly-created parent config must carry its API-assigned ULID
            # before its rows POST (KFR-05), and a transformation's
            # variables_id / variables_values_id can only be resolved once both
            # the variables config and its values row exist (KFR-03). Partition
            # explicitly rather than relying on incidental diff ordering.
            config_changes = [c for c in changes if not bool(c.get("is_row"))]
            row_changes = [c for c in changes if bool(c.get("is_row"))]

            # (component_id, placeholder_id) -> API-assigned ULID, captured
            # before the manifest writeback overwrites the placeholder in place.
            created_id_map: dict[tuple[str, str], str] = {}
            created_configs: list[CreatedConfig] = []

            # ---- Phase A: config creates / updates / deletes -------------
            for change in config_changes:
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
                            config_dir = project_root / branch_path / config_path_str
                            hashes = self._compute_config_hashes(config_dir, component_id)
                            writeback = writeback_create_config_in_manifest(
                                manifest=manifest,
                                component_id=component_id,
                                branch_id=branch_id,
                                config_path_str=config_path_str,
                                new_id=new_id,
                                file_hash=hashes.file_hash,
                                cfg_hash=hashes.cfg_hash,
                            )
                            # Record placeholder -> ULID so child rows and
                            # transformation variable links can be remapped.
                            if writeback.previous_id:
                                created_id_map[(component_id, writeback.previous_id)] = new_id
                            created_configs.append(
                                CreatedConfig(
                                    component_id=component_id,
                                    config_id=new_id,
                                    config_dir=config_dir,
                                )
                            )
                            metadata_error = propagate_kbc_metadata(
                                client, writeback.entry, branch_id
                            )
                            if metadata_error is not None:
                                # The config IS on the remote; only the
                                # follow-up metadata POST failed. Accumulate
                                # like any other per-change error so the rest
                                # of the push continues, and surface the
                                # original cause in the envelope.
                                errors.append(
                                    {
                                        "change_type": "metadata_propagation",
                                        "component_id": component_id,
                                        "config_id": new_id,
                                        "message": metadata_error,
                                    }
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
                        # Update hashes so pull knows local == remote
                        config_dir = project_root / branch_path / config_path_str
                        config_file = config_dir / CONFIG_FILENAME
                        if config_file.exists():
                            hashes = self._compute_config_hashes(config_dir, component_id)
                            for cfg in manifest.configurations:
                                if cfg.component_id == component_id and cfg.id == config_id:
                                    cfg.metadata["pull_hash"] = hashes.file_hash
                                    cfg.metadata["pull_config_hash"] = hashes.cfg_hash
                                    cfg.metadata["pull_extra_hashes"] = hashes.extra_hashes
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
                    # Fail-closed on encryption failures: a partial push that
                    # omits the failed change would leave a plaintext secret
                    # elsewhere or a caller believing the push "mostly succeeded".
                    # Surface to the CLI (exit non-zero) rather than burying in
                    # result["errors"].
                    if (
                        isinstance(exc, KeboolaApiError)
                        and exc.error_code == ErrorCode.ENCRYPTION_FAILED
                    ):
                        raise
                    self._record_push_error(errors, change_type, component_id, config_id, exc)

            # ---- Phase B: row creates / updates / deletes ----------------
            # row placeholder id -> ULID; ULID parent -> rows created under it.
            created_row_id_map: dict[str, str] = {}
            created_rows_by_parent: dict[str, list[str]] = {}
            for change in row_changes:
                change_type = change["change_type"]
                component_id = change["component_id"]
                config_id = change["config_id"]
                config_path_str = change.get("path", "")
                parent_config_id = change.get("parent_config_id", "")
                # Remap the diff-time parent placeholder to the ULID assigned in
                # Phase A so the manifest lookup and create_config_row both hit
                # the real config (KFR-05). UPDATE/DELETE parents already carry
                # a ULID and pass through unchanged.
                effective_parent_id = created_id_map.get(
                    (component_id, parent_config_id), parent_config_id
                )

                try:
                    new_row_id = self._push_row_change(
                        client,
                        change_type=change_type,
                        component_id=component_id,
                        parent_config_id=effective_parent_id,
                        row_id=config_id,
                        row_path_str=config_path_str,
                        project_root=project_root,
                        manifest=manifest,
                        branch_id=branch_id,
                        allow_plaintext_fallback=allow_plaintext_fallback,
                    )
                    manifest_dirty = True
                    if change_type == "added":
                        created += 1
                        if new_row_id:
                            if config_id:
                                created_row_id_map[config_id] = new_row_id
                            created_rows_by_parent.setdefault(effective_parent_id, []).append(
                                new_row_id
                            )
                    elif change_type == "modified":
                        updated += 1
                    elif change_type == "deleted":
                        deleted += 1
                    pushed_details.append(change)

                except Exception as exc:
                    if (
                        isinstance(exc, KeboolaApiError)
                        and exc.error_code == ErrorCode.ENCRYPTION_FAILED
                    ):
                        raise
                    self._record_push_error(errors, change_type, component_id, config_id, exc)

            # ---- Phase C: variable-link backfill (KFR-03) ----------------
            binding = resolve_variable_bindings(
                self,
                client,
                created_configs=created_configs,
                created_id_map=created_id_map,
                created_row_id_map=created_row_id_map,
                created_rows_by_parent=created_rows_by_parent,
                manifest=manifest,
                branch_id=branch_id,
            )
            errors.extend(binding.errors)
            if binding.configs_rewritten:
                manifest_dirty = True

            # ---- Phase D: flow task configId backfill (#426) -------------
            # After variable links, remap keboola.flow task configIds that point
            # at configs created this push (golden/placeholder -> ULID). Reuses
            # created_id_map; a no-op when no flow was created.
            flow_binding = resolve_flow_task_bindings(
                self,
                client,
                created_configs=created_configs,
                created_id_map=created_id_map,
                manifest=manifest,
                branch_id=branch_id,
            )
            errors.extend(flow_binding.errors)
            if flow_binding.configs_rewritten:
                manifest_dirty = True

        # Save manifest with updated hashes / new IDs / removed entries
        if manifest_dirty:
            save_manifest(project_root, manifest)

        result_data: dict[str, Any] = {
            "status": "pushed",
            "created": created,
            "updated": updated,
            "deleted": deleted,
            "errors": errors,
            "pushed_details": pushed_details,
        }
        if flow_binding.tasks_remapped:
            result_data["flow_task_remaps"] = flow_binding.tasks_remapped
        if name_drift_warnings and not no_name_drift_warnings:
            result_data["name_drift_warnings"] = name_drift_warnings
        return result_data

    def clone_project(
        self,
        source: str | Path,
        target_alias: str,
        target_dir: str | Path,
        *,
        overrides: dict[str, Any] | None = None,
        dry_run: bool = False,
        branch_override: int | None = None,
    ) -> dict[str, Any]:
        """Clone a reference synced project into a fresh target project (#426).

        Thin delegator to :func:`._sync_clone.clone_project`; see that function
        (and ``sync-workflow.md``) for the full behavior -- copy + parameterize
        (bucket_map / variable_values / instance_rename) + push, with Phase-C/D
        link remap, a fresh-target guard, and idempotent re-runs.
        """
        return _clone_project_impl(
            self,
            source,
            target_alias,
            target_dir,
            overrides=overrides,
            dry_run=dry_run,
            branch_override=branch_override,
        )

    # Kept for test compatibility: tests exercise fail-closed encryption via
    # ``SyncService._encrypt_secrets_in_config(...)``. Production code uses
    # :func:`encrypt_secrets_in_config` directly.
    _encrypt_secrets_in_config = staticmethod(encrypt_secrets_in_config)

    def _compute_config_hashes(self, config_dir: Path, component_id: str) -> LocalConfigHashes:
        """Recompute the manifest bookkeeping hashes from a config dir on disk.

        Reads the (post-writeback) ``_config.yml``, merges code files for the
        normalized config hash, and hashes each tracked companion file. Used
        after create / update / variable-link backfill so ``sync diff`` sees
        local == remote on the next run.
        """
        config_file = config_dir / CONFIG_FILENAME
        file_hash = self._file_hash(config_file) if config_file.exists() else ""
        local_data = self._read_config_file(config_dir)
        if local_data is not None:
            merge_code_files(component_id, local_data, config_dir)
            cfg_hash = config_hash(local_data)
        else:
            cfg_hash = ""
        extra_hashes: dict[str, str] = {}
        for fname in _EXTRA_HASH_FILENAMES:
            fpath = config_dir / fname
            if fpath.exists():
                extra_hashes[fname] = self._file_hash(fpath)
        return LocalConfigHashes(file_hash=file_hash, cfg_hash=cfg_hash, extra_hashes=extra_hashes)

    def _record_push_error(
        self,
        errors: list[dict[str, str]],
        change_type: str,
        component_id: str,
        config_id: str,
        exc: Exception,
    ) -> None:
        """Log a non-fatal per-change push failure and accumulate it.

        Callers must re-raise fail-closed encryption errors
        (:data:`ErrorCode.ENCRYPTION_FAILED`) *before* delegating here so a
        partial push never silently drops a secret-bearing change.
        """
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

    def _push_row_change(
        self,
        client: Any,
        *,
        change_type: str,
        component_id: str,
        parent_config_id: str,
        row_id: str,
        row_path_str: str,
        project_root: Path,
        manifest: Manifest,
        branch_id: int | None,
        allow_plaintext_fallback: bool = False,
    ) -> str | None:
        """Dispatch a single row-level change (added/modified/deleted) to the API.

        ``#``-prefixed secrets in the row's configuration are encrypted via
        :func:`encrypt_secrets_in_config` before POST/PUT (same fail-closed
        semantics as parent configs). Mutates ``manifest`` in place; the
        caller is responsible for persisting it.

        ``parent_config_id`` must already be the *effective* parent id: on a
        fresh CREATE the caller remaps the diff-time placeholder to the
        API-assigned ULID before dispatch, so both the manifest parent lookup
        and ``create_config_row(config_id=...)`` hit the real config (KFR-05).

        Returns the API-assigned row id on ``added`` (so the caller can map
        placeholder -> ULID for variable-link backfill), else ``None``.
        """
        parent = next(
            (
                c
                for c in manifest.configurations
                if c.component_id == component_id and c.id == parent_config_id
            ),
            None,
        )
        if parent is None and change_type != "deleted":
            raise KeboolaApiError(
                message=(
                    f"Cannot push row {row_id}: parent config {component_id}/"
                    f"{parent_config_id} is not tracked in the manifest."
                ),
                status_code=0,
                error_code=ErrorCode.PARENT_CONFIG_NOT_TRACKED,
            )

        project_id = manifest.project.id if manifest.project else None

        if change_type == "deleted":
            self._push_delete_row(
                client,
                component_id=component_id,
                parent_config_id=parent_config_id,
                row_id=row_id,
                parent=parent,
                branch_id=branch_id,
            )
            return None

        # added / modified both read a local row file and encrypt-then-push.
        assert parent is not None  # guarded above for non-deleted change_types
        source_branch_path = self._resolve_source_branch_path(manifest, project_root, branch_id)
        row_dir = project_root / source_branch_path / parent.path / row_path_str

        if change_type == "added":
            return self._push_create_row(
                client,
                component_id=component_id,
                parent_config_id=parent_config_id,
                row_dir=row_dir,
                parent=parent,
                row_path_str=row_path_str,
                branch_id=branch_id,
                project_id=project_id,
                allow_plaintext_fallback=allow_plaintext_fallback,
            )

        if change_type == "modified":
            self._push_update_row(
                client,
                component_id=component_id,
                parent_config_id=parent_config_id,
                row_id=row_id,
                row_dir=row_dir,
                parent=parent,
                branch_id=branch_id,
                project_id=project_id,
                allow_plaintext_fallback=allow_plaintext_fallback,
            )
            return None

        raise ValueError(f"Unsupported row change_type: {change_type}")

    def _push_create_row(
        self,
        client: Any,
        *,
        component_id: str,
        parent_config_id: str,
        row_dir: Path,
        parent: ManifestConfiguration,
        row_path_str: str,
        branch_id: int | None,
        project_id: int | None,
        allow_plaintext_fallback: bool,
    ) -> str:
        """POST a new row; record API-assigned id + hashes in the parent's row list.

        Returns the API-assigned row id.
        """
        local_data = self._read_config_file(row_dir)
        if local_data is None:
            raise FileNotFoundError(f"Row file not found: {row_dir / CONFIG_FILENAME}")

        pristine_data = copy.deepcopy(local_data)
        name, description, configuration = local_row_to_api(local_data, component_id)
        configuration = encrypt_secrets_in_config(
            client,
            project_id,
            component_id,
            configuration,
            allow_plaintext_fallback=allow_plaintext_fallback,
        )

        result = client.create_config_row(
            component_id=component_id,
            config_id=parent_config_id,
            name=name,
            configuration=configuration,
            description=description,
            branch_id=branch_id,
        )
        new_row_id = str(result.get("id", ""))
        logger.info("Created row %s/%s/%s", component_id, parent_config_id, new_row_id)

        # Write-back: encrypted secrets land in the local file so a subsequent
        # diff sees local == remote. ``config_id=""`` tells the shared helper
        # to skip writing a config_id into ``_keboola`` (rows use ``row_id``).
        writeback_after_push(self, pristine_data, row_dir, "", configuration)

        row_file = row_dir / CONFIG_FILENAME
        new_file_hash = self._file_hash(row_file) if row_file.exists() else ""
        cfg_hash_value = config_hash(pristine_data)
        writeback_create_row_in_manifest(
            parent=parent,
            row_path_str=row_path_str,
            new_row_id=new_row_id,
            file_hash=new_file_hash,
            cfg_hash=cfg_hash_value,
        )
        return new_row_id

    def _push_update_row(
        self,
        client: Any,
        *,
        component_id: str,
        parent_config_id: str,
        row_id: str,
        row_dir: Path,
        parent: ManifestConfiguration,
        branch_id: int | None,
        project_id: int | None,
        allow_plaintext_fallback: bool,
    ) -> None:
        """PUT an existing row; refresh its hashes in the parent's row list."""
        local_data = self._read_config_file(row_dir)
        if local_data is None:
            raise FileNotFoundError(f"Row file not found: {row_dir / CONFIG_FILENAME}")

        pristine_data = copy.deepcopy(local_data)
        name, description, configuration = local_row_to_api(local_data, component_id)
        configuration = encrypt_secrets_in_config(
            client,
            project_id,
            component_id,
            configuration,
            allow_plaintext_fallback=allow_plaintext_fallback,
        )

        client.update_config_row(
            component_id=component_id,
            config_id=parent_config_id,
            row_id=row_id,
            name=name,
            configuration=configuration,
            description=description,
            change_description="Updated via kbagent sync push",
            branch_id=branch_id,
        )
        logger.info("Updated row %s/%s/%s", component_id, parent_config_id, row_id)

        writeback_after_push(self, pristine_data, row_dir, "", configuration)

        row_file = row_dir / CONFIG_FILENAME
        new_file_hash = self._file_hash(row_file) if row_file.exists() else ""
        cfg_hash_value = config_hash(pristine_data)
        for r in parent.rows:
            if r.id == row_id:
                r.metadata["pull_hash"] = new_file_hash
                r.metadata["pull_config_hash"] = cfg_hash_value
                break

    def _push_delete_row(
        self,
        client: Any,
        *,
        component_id: str,
        parent_config_id: str,
        row_id: str,
        parent: ManifestConfiguration | None,
        branch_id: int | None,
    ) -> None:
        """DELETE a row; prune it from the parent's row list in the manifest."""
        client.delete_config_row(
            component_id=component_id,
            config_id=parent_config_id,
            row_id=row_id,
            branch_id=branch_id,
        )
        if parent is not None:
            parent.rows = [r for r in parent.rows if r.id != row_id]
        logger.info("Deleted row %s/%s/%s", component_id, parent_config_id, row_id)

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
        branch_path = self._resolve_source_branch_path(manifest, project_root, branch_id)
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
        configuration = encrypt_secrets_in_config(
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
        writeback_after_push(self, pristine_data, config_dir, new_config_id, configuration)

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
        branch_path = self._resolve_source_branch_path(manifest, project_root, branch_id)
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
        configuration = encrypt_secrets_in_config(
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
        writeback_after_push(self, pristine_data, config_dir, config_id, configuration)

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
        """Pull all registered projects in parallel (see ``_sync_bulk.pull_all``)."""
        return _bulk_pull_all(
            self,
            base_dir,
            force=force,
            dry_run=dry_run,
            job_limit=job_limit,
            no_storage=no_storage,
            no_jobs=no_jobs,
            with_samples=with_samples,
            sample_limit=sample_limit,
            max_samples=max_samples,
        )

    def diff_all(self, base_dir: Path) -> dict[str, Any]:
        """Diff all registered projects with a local manifest (see ``_sync_bulk.diff_all``)."""
        return _bulk_diff_all(self, base_dir)

    def push_all(
        self,
        base_dir: Path,
        dry_run: bool = False,
        force: bool = False,
        allow_plaintext_fallback: bool = False,
    ) -> dict[str, Any]:
        """Push all registered projects with a local manifest (see ``_sync_bulk.push_all``)."""
        return _bulk_push_all(
            self,
            base_dir,
            dry_run=dry_run,
            force=force,
            allow_plaintext_fallback=allow_plaintext_fallback,
        )

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
        """Link the current git branch to a Keboola dev branch (see ``_sync_branch``)."""
        return _branch_link(self, alias, project_root, branch_id, branch_name)

    def branch_unlink(self, project_root: Path) -> dict[str, Any]:
        """Remove the branch mapping for the current git branch (see ``_sync_branch``)."""
        return _branch_unlink(project_root)

    def branch_status(self, project_root: Path) -> dict[str, Any]:
        """Show the branch mapping status for the current git branch (see ``_sync_branch``)."""
        return _branch_status(project_root)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_branch_id(
        project: Any,
        manifest: "Manifest",
        project_root: Path,
        branch_override: int | None = None,
    ) -> int | None:
        """Resolve the Keboola branch ID for sync operations.

        Priority:
        0. ``branch_override`` (CLI ``--branch <id>``) -- wins over everything
        1. Git-branching mode: read branch-mapping.json for current git branch
        2. ``active_branch_id`` from project config (``kbagent branch use``)
        3. First branch in manifest (production fallback)

        Raises ``ConfigError`` if git-branching is enabled but the current
        branch is not the default and is not linked.

        The default git branch always resolves to ``None`` (production), even
        if ``branch-mapping.json`` is missing or has no entry for it. This
        guarantees there is always a recovery path when the mapping file is
        lost (issue #267, Bug E).
        """
        from ..sync.branch_mapping import load_branch_mapping
        from ..sync.git_utils import get_current_branch

        # CLI override beats every persisted source so a user can target a
        # dev branch from a clean git workspace without first running
        # `branch use` or `branch-link`.
        if branch_override is not None:
            return branch_override

        if manifest.git_branching.enabled:
            git_branch = get_current_branch(project_root)
            if git_branch:
                default_branch = manifest.git_branching.default_branch
                is_default = git_branch == default_branch
                try:
                    mapping = load_branch_mapping(project_root)
                except FileNotFoundError:
                    # Mapping missing -- auto-recover for the default branch
                    # so the user is never locked out of production.
                    if is_default:
                        return None
                    raise ConfigError(
                        f"Git branch '{git_branch}' is not linked to a Keboola "
                        f"branch (branch-mapping.json missing). "
                        f"Run 'kbagent sync branch-link --project ALIAS' first."
                    ) from None
                entry = mapping.get(git_branch)
                if entry is not None:
                    # entry.keboola_id is None for production (default branch)
                    return entry.keboola_id
                # No entry for current branch -- default branch is always production
                if is_default:
                    return None
                raise ConfigError(
                    f"Git branch '{git_branch}' is not linked to a Keboola branch. "
                    f"Run 'kbagent sync branch-link --project ALIAS' first."
                )

        # Non git-branching: use active_branch_id or manifest fallback
        branch_id = project.active_branch_id if project is not None else None
        if not branch_id and manifest.branches:
            branch_id = manifest.branches[0].id
        return branch_id

    # ------------------------------------------------------------------
    # Storage metadata / jobs / samples helpers
    # ------------------------------------------------------------------

    def _write_config_file(self, config_dir: Path, config_data: dict[str, Any]) -> str:
        """Write a ``_config.yml`` file and return its SHA256 hash.

        Uses ``newline=""`` so Windows does NOT translate ``\\n`` into ``\\r\\n``
        on write -- without that, the in-memory ``content`` hash diverges from
        the on-disk byte hash (:meth:`_file_hash`) and every post-pull
        ``status`` would report the file as modified.
        """
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / CONFIG_FILENAME
        content = yaml.dump(
            config_data,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            width=120,
        )
        config_file.write_text(content, encoding="utf-8", newline="")
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _file_hash(self, file_path: Path) -> str:
        """Return the SHA256 hex digest of a file's contents."""
        content = file_path.read_bytes()
        return hashlib.sha256(content).hexdigest()

    def _detect_name_drift(self, manifest: Manifest, project_root: Path) -> list[dict[str, str]]:
        """Detect configs where local dir name doesn't match the config name.

        Reads each tracked config's _config.yml to get the current name,
        then compares sanitize_name(name) against the directory basename.

        Returns a list of warning dicts with component_id, config_id,
        local_dirname, and expected_dirname.
        """
        warnings: list[dict[str, str]] = []
        for cfg in manifest.configurations:
            path = cfg.path
            dirname = path.rsplit("/", 1)[-1] if "/" in path else path

            # Find branch dir and read _config.yml
            branch_path = self._find_branch_path(manifest, cfg.branch_id)
            config_dir = project_root / branch_path / path
            local_data = self._read_config_file(config_dir)
            if local_data is None:
                continue

            config_name = local_data.get("name", "")
            if not config_name:
                continue

            expected_dirname = sanitize_name(config_name)
            if dirname != expected_dirname:
                warnings.append(
                    {
                        "component_id": cfg.component_id,
                        "config_id": cfg.id,
                        "local_dirname": dirname,
                        "expected_dirname": expected_dirname,
                        "config_name": config_name,
                    }
                )
        return warnings

    def _rename_directory(self, source: Path, target: Path) -> str:
        """Rename a directory, using git mv if in a git repo, else shutil.move.

        Returns 'git_mv' or 'shutil_move' indicating which method was used.
        """
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

    def _ensure_branch_registered(
        self,
        manifest: Manifest,
        branch_id: int | None,
        client: Any,
    ) -> str | None:
        """Ensure *branch_id* has an entry in ``manifest.branches``.

        If *branch_id* is ``None`` (production) or already present, this is
        a no-op.  Otherwise the branch name is fetched from the API and a
        new :class:`ManifestBranch` is appended.

        Returns:
            The new branch path if one was added, ``None`` otherwise.
        """
        if branch_id is None:
            return None

        # Already registered?
        for branch in manifest.branches:
            if branch.id == branch_id:
                return None

        # Fetch branch info from API to get a human-readable name
        all_branches = client.list_dev_branches()
        branch_name = ""
        for b in all_branches:
            if b.get("id") == branch_id:
                branch_name = b.get("name", "")
                break

        # Generate filesystem-safe path
        path = sanitize_name(branch_name) if branch_name else ""
        if not path:
            path = f"branch-{branch_id}"

        # Handle path uniqueness -- avoid collisions with existing entries
        existing_paths = {br.path for br in manifest.branches}
        if path in existing_paths:
            path = f"{path}-{branch_id}"

        manifest.branches.append(ManifestBranch(id=branch_id, path=path))
        logger.info("Registered dev branch %d as '%s' in manifest", branch_id, path)
        return path

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
        # Fallback to default branch -- should not happen after _ensure_branch_registered
        logger.warning(
            "Branch ID %s not found in manifest, falling back to default path",
            branch_id,
        )
        return manifest.branches[0].path if manifest.branches else "main"

    def _branch_path_has_configs(self, branch_dir: Path) -> bool:
        """Return True if *branch_dir* exists and holds at least one config.

        A config is any ``_config.yml`` below the branch root other than the
        optional branch-level ``_config.yml`` itself. Used to decide whether a
        target-branch subtree is materialized on disk.
        """
        if not branch_dir.is_dir():
            return False
        for config_file in branch_dir.rglob(CONFIG_FILENAME):
            if config_file.parent != branch_dir:
                return True
        return False

    def _resolve_source_branch_path(
        self,
        manifest: Manifest,
        project_root: Path,
        target_branch_id: int | None,
    ) -> str:
        """Resolve the on-disk branch subtree to read local configs from.

        Source (where files live) and target (where the API writes) are
        decoupled. When the target branch has its own materialized
        ``<branch_name>/`` subtree on disk, that subtree is the source
        (unchanged multi-branch-directory behaviour). Otherwise the default
        branch tree (``manifest.branches[0]``, i.e. ``main/``) is the source --
        the "promote the default tree to a target dev branch" path used by
        ``sync push --branch <id>`` when no per-branch subtree exists
        (KFR-07 option-B).

        API calls still target ``target_branch_id``; only the *read* path is
        affected.
        """
        target_path = self._find_branch_path(manifest, target_branch_id)
        if self._branch_path_has_configs(project_root / target_path):
            return target_path
        default_path = manifest.branches[0].path if manifest.branches else target_path
        if default_path != target_path:
            logger.info(
                "No config files under target branch path '%s'; promoting default "
                "tree '%s' to branch %s",
                target_path,
                default_path,
                target_branch_id,
            )
        return default_path

    def _find_untracked_configs(
        self,
        project_root: Path,
        manifest: Manifest,
        resolved_branch_id: int | None = None,
    ) -> list[dict[str, str]]:
        """Scan for _config.yml files that are not tracked in the manifest.

        Scans branch directories that the user is actively working with:
        branches that already have tracked configs, the default branch
        (production), and the branch the caller resolved for the current
        operation (when provided). This supports the documented
        "scaffold locally then push" workflow on git-branching workspaces
        with empty ``manifest.configurations`` (issue #267, Bug B).

        Branches outside this scope are skipped to avoid phantom "added"
        configs from orphaned dev-branch directories left over from
        previous work.
        """
        tracked_paths: set[str] = set()
        in_scope_branch_ids: set[int] = set()
        for cfg in manifest.configurations:
            branch_path = self._find_branch_path(manifest, cfg.branch_id)
            tracked_paths.add(str(project_root / branch_path / cfg.path))
            in_scope_branch_ids.add(cfg.branch_id)

        # Default branch is always in scope -- pushing a brand-new config
        # against production with empty configurations[] is a legitimate flow.
        if manifest.branches:
            in_scope_branch_ids.add(manifest.branches[0].id)

        # The branch the caller resolved is in scope (linked feature branch
        # the user explicitly switched to via git checkout + branch-link).
        if resolved_branch_id is not None:
            in_scope_branch_ids.add(resolved_branch_id)

        added: list[dict[str, str]] = []
        for branch in manifest.branches:
            if branch.id not in in_scope_branch_ids:
                continue
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

    def _find_untracked_rows(self, project_root: Path, manifest: Manifest) -> list[dict[str, Any]]:
        """Scan tracked config dirs for ``rows/*/_config.yml`` not in manifest.

        Paralleling :meth:`_find_untracked_configs` at the row level. A user
        can drop a hand-crafted row directory under a tracked config's
        ``rows/`` folder; this surfaces it so :meth:`diff` can flag it as
        ``"added"`` and :meth:`push` can POST it via ``create_config_row``.

        Each entry contains ``component_id``, ``parent_config_id``,
        ``row_name`` (from the loaded YAML), ``path`` (relative to the parent
        config dir, e.g. ``rows/new-row``), and ``data`` (the loaded dict).
        """
        added: list[dict[str, Any]] = []
        for cfg in manifest.configurations:
            branch_path = self._find_branch_path(manifest, cfg.branch_id)
            parent_dir = project_root / branch_path / cfg.path
            rows_dir = parent_dir / "rows"
            if not rows_dir.is_dir():
                continue
            tracked_row_paths = {row.path for row in cfg.rows}
            for row_subdir in rows_dir.iterdir():
                if not row_subdir.is_dir():
                    continue
                row_rel_path = f"rows/{row_subdir.name}"
                if row_rel_path in tracked_row_paths:
                    continue
                if not (row_subdir / CONFIG_FILENAME).exists():
                    continue
                local_data = self._read_config_file(row_subdir)
                if local_data is None:
                    continue
                added.append(
                    {
                        "component_id": cfg.component_id,
                        "parent_config_id": cfg.id,
                        "row_name": local_data.get("name", ""),
                        "path": row_rel_path,
                        "data": local_data,
                    }
                )
        return added
