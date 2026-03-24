"""Tests for BranchMapping model and I/O (branch_mapping.py).

Covers the BranchMappingEntry and BranchMapping classes, as well
as load/save filesystem round-trips.
"""

import json
from pathlib import Path

import pytest

from keboola_agent_cli.constants import BRANCH_MAPPING_FILENAME, KEBOOLA_DIR_NAME
from keboola_agent_cli.sync.branch_mapping import (
    BranchMapping,
    BranchMappingEntry,
    load_branch_mapping,
    save_branch_mapping,
)


class TestBranchMappingEntry:
    """Tests for BranchMappingEntry."""

    def test_branch_mapping_entry_production(self) -> None:
        """None keboola_id indicates production branch."""
        entry = BranchMappingEntry(keboola_id=None, name="Main")
        assert entry.is_production() is True
        assert entry.keboola_id is None
        assert entry.name == "Main"

    def test_branch_mapping_entry_dev_branch(self) -> None:
        """Non-None keboola_id indicates development branch."""
        entry = BranchMappingEntry(keboola_id="972851", name="feature/auth")
        assert entry.is_production() is False
        assert entry.keboola_id == "972851"
        assert entry.name == "feature/auth"

    def test_branch_mapping_entry_to_dict(self) -> None:
        """to_dict returns the correct JSON-ready structure."""
        entry = BranchMappingEntry(keboola_id="12345", name="my-branch")
        assert entry.to_dict() == {"id": "12345", "name": "my-branch"}

    def test_branch_mapping_entry_production_to_dict(self) -> None:
        """Production entry serializes id as None."""
        entry = BranchMappingEntry(keboola_id=None, name="Main")
        assert entry.to_dict() == {"id": None, "name": "Main"}


class TestBranchMapping:
    """Tests for BranchMapping."""

    def test_branch_mapping_set_get(self) -> None:
        """set and get work correctly."""
        mapping = BranchMapping()
        mapping.set("main", None, "Main")
        mapping.set("feature/auth", "972851", "feature/auth")

        main_entry = mapping.get("main")
        assert main_entry is not None
        assert main_entry.is_production() is True
        assert main_entry.name == "Main"

        feature_entry = mapping.get("feature/auth")
        assert feature_entry is not None
        assert feature_entry.keboola_id == "972851"
        assert feature_entry.name == "feature/auth"

    def test_branch_mapping_get_nonexistent(self) -> None:
        """get returns None for nonexistent branch."""
        mapping = BranchMapping()
        assert mapping.get("nonexistent") is None

    def test_branch_mapping_remove(self) -> None:
        """remove deletes an existing mapping and returns True."""
        mapping = BranchMapping()
        mapping.set("feature/auth", "972851", "feature/auth")

        assert mapping.remove("feature/auth") is True
        assert mapping.get("feature/auth") is None

    def test_branch_mapping_remove_nonexistent(self) -> None:
        """remove returns False for nonexistent branch."""
        mapping = BranchMapping()
        assert mapping.remove("nonexistent") is False

    def test_branch_mapping_round_trip(self) -> None:
        """to_dict/from_dict round-trip preserves data."""
        mapping = BranchMapping()
        mapping.set("main", None, "Main")
        mapping.set("feature/auth", "972851", "feature/auth")
        mapping.set("bugfix/123", "88888", "bugfix/123")

        data = mapping.to_dict()
        restored = BranchMapping.from_dict(data)

        assert restored.version == 1
        assert len(restored.mappings) == 3

        main_entry = restored.get("main")
        assert main_entry is not None
        assert main_entry.is_production() is True
        assert main_entry.name == "Main"

        auth_entry = restored.get("feature/auth")
        assert auth_entry is not None
        assert auth_entry.keboola_id == "972851"
        assert auth_entry.name == "feature/auth"

        bugfix_entry = restored.get("bugfix/123")
        assert bugfix_entry is not None
        assert bugfix_entry.keboola_id == "88888"

    def test_branch_mapping_to_dict_format(self) -> None:
        """to_dict produces the Go CLI compatible format."""
        mapping = BranchMapping()
        mapping.set("main", None, "Main")
        mapping.set("feature/auth", "972851", "feature/auth")

        data = mapping.to_dict()
        assert data["version"] == 1
        assert data["mappings"]["main"] == {"id": None, "name": "Main"}
        assert data["mappings"]["feature/auth"] == {"id": "972851", "name": "feature/auth"}

    def test_branch_mapping_from_dict_empty(self) -> None:
        """from_dict handles empty mappings."""
        data = {"version": 1, "mappings": {}}
        mapping = BranchMapping.from_dict(data)
        assert mapping.version == 1
        assert len(mapping.mappings) == 0

    def test_branch_mapping_from_dict_defaults(self) -> None:
        """from_dict uses defaults when fields are missing."""
        data = {}
        mapping = BranchMapping.from_dict(data)
        assert mapping.version == 1
        assert len(mapping.mappings) == 0


class TestBranchMappingIO:
    """Tests for load/save filesystem operations."""

    def test_load_save_branch_mapping(self, tmp_path: Path) -> None:
        """Filesystem round-trip: save then load preserves data."""
        mapping = BranchMapping()
        mapping.set("main", None, "Main")
        mapping.set("feature/auth", "972851", "feature/auth")

        save_branch_mapping(tmp_path, mapping)

        # Verify file exists
        path = tmp_path / KEBOOLA_DIR_NAME / BRANCH_MAPPING_FILENAME
        assert path.exists()

        # Verify raw JSON content
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["version"] == 1
        assert raw["mappings"]["main"]["id"] is None
        assert raw["mappings"]["feature/auth"]["id"] == "972851"

        # Load and verify
        loaded = load_branch_mapping(tmp_path)
        assert loaded.version == 1
        assert len(loaded.mappings) == 2

        main_entry = loaded.get("main")
        assert main_entry is not None
        assert main_entry.is_production() is True

        auth_entry = loaded.get("feature/auth")
        assert auth_entry is not None
        assert auth_entry.keboola_id == "972851"

    def test_load_branch_mapping_not_found(self, tmp_path: Path) -> None:
        """load_branch_mapping raises FileNotFoundError when file is missing."""
        with pytest.raises(FileNotFoundError, match="Branch mapping not found"):
            load_branch_mapping(tmp_path)

    def test_save_creates_keboola_dir(self, tmp_path: Path) -> None:
        """save_branch_mapping creates .keboola/ directory if it doesn't exist."""
        mapping = BranchMapping()
        mapping.set("main", None, "Main")

        keboola_dir = tmp_path / KEBOOLA_DIR_NAME
        assert not keboola_dir.exists()

        save_branch_mapping(tmp_path, mapping)

        assert keboola_dir.exists()
        assert (keboola_dir / BRANCH_MAPPING_FILENAME).exists()

    def test_save_overwrites_existing(self, tmp_path: Path) -> None:
        """save_branch_mapping overwrites existing file."""
        mapping1 = BranchMapping()
        mapping1.set("main", None, "Main")
        save_branch_mapping(tmp_path, mapping1)

        mapping2 = BranchMapping()
        mapping2.set("main", None, "Main")
        mapping2.set("develop", "99999", "develop")
        save_branch_mapping(tmp_path, mapping2)

        loaded = load_branch_mapping(tmp_path)
        assert len(loaded.mappings) == 2
