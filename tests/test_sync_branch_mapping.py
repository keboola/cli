"""Tests for BranchMapping model and I/O (branch_mapping.py).

Covers the BranchMappingEntry and BranchMapping classes, as well
as load/save filesystem round-trips. After issue #267, ``keboola_id``
is ``int | None``; legacy str-typed values written by older versions
are coerced on load.
"""

import json
from pathlib import Path

import pytest

from keboola_agent_cli.constants import BRANCH_MAPPING_FILENAME, KEBOOLA_DIR_NAME
from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.sync.branch_mapping import (
    BranchMapping,
    BranchMappingEntry,
    cleanup_branch_id_from_mapping,
    find_sync_workspace,
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
        entry = BranchMappingEntry(keboola_id=972851, name="feature/auth")
        assert entry.is_production() is False
        assert entry.keboola_id == 972851
        assert entry.name == "feature/auth"

    def test_branch_mapping_entry_to_dict(self) -> None:
        """to_dict returns the correct JSON-ready structure."""
        entry = BranchMappingEntry(keboola_id=12345, name="my-branch")
        assert entry.to_dict() == {"id": 12345, "name": "my-branch"}

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
        mapping.set("feature/auth", 972851, "feature/auth")

        main_entry = mapping.get("main")
        assert main_entry is not None
        assert main_entry.is_production() is True
        assert main_entry.name == "Main"

        feature_entry = mapping.get("feature/auth")
        assert feature_entry is not None
        assert feature_entry.keboola_id == 972851
        assert feature_entry.name == "feature/auth"

    def test_branch_mapping_get_nonexistent(self) -> None:
        """get returns None for nonexistent branch."""
        mapping = BranchMapping()
        assert mapping.get("nonexistent") is None

    def test_branch_mapping_remove(self) -> None:
        """remove deletes an existing mapping and returns True."""
        mapping = BranchMapping()
        mapping.set("feature/auth", 972851, "feature/auth")

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
        mapping.set("feature/auth", 972851, "feature/auth")
        mapping.set("bugfix/123", 88888, "bugfix/123")

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
        assert auth_entry.keboola_id == 972851
        assert auth_entry.name == "feature/auth"

        bugfix_entry = restored.get("bugfix/123")
        assert bugfix_entry is not None
        assert bugfix_entry.keboola_id == 88888

    def test_branch_mapping_to_dict_format(self) -> None:
        """to_dict produces the Go CLI compatible format."""
        mapping = BranchMapping()
        mapping.set("main", None, "Main")
        mapping.set("feature/auth", 972851, "feature/auth")

        data = mapping.to_dict()
        assert data["version"] == 1
        assert data["mappings"]["main"] == {"id": None, "name": "Main"}
        assert data["mappings"]["feature/auth"] == {"id": 972851, "name": "feature/auth"}

    def test_branch_mapping_from_dict_empty(self) -> None:
        """from_dict handles empty mappings."""
        data = {"version": 1, "mappings": {}}
        mapping = BranchMapping.from_dict(data)
        assert mapping.version == 1
        assert len(mapping.mappings) == 0

    def test_branch_mapping_from_dict_defaults(self) -> None:
        """from_dict uses defaults when fields are missing."""
        data: dict = {}
        mapping = BranchMapping.from_dict(data)
        assert mapping.version == 1
        assert len(mapping.mappings) == 0

    def test_branch_mapping_from_dict_legacy_string_id(self) -> None:
        """Issue #267: legacy ``branch-mapping.json`` with str-typed ``id`` is
        silently migrated to int on load. This guarantees existing users do
        not need to manually edit their workspace after upgrading."""
        data = {
            "version": 1,
            "mappings": {
                "main": {"id": None, "name": "Main"},
                "feature/auth": {"id": "972851", "name": "feature/auth"},
                "bugfix/123": {"id": "88888", "name": "bugfix/123"},
            },
        }
        mapping = BranchMapping.from_dict(data)
        assert mapping.get("main").keboola_id is None
        assert mapping.get("feature/auth").keboola_id == 972851
        assert mapping.get("bugfix/123").keboola_id == 88888

    def test_branch_mapping_from_dict_empty_string_id(self) -> None:
        """Empty string id (rare legacy shape) is treated as production (None)."""
        data = {"version": 1, "mappings": {"main": {"id": "", "name": "Main"}}}
        mapping = BranchMapping.from_dict(data)
        assert mapping.get("main").keboola_id is None

    def test_branch_mapping_from_dict_invalid_id_descriptive_error(self) -> None:
        """Issue #269 sec-20: hand-edited mapping with non-numeric id raises a
        descriptive ValueError, not a raw ``invalid literal for int()``."""
        data = {
            "version": 1,
            "mappings": {
                "main": {"id": None, "name": "Main"},
                "feature/x": {"id": "not-a-number", "name": "feature/x"},
            },
        }
        with pytest.raises(ValueError, match="Invalid branch ID"):
            BranchMapping.from_dict(data)

    def test_load_branch_mapping_invalid_id_raises_config_error(self, tmp_path: Path) -> None:
        """``load_branch_mapping`` wraps the descriptive error with the file
        path AND raises ConfigError so CLI commands surface a clean exit-5
        envelope instead of a Python traceback (issue #269 sec-20 follow-up).
        """
        keboola_dir = tmp_path / KEBOOLA_DIR_NAME
        keboola_dir.mkdir()
        (keboola_dir / BRANCH_MAPPING_FILENAME).write_text(
            json.dumps(
                {
                    "version": 1,
                    "mappings": {
                        "feature/x": {"id": "abc", "name": "feature/x"},
                    },
                }
            )
        )
        with pytest.raises(ConfigError, match=r"Failed to parse .*branch-mapping\.json"):
            load_branch_mapping(tmp_path)


class TestBranchMappingIO:
    """Tests for load/save filesystem operations."""

    def test_load_save_branch_mapping(self, tmp_path: Path) -> None:
        """Filesystem round-trip: save then load preserves data."""
        mapping = BranchMapping()
        mapping.set("main", None, "Main")
        mapping.set("feature/auth", 972851, "feature/auth")

        save_branch_mapping(tmp_path, mapping)

        # Verify file exists
        path = tmp_path / KEBOOLA_DIR_NAME / BRANCH_MAPPING_FILENAME
        assert path.exists()

        # Verify raw JSON content -- ids written as int, not str (issue #267)
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["version"] == 1
        assert raw["mappings"]["main"]["id"] is None
        assert raw["mappings"]["feature/auth"]["id"] == 972851

        # Load and verify
        loaded = load_branch_mapping(tmp_path)
        assert loaded.version == 1
        assert len(loaded.mappings) == 2

        main_entry = loaded.get("main")
        assert main_entry is not None
        assert main_entry.is_production() is True

        auth_entry = loaded.get("feature/auth")
        assert auth_entry is not None
        assert auth_entry.keboola_id == 972851

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
        mapping2.set("develop", 99999, "develop")
        save_branch_mapping(tmp_path, mapping2)

        loaded = load_branch_mapping(tmp_path)
        assert len(loaded.mappings) == 2

    def test_load_silently_migrates_legacy_string_ids_on_disk(self, tmp_path: Path) -> None:
        """A ``branch-mapping.json`` written by older kbagent versions
        with string-typed ``id`` loads and produces int-typed
        ``keboola_id`` (issue #267)."""
        keboola_dir = tmp_path / KEBOOLA_DIR_NAME
        keboola_dir.mkdir()
        legacy_path = keboola_dir / BRANCH_MAPPING_FILENAME
        legacy_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "mappings": {
                        "main": {"id": None, "name": "Main"},
                        "feature/auth": {"id": "972851", "name": "feature/auth"},
                    },
                }
            )
        )
        loaded = load_branch_mapping(tmp_path)
        feature = loaded.get("feature/auth")
        assert feature is not None
        assert feature.keboola_id == 972851  # not "972851"


class TestSyncWorkspaceHelpers:
    """Tests for find_sync_workspace and cleanup_branch_id_from_mapping (issue #267, Bug D)."""

    def test_find_sync_workspace_locates_nearest_ancestor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``find_sync_workspace`` walks upward and finds the closest enclosing
        ``.keboola/branch-mapping.json``."""
        workspace = tmp_path / "my-workspace"
        nested = workspace / "src" / "feature"
        nested.mkdir(parents=True)
        save_branch_mapping(workspace, BranchMapping())

        monkeypatch.chdir(nested)
        found = find_sync_workspace()
        assert found is not None
        assert found.resolve() == workspace.resolve()

    def test_find_sync_workspace_returns_none_when_outside(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns ``None`` when no ancestor contains a sync workspace."""
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        monkeypatch.chdir(unrelated)
        assert find_sync_workspace() is None

    def test_cleanup_removes_only_matching_branch_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``cleanup_branch_id_from_mapping`` removes every git branch that
        maps to the given branch ID and leaves others intact."""
        mapping = BranchMapping()
        mapping.set("main", None, "Main")
        mapping.set("feature/a", 11111, "branch-a")
        mapping.set("feature/b", 22222, "branch-b")
        save_branch_mapping(tmp_path, mapping)

        monkeypatch.chdir(tmp_path)
        result = cleanup_branch_id_from_mapping(11111)
        assert result is not None
        assert result["git_branches_unlinked"] == ["feature/a"]

        # Reload and confirm the change persisted
        reloaded = load_branch_mapping(tmp_path)
        assert reloaded.get("feature/a") is None
        assert reloaded.get("feature/b") is not None
        assert reloaded.get("main") is not None

    def test_cleanup_returns_none_when_branch_not_referenced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns ``None`` when no entry references the branch (no-op)."""
        mapping = BranchMapping()
        mapping.set("main", None, "Main")
        save_branch_mapping(tmp_path, mapping)
        monkeypatch.chdir(tmp_path)
        assert cleanup_branch_id_from_mapping(99999) is None

    def test_cleanup_returns_none_when_no_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns ``None`` when no enclosing sync workspace is found."""
        monkeypatch.chdir(tmp_path)
        assert cleanup_branch_id_from_mapping(99999) is None
