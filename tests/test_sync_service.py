"""Tests for SyncService - init, pull, and status business logic.

Tests use tmp_path for filesystem operations and MagicMock for API client.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from helpers import setup_single_project
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import (
    BRANCH_MAPPING_FILENAME,
    CONFIG_FILENAME,
    KEBOOLA_DIR_NAME,
    MANIFEST_VERSION,
)
from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.models import TokenVerifyResponse
from keboola_agent_cli.services.sync_service import SyncService
from keboola_agent_cli.sync.manifest import load_manifest

# ---------------------------------------------------------------------------
# Sample API data
# ---------------------------------------------------------------------------

SAMPLE_VERIFY_TOKEN = TokenVerifyResponse(
    token_id="tok-001",
    token_description="kbagent-cli",
    project_id=258,
    project_name="Production",
    owner_name="My Org",
)

SAMPLE_BRANCHES = [
    {"id": 12345, "name": "Main", "isDefault": True},
]

SAMPLE_BRANCHES_WITH_DEV = [
    {"id": 12345, "name": "Main", "isDefault": True},
    {"id": 99999, "name": "feature-x", "isDefault": False},
]

SAMPLE_COMPONENTS = [
    {
        "id": "keboola.ex-http",
        "type": "extractor",
        "configurations": [
            {
                "id": "cfg-001",
                "name": "My HTTP Extractor",
                "description": "Fetches data",
                "configuration": {
                    "parameters": {"baseUrl": "https://api.example.com"},
                },
                "rows": [
                    {
                        "id": "row-001",
                        "name": "Users Endpoint",
                        "description": "",
                        "configuration": {
                            "parameters": {"path": "/users"},
                        },
                    }
                ],
            }
        ],
    },
    {
        "id": "keboola.snowflake-transformation",
        "type": "transformation",
        "configurations": [
            {
                "id": "cfg-002",
                "name": "Clean Data",
                "description": "Cleans raw data",
                "configuration": {
                    "parameters": {},
                    "storage": {
                        "output": {
                            "tables": [
                                {
                                    "source": "clean",
                                    "destination": "out.c-main.clean",
                                }
                            ],
                        },
                    },
                },
                "rows": [],
            }
        ],
    },
]

SAMPLE_COMPONENTS_NO_ROWS = [
    {
        "id": "keboola.ex-http",
        "type": "extractor",
        "configurations": [
            {
                "id": "cfg-001",
                "name": "My HTTP Extractor",
                "description": "Fetches data",
                "configuration": {
                    "parameters": {"baseUrl": "https://api.example.com"},
                },
                "rows": [],
            }
        ],
    },
]


# ---------------------------------------------------------------------------
# Mock client factory
# ---------------------------------------------------------------------------


def _make_sync_mock_client(
    verify_token_response: TokenVerifyResponse | None = None,
    components_response: list | None = None,
    branches_response: list | None = None,
) -> MagicMock:
    """Create a mock KeboolaClient suitable for SyncService tests."""
    client = MagicMock()
    # Support context manager usage
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)

    if verify_token_response:
        client.verify_token.return_value = verify_token_response

    if components_response is not None:
        client.list_components_with_configs.return_value = components_response

    if branches_response is not None:
        client.list_dev_branches.return_value = branches_response

    return client


# ===================================================================
# init_sync tests
# ===================================================================


class TestInitSync:
    """Tests for SyncService.init_sync()."""

    def test_init_sync_basic(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """init_sync creates manifest.json with correct project ID, api_host, and branches."""
        mock_client = _make_sync_mock_client(
            verify_token_response=SAMPLE_VERIFY_TOKEN,
            branches_response=SAMPLE_BRANCHES,
        )
        store = setup_single_project(tmp_config_dir)
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        project_root = tmp_path / "project"
        project_root.mkdir()

        result = svc.init_sync(alias="prod", project_root=project_root)

        # Verify result dict
        assert result["status"] == "initialized"
        assert result["project_id"] == 258
        assert result["project_alias"] == "prod"
        assert result["api_host"] == "connection.keboola.com"
        assert result["git_branching"] is False
        assert result["default_branch"] == "main"
        assert len(result["files_created"]) == 1

        # Verify manifest.json was created
        manifest_path = project_root / KEBOOLA_DIR_NAME / "manifest.json"
        assert manifest_path.exists()

        manifest = load_manifest(project_root)
        assert manifest.version == MANIFEST_VERSION
        assert manifest.project.id == 258
        assert manifest.project.api_host == "connection.keboola.com"
        assert len(manifest.branches) == 1
        assert manifest.branches[0].id == 12345
        assert manifest.branches[0].path == "main"
        assert manifest.configurations == []
        assert manifest.git_branching.enabled is False

    def test_init_sync_git_branching(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """init_sync with git_branching=True creates branch-mapping.json."""
        mock_client = _make_sync_mock_client(
            verify_token_response=SAMPLE_VERIFY_TOKEN,
            branches_response=SAMPLE_BRANCHES,
        )
        store = setup_single_project(tmp_config_dir)
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        project_root = tmp_path / "project"
        project_root.mkdir()

        with (
            patch(
                "keboola_agent_cli.services.sync_service.is_git_repo",
                return_value=True,
            ),
            patch(
                "keboola_agent_cli.services.sync_service.get_default_branch",
                return_value="main",
            ),
        ):
            result = svc.init_sync(
                alias="prod",
                project_root=project_root,
                git_branching=True,
            )

        assert result["git_branching"] is True
        assert result["default_branch"] == "main"
        assert len(result["files_created"]) == 2

        # Verify branch-mapping.json was created
        mapping_path = project_root / KEBOOLA_DIR_NAME / BRANCH_MAPPING_FILENAME
        assert mapping_path.exists()

        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        assert mapping["version"] == 1
        assert "main" in mapping["mappings"]
        assert mapping["mappings"]["main"]["name"] == "Main"

        # Verify manifest has git branching enabled
        manifest = load_manifest(project_root)
        assert manifest.git_branching.enabled is True
        assert manifest.git_branching.default_branch == "main"

    def test_init_sync_already_exists(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """init_sync raises FileExistsError when manifest already exists."""
        mock_client = _make_sync_mock_client(
            verify_token_response=SAMPLE_VERIFY_TOKEN,
            branches_response=SAMPLE_BRANCHES,
        )
        store = setup_single_project(tmp_config_dir)
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        project_root = tmp_path / "project"
        project_root.mkdir()

        # Create manifest first time
        svc.init_sync(alias="prod", project_root=project_root)

        # Second time should raise
        with pytest.raises(FileExistsError, match="Manifest already exists"):
            svc.init_sync(alias="prod", project_root=project_root)

    def test_init_sync_project_not_found(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """init_sync raises ConfigError when alias is not configured."""
        store = setup_single_project(tmp_config_dir)
        svc = SyncService(config_store=store)

        project_root = tmp_path / "project"
        project_root.mkdir()

        with pytest.raises(ConfigError, match="not found"):
            svc.init_sync(alias="nonexistent", project_root=project_root)

    def test_init_sync_git_branching_no_git_repo(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """init_sync with git_branching raises ConfigError when not a git repo."""
        mock_client = _make_sync_mock_client(
            verify_token_response=SAMPLE_VERIFY_TOKEN,
            branches_response=SAMPLE_BRANCHES,
        )
        store = setup_single_project(tmp_config_dir)
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        project_root = tmp_path / "project"
        project_root.mkdir()

        with (
            patch(
                "keboola_agent_cli.services.sync_service.is_git_repo",
                return_value=False,
            ),
            pytest.raises(ConfigError, match="Git repository not found"),
        ):
            svc.init_sync(
                alias="prod",
                project_root=project_root,
                git_branching=True,
            )

    def test_init_sync_strips_https_prefix(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """init_sync strips https:// prefix from stack_url for api_host."""
        mock_client = _make_sync_mock_client(
            verify_token_response=SAMPLE_VERIFY_TOKEN,
            branches_response=SAMPLE_BRANCHES,
        )
        store = setup_single_project(tmp_config_dir)
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        project_root = tmp_path / "project"
        project_root.mkdir()

        result = svc.init_sync(alias="prod", project_root=project_root)

        # https://connection.keboola.com -> connection.keboola.com
        assert result["api_host"] == "connection.keboola.com"
        assert not result["api_host"].startswith("https://")


# ===================================================================
# pull tests
# ===================================================================


class TestPull:
    """Tests for SyncService.pull()."""

    def _init_project(
        self,
        tmp_config_dir: Path,
        project_root: Path,
        branches_response: list | None = None,
    ) -> ConfigStore:
        """Helper: init a project and return the ConfigStore for reuse."""
        init_client = _make_sync_mock_client(
            verify_token_response=SAMPLE_VERIFY_TOKEN,
            branches_response=branches_response or SAMPLE_BRANCHES,
        )
        store = setup_single_project(tmp_config_dir)
        init_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: init_client,
        )
        init_svc.init_sync(alias="prod", project_root=project_root)
        return store

    def test_pull_basic(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """pull writes _config.yml files for each configuration."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_project(tmp_config_dir, project_root)

        # Create a new service with the pull client
        pull_client = _make_sync_mock_client(
            components_response=SAMPLE_COMPONENTS_NO_ROWS,
        )
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )

        result = svc.pull(alias="prod", project_root=project_root)

        assert result["status"] == "pulled"
        assert result["project_alias"] == "prod"
        assert result["configs_pulled"] == 1
        assert result["rows_pulled"] == 0
        assert result["files_written"] == 1
        assert result["branch_dir"] == "main"

        # Verify _config.yml was written
        config_files = list(project_root.rglob(CONFIG_FILENAME))
        assert len(config_files) == 1

        config_data = yaml.safe_load(config_files[0].read_text(encoding="utf-8"))
        assert config_data["name"] == "My HTTP Extractor"
        assert config_data["_keboola"]["component_id"] == "keboola.ex-http"
        assert config_data["_keboola"]["config_id"] == "cfg-001"
        assert config_data["parameters"]["baseUrl"] == "https://api.example.com"

    def test_pull_with_rows(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """pull writes config rows under rows/ subdirectory."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_project(tmp_config_dir, project_root)

        pull_client = _make_sync_mock_client(
            components_response=SAMPLE_COMPONENTS,
        )
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )

        result = svc.pull(alias="prod", project_root=project_root)

        assert result["configs_pulled"] == 2
        assert result["rows_pulled"] == 1
        assert result["files_written"] == 3  # 2 configs + 1 row

        # Verify config files exist
        config_files = list(project_root.rglob(CONFIG_FILENAME))
        assert len(config_files) == 3  # 2 configs + 1 row _config.yml

        # Find the row config file (under rows/ subdirectory relative to project_root)
        row_config_files = [f for f in config_files if "/rows/" in str(f.relative_to(project_root))]
        assert len(row_config_files) == 1

        row_data = yaml.safe_load(row_config_files[0].read_text(encoding="utf-8"))
        assert row_data["name"] == "Users Endpoint"
        assert row_data["_keboola"]["component_id"] == "keboola.ex-http"
        assert row_data["_keboola"]["row_id"] == "row-001"
        assert row_data["parameters"]["path"] == "/users"

    def test_pull_updates_manifest(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """pull updates manifest.configurations after downloading."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_project(tmp_config_dir, project_root)

        # Verify manifest starts with no configurations
        manifest_before = load_manifest(project_root)
        assert manifest_before.configurations == []

        pull_client = _make_sync_mock_client(
            components_response=SAMPLE_COMPONENTS,
        )
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )

        svc.pull(alias="prod", project_root=project_root)

        # Verify manifest now has configurations
        manifest_after = load_manifest(project_root)
        assert len(manifest_after.configurations) == 2

        # Verify first config entry
        cfg1 = manifest_after.configurations[0]
        assert cfg1.component_id == "keboola.ex-http"
        assert cfg1.id == "cfg-001"
        assert cfg1.branch_id == 12345
        assert len(cfg1.rows) == 1
        assert cfg1.rows[0].id == "row-001"

        # Verify second config entry (no rows)
        cfg2 = manifest_after.configurations[1]
        assert cfg2.component_id == "keboola.snowflake-transformation"
        assert cfg2.id == "cfg-002"
        assert cfg2.rows == []

    def test_pull_no_manifest(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """pull raises FileNotFoundError when manifest doesn't exist."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = setup_single_project(tmp_config_dir)
        svc = SyncService(config_store=store)

        with pytest.raises(FileNotFoundError, match="Manifest not found"):
            svc.pull(alias="prod", project_root=project_root)

    def test_pull_empty_components(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """pull with no components writes zero files and updates manifest."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_project(tmp_config_dir, project_root)

        pull_client = _make_sync_mock_client(components_response=[])
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )

        result = svc.pull(alias="prod", project_root=project_root)

        assert result["configs_pulled"] == 0
        assert result["rows_pulled"] == 0
        assert result["files_written"] == 0

        manifest = load_manifest(project_root)
        assert manifest.configurations == []


# ===================================================================
# status tests
# ===================================================================


class TestStatus:
    """Tests for SyncService.status()."""

    def _init_and_pull(
        self,
        tmp_config_dir: Path,
        project_root: Path,
        components: list | None = None,
    ) -> SyncService:
        """Helper: init + pull to get a working directory with configs."""
        init_client = _make_sync_mock_client(
            verify_token_response=SAMPLE_VERIFY_TOKEN,
            branches_response=SAMPLE_BRANCHES,
        )
        store = setup_single_project(tmp_config_dir)
        init_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: init_client,
        )
        init_svc.init_sync(alias="prod", project_root=project_root)

        pull_client = _make_sync_mock_client(
            components_response=components if components is not None else SAMPLE_COMPONENTS,
        )
        pull_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )
        pull_svc.pull(alias="prod", project_root=project_root)
        return pull_svc

    def test_status_no_changes(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """status after pull shows all configs unchanged."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        svc = self._init_and_pull(tmp_config_dir, project_root)

        result = svc.status(project_root=project_root)

        assert result["modified"] == []
        assert result["added"] == []
        assert result["deleted"] == []
        assert result["unchanged"] == 2  # 2 configs from SAMPLE_COMPONENTS
        assert result["total_tracked"] == 2

    def test_status_deleted_config(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """status shows deleted when a _config.yml is removed."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        svc = self._init_and_pull(tmp_config_dir, project_root)

        # Delete one config file
        config_files = list(project_root.rglob(CONFIG_FILENAME))
        # Find a config file that is NOT under rows/
        top_level_configs = [f for f in config_files if "rows" not in str(f)]
        assert len(top_level_configs) >= 1

        # Delete the first top-level config
        deleted_file = top_level_configs[0]
        deleted_file.unlink()

        result = svc.status(project_root=project_root)

        assert len(result["deleted"]) == 1
        assert result["deleted"][0]["config_id"] in ("cfg-001", "cfg-002")
        # The other config should still be unchanged
        assert result["unchanged"] == 1

    def test_status_no_manifest(self, tmp_path: Path) -> None:
        """status raises FileNotFoundError when manifest doesn't exist."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        # Use a minimal service (no config store needed for status)
        store = MagicMock()
        svc = SyncService(config_store=store)

        with pytest.raises(FileNotFoundError, match="Manifest not found"):
            svc.status(project_root=project_root)

    def test_status_modified_config(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """status shows modified when _keboola.config_id is changed in a file."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        svc = self._init_and_pull(tmp_config_dir, project_root)

        # Modify a config file by changing the _keboola metadata
        config_files = list(project_root.rglob(CONFIG_FILENAME))
        top_level_configs = [f for f in config_files if "rows" not in str(f)]
        assert len(top_level_configs) >= 1

        modified_file = top_level_configs[0]
        config_data = yaml.safe_load(modified_file.read_text(encoding="utf-8"))
        config_data["_keboola"]["config_id"] = "changed-id"
        modified_file.write_text(
            yaml.dump(config_data, default_flow_style=False),
            encoding="utf-8",
        )

        result = svc.status(project_root=project_root)

        assert len(result["modified"]) == 1
        assert result["unchanged"] == 1

    def test_status_empty_project(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """status with no configurations shows all zeros."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        svc = self._init_and_pull(tmp_config_dir, project_root, components=[])

        result = svc.status(project_root=project_root)

        assert result["modified"] == []
        assert result["added"] == []
        assert result["deleted"] == []
        assert result["unchanged"] == 0
        assert result["total_tracked"] == 0


# ===================================================================
# diff tests
# ===================================================================


class TestDiff:
    """Tests for SyncService.diff()."""

    def _init_and_pull(
        self,
        tmp_config_dir: Path,
        project_root: Path,
        components: list | None = None,
    ) -> tuple[ConfigStore, SyncService]:
        """Helper: init + pull to get a working directory, return (store, svc)."""
        init_client = _make_sync_mock_client(
            verify_token_response=SAMPLE_VERIFY_TOKEN,
            branches_response=SAMPLE_BRANCHES,
        )
        store = setup_single_project(tmp_config_dir)
        init_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: init_client,
        )
        init_svc.init_sync(alias="prod", project_root=project_root)

        pull_client = _make_sync_mock_client(
            components_response=components if components is not None else SAMPLE_COMPONENTS_NO_ROWS,
        )
        pull_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )
        pull_svc.pull(alias="prod", project_root=project_root)
        return store, pull_svc

    def test_diff_no_changes(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """Pull then diff shows no changes when local matches remote."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store, _ = self._init_and_pull(tmp_config_dir, project_root)

        # Create diff service with same components (no changes)
        diff_client = _make_sync_mock_client(
            components_response=SAMPLE_COMPONENTS_NO_ROWS,
        )
        diff_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: diff_client,
        )

        result = diff_svc.diff(alias="prod", project_root=project_root)

        assert result["changes"] == []
        assert result["summary"]["added"] == 0
        assert result["summary"]["modified"] == 0
        assert result["summary"]["deleted"] == 0

    def test_diff_modified_config(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """Modify a local file, diff detects the change."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store, _ = self._init_and_pull(tmp_config_dir, project_root)

        # Modify a local _config.yml file
        config_files = list(project_root.rglob(CONFIG_FILENAME))
        assert len(config_files) >= 1

        modified_file = config_files[0]
        config_data = yaml.safe_load(modified_file.read_text(encoding="utf-8"))
        config_data["parameters"]["baseUrl"] = "https://changed.example.com"
        modified_file.write_text(
            yaml.dump(config_data, default_flow_style=False),
            encoding="utf-8",
        )

        # Create diff service with original components (remote unchanged)
        diff_client = _make_sync_mock_client(
            components_response=SAMPLE_COMPONENTS_NO_ROWS,
        )
        diff_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: diff_client,
        )

        result = diff_svc.diff(alias="prod", project_root=project_root)

        assert result["summary"]["modified"] == 1
        assert len(result["changes"]) == 1
        assert result["changes"][0]["change_type"] == "modified"


# ===================================================================
# push tests
# ===================================================================


class TestPush:
    """Tests for SyncService.push()."""

    def _init_and_pull(
        self,
        tmp_config_dir: Path,
        project_root: Path,
        components: list | None = None,
    ) -> tuple[ConfigStore, SyncService]:
        """Helper: init + pull to get a working directory, return (store, svc)."""
        init_client = _make_sync_mock_client(
            verify_token_response=SAMPLE_VERIFY_TOKEN,
            branches_response=SAMPLE_BRANCHES,
        )
        store = setup_single_project(tmp_config_dir)
        init_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: init_client,
        )
        init_svc.init_sync(alias="prod", project_root=project_root)

        pull_client = _make_sync_mock_client(
            components_response=components if components is not None else SAMPLE_COMPONENTS_NO_ROWS,
        )
        pull_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )
        pull_svc.pull(alias="prod", project_root=project_root)
        return store, pull_svc

    def test_push_no_changes(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """Push when no changes returns status 'no_changes'."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store, _ = self._init_and_pull(tmp_config_dir, project_root)

        # Create push service with same components (no changes)
        push_client = _make_sync_mock_client(
            components_response=SAMPLE_COMPONENTS_NO_ROWS,
        )
        push_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: push_client,
        )

        result = push_svc.push(alias="prod", project_root=project_root)

        assert result["status"] == "no_changes"
        assert result["created"] == 0
        assert result["updated"] == 0
        assert result["deleted"] == 0

    def test_push_dry_run(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """Push with dry_run returns changes without executing them."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store, _ = self._init_and_pull(tmp_config_dir, project_root)

        # Modify a local file to create a change
        config_files = list(project_root.rglob(CONFIG_FILENAME))
        assert len(config_files) >= 1
        modified_file = config_files[0]
        config_data = yaml.safe_load(modified_file.read_text(encoding="utf-8"))
        config_data["parameters"]["baseUrl"] = "https://changed.example.com"
        modified_file.write_text(
            yaml.dump(config_data, default_flow_style=False),
            encoding="utf-8",
        )

        # Dry run should detect changes but not call API
        dry_client = _make_sync_mock_client(
            components_response=SAMPLE_COMPONENTS_NO_ROWS,
        )
        dry_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: dry_client,
        )

        result = dry_svc.push(alias="prod", project_root=project_root, dry_run=True)

        assert result["status"] == "dry_run"
        assert "changes" in result
        assert "summary" in result
        assert result["summary"]["modified"] >= 1
        # Client should NOT have been called for create/update/delete
        dry_client.update_config.assert_not_called()
        dry_client.create_config.assert_not_called()

    def test_push_update(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """Modify local config, push updates via client.update_config mock."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store, _ = self._init_and_pull(tmp_config_dir, project_root)

        # Modify a local file to create a change
        config_files = list(project_root.rglob(CONFIG_FILENAME))
        assert len(config_files) >= 1
        modified_file = config_files[0]
        config_data = yaml.safe_load(modified_file.read_text(encoding="utf-8"))
        config_data["parameters"]["baseUrl"] = "https://updated.example.com"
        modified_file.write_text(
            yaml.dump(config_data, default_flow_style=False),
            encoding="utf-8",
        )

        # The push service needs a client that:
        # 1. Returns original components for diff detection
        # 2. Accepts update_config calls
        # 3. Returns original components again for the post-push pull
        push_client = _make_sync_mock_client(
            components_response=SAMPLE_COMPONENTS_NO_ROWS,
        )
        push_client.update_config.return_value = {"id": "cfg-001"}

        push_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: push_client,
        )

        result = push_svc.push(alias="prod", project_root=project_root)

        assert result["status"] == "pushed"
        assert result["updated"] >= 1
        assert result["errors"] == []
        # Verify the client.update_config was actually called
        push_client.update_config.assert_called()


# ===================================================================
# branch_link tests
# ===================================================================


class TestBranchLink:
    """Tests for SyncService.branch_link()."""

    def _init_git_branching_project(
        self,
        tmp_config_dir: Path,
        project_root: Path,
    ) -> ConfigStore:
        """Helper: init a project with git branching enabled."""
        init_client = _make_sync_mock_client(
            verify_token_response=SAMPLE_VERIFY_TOKEN,
            branches_response=SAMPLE_BRANCHES,
        )
        store = setup_single_project(tmp_config_dir)
        init_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: init_client,
        )
        with (
            patch(
                "keboola_agent_cli.services.sync_service.is_git_repo",
                return_value=True,
            ),
            patch(
                "keboola_agent_cli.services.sync_service.get_default_branch",
                return_value="main",
            ),
        ):
            init_svc.init_sync(
                alias="prod",
                project_root=project_root,
                git_branching=True,
            )
        return store

    def test_branch_link_creates_branch(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """branch_link creates a Keboola branch when none exists with the git branch name."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_git_branching_project(tmp_config_dir, project_root)

        # Mock client that has no existing branch matching "feature/auth",
        # so it creates one
        link_client = _make_sync_mock_client(
            branches_response=[
                {"id": 12345, "name": "Main", "isDefault": True},
            ],
        )
        link_client.create_dev_branch.return_value = {"id": 99999}

        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: link_client,
        )

        with patch(
            "keboola_agent_cli.sync.git_utils.get_current_branch",
            return_value="feature/auth",
        ):
            result = svc.branch_link(
                alias="prod",
                project_root=project_root,
            )

        assert result["status"] == "linked"
        assert result["git_branch"] == "feature/auth"
        assert result["keboola_branch_id"] == "99999"
        assert result["keboola_branch_name"] == "feature/auth"
        link_client.create_dev_branch.assert_called_once_with(name="feature/auth")

        # Verify the mapping was saved to disk
        from keboola_agent_cli.sync.branch_mapping import load_branch_mapping

        mapping = load_branch_mapping(project_root)
        entry = mapping.get("feature/auth")
        assert entry is not None
        assert entry.keboola_id == "99999"

    def test_branch_link_finds_existing_branch(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """branch_link links to an existing Keboola branch that matches the name."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_git_branching_project(tmp_config_dir, project_root)

        link_client = _make_sync_mock_client(
            branches_response=SAMPLE_BRANCHES_WITH_DEV,
        )

        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: link_client,
        )

        with patch(
            "keboola_agent_cli.sync.git_utils.get_current_branch",
            return_value="feature-x",
        ):
            result = svc.branch_link(
                alias="prod",
                project_root=project_root,
            )

        assert result["status"] == "linked"
        assert result["git_branch"] == "feature-x"
        assert result["keboola_branch_id"] == "99999"
        # Should not have created a new branch
        link_client.create_dev_branch.assert_not_called()

    def test_branch_link_default_branch_error(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """branch_link raises ConfigError when on the default (main) branch."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_git_branching_project(tmp_config_dir, project_root)

        svc = SyncService(config_store=store)

        with (
            patch(
                "keboola_agent_cli.sync.git_utils.get_current_branch",
                return_value="main",
            ),
            pytest.raises(ConfigError, match="Cannot link the default branch"),
        ):
            svc.branch_link(
                alias="prod",
                project_root=project_root,
            )

    def test_branch_link_git_branching_not_enabled(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """branch_link raises ConfigError when git branching is not enabled."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        # Init without git branching
        init_client = _make_sync_mock_client(
            verify_token_response=SAMPLE_VERIFY_TOKEN,
            branches_response=SAMPLE_BRANCHES,
        )
        store = setup_single_project(tmp_config_dir)
        init_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: init_client,
        )
        init_svc.init_sync(alias="prod", project_root=project_root)

        svc = SyncService(config_store=store)

        with pytest.raises(ConfigError, match="Git-branching mode is not enabled"):
            svc.branch_link(
                alias="prod",
                project_root=project_root,
            )

    def test_branch_link_already_linked(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """branch_link returns already_linked when mapping already exists."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_git_branching_project(tmp_config_dir, project_root)

        link_client = _make_sync_mock_client(
            branches_response=SAMPLE_BRANCHES_WITH_DEV,
        )

        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: link_client,
        )

        # First link
        with patch(
            "keboola_agent_cli.sync.git_utils.get_current_branch",
            return_value="feature-x",
        ):
            svc.branch_link(alias="prod", project_root=project_root)

            # Second link should return already_linked
            result = svc.branch_link(alias="prod", project_root=project_root)

        assert result["status"] == "already_linked"
        assert result["git_branch"] == "feature-x"
        assert result["keboola_branch_id"] == "99999"

    def test_branch_link_with_branch_id(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """branch_link with --branch-id links to a specific existing branch."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_git_branching_project(tmp_config_dir, project_root)

        link_client = _make_sync_mock_client(
            branches_response=SAMPLE_BRANCHES_WITH_DEV,
        )

        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: link_client,
        )

        with patch(
            "keboola_agent_cli.sync.git_utils.get_current_branch",
            return_value="my-feature",
        ):
            result = svc.branch_link(
                alias="prod",
                project_root=project_root,
                branch_id=99999,
            )

        assert result["status"] == "linked"
        assert result["keboola_branch_id"] == "99999"
        assert result["keboola_branch_name"] == "feature-x"

    def test_branch_link_with_branch_name_creates(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """branch_link with --branch-name creates a branch with that name."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_git_branching_project(tmp_config_dir, project_root)

        link_client = _make_sync_mock_client(
            branches_response=SAMPLE_BRANCHES,  # no "custom-name" branch
        )
        link_client.create_dev_branch.return_value = {"id": 77777}

        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: link_client,
        )

        with patch(
            "keboola_agent_cli.sync.git_utils.get_current_branch",
            return_value="my-feature",
        ):
            result = svc.branch_link(
                alias="prod",
                project_root=project_root,
                branch_name="custom-name",
            )

        assert result["status"] == "linked"
        assert result["keboola_branch_id"] == "77777"
        assert result["keboola_branch_name"] == "custom-name"
        link_client.create_dev_branch.assert_called_once_with(name="custom-name")


# ===================================================================
# branch_unlink tests
# ===================================================================


class TestBranchUnlink:
    """Tests for SyncService.branch_unlink()."""

    def _init_and_link(
        self,
        tmp_config_dir: Path,
        project_root: Path,
    ) -> ConfigStore:
        """Helper: init with git branching, then link feature-x."""
        init_client = _make_sync_mock_client(
            verify_token_response=SAMPLE_VERIFY_TOKEN,
            branches_response=SAMPLE_BRANCHES,
        )
        store = setup_single_project(tmp_config_dir)
        init_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: init_client,
        )
        with (
            patch(
                "keboola_agent_cli.services.sync_service.is_git_repo",
                return_value=True,
            ),
            patch(
                "keboola_agent_cli.services.sync_service.get_default_branch",
                return_value="main",
            ),
        ):
            init_svc.init_sync(
                alias="prod",
                project_root=project_root,
                git_branching=True,
            )

        # Link feature-x
        link_client = _make_sync_mock_client(
            branches_response=SAMPLE_BRANCHES_WITH_DEV,
        )
        link_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: link_client,
        )
        with patch(
            "keboola_agent_cli.sync.git_utils.get_current_branch",
            return_value="feature-x",
        ):
            link_svc.branch_link(alias="prod", project_root=project_root)

        return store

    def test_branch_unlink_success(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """branch_unlink removes the mapping for the current git branch."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_and_link(tmp_config_dir, project_root)

        svc = SyncService(config_store=store)

        with patch(
            "keboola_agent_cli.sync.git_utils.get_current_branch",
            return_value="feature-x",
        ):
            result = svc.branch_unlink(project_root=project_root)

        assert result["status"] == "unlinked"
        assert result["git_branch"] == "feature-x"
        assert result["keboola_branch_id"] == "99999"
        assert result["keboola_branch_name"] == "feature-x"

        # Verify mapping was removed from disk
        from keboola_agent_cli.sync.branch_mapping import load_branch_mapping

        mapping = load_branch_mapping(project_root)
        assert mapping.get("feature-x") is None

    def test_branch_unlink_not_linked(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """branch_unlink returns not_linked when branch has no mapping."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_and_link(tmp_config_dir, project_root)

        svc = SyncService(config_store=store)

        with patch(
            "keboola_agent_cli.sync.git_utils.get_current_branch",
            return_value="other-branch",
        ):
            result = svc.branch_unlink(project_root=project_root)

        assert result["status"] == "not_linked"
        assert result["git_branch"] == "other-branch"

    def test_branch_unlink_default_branch_error(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """branch_unlink raises ConfigError when on default branch."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_and_link(tmp_config_dir, project_root)

        svc = SyncService(config_store=store)

        with (
            patch(
                "keboola_agent_cli.sync.git_utils.get_current_branch",
                return_value="main",
            ),
            pytest.raises(ConfigError, match="Cannot unlink the default branch"),
        ):
            svc.branch_unlink(project_root=project_root)


# ===================================================================
# branch_status tests
# ===================================================================


class TestBranchStatus:
    """Tests for SyncService.branch_status()."""

    def _init_git_branching_project(
        self,
        tmp_config_dir: Path,
        project_root: Path,
    ) -> ConfigStore:
        """Helper: init a project with git branching enabled."""
        init_client = _make_sync_mock_client(
            verify_token_response=SAMPLE_VERIFY_TOKEN,
            branches_response=SAMPLE_BRANCHES,
        )
        store = setup_single_project(tmp_config_dir)
        init_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: init_client,
        )
        with (
            patch(
                "keboola_agent_cli.services.sync_service.is_git_repo",
                return_value=True,
            ),
            patch(
                "keboola_agent_cli.services.sync_service.get_default_branch",
                return_value="main",
            ),
        ):
            init_svc.init_sync(
                alias="prod",
                project_root=project_root,
                git_branching=True,
            )
        return store

    def test_branch_status_linked(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """branch_status shows linked status when mapping exists."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_git_branching_project(tmp_config_dir, project_root)

        # Link feature-x first
        link_client = _make_sync_mock_client(
            branches_response=SAMPLE_BRANCHES_WITH_DEV,
        )
        link_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: link_client,
        )
        with patch(
            "keboola_agent_cli.sync.git_utils.get_current_branch",
            return_value="feature-x",
        ):
            link_svc.branch_link(alias="prod", project_root=project_root)

        # Now check status
        svc = SyncService(config_store=store)
        with patch(
            "keboola_agent_cli.sync.git_utils.get_current_branch",
            return_value="feature-x",
        ):
            result = svc.branch_status(project_root=project_root)

        assert result["git_branching"] is True
        assert result["git_branch"] == "feature-x"
        assert result["linked"] is True
        assert result["keboola_branch_id"] == "99999"
        assert result["keboola_branch_name"] == "feature-x"
        assert result["is_production"] is False

    def test_branch_status_not_linked(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """branch_status shows not linked when no mapping exists."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_git_branching_project(tmp_config_dir, project_root)

        svc = SyncService(config_store=store)
        with patch(
            "keboola_agent_cli.sync.git_utils.get_current_branch",
            return_value="unlinked-branch",
        ):
            result = svc.branch_status(project_root=project_root)

        assert result["git_branching"] is True
        assert result["git_branch"] == "unlinked-branch"
        assert result["linked"] is False

    def test_branch_status_production(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """branch_status shows is_production=True for the main branch mapping."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_git_branching_project(tmp_config_dir, project_root)

        svc = SyncService(config_store=store)
        with patch(
            "keboola_agent_cli.sync.git_utils.get_current_branch",
            return_value="main",
        ):
            result = svc.branch_status(project_root=project_root)

        assert result["git_branching"] is True
        assert result["git_branch"] == "main"
        assert result["linked"] is True
        assert result["is_production"] is True
        assert result["keboola_branch_id"] is None

    def test_branch_status_git_branching_disabled(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """branch_status returns git_branching=False when not enabled."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        # Init without git branching
        init_client = _make_sync_mock_client(
            verify_token_response=SAMPLE_VERIFY_TOKEN,
            branches_response=SAMPLE_BRANCHES,
        )
        store = setup_single_project(tmp_config_dir)
        init_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: init_client,
        )
        init_svc.init_sync(alias="prod", project_root=project_root)

        svc = SyncService(config_store=store)
        result = svc.branch_status(project_root=project_root)

        assert result == {"git_branching": False}

    def test_branch_status_no_mapping_file(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """branch_status returns linked=False when mapping file is missing."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_git_branching_project(tmp_config_dir, project_root)

        # Delete the branch-mapping.json
        mapping_path = project_root / KEBOOLA_DIR_NAME / BRANCH_MAPPING_FILENAME
        mapping_path.unlink()

        svc = SyncService(config_store=store)
        with patch(
            "keboola_agent_cli.sync.git_utils.get_current_branch",
            return_value="feature-x",
        ):
            result = svc.branch_status(project_root=project_root)

        assert result["git_branching"] is True
        assert result["git_branch"] == "feature-x"
        assert result["linked"] is False
