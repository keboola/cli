"""Manifest project checks: a sync tree belongs to one project (CLI-31).

``sync pull``, ``sync diff`` and ``sync push`` take the target twice: the
manifest records ``project.id`` and ``apiHost``, and ``--project`` names an
alias. Without a check, a tree of project A used with ``--project B`` is
pulled over or pushed into B. Project IDs are unique only per stack, so the
host is compared too. ``sync init --adopt-existing`` has its own check here.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ..errors import ConfigError, KeboolaApiError
from ..sync.manifest import Manifest, load_manifest, save_manifest

if TYPE_CHECKING:
    from ..models import ProjectConfig
    from .sync_service import SyncService


def _host(value: str) -> str:
    """Return the lowercase host of a stack URL or a bare ``apiHost``."""
    if "://" not in value:
        value = f"https://{value}"
    return urlparse(value).netloc.lower()


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
    manifest = load_manifest(project_root)
    tree_host = _host(manifest.project.api_host)
    project_host = _host(project.stack_url)
    project_id = project.project_id
    if project_id is None and tree_host == project_host:
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
