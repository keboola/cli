"""Branch directory registration for sync workspaces.

Extracted from ``SyncService`` (file-size budget): resolving/registering the
``manifest.branches`` entry for a dev branch is needed both by ``sync pull``
/ ``sync diff`` (ensure the pulled branch has a directory) and by
``config new --push --output-dir`` (issue #644: a config created in a dev
branch must scaffold into that branch's subtree, never into the default
branch's tree).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..errors import ConfigError
from .manifest import Manifest, ManifestBranch, load_manifest, save_manifest
from .naming import sanitize_name

logger = logging.getLogger(__name__)


def ensure_branch_registered(
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


def register_branch_dir(
    project: Any,
    project_root: Path,
    branch_id: int,
    client_factory: Any,
) -> str:
    """Resolve (and register if needed) the on-disk directory for *branch_id*.

    Used by ``config new --push --output-dir`` (issue #644): a config
    created in a dev branch must scaffold into that branch's subtree, not
    into the default branch's tree -- a wrong-branch file is invisible to
    the dev-branch push and later duplicates the config on a production
    push. When the branch is not yet in ``manifest.branches``, it is
    registered exactly the way ``sync pull --branch`` would
    (:func:`ensure_branch_registered`: branch name fetched from the API,
    sanitized path, ``branch-{id}`` fallback), so a later pull reuses the
    same directory.

    Args:
        project: Resolved project config (``stack_url``, ``token``,
            ``project_id`` attributes).
        project_root: Sync workspace root (must contain ``.keboola/``).
        branch_id: Dev branch the configuration was created in.
        client_factory: ``(stack_url, token) -> KeboolaClient`` callable.

    Raises:
        ConfigError: manifest missing/unreadable, or the manifest belongs
            to a different project than *project*.
        KeboolaApiError: branch-name lookup failed.
    """
    manifest = load_manifest(project_root)
    if project.project_id is not None and manifest.project.id != project.project_id:
        raise ConfigError(
            f"Manifest in {project_root} belongs to project {manifest.project.id}, "
            f"not to project {project.project_id}"
        )
    for branch in manifest.branches:
        if branch.id == branch_id:
            return branch.path
    client = client_factory(project.stack_url, project.token)
    with client:
        path = ensure_branch_registered(manifest, branch_id, client)
    save_manifest(project_root, manifest)
    if path:
        return path
    # ensure_branch_registered returned None despite the pre-check miss --
    # defensive re-lookup so the caller always gets a directory.
    for branch in manifest.branches:
        if branch.id == branch_id:
            return branch.path
    return f"branch-{branch_id}"
