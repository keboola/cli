"""Manifest project checks: a sync tree belongs to one project (CLI-31).

``sync pull``, ``sync diff`` and ``sync push`` take the target twice: the
manifest records ``project.id`` and ``apiHost``, and ``--project`` names an
alias. Without a check, a tree of project A used with ``--project B`` is
pulled over or pushed into B. Project IDs are unique only per stack, so the
host is compared too. ``sync init --adopt-existing`` has its own check here.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ..constants import KEBOOLA_DIR_NAME, MANIFEST_FILENAME, MANIFEST_VERSION
from ..errors import ConfigError, KeboolaApiError, SyncInitError
from ..sync.dir_lock import sync_dir_lock
from ..sync.manifest import (
    Manifest,
    ManifestBranch,
    ManifestNaming,
    ManifestProject,
    load_manifest,
    save_manifest,
)
from .base import find_default_branch_id

if TYPE_CHECKING:
    from ..models import ProjectConfig
    from .sync_service import SyncService


def _host(value: str) -> str:
    """Return the lowercase host of a stack URL or a bare ``apiHost``."""
    if "://" not in value:
        value = f"https://{value}"
    return urlparse(value).netloc.lower()


def verified_project_id(
    service: SyncService, alias: str, project: ProjectConfig, project_root: Path
) -> int:
    """Return the project ID of *project* from one token verification call.

    Raises:
        ConfigError: the call fails or returns no project ID.
    """
    client = service._client_factory(project.stack_url, project.token)
    try:
        with client:
            project_id = client.verify_token().project_id
    except KeboolaApiError as exc:
        raise ConfigError(
            f"Cannot check that {project_root} belongs to project '{alias}': "
            f"token verification failed ({exc.message}). Set a valid token with "
            f"'kbagent project edit --project {alias} --token-stdin'."
        ) from None
    if project_id is None:
        raise ConfigError(
            f"Cannot check that {project_root} belongs to project '{alias}': "
            "token verification returned no project ID."
        )
    return project_id


def load_project_manifest(
    service: SyncService,
    alias: str,
    project: ProjectConfig,
    project_root: Path,
) -> Manifest:
    """Load the manifest of *project_root* and refuse it if it belongs to another project.

    The stored ``project_id`` is used when present. Without it, the ID is read
    once from token verification; that call happens only when the hosts match.

    Raises:
        FileNotFoundError: *project_root* has no manifest.
        ConfigError: the manifest project ID or ``apiHost`` differs from the
            project, or the project ID cannot be read from the token.
    """
    return validate_project_manifest(
        service, alias, project, project_root, load_manifest(project_root)
    )


def validate_project_manifest(
    service: SyncService,
    alias: str,
    project: ProjectConfig,
    project_root: Path,
    manifest: Manifest,
) -> Manifest:
    """Refuse a disk-backed or in-memory manifest that belongs to another project."""
    tree_host = _host(manifest.project.api_host)
    project_host = _host(project.stack_url)
    project_id = project.project_id
    if project_id is None and tree_host == project_host:
        project_id = verified_project_id(service, alias, project, project_root)
    if tree_host == project_host and manifest.project.id == project_id:
        return manifest
    tree = f"project {manifest.project.id}"
    target = f"project {project_id}" if project_id is not None else "a project"
    if tree_host != project_host:
        tree += f" on {tree_host}"
        target += f" on {project_host}"
    raise ConfigError(
        f"Sync directory {project_root} belongs to {tree}, but --project '{alias}' "
        f"is {target}. Use the directory of project '{alias}', or copy configs "
        "between projects with 'kbagent sync clone'."
    )


def adopt_existing_manifest(
    service: SyncService,
    alias: str,
    project_root: Path,
    project: Any,
    sync_workspaces: bool = False,
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

    client = service._client_factory(project.stack_url, project.token)
    with client:
        token_info = client.verify_token()

    if existing.project.id != token_info.project_id:
        raise ConfigError(
            f"Manifest project_id={existing.project.id} does not match alias "
            f"'{alias}' project_id={token_info.project_id}; refusing to overwrite. "
            "Check that --project points to the correct alias."
        )

    api_host = project.stack_url.replace("https://", "").rstrip("/")
    existing.sync_workspaces = existing.sync_workspaces or sync_workspaces
    save_manifest(project_root, existing)

    return {
        "status": "adopted",
        "project_id": token_info.project_id,
        "project_alias": alias,
        "api_host": api_host,
        "git_branching": existing.git_branching.enabled,
        "default_branch": existing.git_branching.default_branch,
        "sync_workspaces": existing.sync_workspaces,
        "files_created": [],
    }


def build_initial_manifest(
    service: SyncService,
    project: ProjectConfig,
    *,
    sync_workspaces: bool = False,
) -> Manifest:
    """Fetch the API metadata for a new manifest without writing it."""
    client = service._client_factory(project.stack_url, project.token)
    with client:
        token_info = client.verify_token()
        branches = client.list_dev_branches()

    project_id = token_info.project_id
    if project_id is None:
        raise ConfigError("Token verification returned no project ID; cannot build manifest.")
    api_host = project.stack_url.replace("https://", "").rstrip("/")
    default_branch_id = find_default_branch_id(branches)
    return Manifest(
        version=MANIFEST_VERSION,
        project=ManifestProject(id=project_id, apiHost=api_host),
        allowTargetEnv=True,
        naming=ManifestNaming(),
        syncWorkspaces=sync_workspaces,
        branches=[ManifestBranch(id=default_branch_id, path="main")]
        if default_branch_id is not None
        else [],
        configurations=[],
    )


def init_missing_tree(
    service: SyncService, alias: str, project_root: Path, *, dry_run: bool
) -> Manifest | None:
    """Initialize a missing manifest, returning an in-memory copy only for a preview.

    A real first pull initializes under the directory lock. A failed init
    or preview-manifest build is :class:`SyncInitError`.
    """
    if (project_root / KEBOOLA_DIR_NAME / MANIFEST_FILENAME).exists():
        return None
    try:
        if dry_run:
            project = service.resolve_projects([alias])[alias]
            return build_initial_manifest(service, project)
        service.init_sync(alias, project_root)
    except Exception as exc:
        raise SyncInitError(exc) from exc
    return None


@contextmanager
def sync_tree_scope(directory: Path | None) -> Iterator[bool]:
    """Decide once whether *directory* is a sync tree, and lock it for the block (L5).

    Yields True for a folder that has ``.keboola/`` now: it is locked until the
    block ends (``SYNC_DIRECTORY_BUSY`` / ``ConfigError`` before the block).
    Yields False otherwise: a plain folder for the whole block -- not locked,
    and the caller must never read or write a manifest there, not even one a
    first `sync pull` creates meanwhile (#827 round 2). Used by
    ``config new --output-dir`` and ``config rename --directory``.
    """
    if directory is None or not (directory / KEBOOLA_DIR_NAME).is_dir():
        yield False
        return
    with sync_dir_lock(directory):
        yield True
