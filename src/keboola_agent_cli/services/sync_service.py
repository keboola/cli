"""Sync service - business logic for project pull/push/status operations.

Handles downloading Keboola project configurations to the local filesystem
in a dev-friendly format (YAML configs), and tracking local changes.
"""

import contextlib
import hashlib
import json
import logging
import shutil
import subprocess
from functools import partial
from pathlib import Path
from typing import Any

import yaml

from ..config_store import ConfigStore
from ..constants import (
    BRANCH_MAPPING_FILENAME,
    CONFIG_FILENAME,
    DEFAULT_JOBS_PER_CONFIG,
    DEFAULT_MAX_SAMPLES,
    DEFAULT_SAMPLE_LIMIT,
    FOLDER_METADATA_KEY,
    KEBOOLA_DIR_NAME,
    MANIFEST_VERSION,
)
from ..errors import ConfigError, KeboolaApiError, SyncConflictError
from ..sync.branch_registry import (
    ScaffoldPlacement,
    ensure_branch_registered,
    register_branch_dir,
    resolve_scaffold_placement,
)
from ..sync.branch_scope import (
    UNKNOWN_BASE_HASH,
    UNKNOWN_BASE_KEY,
    VERDICT_CREATE,
    VERDICT_ORPHAN,
    branch_tree_path,
    classify_untracked,
    entry_tree,
    find_untracked_configs,
    is_production,
    same_branch,
    scope_manifest,
    stale_tree_record,
)
from ..sync.code_extraction import merge_code_files
from ..sync.config_format import (
    api_config_to_local,
    api_row_to_local,
    classify_component_type,
    dump_config_yaml,
    local_config_to_api,
    local_row_to_api,
)
from ..sync.diff_engine import compute_changeset, compute_row_changeset, config_hash
from ..sync.git_utils import get_default_branch, is_git_repo
from ..sync.manifest import (
    Manifest,
    ManifestBranch,
    ManifestConfiguration,
    ManifestGitBranching,
    ManifestNaming,
    ManifestProject,
    load_manifest,
    save_manifest,
)
from ..sync.naming import config_path
from . import _sync_push_ops as push_ops
from . import _sync_workspace as sql_workspaces
from ._encryption import (
    encrypt_secrets_in_config,
    find_plaintext_secret_keys,
)
from ._sync_baseline import (
    adopted_skip_detail,
    detect_force_pull_conflicts,
    effective_stored_hash,
    raise_on_legacy_boundary,
)
from ._sync_bindings import resolve_run_target_bindings
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
from ._sync_companions import (
    companion_conflict,
    companion_diagnostics,
    config_locally_modified,
    current_companions,
    kept_companion_warning,
    local_unchanged,
    merge_with_remote,
    preserved_baseline,
    pulled_baseline,
    write_pulled_config,
)
from ._sync_data_app import (
    load_data_app_types,
    push_ds_client,
    resolve_ds_branch_id,
    resolve_pull_type,
    type_needs_rewrite,
)
from ._sync_deletes import DeleteInputs, DeleteTarget, source_deletions, track_source_deletions
from ._sync_diff_report import DiffExtras, DiffSides, diff_report
from ._sync_disabled import file_disables
from ._sync_link_pending import link_pending_entries, planned_row_refs
from ._sync_link_recovery import resolve_transformation_links
from ._sync_local_files import check_local_files, drop_held, load_config_yaml
from ._sync_models import LocalConfigHashes
from ._sync_moves import move_fields, persist_moves, relocate, settle_moves, status_moves
from ._sync_project_guard import adopt_existing_manifest, load_project_manifest
from ._sync_promote_remap import (
    PromoteRemap,
    diff_decisions,
    finish_promote_rebind,
    hold_pending,
    pending_rebinds,
)
from ._sync_pull_dirs import (
    RowPullContext,
    ambiguous_id_warnings,
    carry_promote_metadata,
    collect_config_folders,
    finalize_kept_entries,
    free_path,
    partition_pull_entries,
    pull_config_rows,
    scan_config_dirs,
    tracked_row,
)
from ._sync_push_create import (
    CreateLedger,
    CreateTarget,
    checkpoint_manifest,
    is_fatal_push_error,
    record_created_config,
    register_adopted_parent,
)
from ._sync_push_ops import detect_name_drift, plan_push, push_create, push_row_change, push_update
from ._sync_stale import (
    apply_stale_sweep,
    find_stale_entries,
    remote_deleted_conflicts,
    reserved_paths,
)
from ._sync_storage import (
    fetch_jobs_per_config,
    fetch_samples,
    write_per_config_jobs,
    write_storage_metadata,
)
from ._sync_untracked import (
    UntrackedRows,
    collect_tracked_parent_rows,
    collect_untracked_rows,
    component_change_problem,
    raise_on_component_change,
)
from ._sync_writeback import reconcile_promote_links, stamp_updated_config
from .base import BaseService, ClientFactory, find_default_branch_id
from .data_app_service import (
    DataScienceClientFactory,
    make_default_ds_client_factory,
)

logger = logging.getLogger(__name__)


def _reconcile_saved_promote_links(project_root: Path) -> None:
    """Link the saved manifest's promote entries (risk 76) and save it when that changed.

    A tree without a readable manifest is left to the diff, which reports it.
    """
    try:
        manifest = load_manifest(project_root)
    except (FileNotFoundError, ValueError):
        return
    if reconcile_promote_links(manifest):
        save_manifest(project_root, manifest)


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

    def __init__(
        self,
        config_store: ConfigStore,
        client_factory: ClientFactory | None = None,
        ds_client_factory: DataScienceClientFactory | None = None,
    ) -> None:
        # ``ds_client_factory`` builds a Data Science client. It is used only
        # for ``keboola.data-apps`` configs, whose runtime type lives on the DS
        # ``/apps`` record and would otherwise be lost by pull/clone (CLI-8).
        super().__init__(config_store=config_store, client_factory=client_factory)
        self._ds_client_factory = ds_client_factory or make_default_ds_client_factory(config_store)

    # ------------------------------------------------------------------
    # init
    # ------------------------------------------------------------------

    def init_sync(
        self,
        alias: str,
        project_root: Path,
        git_branching: bool = False,
        adopt_existing: bool = False,
        sync_workspaces: bool = False,
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
            sync_workspaces: Set the manifest key ``syncWorkspaces`` (CLI-25),
                so pull/diff/push/clone also handle shared SQL workspaces. On
                ``adopt_existing`` it only turns the key on, never off.

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
                return adopt_existing_manifest(self, alias, project_root, project, sync_workspaces)
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
        default_branch_id = find_default_branch_id(branches)
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
            syncWorkspaces=sync_workspaces,
            branches=[
                ManifestBranch(
                    id=default_branch_id,
                    path=default_branch_name,
                )
            ]
            if default_branch_id is not None
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
            "sync_workspaces": sync_workspaces,
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
        branch_override: int | None = None,
        theirs: bool = False,
    ) -> dict[str, Any]:
        """Download all configurations from Keboola to local filesystem.

        Args:
            alias: Project alias from config store.
            project_root: Root directory of the sync working tree.
            force: If True, overwrite existing local files without checking.
            theirs: Remote wins everywhere (issue #466): locally-modified
                configs/rows are overwritten with the remote state, true
                merge conflicts are resolved by taking remote instead of
                aborting, and missing local files are re-materialized.
                The supported "discard local changes" reconcile path.
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
        manifest = load_project_manifest(self, alias, project, project_root)

        # Determine branch to pull from (git-branching aware)
        branch_id = self._resolve_branch_id(
            alias, manifest, project_root, branch_override=branch_override
        )

        # Fetch all components with configs from API (+ storage metadata + jobs)
        client = self._client_factory(project.stack_url, project.token)
        buckets_data: list[dict[str, Any]] = []
        tables_data: list[dict[str, Any]] = []
        jobs_grouped: list[dict[str, Any]] = []
        samples_data: dict[str, str] = {}  # table_id -> CSV string
        with client:
            components = client.list_components_with_configs(branch_id=branch_id)
            components = sql_workspaces.scope_listing(components, manifest)
            self._ensure_branch_registered(manifest, branch_id, client)
            folder_map = collect_config_folders(components)

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

        # Data-app runtime types (CLI-8): one DS /apps call per project, resolved
        # per config below. Empty when the tree has no data apps or the lookup
        # fails. See _sync_data_app.load_data_app_types.
        data_app_types = load_data_app_types(self._ds_client_factory, project, components)

        # Determine branch directory name
        branch_dir_name = self._find_branch_path(manifest, branch_id)

        branch_dir = project_root / branch_dir_name

        # Track stats and change details
        configs_pulled = 0
        rows_pulled = 0
        files_written = 0
        new_configurations: list[ManifestConfiguration] = []
        used_paths: set[str] = set()  # detect naming collisions
        # Resolved once and shared by the moves, the conflict guard, the fetch
        # loop and the stale-entry sweep below -- see ``effective_ignored_components``.
        ignored_components = sql_workspaces.effective_ignored_components(manifest)
        # A hand-moved config dir is pulled into where it is now (#792 I); an
        # ambiguous move is held: pull writes none of its directories.
        moves = settle_moves(
            self, manifest, project_root, branch_dir_name, ignored_components, drop_held=False
        )
        pull_details: list[dict[str, str]] = moves.pull_details()  # per-config change info

        # Build lookups for existing manifest state -- of the PULLED branch only.
        # An entry belongs to exactly one branch, and a dev branch created from
        # production holds the same config ids: reading another branch's entry
        # here judged this tree by the other tree's baselines (a false "locally
        # modified") and reused its path (a config renamed on the dev branch
        # was written to a new main/ directory, the old one became an untracked
        # copy and push created a duplicate). Other branches' entries are never
        # looked up, never swept as stale and never delete a file; the promote
        # entries among them (files in this tree, none in their own) are kept.
        entries = partition_pull_entries(manifest, project_root, branch_id, branch_dir_name)
        entries.hold_ignored_promotes(ignored_components)
        own_entries = entries.own
        # Held entries are kept as they are, listed by the remote or not (F2).
        new_configurations += moves.held_in(own_entries)
        old_entries = {f"{c.component_id}/{c.id}": c for c in own_entries}
        untracked_dirs = scan_config_dirs(self, project_root, manifest, branch_dir_name)
        pull_warnings: list[dict[str, Any]] = ambiguous_id_warnings(untracked_dirs)
        companion_warnings: list[dict[str, str]] = []
        # Paths owned on disk before this pull allocates any (D14): a new or
        # renamed config never lands in one, whatever order the remote lists.
        reserved = (
            untracked_dirs.paths
            | {c.path for c in own_entries}
            | entries.kept_paths_in(branch_dir_name)
        )
        existing_paths: dict[str, str] = {f"{c.component_id}/{c.id}": c.path for c in own_entries}
        existing_keys: set[str] = set(existing_paths.keys())
        # Full metadata per entry -- the shape-migration checks need the
        # ``config_hash_version`` marker alongside the hashes (issue #686).
        existing_metadata: dict[str, dict[str, Any]] = {
            f"{c.component_id}/{c.id}": c.metadata for c in own_entries
        }
        existing_config_hashes: dict[str, str] = {
            f"{c.component_id}/{c.id}": c.metadata.get("pull_config_hash", "") for c in own_entries
        }
        # Row-level state lookup, keyed by "{component_id}/{config_id}/{row_id}".
        # Values: dict with path, pull_hash, pull_config_hash (all optional).
        existing_rows: dict[str, dict[str, str]] = {}
        for c in own_entries:
            for r in c.rows:
                existing_rows[f"{c.component_id}/{c.id}/{r.id}"] = tracked_row(r)
        # Rows of this tree an earlier re-targeting pull dropped (D6).
        existing_rows.update(entries.retired_rows_for(existing_keys, components))

        # Entries the remote no longer lists (#792 A/C); their on-disk dirs are
        # reserved so a same-named new config cannot land in one.
        stale = find_stale_entries(
            self,
            moves.unheld(own_entries),
            components,
            branch_dir,
            ignored_components,
            manifest.ignored_components,
        )
        used_paths |= reserved_paths(stale, branch_dir)

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
        # ``--theirs`` skips the guard entirely: the user explicitly asked for
        # remote to win, so conflicts are resolved by overwriting, not aborting.
        if force and not theirs:
            conflicts = detect_force_pull_conflicts(
                self,
                components,
                branch_dir,
                ignored_components=ignored_components,
                existing_keys=existing_keys,
                existing_paths=existing_paths,
                existing_metadata=existing_metadata,
                existing_rows=existing_rows,
            ) + remote_deleted_conflicts(stale)
            if conflicts:
                raise SyncConflictError(conflicts)

        for component in components:
            component_id = component.get("id", "")
            if component_id in ignored_components:
                continue
            component_type = classify_component_type(component.get("type", "other"))
            configs = component.get("configurations", [])

            for cfg in configs:
                config_id = str(cfg.get("id", ""))
                config_name = cfg.get("name", "untitled")

                # Reuse existing path if config is already tracked (stable paths)
                lookup_key = f"{component_id}/{config_id}"
                if lookup_key in moves.held_keys:
                    continue  # kept as it was, above
                is_new = lookup_key not in existing_keys
                generated = False
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
                        rename_target = free_path(
                            rename_target, config_id[:8], used_paths | reserved
                        )

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
                elif lookup_key in untracked_dirs.by_id:
                    # The directory that already holds this config (its file
                    # carries the id), not a new one named after the config.
                    rel_path = untracked_dirs.by_id[lookup_key]
                else:
                    # Generate new filesystem path with collision detection
                    rel_path = config_path(
                        manifest.naming.config,
                        component_type,
                        component_id,
                        config_name,
                    )
                    generated = True
                # Short config ID suffix on a collision; every candidate is
                # checked against the owned paths (D26).
                rel_path = free_path(
                    rel_path, config_id[:8], used_paths | reserved if generated else used_paths
                )
                used_paths.add(rel_path)
                config_dir = branch_dir / rel_path
                # Defense-in-depth: refuse to write outside the branch dir
                # (issue #269 sec-01). naming.sanitize_path_segment() should
                # already neutralize traversal; this check guards against any
                # future regression in the sanitizer or template parsing.
                _ensure_within_branch(branch_dir, config_dir, component_id, config_id)

                # Convert API format to local _config.yml. For a data app the
                # runtime type is resolved from the DS list, keeping the on-disk
                # value on an absence. See _sync_data_app.resolve_pull_type.
                da_type, on_disk_da_type = resolve_pull_type(
                    self._read_config_file, config_dir, component_id, data_app_types, config_id
                )
                local_data = api_config_to_local(
                    component_id,
                    cfg,
                    config_id,
                    data_app_type=da_type,
                )

                # Hash of API-converted data.  Stored as pull_config_hash so
                # diff can compare it directly with fresh remote data without
                # a lossy file roundtrip.
                api_cfg_hash = config_hash(local_data)
                pull_cfg_hash = api_cfg_hash

                # An untracked file that carries this config's id keeps its
                # local changes; only --theirs overwrites it (issue #792 E).
                if is_new and not theirs:
                    skipped = adopted_skip_detail(self, config_dir, component_id, cfg, rel_path)
                    if skipped:
                        pull_details.append(skipped)
                        continue

                # Detect local modifications: the config's whole local
                # representation -- ``_config.yml`` plus its companion SET
                # (a new, deleted or edited companion file, issue #792 B, D2)
                # -- differs from the pull state, so preserve it instead of
                # overwriting. This runs even under ``--force``: a force-pull
                # reaching here has passed the conflict guard (remote
                # unchanged), and re-stamping the baseline from the edited
                # files would strand the un-pushed edits. ``--theirs``
                # disables the preserve entirely: remote wins.
                old_metadata = existing_metadata.get(lookup_key, {})
                locally_modified = (
                    not is_new
                    and not theirs
                    and config_locally_modified(self, config_dir, component_id, old_metadata)
                )

                file_hash = ""
                if locally_modified:
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
                    # Remote unchanged since the last pull -> the write may be
                    # skipped (idempotent). The lookups hold this branch's
                    # entries only, so a branch switch makes every config new.
                    # The file-existence guard enforces the manifest<->disk
                    # invariant (#472/#466): a tracked config whose file is
                    # missing is re-materialized. A data app's type lives in
                    # _keboola, invisible to config_hash, so a changed type
                    # forces the write too.
                    old_cfg_hash = existing_config_hashes.get(lookup_key, "")
                    remote_unchanged = bool(
                        not is_new
                        and old_cfg_hash
                        and old_cfg_hash == api_cfg_hash
                        and (config_dir / CONFIG_FILENAME).exists()
                        and not type_needs_rewrite(component_id, da_type, on_disk_da_type)
                    )
                    pulled = write_pulled_config(
                        self,
                        config_dir,
                        component_id,
                        local_data,
                        old_metadata,
                        remote_unchanged=remote_unchanged,
                        theirs=theirs,
                        dry_run=dry_run,
                    )
                    file_hash = pulled.file_hash
                    companion_warnings.extend(
                        kept_companion_warning(component_id, rel_path, name) for name in pulled.kept
                    )
                    if pulled.written:
                        configs_pulled += 1
                        files_written += 1
                        pull_details.append(
                            {
                                "action": "new" if is_new else "updated",
                                "component_id": component_id,
                                "config_name": config_name,
                                "path": rel_path,
                            }
                        )

                rows = pull_config_rows(
                    self,
                    RowPullContext(
                        component_id=component_id,
                        config_id=config_id,
                        config_name=config_name,
                        rel_path=rel_path,
                        config_dir=config_dir,
                        row_naming=manifest.naming.config_row,
                        adopt=is_new,
                        theirs=theirs,
                        dry_run=dry_run,
                    ),
                    cfg.get("rows", []),
                    existing_rows,
                    pull_details,
                    pull_warnings,
                )
                files_written += rows.files_written
                rows_pulled += rows.rows_pulled

                # Record in manifest: the old baseline verbatim for a skipped
                # config, the fresh one otherwise (issue #792 B, #686, R4).
                cfg_metadata = (
                    preserved_baseline(old_metadata)
                    if locally_modified
                    else pulled_baseline(file_hash, pull_cfg_hash, pulled)
                )
                # Keep the UI folder with the config. Pull dropped it before,
                # so every pulled or cloned config landed in the root (CLI-9).
                # The manifest stores it under the legacy metadata key name
                # (existing trees and scaffolds use it); the push create path
                # sends it as the configuration's ``folder`` field. When the
                # listing carried no ``folder`` (folder_map is None), keep the
                # folder a previous pull captured rather than stripping it.
                if folder_map is not None:
                    folder_name = folder_map.get(lookup_key)
                else:
                    folder_name = existing_metadata.get(lookup_key, {}).get(FOLDER_METADATA_KEY)
                if folder_name:
                    cfg_metadata[FOLDER_METADATA_KEY] = folder_name
                carry_promote_metadata(old_entries.get(lookup_key), cfg_metadata, rows)
                new_configurations.append(
                    ManifestConfiguration(
                        branchId=branch_id or 0,
                        componentId=component_id,
                        id=config_id,
                        path=rel_path,
                        metadata=cfg_metadata,
                        rows=rows.rows,
                    )
                )

        # Report configs dropped from the manifest ("removed" from the remote vs
        # "ignored" component, issue #689) and delete their directories --
        # except a locally edited one (#792 C) or one this pull owns (#792 A).
        apply_stale_sweep(
            stale,
            branch_dir,
            theirs=theirs,
            dry_run=dry_run,
            new_configurations=new_configurations,
            pull_details=pull_details,
        )

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
            manifest.configurations = new_configurations + finalize_kept_entries(
                entries.kept, new_configurations, branch_dir, ignored_components
            )
            entries.retire(manifest, project_root, branch_dir_name, new_configurations)
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
            # True when the listing carried no ``folder`` field (a stack that
            # predates it): folders were kept from the previous pull, not
            # refreshed. Lets a caller tell "no folders" from "unknown" (CLI-9).
            "folder_lookup_failed": folder_map is None,
            **({"warnings": pull_warnings} if pull_warnings else {}),
            **({"companion_warnings": companion_warnings} if companion_warnings else {}),
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
        moves = status_moves(self, manifest, project_root)  # in memory only (#792 I)

        modified: list[dict[str, str]] = []
        deleted: list[dict[str, str]] = []
        never_fetched: list[dict[str, str]] = []
        unchanged = 0

        # Check each manifest entry against local files
        for cfg in manifest.configurations:
            config_dir = project_root / entry_tree(manifest, cfg) / cfg.path
            config_file = config_dir / CONFIG_FILENAME

            if not config_file.exists():
                entry = {
                    "component_id": cfg.component_id,
                    "config_id": cfg.id,
                    "path": str(cfg.path),
                }
                # A missing file with an EMPTY pull_hash was never
                # materialized (pre-0.72 phantom, issue #472) -- reporting it
                # as "deleted" would suggest a push should delete the remote.
                if cfg.metadata.get("pull_hash"):
                    deleted.append(entry)
                else:
                    never_fetched.append(entry)
                continue

            # _config.yml AND the companion set, the same check diff() makes.
            if local_unchanged(self, config_dir, cfg.component_id, cfg.metadata):
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
            "never_fetched": never_fetched,
            "unchanged": unchanged,
            "total_tracked": len(manifest.configurations),
            "plaintext_secret_warnings": plaintext_secret_warnings,
            "moves": moves,
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
        manifest = load_project_manifest(self, alias, project, project_root)

        branch_id = self._resolve_branch_id(
            alias, manifest, project_root, branch_override=branch_override
        )

        # Fetch remote state
        client = self._client_factory(project.stack_url, project.token)
        with client:
            components = client.list_components_with_configs(branch_id=branch_id)
            components = sql_workspaces.scope_listing(components, manifest)
            self._ensure_branch_registered(manifest, branch_id, client)

        # One ignored set for BOTH sides of this diff -- the remote lookups
        # below and the local scoping further down.
        ignored_components = sql_workspaces.effective_ignored_components(manifest)

        # Build remote lookups:
        #   remote_configs: "{component_id}/{config_id}" -> parent config data
        #   remote_rows:    "{component_id}/{parent_config_id}/rows/{row_id}" -> row data
        remote_configs: dict[str, dict[str, Any]] = {}
        remote_rows: dict[str, dict[str, Any]] = {}
        # Raw (unconverted) API configs, kept so a manifest entry without
        # ``config_hash_version`` can be checked against the pre-#686 hash of
        # this very config -- the migration leniency in ``effective_stored_hash``.
        remote_raw: dict[str, dict[str, Any]] = {}
        for component in components:
            component_id = component.get("id", "")
            if component_id in ignored_components:
                continue
            for cfg in component.get("configurations", []):
                config_id = str(cfg.get("id", ""))
                key = f"{component_id}/{config_id}"
                remote_configs[key] = api_config_to_local(component_id, cfg, config_id)
                remote_raw[key] = cfg
                for row in cfg.get("rows", []):
                    row_id = str(row.get("id", ""))
                    row_key = f"{component_id}/{config_id}/rows/{row_id}"
                    remote_rows[row_key] = api_row_to_local(row, component_id)

        # Scope the manifest to the ONE tree this diff reads from (issue #649).
        # ``manifest.configurations`` is flat and may reference several branch
        # trees at once -- ``sync pull --branch <dev>`` re-targets every entry
        # to the dev branch and orphans the previously pulled ``main/`` tree on
        # disk. Entries belonging to another tree must not diff against this
        # target: push reads every file through ``source_branch_path`` anyway,
        # so they would be pushed from a different file than the one that
        # produced their classification -- and a dev-only config compared
        # against production came out as ``added``, i.e. a duplicate create.
        # They are excluded and reported under ``orphaned`` instead.
        source_branch_path = self._resolve_source_branch_path(manifest, project_root, branch_id)
        # A hand-moved config dir keeps its entry (#792 I), relocated in memory.
        moves = settle_moves(
            self, manifest, project_root, source_branch_path, ignored_components, drop_held=True
        )
        remote_keys = set(remote_configs)
        scope = scope_manifest(
            manifest,
            project_root,
            source_branch_path,
            remote_keys,
            ignored_components=ignored_components,
            target_branch_id=branch_id,
        )
        never_fetched = scope.never_fetched
        never_fetched_keys = scope.never_fetched_keys
        # An unusable _config.yml holds its config back: reported in errors[],
        # never read as "no file" (a delete) nor pushed without its unknown keys.
        bad = check_local_files(project_root, source_branch_path, scope.in_tree, ignored_components)
        tracked_keys = scope.tracked_keys
        orphaned: list[dict[str, Any]] = list(scope.orphaned)

        # Build local configs list from the in-tree manifest entries.
        # For files unchanged since pull, use the stored pull_config_hash
        # directly (avoids lossy code extraction roundtrip).
        # For locally modified files, merge code back for real comparison.
        local_configs: list[dict[str, Any]] = []
        file_unchanged: dict[str, bool] = {}
        local_override_hashes: dict[str, str] = {}
        stored_hashes: dict[str, str] = {}
        diff_warnings: list[dict[str, str]] = []
        relabelled: list[str] = []
        untracked_rows = UntrackedRows()
        for cfg in scope.in_tree:
            config_dir = project_root / source_branch_path / cfg.path
            key = f"{cfg.component_id}/{cfg.id}"
            # Baseline hash, leniently upgraded for entries written before the
            # script-shape change (issue #686). Strict for versioned entries.
            # Read before the file: a local delete is three-way too (D1).
            stored_hashes[key] = stored_cfg_hash = effective_stored_hash(
                cfg.metadata,
                component_id=cfg.component_id,
                config_id=cfg.id,
                raw_remote=remote_raw.get(key),
                remote_local=remote_configs.get(key),
            )
            held = cfg.path in bad.held_paths
            local_data = None if held else self._read_config_file(config_dir)
            if local_data is None:
                continue
            problem = component_change_problem(cfg.component_id, local_data, cfg.path)
            if problem:
                relabelled.append(problem)
            diff_warnings += companion_diagnostics(
                config_dir, cfg.component_id, cfg.path, local_data
            )

            # Check if ANY file of this config changed since pull: _config.yml
            # and the companion set -- a new, deleted or edited companion (D2).
            is_unchanged = local_unchanged(self, config_dir, cfg.component_id, cfg.metadata)
            file_unchanged[key] = is_unchanged

            # A companion pull kept although the remote cleared it: no
            # baseline says which side is right, so a difference is a conflict.
            if companion_conflict(config_dir, cfg.metadata):
                stored_cfg_hash = UNKNOWN_BASE_HASH
            stored_hashes[key] = stored_cfg_hash

            if is_unchanged and stored_cfg_hash:
                # All files match pull state -- use stored API hash
                local_override_hashes[key] = stored_cfg_hash
            # Always merge for local_data (needed for deep_diff details)
            merge_with_remote(cfg.component_id, local_data, config_dir, remote_configs.get(key))

            local_configs.append(
                {
                    "component_id": cfg.component_id,
                    "config_id": cfg.id,
                    "config_name": local_data.get("name", ""),
                    "path": cfg.path,
                    "data": local_data,
                }
            )

        # Also add untracked local configs (new files). Scan ONLY the branch
        # subtree push reads from -- scanning another branch's tree (e.g.
        # ``main/`` while targeting a dev branch) turned configs orphaned by a
        # branch switch into phantom "added" entries, and push then created a
        # duplicate on the target branch for each of them (issue #482).
        for added_cfg in self._find_untracked_configs(
            project_root, manifest, only_branch_path=source_branch_path
        ):
            component_id = added_cfg.get("component_id", "unknown")
            # A promoted target-branch entry already diffs this directory (its
            # files are in the source tree, not its own); a second, adopted
            # reading of it doubled every change and called an edit a conflict.
            # An ambiguous move's candidate is neither created nor adopted.
            if added_cfg["path"] in scope.in_tree_paths | moves.held_paths:
                continue
            # An ignored component's leftover directory (its manifest entry is
            # gone, so the walk sees it as untracked) must not be planned as a
            # create -- push would re-add exactly what pull refuses to fetch.
            if component_id in ignored_components or added_cfg["path"] in bad.held_paths:
                continue
            config_dir = project_root / source_branch_path / added_cfg["path"]
            local_data = self._read_config_file(config_dir)
            if local_data is None:
                continue
            # Adopt-by-id guard (issues #482 / #649): an untracked file whose
            # ``_keboola.config_id`` resolves on the target branch and is not
            # claimed by a manifest entry IN THIS TREE refers to an EXISTING
            # remote config -- diff against it (unchanged/modified) instead of
            # letting push create a duplicate. A same-tree claim means the user
            # copied a tracked config dir to fork it: keep the create.
            untracked_id = added_cfg.get("config_id", "")
            verdict = classify_untracked(
                component_id=component_id,
                config_id=untracked_id,
                claims=scope.claims,
                source_branch_path=source_branch_path,
                remote_keys=remote_keys,
            )
            if verdict == VERDICT_ORPHAN:
                orphaned.append(
                    stale_tree_record(
                        component_id=component_id,
                        config_id=untracked_id,
                        path=added_cfg["path"],
                        claims=scope.claims,
                    )
                )
                continue
            if verdict == VERDICT_CREATE:
                untracked_id = ""  # new config, push creates it
            # Merged once the identity is known: packages a pyproject.toml left
            # undecided are the adopted remote config's (T1).
            remote_local = remote_configs.get(f"{component_id}/{untracked_id}")
            # Before the merge: it reads the file's own description (D11).
            diff_warnings += companion_diagnostics(
                config_dir, component_id, added_cfg["path"], local_data
            )
            merge_with_remote(component_id, local_data, config_dir, remote_local)
            # Its rows join the plan: created with it, or diffed by id (D7, D8).
            collect_untracked_rows(
                self,
                untracked_rows,
                config_dir,
                component_id=component_id,
                config_id=untracked_id,
                config_path=added_cfg["path"],
                remote_rows=remote_rows,
            )
            # An adopted file has no manifest baseline: compute_changeset reads
            # the one a `config new --push` scaffold records (issue #792 E).
            local_configs.append(
                {
                    "component_id": component_id,
                    "config_id": untracked_id,
                    "config_name": local_data.get("name", ""),
                    "path": added_cfg["path"],
                    "data": local_data,
                    "adopted": bool(untracked_id),
                }
            )

        raise_on_component_change(relabelled)

        # Build base hashes for 3-way diff.
        # Preferred: pull_config_hash (normalized hash stored at pull time).
        # Fallback: if file is unchanged since pull, use current config_hash
        # as the base (since local == base when file hasn't been modified).
        base_hashes: dict[str, str] = {}
        for cfg in scope.in_tree:
            key = f"{cfg.component_id}/{cfg.id}"
            pch = stored_hashes.get(key)
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
            scope.target_tracked_keys,
        )
        changeset = drop_held(changeset, bad.held_keys)

        # Row-level diff: walk manifest rows, load local YAML, feed into
        # compute_row_changeset alongside remote_rows built above.
        local_rows: list[dict[str, Any]] = untracked_rows.local_rows
        tracked_row_keys: set[str] = untracked_rows.tracked_row_keys
        row_base_hashes: dict[str, str] = untracked_rows.base_hashes
        # ``scope.in_tree`` already drops never-fetched parents (whose rows were
        # never materialized either, so tracking them would plan remote row
        # deletes -- issue #472) and parents tracked on another branch's tree
        # (whose rows belong to that branch -- issue #649).
        for cfg in scope.in_tree:
            parent_dir = project_root / source_branch_path / cfg.path
            if source_branch_path != branch_tree_path(manifest, branch_id):  # promote (D7)
                deleted = source_deletions(manifest, cfg, branch_id)
                track_source_deletions(deleted, cfg, remote_rows, tracked_row_keys, row_base_hashes)
            for row in cfg.rows:
                row_key = f"{cfg.component_id}/{cfg.id}/rows/{row.id}"
                tracked_row_keys.add(row_key)
                # The base first: a local delete of an unknown-base row must be
                # a conflict too, never a DELETE (D22).
                pch = row.metadata.get("pull_config_hash") if row.metadata else ""
                if pch:
                    row_base_hashes[row_key] = pch
                elif row.metadata.get(UNKNOWN_BASE_KEY):
                    row_base_hashes[row_key] = UNKNOWN_BASE_HASH  # D22: a difference conflicts
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

        # Also add untracked local rows (new row dirs under a tracked config).
        ambiguous_rows = collect_tracked_parent_rows(
            self,
            untracked_rows,
            project_root,
            manifest,
            source_branch_path,
            scope.in_tree,
            branch_id=branch_id,
            remote_rows=remote_rows,
            ignored_components=ignored_components,
        )

        row_changeset = compute_row_changeset(
            local_rows,
            remote_rows,
            tracked_row_keys,
            row_base_hashes or None,
            scope.target_tracked_row_keys | untracked_rows.gone_row_keys,
            {
                f"{c.component_id}/{c.config_id}"
                for c in changeset
                if c.change_type == "remote_deleted"
            },
        )
        changeset.extend(drop_held(row_changeset + ambiguous_rows, bad.held_keys))
        # Remote-only configs and rows, three-way local deletes, summary (D1-D5).
        report = diff_report(
            changeset,
            DiffSides(
                local_configs=local_configs,
                local_rows=local_rows,
                remote_configs=remote_configs,
                remote_rows=remote_rows,
                tracked_keys=tracked_keys,
                tracked_row_keys=tracked_row_keys,
                never_fetched_keys=never_fetched_keys,
            ),
            DeleteInputs(
                config_bases=base_hashes,
                row_bases=row_base_hashes,
                target_row_keys=scope.target_tracked_row_keys,
                remote_rows=remote_rows,
            ),
            DeleteTarget(
                branch_id=branch_id,
                production=is_production(manifest, branch_id),
                source_tree=source_branch_path,
                target_tree=branch_tree_path(manifest, branch_id),
            ),
            DiffExtras(never_fetched, orphaned, diff_warnings, bad.errors),
        )
        return moves.report(report)

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
            force: If True, delete remote configs and rows whose local files
                were removed. Without it push deletes nothing and lists the
                deletions under ``skipped_deletions`` (issue #792 G).
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
        if not dry_run:
            # Before the diff and any early return, so the plan sees the links
            # a crash or an older version left out (risk 80).
            _reconcile_saved_promote_links(project_root)
        diff_result = self.diff(alias, project_root, branch_override=branch_override)
        all_changes = diff_result["changes"]
        companion_warnings = diff_result.get("companion_warnings") or []
        shown = {"companion_warnings": companion_warnings} if companion_warnings else {}
        shown.update(move_fields(diff_result))  # #792 I: in every push result
        never_fetched = diff_result.get("never_fetched", [])
        # Configs held back by an unusable _config.yml: errors in every result.
        invalid_files: list[dict[str, str]] = diff_result.get("errors") or []
        # Configs excluded from the changeset because they belong to another
        # branch's tree (issue #649) -- reported, never pushed.
        orphaned = diff_result.get("orphaned", [])

        # Push applies local-side changes only, and deletes only with --force.
        # What it holds back is listed in every result (issue #792 G, H).
        plan = plan_push(all_changes, force=force)
        changes = plan.changes

        # A promote create whose references still need a rebind (D24) is pushed
        # again although nothing changed locally.
        stored = load_manifest(project_root)
        target_id = self._resolve_branch_id(alias, stored, project_root, branch_override)
        pending = pending_rebinds(stored, target_id) or link_pending_entries(stored, target_id)
        if not changes and not pending:
            # An unchanged pure move is still saved (#792 I).
            persist_moves(project_root, diff_result, dry_run=dry_run)
            result: dict[str, Any] = {
                "status": "no_changes",
                "created": 0,
                "updated": 0,
                "deleted": 0,
                "errors": list(invalid_files),
                **plan.report(),
                **shown,
            }
            if never_fetched:
                result["never_fetched"] = never_fetched
            if orphaned:
                result["orphaned"] = orphaned
            return result

        if dry_run:
            dry_result: dict[str, Any] = {
                "status": "dry_run",
                "changes": changes,
                "summary": {**diff_result["summary"], "deleted": plan.deletions},
                **plan.report(),
                **shown,
                # The creates a real push refuses (T1, round 5).
                "errors": invalid_files
                + push_ops.create_refusals(self, changes, stored, project_root, target_id),
            }
            if never_fetched:
                dry_result["never_fetched"] = never_fetched
            if orphaned:
                dry_result["orphaned"] = orphaned
            sql_workspaces.preview_deletes(self, alias, project_root, branch_override, dry_result)
            return dry_result

        projects = self.resolve_projects([alias])
        project = projects[alias]
        manifest = load_manifest(project_root)

        branch_id = self._resolve_branch_id(
            alias, manifest, project_root, branch_override=branch_override
        )

        # Detect name drift: local dir name doesn't match config name
        name_drift_warnings = detect_name_drift(self, manifest, project_root)

        client = self._client_factory(project.stack_url, project.token)
        created = 0
        updated = 0
        deleted = 0
        errors: list[dict[str, str]] = list(invalid_files)
        # Non-fatal push warnings: unstampable manifest baselines (issue #686,
        # the API state could not be read back after a write) and ``script[]``
        # runtime-safety normalizations (``change_type`` ``script_normalization``,
        # whose records carry non-string values such as ``after_length``).
        warnings: list[dict[str, Any]] = []
        pushed_details: list[dict[str, str]] = []
        # The diff's moves, applied before any path is resolved (#792 I).
        manifest_dirty = relocate(manifest, diff_result) > 0

        # A data-app CREATE needs a Data Science client so its runtime type
        # travels into the target (CLI-8). Built only when the changeset
        # actually creates a data app, and entered alongside ``client`` so its
        # close() runs on every exit from the push block, a mid-push raise
        # included.
        ds_client = push_ds_client(self._ds_client_factory, project, changes)
        ds_context = ds_client if ds_client is not None else contextlib.nullcontext()

        with client, ds_context:
            self._ensure_branch_registered(manifest, branch_id, client)
            # POST /apps wants branchId=null for production, the numeric id for
            # a dev branch. Resolved against the API's default branch, not the
            # first manifest branch (#808), and only when a data app is created.
            ds_branch_id = resolve_ds_branch_id(client, ds_client, branch_id, warnings)
            branch_path = self._resolve_source_branch_path(manifest, project_root, branch_id)
            # An encryption failure stops push before its first write (#792 F).
            known_secrets = plan.encrypt_secrets(
                self,
                client,
                project_root / branch_path,
                manifest,
                allow_plaintext_fallback,
                branch_id=branch_id,
            )

            # Process configs before rows, and rebind variable links last.
            # A freshly-created parent config must carry its API-assigned ULID
            # before its rows POST (KFR-05), and a transformation's
            # variables_id / variables_values_id can only be resolved once both
            # the variables config and its values row exist (KFR-03). Partition
            # explicitly rather than relying on incidental diff ordering.
            config_changes = [c for c in changes if not bool(c.get("is_row"))]
            row_changes = [c for c in changes if bool(c.get("is_row"))]

            # The ids this push creates, for the rows and the link backfill.
            target = CreateTarget(project_root, branch_path, branch_id)
            ledger = CreateLedger.plan(self, manifest, changes, target)
            # (ULID parent, row placeholder id) -> row ULID; ULID parent -> rows
            # created under it. Keyed per parent: two configs can use one row id.
            created_row_id_map: dict[tuple[str, str], str] = {}
            created_rows_by_parent: dict[str, list[str]] = {}
            # Promote path (no tree of the target's own): every payload sent is
            # remapped source id -> target id in memory (D24).
            promoting = branch_path != self._find_branch_path(manifest, branch_id)
            checkpoint = partial(
                checkpoint_manifest,
                project_root,
                manifest,
                promote_branch_id=branch_id if promoting else None,
            )
            remap = PromoteRemap() if promoting else None
            if remap is not None:
                remap.plan(changes)
                remap.note_gone(all_changes)
                remap.use_target(client, branch_id)
                remap.hold = partial(hold_pending, client, manifest, target, checkpoint)

            # ---- Phase A: config creates / updates / deletes -------------
            for change in config_changes:
                change_type = change["change_type"]
                component_id = change["component_id"]
                config_id = change["config_id"]
                config_path_str = change.get("path", "")
                if remap is not None:
                    remap.refresh(manifest, branch_id, ledger.created_id_map, created_row_id_map)

                try:
                    if change_type == "added":
                        result = push_create(
                            self,
                            client,
                            component_id,
                            config_path_str,
                            project_root,
                            manifest,
                            branch_id,
                            allow_plaintext_fallback=allow_plaintext_fallback,
                            warnings=warnings,
                            ds_client=ds_client,
                            ds_branch_id=ds_branch_id,
                            known_secrets=known_secrets,
                            remap=remap,
                        )
                        if result:
                            manifest_dirty = True
                            record_created_config(
                                self,
                                client,
                                ledger,
                                manifest=manifest,
                                target=target,
                                component_id=component_id,
                                source_config_id=config_id,
                                config_path_str=config_path_str,
                                outcome=result,
                                warnings=warnings,
                                errors=errors,
                            )
                            created += 1
                            pushed_details.append(change)

                    elif change_type == "modified":
                        config_dir = project_root / branch_path / config_path_str
                        raise_on_legacy_boundary(
                            self,
                            client,
                            component_id=component_id,
                            config_id=config_id,
                            config_dir=config_dir,
                            manifest=manifest,
                            branch_id=branch_id,
                        )
                        response = push_update(
                            self,
                            client,
                            component_id,
                            config_id,
                            config_path_str,
                            project_root,
                            manifest,
                            branch_id,
                            allow_plaintext_fallback=allow_plaintext_fallback,
                            warnings=warnings,
                            known_secrets=known_secrets,
                            remap=remap,
                        )
                        # Update hashes so pull knows local == remote
                        if (config_dir / CONFIG_FILENAME).exists():
                            stamp_updated_config(
                                client,
                                manifest=manifest,
                                component_id=component_id,
                                config_id=config_id,
                                branch_id=branch_id,
                                config_path_str=config_path_str,
                                hashes=self._compute_config_hashes(config_dir, component_id),
                                response=response,
                                warnings=warnings,
                                source_tree=branch_path,
                                config_dir=config_dir,
                            )
                            manifest_dirty = True
                        updated += 1
                        pushed_details.append(change)

                    elif change_type == "deleted":
                        # A workspace's SQL editor sessions go first (CLI-25).
                        sql_workspaces.delete_remote_config(client, change, branch_id)
                        # Remove the target branch's entry only: another
                        # branch's entry for the same id still describes a
                        # config that exists there.
                        manifest.configurations = [
                            c
                            for c in manifest.configurations
                            if not (
                                c.component_id == component_id
                                and c.id == config_id
                                and same_branch(manifest, c.branch_id, branch_id)
                            )
                        ]
                        manifest_dirty = True
                        deleted += 1
                        pushed_details.append(change)

                except Exception as exc:
                    # Fail-closed on encryption failures: a partial push that
                    # omits the failed change would leave a plaintext secret
                    # elsewhere or a caller believing the push "mostly succeeded".
                    # Surface to the CLI (exit non-zero) rather than burying in
                    # result["errors"]. Same for a create the manifest could not
                    # record (D25).
                    if is_fatal_push_error(exc):
                        raise
                    self._record_push_error(errors, change_type, component_id, config_id, exc)

            # ---- Phase B: row creates / updates / deletes ----------------
            for change in row_changes:
                change_type = change["change_type"]
                component_id = change["component_id"]
                config_id = change["config_id"]
                source_row_id = change.get("source_row_id") or config_id  # risk 71
                config_path_str = change.get("path", "")
                parent_config_id = change.get("parent_config_id", "")
                manifest_dirty = True  # before a create's checkpoint (D25)
                try:
                    # The parent's id on the target after Phase A: a placeholder
                    # maps to its ULID (KFR-05), a new config dir to the config
                    # created there (D7) -- a parent that was not created skips
                    # the row with an error. An adopted dir is registered (D8).
                    effective_parent_id = ledger.row_parent(change)
                    if change.get("parent_path") and parent_config_id:
                        register_adopted_parent(
                            self,
                            client,
                            manifest=manifest,
                            target=target,
                            component_id=component_id,
                            config_id=parent_config_id,
                            config_path=change["parent_path"],
                        )
                    if remap is not None:
                        remap.refresh(
                            manifest, branch_id, ledger.created_id_map, created_row_id_map
                        )
                    new_row_id = push_row_change(
                        self,
                        client,
                        change_type=change_type,
                        component_id=component_id,
                        parent_config_id=effective_parent_id,
                        row_id=config_id,
                        source_row_id=source_row_id,
                        row_path_str=config_path_str,
                        project_root=project_root,
                        manifest=manifest,
                        branch_id=branch_id,
                        allow_plaintext_fallback=allow_plaintext_fallback,
                        warnings=warnings,
                        known_secrets=known_secrets,
                        checkpoint=checkpoint,
                        remap=remap,
                    )
                    if change_type == "added":
                        created += 1
                        if new_row_id:
                            if source_row_id:
                                created_row_id_map[(effective_parent_id, source_row_id)] = (
                                    new_row_id
                                )
                            created_rows_by_parent.setdefault(effective_parent_id, []).append(
                                new_row_id
                            )
                    elif change_type == "modified":
                        updated += 1
                    elif change_type == "deleted":
                        deleted += 1
                    pushed_details.append(change)

                except Exception as exc:
                    if is_fatal_push_error(exc):
                        raise
                    self._record_push_error(errors, change_type, component_id, config_id, exc)

            # ---- Promote path: references to configs created this push (D24)
            # Also after a pull moved promoted entries into their own tree.
            if remap is not None:
                remap.refresh(manifest, branch_id, ledger.created_id_map, created_row_id_map)
            manifest_dirty |= finish_promote_rebind(
                self,
                client,
                remap,
                manifest=manifest,
                target=target,
                warnings=warnings,
                errors=errors,
                diff_changes=diff_decisions(all_changes),
                ignored_components=sql_workspaces.effective_ignored_components(manifest),
            )

            # ---- Phase C: variable + shared-code links (KFR-03, CLI-24) --
            binding = resolve_transformation_links(
                self,
                client,
                ledger=ledger,
                created_row_id_map=created_row_id_map,
                created_rows_by_parent=created_rows_by_parent,
                manifest=manifest,
                branch_id=branch_id,
                source_dir=None if promoting else project_root / branch_path,
            )
            errors.extend(binding.errors)
            warnings.extend(binding.warnings)
            manifest_dirty |= bool(binding.configs_rewritten or binding.pending_changed)

            # ---- Phase D: flow/orchestrator task + schedule target backfill
            # After transformation links, remap the configIds that flows,
            # orchestrations and schedules run when they point at configs created
            # this push (golden/placeholder -> ULID). Reuses created_id_map.
            flow_binding = resolve_run_target_bindings(
                self,
                client,
                created_configs=ledger.created_configs,
                created_id_map=ledger.created_id_map,
                created_row_id_map=created_row_id_map,
                manifest=manifest,
                branch_id=branch_id,
                source_dir=None if promoting else project_root / branch_path,
                planned_rows=planned_row_refs(row_changes),
                targets=binding.targets,
            )
            errors.extend(flow_binding.errors)
            warnings.extend(flow_binding.warnings)
            manifest_dirty |= bool(flow_binding.configs_rewritten or flow_binding.pending_changed)

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
            **plan.report(),
            **shown,
        }
        if warnings:
            result_data["warnings"] = warnings
        result_data.update(flow_binding.push_fields(binding.shared_code_links))
        if name_drift_warnings and not no_name_drift_warnings:
            result_data["name_drift_warnings"] = name_drift_warnings
        if never_fetched:
            result_data["never_fetched"] = never_fetched
        if orphaned:
            result_data["orphaned"] = orphaned
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
        merged = merge_code_files(component_id, local_data, config_dir) if local_data else None
        cfg_hash = config_hash(merged) if merged is not None else ""
        return LocalConfigHashes(
            file_hash=file_hash,
            cfg_hash=cfg_hash,
            extra_hashes=current_companions(self, config_dir, component_id),
            disabled=file_disables(local_data),
        )

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
        record: dict[str, str] = {
            "change_type": change_type,
            "component_id": component_id,
            "config_id": config_id,
            "message": str(exc),
        }
        if isinstance(exc, KeboolaApiError) and exc.error_code:
            record["error_code"] = str(exc.error_code)
        errors.append(record)

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
        theirs: bool = False,
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
            theirs=theirs,
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

    def _resolve_branch_id(
        self,
        alias: str,
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
        from ..effective_branch import record_branch, resolve_branch
        from ..sync.branch_mapping import load_branch_mapping
        from ..sync.git_utils import get_current_branch

        # CLI override beats every persisted source so a user can target a
        # dev branch from a clean git workspace without first running
        # `branch use` or `branch-link`.
        if branch_override is not None:
            return record_branch(self._config_store, alias, branch_override, "explicit")

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
                        return record_branch(self._config_store, alias, None, "production")
                    raise ConfigError(
                        f"Git branch '{git_branch}' is not linked to a Keboola "
                        f"branch (branch-mapping.json missing). "
                        f"Run 'kbagent sync branch-link --project ALIAS' first."
                    ) from None
                entry = mapping.get(git_branch)
                if entry is not None:
                    # entry.keboola_id is None for production (default branch)
                    return record_branch(self._config_store, alias, entry.keboola_id, "git_mapping")
                # No entry for current branch -- default branch is always production
                if is_default:
                    return record_branch(self._config_store, alias, None, "production")
                raise ConfigError(
                    f"Git branch '{git_branch}' is not linked to a Keboola branch. "
                    f"Run 'kbagent sync branch-link --project ALIAS' first."
                )

        # Non git-branching: use active_branch_id or manifest fallback
        fallback = manifest.branches[0].id if manifest.branches else None
        return resolve_branch(self._config_store, alias, None, manifest_branch_id=fallback)

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
        content = dump_config_yaml(config_data)
        config_file.write_text(content, encoding="utf-8", newline="")
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _file_hash(self, file_path: Path) -> str:
        """Return the SHA256 hex digest of a file's contents."""
        content = file_path.read_bytes()
        return hashlib.sha256(content).hexdigest()

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
        """Read and parse a ``_config.yml`` file.

        Returns None when the file is missing, is not valid YAML or is not a
        mapping. ``sync diff`` / ``sync push`` check every file first
        (:func:`._sync_local_files.check_local_files`), so None there means
        "no file", never "a file that could not be read".
        """
        config_file = config_dir / CONFIG_FILENAME
        if not config_file.exists():
            return None
        data, problem = load_config_yaml(config_file)
        if problem:
            # diff / push report it (check_local_files); never read as data.
            logger.warning("%s %s", config_file, problem)
        return data

    def register_branch_dir(
        self,
        alias: str,
        project_root: Path,
        branch_id: int,
    ) -> str:
        """Resolve (and register if needed) the on-disk directory for *branch_id*.

        Thin wrapper over :func:`..sync.branch_registry.register_branch_dir`
        (issue #644); see that module for the semantics.
        """
        projects = self.resolve_projects([alias])
        return register_branch_dir(projects[alias], project_root, branch_id, self._client_factory)

    def resolve_scaffold_placement(
        self,
        alias: str,
        project_root: Path,
        branch_id: int | None,
    ) -> ScaffoldPlacement:
        """Resolve where a ``config new --push`` scaffold belongs on disk.

        Thin wrapper over :func:`..sync.branch_registry.resolve_scaffold_placement`
        (issue #644).
        """
        # Resolved for BOTH paths: the production path needs the project id
        # for the foreign-workspace mismatch check (PR #653 review sweep).
        project = self.resolve_projects([alias])[alias]
        return resolve_scaffold_placement(project, project_root, branch_id, self._client_factory)

    def _ensure_branch_registered(
        self,
        manifest: Manifest,
        branch_id: int | None,
        client: Any,
    ) -> str | None:
        """Delegate to :func:`..sync.branch_registry.ensure_branch_registered`."""
        return ensure_branch_registered(manifest, branch_id, client)

    def _find_branch_path(self, manifest: Manifest, branch_id: int | None) -> str:
        """Find the branch directory name for a given branch ID.

        Thin wrapper over :func:`sync.branch_scope.branch_tree_path`, which is
        also the normalizer the branch-scoping logic (issue #649) compares
        with: production is spelled ``None``, ``0`` and "the default branch's
        numeric id" depending on how the manifest entry was written, and all
        three must map to the same tree.
        """
        return branch_tree_path(manifest, branch_id)

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
        only_branch_path: str | None = None,
    ) -> list[dict[str, str]]:
        """Scan for _config.yml files that are not tracked in the manifest.

        Delegates to :func:`sync.branch_scope.find_untracked_configs`; see
        there for the branch-scoping rules (issues #267, #482, #649).
        """
        return find_untracked_configs(
            project_root, manifest, self._read_config_file, only_branch_path
        )
