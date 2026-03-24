"""Tests for sync manifest models and load/save functions."""

import json

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
        project = ManifestProject(id=12345, api_host="connection.keboola.com")
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
        branching = ManifestGitBranching(enabled=True, default_branch="develop")
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


class TestManifestRoundTrip:
    """Tests for Manifest load/save round-trip."""

    def _make_manifest(self) -> Manifest:
        """Create a full manifest for testing."""
        return Manifest(
            version=2,
            project=ManifestProject(id=42, api_host="connection.keboola.com"),
            allow_target_env=True,
            git_branching=ManifestGitBranching(enabled=False, default_branch="main"),
            sort_by="id",
            naming=ManifestNaming(),
            allowed_branches=["main"],
            ignored_components=["keboola.sandboxes"],
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
