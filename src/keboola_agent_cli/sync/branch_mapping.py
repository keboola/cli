"""Branch mapping for git-to-Keboola branch mapping.

Manages .keboola/branch-mapping.json which maps git branch names
to Keboola development branch IDs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..constants import BRANCH_MAPPING_FILENAME, KEBOOLA_DIR_NAME


def _coerce_keboola_id(raw: Any) -> int | None:
    """Coerce a raw ``id`` field from JSON to ``int | None``.

    Older kbagent versions (<= 0.30.3) wrote branch IDs as strings (e.g.
    ``"99999"``) due to issue #267. ``None`` means production. Empty
    string is also treated as production for legacy tolerance.

    Raises ``ValueError`` with a descriptive message if *raw* is neither
    None, empty, nor parseable as an int (e.g. a hand-edited
    ``branch-mapping.json`` containing ``"id": "not-a-number"``). The
    caller (typically ``BranchMapping.from_dict``) should let this
    bubble up to ``load_branch_mapping`` which converts it to a
    ConfigError surface (issue #269 sec-20).
    """
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid branch ID in branch-mapping.json: {raw!r}. "
            f"Expected null or an integer; got {type(raw).__name__}."
        ) from exc


class BranchMappingEntry:
    """A single git branch -> Keboola branch mapping."""

    def __init__(self, keboola_id: int | None, name: str):
        self.keboola_id = keboola_id  # None = production
        self.name = name

    def is_production(self) -> bool:
        return self.keboola_id is None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.keboola_id, "name": self.name}


class BranchMapping:
    """Manages git-to-Keboola branch mappings."""

    def __init__(self) -> None:
        self.version: int = 1
        self.mappings: dict[str, BranchMappingEntry] = {}

    def get(self, git_branch: str) -> BranchMappingEntry | None:
        return self.mappings.get(git_branch)

    def set(self, git_branch: str, keboola_id: int | None, name: str) -> None:
        self.mappings[git_branch] = BranchMappingEntry(keboola_id, name)

    def remove(self, git_branch: str) -> bool:
        if git_branch in self.mappings:
            del self.mappings[git_branch]
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "mappings": {k: v.to_dict() for k, v in self.mappings.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BranchMapping:
        mapping = cls()
        mapping.version = data.get("version", 1)
        for git_branch, entry in data.get("mappings", {}).items():
            mapping.mappings[git_branch] = BranchMappingEntry(
                keboola_id=_coerce_keboola_id(entry.get("id")),
                name=entry.get("name", ""),
            )
        return mapping


def load_branch_mapping(project_root: Path) -> BranchMapping:
    """Load .keboola/branch-mapping.json.

    Raises:
        FileNotFoundError: If the mapping file does not exist.
        ValueError: If the JSON cannot be parsed or contains a malformed
            branch ID. The descriptive message names the offending file
            so the user can find and fix it (issue #269 sec-20).
    """
    path = project_root / KEBOOLA_DIR_NAME / BRANCH_MAPPING_FILENAME
    if not path.exists():
        raise FileNotFoundError(f"Branch mapping not found at {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return BranchMapping.from_dict(data)
    except ValueError as exc:
        # _coerce_keboola_id raises ValueError on malformed IDs; wrap with
        # path context so the user knows which file to fix.
        raise ValueError(f"Failed to parse {path}: {exc}") from exc


def save_branch_mapping(project_root: Path, mapping: BranchMapping) -> None:
    """Save branch mapping to .keboola/branch-mapping.json."""
    path = project_root / KEBOOLA_DIR_NAME / BRANCH_MAPPING_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(mapping.to_dict(), indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def find_sync_workspace(start: Path | None = None) -> Path | None:
    """Locate the nearest enclosing sync workspace.

    Walks up from *start* (or the current working directory) and returns
    the first directory that contains a ``.keboola/branch-mapping.json``
    file, or ``None`` if none is found before the filesystem root.
    """
    cursor = (start or Path.cwd()).resolve()
    for candidate in [cursor, *cursor.parents]:
        if (candidate / KEBOOLA_DIR_NAME / BRANCH_MAPPING_FILENAME).exists():
            return candidate
    return None


def cleanup_branch_id_from_mapping(branch_id: int) -> dict[str, Any] | None:
    """Remove every git-branch entry that maps to *branch_id* from the
    nearest enclosing sync workspace, if one exists.

    Designed to be a best-effort cleanup hook for ``branch delete`` and
    ``branch merge``: locates ``.keboola/branch-mapping.json`` via
    :func:`find_sync_workspace`, removes any entries whose ``keboola_id``
    equals *branch_id*, and persists the change. Returns a dict
    describing what was unlinked, or ``None`` if no workspace was found
    or no entry referenced the branch (no-op).
    """
    project_root = find_sync_workspace()
    if project_root is None:
        return None
    try:
        mapping = load_branch_mapping(project_root)
    except (FileNotFoundError, ValueError):
        return None

    removed: list[str] = []
    for git_branch, entry in list(mapping.mappings.items()):
        if entry.keboola_id == branch_id:
            mapping.remove(git_branch)
            removed.append(git_branch)

    if not removed:
        return None
    save_branch_mapping(project_root, mapping)
    return {"project_root": str(project_root), "git_branches_unlinked": removed}
