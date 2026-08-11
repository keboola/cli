"""Tests for sync manifest models and load/save functions."""

import json
from pathlib import Path

import pytest

from keboola_agent_cli.sync.manifest import (
    Manifest,
    ManifestBranch,
    ManifestConfigRow,
    ManifestConfiguration,
    ManifestGitBranching,
    ManifestNaming,
    ManifestProject,
    load_manifest,
    save_manifest,
)


class TestManifestProject:
    """Tests for ManifestProject model."""

    def test_manifest_project_model(self) -> None:
        """ManifestProject stores id and apiHost correctly."""
        project = ManifestProject(id=12345, apiHost="connection.keboola.com")
        assert project.id == 12345
        assert project.api_host == "connection.keboola.com"

    def test_manifest_project_alias(self) -> None:
        """ManifestProject can be created with the camelCase alias."""
        project = ManifestProject(id=99, apiHost="connection.eu-central-1.keboola.com")
        assert project.api_host == "connection.eu-central-1.keboola.com"


class TestManifestGitBranching:
    """Tests for ManifestGitBranching model."""

    def test_manifest_git_branching_defaults(self) -> None:
        """Default values: enabled=False, defaultBranch='main'."""
        branching = ManifestGitBranching()
        assert branching.enabled is False
        assert branching.default_branch == "main"

    def test_manifest_git_branching_custom(self) -> None:
        """Custom values override defaults."""
        branching = ManifestGitBranching(enabled=True, defaultBranch="develop")
        assert branching.enabled is True
        assert branching.default_branch == "develop"


class TestManifestNaming:
    """Tests for ManifestNaming model."""

    def test_manifest_naming_defaults(self) -> None:
        """All naming template defaults match the expected patterns."""
        naming = ManifestNaming()
        assert naming.branch == "{branch_name}"
        assert naming.config == "{component_type}/{component_id}/{config_name}"
        assert naming.config_row == "rows/{config_row_name}"
        assert naming.scheduler_config == "schedules/{config_name}"
        assert naming.shared_code_config == "_shared/{target_component_id}"
        assert naming.shared_code_config_row == "codes/{config_row_name}"
        assert naming.variables_config == "variables"
        assert naming.variables_values_row == "values/{config_row_name}"
        assert naming.data_app_config == "app/{component_id}/{config_name}"


class TestManifestConfiguration:
    """Tests for ManifestConfiguration model."""

    def test_manifest_configuration_aliases(self) -> None:
        """ManifestConfiguration accepts camelCase aliases for branchId and componentId."""
        config = ManifestConfiguration(
            branchId=1,
            componentId="keboola.ex-db-snowflake",
            id="cfg-1",
            path="extractor/keboola.ex-db-snowflake/my-config",
        )
        assert config.branch_id == 1
        assert config.component_id == "keboola.ex-db-snowflake"
        assert config.id == "cfg-1"
        assert config.path == "extractor/keboola.ex-db-snowflake/my-config"
        assert config.metadata == {}
        assert config.rows == []

    def test_manifest_configuration_with_rows(self) -> None:
        """ManifestConfiguration can have row entries."""
        config = ManifestConfiguration(
            branchId=1,
            componentId="keboola.ex-db-snowflake",
            id="cfg-1",
            path="extractor/keboola.ex-db-snowflake/my-config",
            rows=[
                ManifestConfigRow(id="row-1", path="rows/my-row"),
            ],
        )
        assert len(config.rows) == 1
        assert config.rows[0].id == "row-1"
        assert config.rows[0].path == "rows/my-row"
        assert config.rows[0].metadata == {}

    def test_manifest_config_row_with_metadata(self) -> None:
        """ManifestConfigRow stores pull-time hashes in the metadata dict."""
        row = ManifestConfigRow(
            id="row-1",
            path="rows/my-row",
            metadata={"pull_hash": "abc123", "pull_config_hash": "def456"},
        )
        assert row.metadata["pull_hash"] == "abc123"
        assert row.metadata["pull_config_hash"] == "def456"


class TestManifestRoundTrip:
    """Tests for Manifest load/save round-trip."""

    def _make_manifest(self) -> Manifest:
        """Create a full manifest for testing."""
        return Manifest(
            version=2,
            project=ManifestProject(id=42, apiHost="connection.keboola.com"),
            allowTargetEnv=True,
            gitBranching=ManifestGitBranching(enabled=False, defaultBranch="main"),
            sortBy="id",
            naming=ManifestNaming(),
            allowedBranches=["main"],
            ignoredComponents=["keboola.sandboxes"],
            branches=[ManifestBranch(id=1, path="main")],
            configurations=[
                ManifestConfiguration(
                    branchId=1,
                    componentId="keboola.ex-db-snowflake",
                    id="cfg-123",
                    path="extractor/keboola.ex-db-snowflake/my-config",
                    rows=[ManifestConfigRow(id="row-1", path="rows/first-row")],
                )
            ],
        )

    def test_manifest_round_trip(self, tmp_path) -> None:
        """Save manifest, load it back, verify equality."""
        original = self._make_manifest()

        save_manifest(tmp_path, original)
        loaded = load_manifest(tmp_path)

        assert loaded.version == original.version
        assert loaded.project.id == original.project.id
        assert loaded.project.api_host == original.project.api_host
        assert loaded.allow_target_env == original.allow_target_env
        assert loaded.git_branching.enabled == original.git_branching.enabled
        assert loaded.git_branching.default_branch == original.git_branching.default_branch
        assert loaded.sort_by == original.sort_by
        assert loaded.naming.config == original.naming.config
        assert loaded.allowed_branches == original.allowed_branches
        assert loaded.ignored_components == original.ignored_components
        assert len(loaded.branches) == 1
        assert loaded.branches[0].id == 1
        assert len(loaded.configurations) == 1
        assert loaded.configurations[0].component_id == "keboola.ex-db-snowflake"
        assert loaded.configurations[0].rows[0].id == "row-1"

    def test_manifest_camelcase_output(self, tmp_path) -> None:
        """Saved manifest uses camelCase keys in JSON."""
        manifest = self._make_manifest()
        save_manifest(tmp_path, manifest)

        raw = json.loads((tmp_path / ".keboola" / "manifest.json").read_text())

        # Top-level camelCase keys
        assert "allowTargetEnv" in raw
        assert "gitBranching" in raw
        assert "sortBy" in raw
        assert "allowedBranches" in raw
        assert "ignoredComponents" in raw

        # Nested camelCase keys
        assert "apiHost" in raw["project"]
        assert "defaultBranch" in raw["gitBranching"]
        assert "configRow" in raw["naming"]
        assert "schedulerConfig" in raw["naming"]
        assert "sharedCodeConfig" in raw["naming"]
        assert "sharedCodeConfigRow" in raw["naming"]
        assert "variablesConfig" in raw["naming"]
        assert "variablesValuesRow" in raw["naming"]
        assert "dataAppConfig" in raw["naming"]

        # Configuration entries
        assert "branchId" in raw["configurations"][0]
        assert "componentId" in raw["configurations"][0]

    def test_save_creates_directory(self, tmp_path) -> None:
        """save_manifest creates .keboola/ directory if it does not exist."""
        project_root = tmp_path / "fresh-project"
        project_root.mkdir()

        manifest = self._make_manifest()
        save_manifest(project_root, manifest)

        keboola_dir = project_root / ".keboola"
        assert keboola_dir.exists()
        assert keboola_dir.is_dir()
        assert (keboola_dir / "manifest.json").exists()


class TestLoadManifest:
    """Tests for load_manifest error handling."""

    def test_load_manifest_file_not_found(self, tmp_path) -> None:
        """FileNotFoundError raised when manifest.json does not exist."""
        with pytest.raises(FileNotFoundError, match="Manifest not found"):
            load_manifest(tmp_path)

    def test_load_v2_manifest_tolerates_missing_row_metadata(self, tmp_path) -> None:
        """v2 manifests (rows without metadata) load cleanly and default metadata to {}.

        Covers the v2→v3 upgrade path: existing on-disk manifests pre-date the
        row-level metadata field introduced for row-push hashing.
        """
        keboola_dir = tmp_path / ".keboola"
        keboola_dir.mkdir()
        v2_manifest_data = {
            "version": 2,
            "project": {"id": 1, "apiHost": "connection.keboola.com"},
            "allowTargetEnv": True,
            "gitBranching": {"enabled": False, "defaultBranch": "main"},
            "sortBy": "id",
            "naming": {"branch": "{branch_name}"},
            "branches": [],
            "configurations": [
                {
                    "branchId": 1,
                    "componentId": "keboola.variables",
                    "id": "vars-1",
                    "path": "variables",
                    "rows": [{"id": "row-1", "path": "values/main"}],
                }
            ],
        }
        (keboola_dir / "manifest.json").write_text(json.dumps(v2_manifest_data))

        loaded = load_manifest(tmp_path)

        assert loaded.version == 2
        row = loaded.configurations[0].rows[0]
        assert row.id == "row-1"
        assert row.metadata == {}


class TestManifestExtraFields:
    """Tests for extra field preservation."""

    def test_manifest_extra_fields_preserved(self, tmp_path) -> None:
        """Unknown fields in manifest JSON are preserved via extra='allow'."""
        keboola_dir = tmp_path / ".keboola"
        keboola_dir.mkdir()
        manifest_data = {
            "version": 2,
            "project": {"id": 1, "apiHost": "connection.keboola.com", "unknownField": "kept"},
            "allowTargetEnv": True,
            "gitBranching": {"enabled": False, "defaultBranch": "main"},
            "sortBy": "id",
            "naming": {"branch": "{branch_name}"},
            "branches": [],
            "configurations": [],
            "customTopLevel": "preserved",
        }
        (keboola_dir / "manifest.json").write_text(json.dumps(manifest_data))

        loaded = load_manifest(tmp_path)

        # Extra field on root model
        dumped = loaded.model_dump(mode="json", by_alias=True)
        assert dumped["customTopLevel"] == "preserved"

        # Extra field on nested model
        project_dumped = loaded.project.model_dump(mode="json", by_alias=True)
        assert project_dumped["unknownField"] == "kept"


class TestManifestPathsArePortable:
    """The manifest is tracked in git, so its paths must be OS-neutral.

    The asymmetry is what makes this dangerous: `Path()` on Windows accepts
    `a/b`, but on POSIX `a\\b` is a single filename containing a backslash. A
    manifest written on Windows therefore stops resolving for every teammate on
    macOS or Linux, while the reverse direction works and hides the problem.
    """

    def test_windows_separators_are_normalised_on_load(self) -> None:
        """A manifest already committed by a Windows kbagent repairs itself."""
        cfg = ManifestConfiguration(
            branchId=1,
            componentId="keboola.ex-http",
            id="123",
            path=r"extractor\keboola.ex-http\adopted-extractor",
        )
        assert cfg.path == "extractor/keboola.ex-http/adopted-extractor"

    def test_branch_and_row_paths_are_normalised_too(self) -> None:
        assert ManifestBranch(id=1, path=r"main\nested").path == "main/nested"
        assert ManifestConfigRow(id="r1", path=r"rows\first").path == "rows/first"

    def test_posix_paths_are_left_alone(self) -> None:
        """The common case must be untouched, not round-tripped through a rewrite."""
        cfg = ManifestConfiguration(branchId=1, componentId="c", id="1", path="extractor/c/name")
        assert cfg.path == "extractor/c/name"

    def test_a_saved_manifest_holds_forward_slashes(self, tmp_path: Path) -> None:
        """End to end: what lands in git is portable regardless of the writer's OS."""
        manifest = Manifest(
            project=ManifestProject(id=1, apiHost="connection.keboola.com"),
            naming=ManifestNaming(),
            branches=[ManifestBranch(id=1, path="main")],
            configurations=[
                ManifestConfiguration(branchId=1, componentId="c", id="1", path=r"extractor\c\name")
            ],
        )
        save_manifest(tmp_path, manifest)

        raw = (tmp_path / ".keboola" / "manifest.json").read_text(encoding="utf-8")
        assert "extractor/c/name" in raw
        assert "\\\\" not in raw, "a backslash in the tracked manifest breaks POSIX checkouts"
