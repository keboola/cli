"""Branch mapping for git-to-Keboola branch mapping.

Manages .keboola/branch-mapping.json which maps git branch names
to Keboola development branch IDs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..constants import BRANCH_MAPPING_FILENAME, KEBOOLA_DIR_NAME


class BranchMappingEntry:
    """A single git branch -> Keboola branch mapping."""

    def __init__(self, keboola_id: str | None, name: str):
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

    def set(self, git_branch: str, keboola_id: str | None, name: str) -> None:
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
                keboola_id=entry.get("id"),
                name=entry.get("name", ""),
            )
        return mapping


def load_branch_mapping(project_root: Path) -> BranchMapping:
    """Load .keboola/branch-mapping.json."""
    path = project_root / KEBOOLA_DIR_NAME / BRANCH_MAPPING_FILENAME
    if not path.exists():
        raise FileNotFoundError(f"Branch mapping not found at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return BranchMapping.from_dict(data)


def save_branch_mapping(project_root: Path, mapping: BranchMapping) -> None:
    """Save branch mapping to .keboola/branch-mapping.json."""
    path = project_root / KEBOOLA_DIR_NAME / BRANCH_MAPPING_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(mapping.to_dict(), indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
