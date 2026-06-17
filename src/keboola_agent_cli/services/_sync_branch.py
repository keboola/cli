"""Git-branch <-> Keboola-dev-branch linking (from sync_service.py).

Backs ``kbagent sync branch-link / branch-unlink / branch-status`` in
git-branching mode: maps the current git branch to a Keboola dev branch in
``.keboola/branch-mapping.json``. ``branch_link`` needs the ``SyncService`` (to
resolve the project + build a client); the read/unlink helpers are pure.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import ConfigError
from ..sync.branch_mapping import BranchMapping, load_branch_mapping, save_branch_mapping
from ..sync.manifest import load_manifest

if TYPE_CHECKING:
    from .sync_service import SyncService


def branch_link(
    service: SyncService,
    alias: str,
    project_root: Path,
    branch_id: int | None = None,
    branch_name: str | None = None,
) -> dict[str, Any]:
    """Link the current git branch to a Keboola development branch.

    If no ``branch_id`` or ``branch_name`` is given: read the current git branch,
    search for an existing Keboola branch with the same name, create one if
    absent, and save the mapping. Returns a dict with the link result.
    """
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

    projects = service.resolve_projects([alias])
    project = projects[alias]
    client = service._client_factory(project.stack_url, project.token)

    with client:
        if branch_id:
            # Link to existing branch by ID
            branches = client.list_dev_branches()
            branch_info = next((b for b in branches if b["id"] == branch_id), None)
            if branch_info is None:
                raise ConfigError(f"Keboola branch {branch_id} not found.")
            kbc_branch_id = int(branch_info["id"])
            kbc_branch_name = branch_info.get("name", "")
        elif branch_name:
            # Search by name or create
            branches = client.list_dev_branches()
            branch_info = next((b for b in branches if b.get("name") == branch_name), None)
            if branch_info:
                kbc_branch_id = int(branch_info["id"])
                kbc_branch_name = branch_info.get("name", "")
            else:
                result = client.create_dev_branch(name=branch_name)
                kbc_branch_id = int(result["id"])
                kbc_branch_name = branch_name
        else:
            # Default: use git branch name to search/create
            branches = client.list_dev_branches()
            branch_info = next((b for b in branches if b.get("name") == git_branch), None)
            if branch_info:
                kbc_branch_id = int(branch_info["id"])
                kbc_branch_name = branch_info.get("name", "")
            else:
                result = client.create_dev_branch(name=git_branch)
                kbc_branch_id = int(result["id"])
                kbc_branch_name = git_branch

    mapping.set(git_branch, kbc_branch_id, kbc_branch_name)
    save_branch_mapping(project_root, mapping)

    return {
        "status": "linked",
        "git_branch": git_branch,
        "keboola_branch_id": kbc_branch_id,
        "keboola_branch_name": kbc_branch_name,
    }


def branch_unlink(project_root: Path) -> dict[str, Any]:
    """Remove the branch mapping for the current git branch."""
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


def branch_status(project_root: Path) -> dict[str, Any]:
    """Show the branch mapping status for the current git branch."""
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
