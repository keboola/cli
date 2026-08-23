"""Branch directory registration for sync workspaces.

Extracted from ``SyncService`` (file-size budget): resolving/registering the
``manifest.branches`` entry for a dev branch is needed both by ``sync pull``
/ ``sync diff`` (ensure the pulled branch has a directory) and by
``config new --push --output-dir`` (issue #644: a config created in a dev
branch must scaffold into that branch's subtree, never into the default
branch's tree).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..constants import KEBOOLA_DIR_NAME, MANIFEST_FILENAME
from ..errors import ConfigError
from .manifest import Manifest, ManifestBranch, load_manifest, save_manifest
from .naming import sanitize_name

logger = logging.getLogger(__name__)


def fallback_branch_dir(branch_id: int) -> str:
    """Canonical directory name for a branch whose real name is unknown.

    Single source of the ``branch-{id}`` convention -- one spelling
    everywhere means a later ``sync pull --branch`` lands in the same
    directory a fallback scaffold was written to.
    """
    return f"branch-{branch_id}"


def default_branch_prefix(project_root: Path) -> str | None:
    """Tolerant read of the default branch's directory from the manifest.

    Returns ``None`` when *project_root* is not a sync workspace or the
    manifest is unreadable -- callers then use a flat layout. Deliberately a
    raw JSON peek, not :func:`load_manifest`: the historical
    ``_detect_branch_prefix`` behaviour of ``config new`` tolerates partial
    manifests (e.g. hand-written or older shapes missing optional sections),
    and a read-only prefix lookup has no business schema-validating the
    whole file.
    """
    raw = _peek_manifest(project_root)
    if raw:
        branches = raw.get("branches", [])
        if branches:
            return branches[0].get("path") or None
    return None


def _peek_manifest(project_root: Path) -> dict[str, Any] | None:
    """Raw, tolerant read of ``.keboola/manifest.json`` (None on any problem)."""
    manifest_path = project_root / KEBOOLA_DIR_NAME / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None
    try:
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else None
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Could not read manifest under %s: %s", project_root, exc)
        return None


@dataclass(frozen=True)
class ScaffoldPlacement:
    """Where a pushed scaffold's files belong, plus an optional warning.

    ``branch_prefix`` is the branch directory relative to the output dir
    (``None`` = flat layout, no sync workspace). ``warning`` is set when
    placement degraded (branch registration failed) and should be surfaced
    to the user.
    """

    branch_prefix: str | None
    warning: str | None = None


def resolve_scaffold_placement(
    project: Any,
    project_root: Path,
    branch_id: int | None,
    client_factory: Any,
) -> ScaffoldPlacement:
    """Resolve the branch subtree a pushed scaffold must be written into.

    - Production create (``branch_id is None``): the default branch's
      directory when *project_root* is a sync workspace, flat otherwise
      (pre-#644 behaviour).
    - Dev-branch create outside a sync workspace: flat layout.
    - Dev-branch create inside a sync workspace: the branch's directory
      from the manifest, registering the branch first when unknown
      (:func:`register_branch_dir`). On any failure the files fall back to
      ``branch-{id}/`` with a warning -- NEVER to the default branch tree,
      because a wrong-branch file is exactly the duplicate factory issue
      #644 describes. This includes a workspace belonging to a different
      project: the config was already created remotely, so the files are
      still written (inert, untracked by that workspace's manifest) rather
      than lost, and the warning names the mismatch.
    """
    if branch_id is None:
        mismatch = _project_mismatch_warning(project, project_root)
        if mismatch:
            # Foreign workspace: write FLAT (outside every branch tree, so a
            # later `sync push` there can never pick the files up as a new
            # config of the WRONG project) and say so.
            return ScaffoldPlacement(None, mismatch)
        return ScaffoldPlacement(default_branch_prefix(project_root))
    if not (project_root / KEBOOLA_DIR_NAME / MANIFEST_FILENAME).is_file():
        return ScaffoldPlacement(None)
    try:
        return ScaffoldPlacement(
            register_branch_dir(project, project_root, int(branch_id), client_factory)
        )
    except Exception as exc:
        fallback = fallback_branch_dir(branch_id)
        warning = (
            f"Could not resolve the manifest directory for branch {branch_id} "
            f"({exc}); scaffold written under '{fallback}/'. Run "
            f"'kbagent sync pull --branch {branch_id}' to reconcile."
        )
        logger.warning(warning)
        return ScaffoldPlacement(fallback, warning)


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


def _project_mismatch_warning(project: Any, project_root: Path) -> str | None:
    """Warning text when *project_root*'s manifest belongs to another project.

    ``None`` when the identities match or either side is unknown (no
    manifest, unreadable manifest, or a project without a stored id).
    """
    if project is None or getattr(project, "project_id", None) is None:
        return None
    raw = _peek_manifest(project_root)
    manifest_project_id = (raw or {}).get("project", {}).get("id")
    if manifest_project_id is None or manifest_project_id == project.project_id:
        return None
    return (
        f"Manifest in {project_root} belongs to project {manifest_project_id}, "
        f"not to project {project.project_id}; scaffold written flat at the "
        f"workspace root (untracked by that workspace's sync tree)."
    )
