"""Tests for SyncService - init, pull, and status business logic.

Tests use tmp_path for filesystem operations and MagicMock for API client.
"""

import json
from pathlib import Path
from typing import Any
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
from keboola_agent_cli.sync.manifest import Manifest, load_manifest

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

        # Find the row config file (under rows/ subdirectory relative to project_root).
        # Use Path.parts so the test is OS-agnostic (Windows uses '\' separators).
        row_config_files = [f for f in config_files if "rows" in f.relative_to(project_root).parts]
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

    def test_pull_removes_orphaned_directories(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """pull deletes directories for configs removed from remote (issue #90)."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_project(tmp_config_dir, project_root)

        # First pull: download 2 configs (extractor + transformation)
        pull_client = _make_sync_mock_client(
            components_response=SAMPLE_COMPONENTS,
        )
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )
        result = svc.pull(alias="prod", project_root=project_root)
        assert result["configs_pulled"] == 2

        # Verify both config dirs exist on disk
        config_dirs = list(project_root.rglob(CONFIG_FILENAME))
        snowflake_dirs = [d for d in config_dirs if "keboola.snowflake-transformation" in str(d)]
        assert len(snowflake_dirs) == 1
        orphan_dir = snowflake_dirs[0].parent
        assert orphan_dir.exists()

        # Second pull: only the extractor remains (transformation deleted remotely)
        pull_client2 = _make_sync_mock_client(
            components_response=SAMPLE_COMPONENTS_NO_ROWS,
        )
        svc2 = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client2,
        )
        result2 = svc2.pull(alias="prod", project_root=project_root, force=True)

        # Verify transformation was detected as removed
        removed = [d for d in result2["details"] if d["action"] == "removed"]
        assert len(removed) == 1
        assert removed[0]["component_id"] == "keboola.snowflake-transformation"

        # Verify the orphan directory no longer exists on disk
        assert not orphan_dir.exists(), "Orphaned config directory should be deleted"

        # Verify the manifest no longer has the removed config
        manifest = load_manifest(project_root)
        assert len(manifest.configurations) == 1
        assert manifest.configurations[0].component_id == "keboola.ex-http"

    def test_pull_removes_empty_parent_directories(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """pull cleans up empty component-type dirs after removing last config."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_project(tmp_config_dir, project_root)

        # Pull only the snowflake transformation
        snowflake_only = [
            c for c in SAMPLE_COMPONENTS if c["id"] == "keboola.snowflake-transformation"
        ]
        pull_client = _make_sync_mock_client(components_response=snowflake_only)
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )
        svc.pull(alias="prod", project_root=project_root)

        # Verify component dir exists
        component_dir = (
            project_root / "main" / "transformation" / "keboola.snowflake-transformation"
        )
        assert component_dir.exists()

        # Second pull: no components at all (everything deleted)
        pull_client2 = _make_sync_mock_client(components_response=[])
        svc2 = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client2,
        )
        svc2.pull(alias="prod", project_root=project_root, force=True)

        # The component dir AND the type dir should be cleaned up
        assert not component_dir.exists()
        # Parent type dir should also be removed if empty
        type_dir = component_dir.parent
        assert not type_dir.exists(), "Empty component-type directory should be cleaned up"

    def test_pull_dry_run_preserves_directories(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """pull --dry-run reports removed configs but does NOT delete directories."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_project(tmp_config_dir, project_root)

        # First pull: 2 configs
        pull_client = _make_sync_mock_client(components_response=SAMPLE_COMPONENTS)
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )
        svc.pull(alias="prod", project_root=project_root)

        # Capture the snowflake dir path
        snowflake_configs = list(project_root.rglob("keboola.snowflake-transformation"))
        assert len(snowflake_configs) >= 1
        snowflake_dir = snowflake_configs[0]

        # Dry-run pull with only extractor (transformation gone)
        pull_client2 = _make_sync_mock_client(components_response=SAMPLE_COMPONENTS_NO_ROWS)
        svc2 = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client2,
        )
        result = svc2.pull(alias="prod", project_root=project_root, dry_run=True)

        assert result["status"] == "dry_run"
        # Directory must still exist after dry-run
        assert snowflake_dir.exists(), "Dry-run should NOT delete directories"

    def test_pull_auto_renames_config_on_remote_name_change(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """pull auto-renames local directory when config name changed on remote."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = self._init_project(tmp_config_dir, project_root)

        # First pull: download config with original name "My HTTP Extractor"
        pull_client = _make_sync_mock_client(
            components_response=SAMPLE_COMPONENTS_NO_ROWS,
        )
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )
        result1 = svc.pull(alias="prod", project_root=project_root)
        assert result1["configs_pulled"] == 1

        # Verify original directory exists at expected path
        old_dir = project_root / "main" / "extractor" / "keboola.ex-http" / "my-http-extractor"
        assert old_dir.exists(), "Original config directory should exist after first pull"
        assert (old_dir / CONFIG_FILENAME).exists()

        # Verify manifest tracks the original path
        manifest_before = load_manifest(project_root)
        assert len(manifest_before.configurations) == 1
        assert (
            manifest_before.configurations[0].path == "extractor/keboola.ex-http/my-http-extractor"
        )

        # Second pull: same config ID but with renamed name
        renamed_components = [
            {
                "id": "keboola.ex-http",
                "type": "extractor",
                "configurations": [
                    {
                        "id": "cfg-001",
                        "name": "Renamed HTTP Extractor",
                        "description": "Fetches data",
                        "configuration": {
                            "parameters": {"baseUrl": "https://api.example.com"},
                        },
                        "rows": [],
                    }
                ],
            },
        ]
        pull_client2 = _make_sync_mock_client(components_response=renamed_components)
        svc2 = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client2,
        )
        result2 = svc2.pull(alias="prod", project_root=project_root)

        # Verify the old directory no longer exists
        assert not old_dir.exists(), "Old config directory should be gone after rename"

        # Verify the new directory exists
        new_dir = project_root / "main" / "extractor" / "keboola.ex-http" / "renamed-http-extractor"
        assert new_dir.exists(), "Renamed config directory should exist"
        assert (new_dir / CONFIG_FILENAME).exists()

        # Verify manifest path was updated
        manifest_after = load_manifest(project_root)
        assert len(manifest_after.configurations) == 1
        assert (
            manifest_after.configurations[0].path
            == "extractor/keboola.ex-http/renamed-http-extractor"
        )

        # Verify pull_details contains a "renamed" action
        renamed_details = [d for d in result2["details"] if d["action"] == "renamed"]
        assert len(renamed_details) == 1
        assert renamed_details[0]["component_id"] == "keboola.ex-http"
        assert renamed_details[0]["config_name"] == "Renamed HTTP Extractor"
        assert renamed_details[0]["old_path"] == "extractor/keboola.ex-http/my-http-extractor"
        assert renamed_details[0]["path"] == "extractor/keboola.ex-http/renamed-http-extractor"

    def test_pull_dev_branch_writes_rows(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """Regression: first pull of a new dev branch must write config rows (issue #193).

        A fresh branch is a clone of main -- API hashes are identical. The idempotent
        skip guard must NOT fire when the row file doesn't exist on disk yet.
        """
        project_root = tmp_path / "project"
        project_root.mkdir()

        # Init with both main + dev branch registered
        store = self._init_project(
            tmp_config_dir, project_root, branches_response=SAMPLE_BRANCHES_WITH_DEV
        )

        # Pull main (branch_id 12345) first so manifest carries rows from main
        main_client = _make_sync_mock_client(
            components_response=SAMPLE_COMPONENTS,
        )
        main_client.list_dev_branches.return_value = SAMPLE_BRANCHES_WITH_DEV
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: main_client,
        )
        result_main = svc.pull(alias="prod", project_root=project_root)
        assert result_main["rows_pulled"] == 1

        # Switch active branch to the dev branch (branch_id 99999)
        store.set_project_branch("prod", 99999)

        # Pull dev branch -- same API payload (clone of main, identical hashes)
        dev_client = _make_sync_mock_client(
            components_response=SAMPLE_COMPONENTS,
        )
        dev_client.list_dev_branches.return_value = SAMPLE_BRANCHES_WITH_DEV
        svc2 = SyncService(
            config_store=store,
            client_factory=lambda url, token: dev_client,
        )
        result_dev = svc2.pull(alias="prod", project_root=project_root)

        # Row files must be written for the dev branch directory
        assert result_dev["rows_pulled"] == 1, (
            "Dev branch pull must write row files even when API hash matches main"
        )
        dev_branch_dir = project_root / result_dev["branch_dir"]
        row_files = [f for f in dev_branch_dir.rglob(CONFIG_FILENAME) if "rows" in f.parts]
        assert len(row_files) >= 1, "Row _config.yml files must exist under dev branch dir"


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


class TestPushRows:
    """Row-level push tests: create/update/delete + encryption.

    These lock the P0-1 + P1-5 contract: sync push must deploy variable rows
    via the Storage API ``/rows`` endpoint and encrypt ``#``-prefixed secrets
    before transmission.
    """

    def _init_and_pull(
        self,
        tmp_config_dir: Path,
        project_root: Path,
    ) -> tuple[ConfigStore, SyncService]:
        """init + pull with SAMPLE_COMPONENTS (includes one row)."""
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

        pull_client = _make_sync_mock_client(components_response=SAMPLE_COMPONENTS)
        pull_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )
        pull_svc.pull(alias="prod", project_root=project_root)
        return store, pull_svc

    def test_push_row_update_calls_update_config_row(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Editing a row YAML triggers client.update_config_row with the new configuration."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store, _ = self._init_and_pull(tmp_config_dir, project_root)

        # Locate the row file (SAMPLE_COMPONENTS has one row under cfg-001).
        config_files = list(project_root.rglob(CONFIG_FILENAME))
        row_files = [f for f in config_files if "rows" in f.relative_to(project_root).parts]
        assert len(row_files) == 1, f"Expected exactly one row file, got {row_files}"

        row_file = row_files[0]
        row_data = yaml.safe_load(row_file.read_text(encoding="utf-8"))
        row_data["parameters"]["path"] = "/users/changed"
        row_file.write_text(yaml.dump(row_data, default_flow_style=False), encoding="utf-8")

        push_client = _make_sync_mock_client(components_response=SAMPLE_COMPONENTS)
        push_client.update_config_row.return_value = {"id": "row-001"}
        push_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: push_client,
        )

        result = push_svc.push(alias="prod", project_root=project_root)

        assert result["status"] == "pushed"
        assert result["updated"] == 1
        assert result["errors"] == []
        push_client.update_config_row.assert_called_once()
        call_kwargs = push_client.update_config_row.call_args.kwargs
        assert call_kwargs["component_id"] == "keboola.ex-http"
        assert call_kwargs["config_id"] == "cfg-001"
        assert call_kwargs["row_id"] == "row-001"
        # configuration dict passed in has the edited value
        assert call_kwargs["configuration"]["parameters"]["path"] == "/users/changed"

    def test_push_row_delete_calls_delete_config_row(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Removing a row YAML file triggers client.delete_config_row + manifest pruning."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store, _ = self._init_and_pull(tmp_config_dir, project_root)

        row_files = [
            f
            for f in project_root.rglob(CONFIG_FILENAME)
            if "rows" in f.relative_to(project_root).parts
        ]
        assert len(row_files) == 1
        row_files[0].unlink()

        push_client = _make_sync_mock_client(components_response=SAMPLE_COMPONENTS)
        push_client.delete_config_row.return_value = None
        push_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: push_client,
        )

        result = push_svc.push(alias="prod", project_root=project_root)

        assert result["status"] == "pushed"
        assert result["deleted"] == 1
        push_client.delete_config_row.assert_called_once()
        call_kwargs = push_client.delete_config_row.call_args.kwargs
        assert call_kwargs["component_id"] == "keboola.ex-http"
        assert call_kwargs["config_id"] == "cfg-001"
        assert call_kwargs["row_id"] == "row-001"

        # Manifest should no longer list the deleted row
        manifest = load_manifest(project_root)
        parent = next(c for c in manifest.configurations if c.id == "cfg-001")
        assert all(r.id != "row-001" for r in parent.rows)

    def test_push_row_encrypts_hash_secrets_before_api_call(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """#-prefixed secrets in row YAML are sent through the Encryption API first.

        Locks P1-5: the PUT body contains KBC::-prefixed ciphertext, never the
        plaintext the user wrote locally.
        """
        project_root = tmp_path / "project"
        project_root.mkdir()
        store, _ = self._init_and_pull(tmp_config_dir, project_root)

        row_files = [
            f
            for f in project_root.rglob(CONFIG_FILENAME)
            if "rows" in f.relative_to(project_root).parts
        ]
        row_file = row_files[0]
        row_data = yaml.safe_load(row_file.read_text(encoding="utf-8"))
        row_data["parameters"]["#api_token"] = "plain-secret"
        row_file.write_text(yaml.dump(row_data, default_flow_style=False), encoding="utf-8")

        push_client = _make_sync_mock_client(components_response=SAMPLE_COMPONENTS)
        push_client.update_config_row.return_value = {"id": "row-001"}

        # Real encryption API returns the SAME keys the caller sent, with values
        # replaced by ciphertext. _collect_secrets flattens the path, so the key
        # the caller sends is "#parameters.#api_token". Mirror that here so
        # _apply_encrypted successfully replaces the leaf value on round-trip.
        def fake_encrypt(*, project_id, component_id, data):
            return {k: "KBC::ComponentSecure::ciphertext" for k in data}

        push_client.encrypt_values.side_effect = fake_encrypt
        push_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: push_client,
        )

        result = push_svc.push(alias="prod", project_root=project_root)

        assert result["status"] == "pushed"
        assert result["updated"] == 1
        push_client.encrypt_values.assert_called_once()
        enc_kwargs = push_client.encrypt_values.call_args.kwargs
        assert enc_kwargs["component_id"] == "keboola.ex-http"
        # Plaintext was collected and sent; key is path-prefixed so the API can
        # encrypt it under the right scope.
        assert any(v == "plain-secret" for v in enc_kwargs["data"].values())

        put_kwargs = push_client.update_config_row.call_args.kwargs
        assert (
            put_kwargs["configuration"]["parameters"]["#api_token"]
            == "KBC::ComponentSecure::ciphertext"
        )

    def test_push_row_update_error_accumulates(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """Row push failure is captured in result['errors'], other changes still pushed."""
        from keboola_agent_cli.errors import KeboolaApiError

        project_root = tmp_path / "project"
        project_root.mkdir()
        store, _ = self._init_and_pull(tmp_config_dir, project_root)

        row_files = [
            f
            for f in project_root.rglob(CONFIG_FILENAME)
            if "rows" in f.relative_to(project_root).parts
        ]
        row_file = row_files[0]
        row_data = yaml.safe_load(row_file.read_text(encoding="utf-8"))
        row_data["parameters"]["path"] = "/users/changed"
        row_file.write_text(yaml.dump(row_data, default_flow_style=False), encoding="utf-8")

        push_client = _make_sync_mock_client(components_response=SAMPLE_COMPONENTS)
        push_client.update_config_row.side_effect = KeboolaApiError(
            message="validation failed",
            status_code=400,
            error_code="validation",
        )
        push_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: push_client,
        )

        result = push_svc.push(alias="prod", project_root=project_root)

        assert result["status"] == "pushed"
        assert result["updated"] == 0
        assert len(result["errors"]) == 1
        assert result["errors"][0]["config_id"] == "row-001"
        assert "validation failed" in result["errors"][0]["message"]

    def test_push_encryption_failure_aborts_fail_closed(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """ENCRYPTION_FAILED on a per-change row push aborts the whole push.

        Not buried in ``result["errors"]`` -- the exception propagates so the
        CLI exits non-zero. A plaintext secret must never reach the wire, and
        the caller must NEVER see ``status=pushed`` when any ``#``-secret
        failed to encrypt.
        """
        from keboola_agent_cli.errors import KeboolaApiError

        project_root = tmp_path / "project"
        project_root.mkdir()
        store, _ = self._init_and_pull(tmp_config_dir, project_root)

        row_files = [
            f
            for f in project_root.rglob(CONFIG_FILENAME)
            if "rows" in f.relative_to(project_root).parts
        ]
        row_file = row_files[0]
        row_data = yaml.safe_load(row_file.read_text(encoding="utf-8"))
        row_data["parameters"]["#api_token"] = "plain-secret"
        row_file.write_text(yaml.dump(row_data, default_flow_style=False), encoding="utf-8")

        push_client = _make_sync_mock_client(components_response=SAMPLE_COMPONENTS)
        push_client.encrypt_values.side_effect = Exception("encryption service unreachable")
        push_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: push_client,
        )

        with pytest.raises(KeboolaApiError) as excinfo:
            push_svc.push(alias="prod", project_root=project_root)
        assert excinfo.value.error_code == "ENCRYPTION_FAILED"
        # No row mutation on the server: update_config_row must never have been called.
        push_client.update_config_row.assert_not_called()

    def test_push_untracked_row_dir_calls_create_config_row(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A hand-crafted row dir under a tracked config gets POSTed via create_config_row.

        Locks the `_find_untracked_rows` contract: dropping a
        ``rows/new-row/_config.yml`` into a tracked config's ``rows/``
        directory must be detected as an added row and pushed via the
        row-create endpoint.
        """
        project_root = tmp_path / "project"
        project_root.mkdir()
        store, _ = self._init_and_pull(tmp_config_dir, project_root)

        manifest = load_manifest(project_root)
        parent_cfg = next(c for c in manifest.configurations if c.id == "cfg-001")
        branch_path = manifest.branches[0].path
        parent_dir = project_root / branch_path / parent_cfg.path
        new_row_dir = parent_dir / "rows" / "hand-crafted-row"
        new_row_dir.mkdir(parents=True)
        new_row_file = new_row_dir / CONFIG_FILENAME
        new_row_file.write_text(
            yaml.dump(
                {
                    "name": "Hand crafted row",
                    "parameters": {"path": "/new/endpoint"},
                    "_keboola": {"component_id": "keboola.ex-http"},
                },
                default_flow_style=False,
            ),
            encoding="utf-8",
        )

        push_client = _make_sync_mock_client(components_response=SAMPLE_COMPONENTS)
        push_client.create_config_row.return_value = {"id": "row-new-001"}
        push_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: push_client,
        )

        result = push_svc.push(alias="prod", project_root=project_root)

        assert result["status"] == "pushed"
        assert result["created"] == 1
        assert result["errors"] == []
        push_client.create_config_row.assert_called_once()
        call_kwargs = push_client.create_config_row.call_args.kwargs
        assert call_kwargs["component_id"] == "keboola.ex-http"
        assert call_kwargs["config_id"] == "cfg-001"
        assert call_kwargs["name"] == "Hand crafted row"
        assert call_kwargs["configuration"]["parameters"]["path"] == "/new/endpoint"

        # Manifest should now track the newly created row with the API-assigned id.
        post_manifest = load_manifest(project_root)
        parent_after = next(c for c in post_manifest.configurations if c.id == "cfg-001")
        new_row_ids = [r.id for r in parent_after.rows if r.path == "rows/hand-crafted-row"]
        assert new_row_ids == ["row-new-001"]

    def _build_variables_row_dir(self, tmp_path: Path) -> Path:
        """Write a minimal ``keboola.variables`` row YAML with a hoisted ``values`` list."""
        from keboola_agent_cli.constants import CONFIG_FILENAME, CONFIG_YML_VERSION

        row_dir = tmp_path / "rows" / "default"
        row_dir.mkdir(parents=True)
        (row_dir / CONFIG_FILENAME).write_text(
            yaml.dump(
                {
                    "version": CONFIG_YML_VERSION,
                    "name": "default",
                    "description": "",
                    "values": [{"name": "#api_key", "value": "plain-secret-xyz"}],
                    "_keboola": {
                        "component_id": "keboola.variables",
                        "row_id": "vals-default",
                    },
                },
                default_flow_style=False,
            ),
            encoding="utf-8",
        )
        return row_dir

    def test_push_variables_row_encrypts_hash_prefixed_name(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """``keboola.variables`` rows with ``#``-prefixed names encrypt before PUT.

        Locks the PR #190 review fix: the row-hoisted ``values: [{name, value}]``
        shape must flow through the Encryption API, not bypass it.
        """
        from keboola_agent_cli.sync.manifest import ManifestConfigRow, ManifestConfiguration

        row_dir = self._build_variables_row_dir(tmp_path)

        push_client = _make_sync_mock_client()
        push_client.update_config_row.return_value = {"id": "vals-default"}

        def fake_encrypt(*, project_id, component_id, data):
            return {k: "KBC::ProjectSecure::ciphertext" for k in data}

        push_client.encrypt_values.side_effect = fake_encrypt

        store = setup_single_project(tmp_config_dir)
        push_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: push_client,
        )

        parent = ManifestConfiguration(
            branchId=12345,
            componentId="keboola.variables",
            id="vars-001",
            path="other/keboola.variables/shared-variables",
            rows=[ManifestConfigRow(id="vals-default", path="rows/default", metadata={})],
        )

        push_svc._push_update_row(
            push_client,
            component_id="keboola.variables",
            parent_config_id="vars-001",
            row_id="vals-default",
            row_dir=row_dir,
            parent=parent,
            branch_id=None,
            project_id=258,
            allow_plaintext_fallback=False,
        )

        push_client.encrypt_values.assert_called_once()
        enc_kwargs = push_client.encrypt_values.call_args.kwargs
        assert enc_kwargs["component_id"] == "keboola.variables"
        assert any(v == "plain-secret-xyz" for v in enc_kwargs["data"].values())

        put_kwargs = push_client.update_config_row.call_args.kwargs
        assert put_kwargs["component_id"] == "keboola.variables"
        assert put_kwargs["row_id"] == "vals-default"
        assert put_kwargs["configuration"]["values"][0]["value"] == "KBC::ProjectSecure::ciphertext"
        assert put_kwargs["configuration"]["values"][0]["name"] == "#api_key"

    def test_push_variables_row_encrypt_failure_aborts(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Encryption failure on a variables row aborts fail-closed.

        ``update_config_row`` must not be called, so plaintext never hits Storage.
        """
        from keboola_agent_cli.errors import KeboolaApiError
        from keboola_agent_cli.sync.manifest import ManifestConfigRow, ManifestConfiguration

        row_dir = self._build_variables_row_dir(tmp_path)

        push_client = _make_sync_mock_client()
        push_client.encrypt_values.side_effect = Exception("encryption service down")

        store = setup_single_project(tmp_config_dir)
        push_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: push_client,
        )

        parent = ManifestConfiguration(
            branchId=12345,
            componentId="keboola.variables",
            id="vars-001",
            path="other/keboola.variables/shared-variables",
            rows=[ManifestConfigRow(id="vals-default", path="rows/default", metadata={})],
        )

        with pytest.raises(KeboolaApiError) as excinfo:
            push_svc._push_update_row(
                push_client,
                component_id="keboola.variables",
                parent_config_id="vars-001",
                row_id="vals-default",
                row_dir=row_dir,
                parent=parent,
                branch_id=None,
                project_id=258,
                allow_plaintext_fallback=False,
            )
        assert excinfo.value.error_code == "ENCRYPTION_FAILED"
        push_client.update_config_row.assert_not_called()


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
        assert result["keboola_branch_id"] == 99999
        assert result["keboola_branch_name"] == "feature/auth"
        link_client.create_dev_branch.assert_called_once_with(name="feature/auth")

        # Verify the mapping was saved to disk
        from keboola_agent_cli.sync.branch_mapping import load_branch_mapping

        mapping = load_branch_mapping(project_root)
        entry = mapping.get("feature/auth")
        assert entry is not None
        assert entry.keboola_id == 99999

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
        assert result["keboola_branch_id"] == 99999
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
        assert result["keboola_branch_id"] == 99999

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
        assert result["keboola_branch_id"] == 99999
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
        assert result["keboola_branch_id"] == 77777
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
        assert result["keboola_branch_id"] == 99999
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
        assert result["keboola_branch_id"] == 99999
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


# ===================================================================
# _ensure_branch_registered tests
# ===================================================================


class TestEnsureBranchRegistered:
    """Tests for SyncService._ensure_branch_registered()."""

    @staticmethod
    def _make_manifest(branches: list[dict[str, Any]] | None = None) -> Manifest:
        """Build a minimal Manifest with given branches."""
        from keboola_agent_cli.sync.manifest import (
            ManifestBranch,
            ManifestGitBranching,
            ManifestNaming,
            ManifestProject,
        )

        default_branches: list[dict[str, Any]] = [{"id": 12345, "path": "main"}]
        return Manifest(
            version=MANIFEST_VERSION,
            project=ManifestProject(id=258, apiHost="connection.keboola.com"),
            naming=ManifestNaming(),
            gitBranching=ManifestGitBranching(),
            branches=[ManifestBranch(**b) for b in (branches or default_branches)],
            configurations=[],
        )

    def test_noop_when_branch_id_is_none(self) -> None:
        """No-op for production (branch_id=None)."""
        store = MagicMock()
        svc = SyncService(config_store=store)
        manifest = self._make_manifest()
        client = MagicMock()

        result = svc._ensure_branch_registered(manifest, None, client)

        assert result is None
        client.list_dev_branches.assert_not_called()
        assert len(manifest.branches) == 1

    def test_noop_when_branch_already_registered(self) -> None:
        """No-op when branch_id is already in manifest.branches."""
        store = MagicMock()
        svc = SyncService(config_store=store)
        manifest = self._make_manifest(
            branches=[
                {"id": 12345, "path": "main"},
                {"id": 99999, "path": "feature-x"},
            ]
        )
        client = MagicMock()

        result = svc._ensure_branch_registered(manifest, 99999, client)

        assert result is None
        client.list_dev_branches.assert_not_called()
        assert len(manifest.branches) == 2

    def test_adds_missing_branch(self) -> None:
        """Adds a new branch entry when branch_id is missing from manifest."""
        store = MagicMock()
        svc = SyncService(config_store=store)
        manifest = self._make_manifest()
        client = MagicMock()
        client.list_dev_branches.return_value = [
            {"id": 12345, "name": "Main", "isDefault": True},
            {"id": 99999, "name": "My Feature Branch", "isDefault": False},
        ]

        result = svc._ensure_branch_registered(manifest, 99999, client)

        assert result == "my-feature-branch"
        assert len(manifest.branches) == 2
        new_branch = manifest.branches[1]
        assert new_branch.id == 99999
        assert new_branch.path == "my-feature-branch"

    def test_handles_path_collision(self) -> None:
        """Appends branch_id when sanitized name collides with existing path."""
        store = MagicMock()
        svc = SyncService(config_store=store)
        # Pre-populate with a branch that has path "main" (which is the default)
        manifest = self._make_manifest(
            branches=[
                {"id": 12345, "path": "main"},
                {"id": 88888, "path": "feature-x"},
            ]
        )
        client = MagicMock()
        # New branch whose sanitized name would be "feature-x" -- collision
        client.list_dev_branches.return_value = [
            {"id": 77777, "name": "Feature X", "isDefault": False},
        ]

        result = svc._ensure_branch_registered(manifest, 77777, client)

        assert result == "feature-x-77777"
        assert len(manifest.branches) == 3
        assert manifest.branches[2].path == "feature-x-77777"

    def test_handles_empty_branch_name(self) -> None:
        """Falls back to 'branch-{id}' when branch name is empty."""
        store = MagicMock()
        svc = SyncService(config_store=store)
        manifest = self._make_manifest()
        client = MagicMock()
        client.list_dev_branches.return_value = [
            {"id": 55555, "name": "", "isDefault": False},
        ]

        result = svc._ensure_branch_registered(manifest, 55555, client)

        assert result == "branch-55555"
        assert len(manifest.branches) == 2
        assert manifest.branches[1].path == "branch-55555"

    def test_handles_branch_not_found_in_api(self) -> None:
        """Falls back to 'branch-{id}' when branch_id not in API response."""
        store = MagicMock()
        svc = SyncService(config_store=store)
        manifest = self._make_manifest()
        client = MagicMock()
        # API returns branches but none match the requested ID
        client.list_dev_branches.return_value = [
            {"id": 12345, "name": "Main", "isDefault": True},
        ]

        result = svc._ensure_branch_registered(manifest, 44444, client)

        assert result == "branch-44444"
        assert len(manifest.branches) == 2
        assert manifest.branches[1].path == "branch-44444"


# ===========================================================================
# adopt-existing tests
# ===========================================================================


class TestAdoptExistingManifest:
    """Tests for SyncService.init_sync(adopt_existing=True)."""

    def _kbc_style_manifest(self, project_id: int = 258) -> dict:
        """Return a manifest as written by the kbc Go CLI (camelCase, gitBranching present)."""
        return {
            "version": 2,
            "project": {"id": project_id, "apiHost": "connection.keboola.com"},
            "allowTargetEnv": True,
            "sortBy": "id",
            "gitBranching": {"enabled": False, "defaultBranch": "main"},
            "naming": {"branch": "{branch_name}"},
            "branches": [{"id": 12345, "path": "main"}],
            "configurations": [
                {
                    "branchId": 12345,
                    "componentId": "keboola.ex-http",
                    "id": "cfg-001",
                    "path": "extractor/keboola.ex-http/my-extractor",
                    "rows": [],
                }
            ],
        }

    def test_adopt_existing_normalises_kbc_manifest(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """adopt_existing=True loads, validates, and saves a kbc manifest without data loss."""
        mock_client = _make_sync_mock_client(
            verify_token_response=SAMPLE_VERIFY_TOKEN,  # project_id=258
            branches_response=SAMPLE_BRANCHES,
        )
        store = setup_single_project(tmp_config_dir)
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        project_root = tmp_path / "project"
        project_root.mkdir()
        keboola_dir = project_root / ".keboola"
        keboola_dir.mkdir()
        manifest_path = keboola_dir / "manifest.json"
        import json as _json

        manifest_path.write_text(
            _json.dumps(self._kbc_style_manifest(project_id=258)), encoding="utf-8"
        )

        result = svc.init_sync(
            alias="prod",
            project_root=project_root,
            adopt_existing=True,
        )

        assert result["status"] == "adopted"
        assert result["project_id"] == 258
        assert result["project_alias"] == "prod"
        assert result["files_created"] == []

        # Original configuration entry preserved in manifest
        saved = load_manifest(project_root)
        assert len(saved.configurations) == 1
        assert saved.configurations[0].component_id == "keboola.ex-http"

    def test_adopt_existing_rejects_project_id_mismatch(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """adopt_existing raises ConfigError when manifest project_id != alias project_id."""
        mock_client = _make_sync_mock_client(
            verify_token_response=SAMPLE_VERIFY_TOKEN,  # project_id=258
            branches_response=SAMPLE_BRANCHES,
        )
        store = setup_single_project(tmp_config_dir)
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        project_root = tmp_path / "project"
        project_root.mkdir()
        keboola_dir = project_root / ".keboola"
        keboola_dir.mkdir()
        import json as _json

        # Manifest claims a different project (id=999, not 258)
        (keboola_dir / "manifest.json").write_text(
            _json.dumps(self._kbc_style_manifest(project_id=999)), encoding="utf-8"
        )

        with pytest.raises(ConfigError, match="project_id=999"):
            svc.init_sync(alias="prod", project_root=project_root, adopt_existing=True)

    def test_adopt_existing_falls_through_to_normal_init_when_no_manifest(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """adopt_existing=True with no manifest runs normal init (creates manifest)."""
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

        result = svc.init_sync(
            alias="prod",
            project_root=project_root,
            adopt_existing=True,
        )

        # Should behave like a normal init when no manifest exists
        assert result["status"] == "initialized"
        assert (project_root / ".keboola" / "manifest.json").exists()

    def test_adopt_existing_false_still_raises_on_existing_manifest(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Default (adopt_existing=False) still raises FileExistsError when manifest exists."""
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
        svc.init_sync(alias="prod", project_root=project_root)

        # Second call without adopt_existing should still fail
        with pytest.raises(FileExistsError, match="--adopt-existing"):
            svc.init_sync(alias="prod", project_root=project_root)

    def test_adopt_existing_idempotent(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """adopt_existing is idempotent: calling it twice leaves the manifest unchanged."""
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
        keboola_dir = project_root / ".keboola"
        keboola_dir.mkdir()
        import json as _json

        (keboola_dir / "manifest.json").write_text(
            _json.dumps(self._kbc_style_manifest(project_id=258)), encoding="utf-8"
        )

        result1 = svc.init_sync(alias="prod", project_root=project_root, adopt_existing=True)
        result2 = svc.init_sync(alias="prod", project_root=project_root, adopt_existing=True)

        assert result1["status"] == "adopted"
        assert result2["status"] == "adopted"
        # Both calls return the same project data
        assert result1["project_id"] == result2["project_id"]


# ===================================================================
# Issue #267 regression tests
# ===================================================================


class TestIssue267Regressions:
    """Regression coverage for the chained sync git-branching bugs (issue #267).

    Each test would have failed against ``main`` before the fix:

    * Bug A — branch_id type confusion (str in branch-mapping.json, int in
      manifest) caused ``branch.id == branch_id`` cross-type compares to
      always be False, misrouting pulls to ``main/`` and inflating
      ``manifest.branches[]`` on every call.
    * Bug B — ``_find_untracked_configs`` walker scope was scoped to
      branches with already-tracked configs, blocking the documented
      "scaffold locally then push" flow when ``configurations: []``.
    * Bug E — ``_resolve_branch_id`` raised ``ConfigError`` for the
      default git branch when ``branch-mapping.json`` was missing,
      leaving users with no recovery path.
    """

    def _init_git_branching_project(
        self,
        tmp_config_dir: Path,
        project_root: Path,
    ) -> ConfigStore:
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

    # ------------------------------------------------------------------
    # Bug A
    # ------------------------------------------------------------------

    def test_branch_link_persists_keboola_id_as_int(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """branch-mapping.json on disk stores ``id`` as int, never str."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init_git_branching_project(tmp_config_dir, project_root)

        link_client = _make_sync_mock_client(branches_response=SAMPLE_BRANCHES_WITH_DEV)
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: link_client,
        )

        with patch(
            "keboola_agent_cli.sync.git_utils.get_current_branch",
            return_value="feature-x",
        ):
            svc.branch_link(
                alias="prod",
                project_root=project_root,
                branch_id=99999,
            )

        # Read the file directly: persisted JSON must have id as a JSON number.
        raw = json.loads((project_root / KEBOOLA_DIR_NAME / BRANCH_MAPPING_FILENAME).read_text())
        assert raw["mappings"]["feature-x"]["id"] == 99999
        assert isinstance(raw["mappings"]["feature-x"]["id"], int)

    def test_repeated_pulls_do_not_grow_manifest_branches(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Running ``sync pull`` N times against a linked dev branch must not
        append duplicate entries to ``manifest.branches`` (Bug A symptom)."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init_git_branching_project(tmp_config_dir, project_root)

        # Link feature-x -> 99999
        link_client = _make_sync_mock_client(branches_response=SAMPLE_BRANCHES_WITH_DEV)
        link_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: link_client,
        )
        with patch(
            "keboola_agent_cli.sync.git_utils.get_current_branch",
            return_value="feature-x",
        ):
            link_svc.branch_link(
                alias="prod",
                project_root=project_root,
                branch_id=99999,
            )

            # Pull three times against the same linked branch
            pull_client = _make_sync_mock_client(
                components_response=[],
                branches_response=SAMPLE_BRANCHES_WITH_DEV,
            )
            pull_client.list_buckets_with_metadata.return_value = []
            pull_client.list_tables_with_metadata.return_value = []
            pull_client.list_jobs_grouped.return_value = []
            pull_svc = SyncService(
                config_store=store,
                client_factory=lambda url, token: pull_client,
            )
            for _ in range(3):
                pull_svc.pull(
                    alias="prod",
                    project_root=project_root,
                    no_storage=True,
                    no_jobs=True,
                )

        manifest = load_manifest(project_root)
        # Default branch + linked dev branch -- nothing more.
        assert len(manifest.branches) == 2
        ids = sorted(b.id for b in manifest.branches)
        assert ids == [12345, 99999]
        # The dev branch path is the API-provided name (sanitized), not a
        # numeric fallback. Bug A's type confusion previously prevented the
        # name lookup from succeeding, leaving us with ``branch-99999``.
        paths = sorted(b.path for b in manifest.branches)
        assert paths == ["feature-x", "main"]

    def test_pull_response_routes_to_linked_branch_dir(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """``sync pull`` against a linked dev branch reports ``branch_dir`` as
        the linked branch path, not ``main`` (Bug A misroute)."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init_git_branching_project(tmp_config_dir, project_root)

        link_client = _make_sync_mock_client(branches_response=SAMPLE_BRANCHES_WITH_DEV)
        link_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: link_client,
        )
        with patch(
            "keboola_agent_cli.sync.git_utils.get_current_branch",
            return_value="feature-x",
        ):
            link_svc.branch_link(
                alias="prod",
                project_root=project_root,
                branch_id=99999,
            )

            pull_client = _make_sync_mock_client(
                components_response=[],
                branches_response=SAMPLE_BRANCHES_WITH_DEV,
            )
            pull_client.list_buckets_with_metadata.return_value = []
            pull_client.list_tables_with_metadata.return_value = []
            pull_client.list_jobs_grouped.return_value = []
            pull_svc = SyncService(
                config_store=store,
                client_factory=lambda url, token: pull_client,
            )
            result = pull_svc.pull(
                alias="prod",
                project_root=project_root,
                no_storage=True,
                no_jobs=True,
            )

        # branch_id in the response is an int (issue #267) and branch_dir
        # is the linked branch's path, never "main".
        assert result["branch_id"] == 99999
        assert result["branch_dir"] != "main"

    # ------------------------------------------------------------------
    # Bug B
    # ------------------------------------------------------------------

    def test_walker_finds_untracked_with_empty_configurations(self, tmp_path: Path) -> None:
        """``_find_untracked_configs`` surfaces a scaffold under a non-default
        branch even when ``manifest.configurations: []``, given that the
        branch is the resolved one for this op (Bug B fix)."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        # Build a manifest by hand with main + a linked dev branch and no
        # tracked configs -- the exact pre-condition Bug B blocks on.
        keboola_dir = project_root / KEBOOLA_DIR_NAME
        keboola_dir.mkdir()
        (keboola_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "version": MANIFEST_VERSION,
                    "project": {"id": 258, "apiHost": "connection.keboola.com"},
                    "allowTargetEnv": True,
                    "gitBranching": {"enabled": True, "defaultBranch": "main"},
                    "sortBy": "id",
                    "naming": {
                        "branch": "{branch_name}",
                        "config": "{component_type}/{component_id}/{config_name}",
                        "configRow": "rows/{config_row_name}",
                        "schedulerConfig": "schedules/{config_name}",
                        "sharedCodeConfig": "_shared/{target_component_id}",
                        "sharedCodeConfigRow": "codes/{config_row_name}",
                        "variablesConfig": "variables",
                        "variablesValuesRow": "values/{config_row_name}",
                        "dataAppConfig": "app/{component_id}/{config_name}",
                    },
                    "allowedBranches": [],
                    "ignoredComponents": [],
                    "branches": [
                        {"id": 12345, "path": "main", "metadata": {}},
                        {"id": 99999, "path": "branch-99999", "metadata": {}},
                    ],
                    "configurations": [],
                }
            )
        )

        # Drop a scaffold under branch-99999/
        scaffold = (
            project_root / "branch-99999" / "application" / "test-component" / "my-test-config"
        )
        scaffold.mkdir(parents=True)
        (scaffold / CONFIG_FILENAME).write_text(yaml.safe_dump({"name": "my-test-config"}))

        manifest = load_manifest(project_root)
        svc = SyncService(config_store=MagicMock())
        added = svc._find_untracked_configs(
            project_root,
            manifest,
            resolved_branch_id=99999,
        )

        assert len(added) == 1
        assert added[0]["path"].endswith("my-test-config")

    def test_walker_skips_orphaned_branch_dirs(self, tmp_path: Path) -> None:
        """Phantom-add protection still holds: a directory under a branch
        whose id is neither tracked, default, nor explicitly resolved is
        ignored. Guards against regression of Bug B in the wrong direction."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        keboola_dir = project_root / KEBOOLA_DIR_NAME
        keboola_dir.mkdir()
        (keboola_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "version": MANIFEST_VERSION,
                    "project": {"id": 258, "apiHost": "connection.keboola.com"},
                    "allowTargetEnv": True,
                    "gitBranching": {"enabled": True, "defaultBranch": "main"},
                    "sortBy": "id",
                    "naming": {
                        "branch": "{branch_name}",
                        "config": "{component_type}/{component_id}/{config_name}",
                        "configRow": "rows/{config_row_name}",
                        "schedulerConfig": "schedules/{config_name}",
                        "sharedCodeConfig": "_shared/{target_component_id}",
                        "sharedCodeConfigRow": "codes/{config_row_name}",
                        "variablesConfig": "variables",
                        "variablesValuesRow": "values/{config_row_name}",
                        "dataAppConfig": "app/{component_id}/{config_name}",
                    },
                    "allowedBranches": [],
                    "ignoredComponents": [],
                    "branches": [
                        {"id": 12345, "path": "main", "metadata": {}},
                        {"id": 88888, "path": "branch-orphan", "metadata": {}},
                    ],
                    "configurations": [],
                }
            )
        )

        # Drop a scaffold under branch-orphan/ -- should be ignored because
        # we resolve to branch 99999 (a different one).
        scaffold = (
            project_root / "branch-orphan" / "application" / "test-component" / "ignored-config"
        )
        scaffold.mkdir(parents=True)
        (scaffold / CONFIG_FILENAME).write_text(yaml.safe_dump({"name": "ignored-config"}))

        manifest = load_manifest(project_root)
        svc = SyncService(config_store=MagicMock())
        added = svc._find_untracked_configs(
            project_root,
            manifest,
            resolved_branch_id=99999,  # nothing on disk under this branch
        )
        assert added == []

    # ------------------------------------------------------------------
    # Bug E
    # ------------------------------------------------------------------

    def test_resolve_branch_id_default_branch_without_mapping(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """When on the default git branch, ``_resolve_branch_id`` returns
        ``None`` (production) even if ``branch-mapping.json`` is missing
        entirely. There is always a recovery path to production."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init_git_branching_project(tmp_config_dir, project_root)

        # Delete branch-mapping.json after init -- simulates accidental loss.
        (project_root / KEBOOLA_DIR_NAME / BRANCH_MAPPING_FILENAME).unlink()

        manifest = load_manifest(project_root)
        project = store.get_project("prod")
        with patch(
            "keboola_agent_cli.sync.git_utils.get_current_branch",
            return_value="main",
        ):
            resolved = SyncService._resolve_branch_id(project, manifest, project_root)
        assert resolved is None

    def test_resolve_branch_id_dev_branch_without_mapping_still_errors(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """The recovery path is intentionally narrow: only the default branch
        gets the silent fall-through. A non-default branch with no mapping
        still raises ``ConfigError`` so the user is told to link it."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init_git_branching_project(tmp_config_dir, project_root)

        (project_root / KEBOOLA_DIR_NAME / BRANCH_MAPPING_FILENAME).unlink()

        manifest = load_manifest(project_root)
        project = store.get_project("prod")
        with (
            patch(
                "keboola_agent_cli.sync.git_utils.get_current_branch",
                return_value="feature-x",
            ),
            pytest.raises(ConfigError, match="not linked"),
        ):
            SyncService._resolve_branch_id(project, manifest, project_root)


# ---------------------------------------------------------------------------
# Issue #269 sec-01 -- defense-in-depth path-confinement guard
# ---------------------------------------------------------------------------


class TestEnsureWithinBranch:
    """Direct unit tests for ``_ensure_within_branch`` defensive guard.

    The primary defense is ``naming.sanitize_path_segment()`` (covered in
    test_sync_naming.py). This guard is belt-and-suspenders for the case
    where the sanitizer regresses or a future code path bypasses it.
    """

    def test_passes_for_path_within_branch(self, tmp_path: Path) -> None:
        """A normal config_dir under branch_dir does not raise."""
        from keboola_agent_cli.services.sync_service import _ensure_within_branch

        branch_dir = tmp_path / "main"
        branch_dir.mkdir()
        config_dir = branch_dir / "extractor" / "keboola.ex-http" / "my-config"

        # Should not raise
        _ensure_within_branch(branch_dir, config_dir, "keboola.ex-http", "12345")

    def test_raises_for_path_escape_via_dotdot(self, tmp_path: Path) -> None:
        """A config_dir that resolves outside branch_dir raises ConfigError."""
        from keboola_agent_cli.errors import ConfigError
        from keboola_agent_cli.services.sync_service import _ensure_within_branch

        branch_dir = tmp_path / "main"
        branch_dir.mkdir()
        # Synthesize the misuse the sanitizer is supposed to prevent.
        # The guard should fire even when the sanitizer didn't.
        attacker_dir = branch_dir / ".." / "outside-workspace" / "config"

        with pytest.raises(ConfigError, match="escapes sync workspace"):
            _ensure_within_branch(branch_dir, attacker_dir, "evil-component", "cfg-id")

    def test_raises_with_component_id_in_message(self, tmp_path: Path) -> None:
        """Error message names the offending component / config so operators
        can locate the bad API response."""
        from keboola_agent_cli.errors import ConfigError
        from keboola_agent_cli.services.sync_service import _ensure_within_branch

        branch_dir = tmp_path / "main"
        branch_dir.mkdir()
        attacker_dir = branch_dir / ".." / "tmp"

        with pytest.raises(ConfigError) as excinfo:
            _ensure_within_branch(branch_dir, attacker_dir, "k.ex-bad", "01abc")
        assert "k.ex-bad" in str(excinfo.value)
        assert "01abc" in str(excinfo.value)

    def test_passes_for_absolute_path_inside_branch(self, tmp_path: Path) -> None:
        """An absolute path that happens to be inside branch_dir resolves OK."""
        from keboola_agent_cli.services.sync_service import _ensure_within_branch

        branch_dir = tmp_path / "main"
        branch_dir.mkdir()
        config_dir = branch_dir / "x" / "y"

        # Resolve and then re-pass: should still pass
        _ensure_within_branch(branch_dir, config_dir.resolve(), "comp", "id")


# ---------------------------------------------------------------------------
# Fresh-CREATE writeback + KBC.* metadata propagation (v0.47.0 / FIIA migration)
# ---------------------------------------------------------------------------


class TestFreshCreateWriteback:
    """Cover the fresh-CREATE manifest writeback + KBC.* metadata propagation.

    Closes the gap where a downstream caller (FIIA / scaffold-style emitter)
    pre-populates manifest entries with placeholder ids and folder metadata
    before the first ``sync push``. Pre-v0.47.0 every create unconditionally
    appended a new manifest entry (manifest doubled in size, re-pushes flagged
    every placeholder as ``added`` again, ``KBC.configuration.folderName``
    silently dropped on the floor).
    """

    @staticmethod
    def _make_svc(tmp_config_dir: Path) -> SyncService:
        return SyncService(config_store=setup_single_project(tmp_config_dir))

    def test_writeback_config_in_place_updates_placeholder(self, tmp_config_dir: Path) -> None:
        """A placeholder entry at the same ``(component_id, path)`` is updated
        in place; the manifest does not grow."""
        from keboola_agent_cli.sync.manifest import ManifestConfiguration

        svc = self._make_svc(tmp_config_dir)
        manifest = Manifest.model_construct(
            project={"id": 1, "apiHost": "connection.keboola.com"},  # type: ignore[arg-type]
            naming={"config": "{component_type}/{component_id}/{config_name}"},  # type: ignore[arg-type]
            configurations=[
                ManifestConfiguration(
                    branchId=0,
                    componentId="keboola.snowflake-transformation",
                    id="PLACEHOLDER-TX1",
                    path="transformation/keboola.snowflake-transformation/01_stage",
                    metadata={"KBC.configuration.folderName": "FI Pipeline"},
                )
            ],
        )

        entry = svc._writeback_create_config_in_manifest(
            manifest=manifest,
            component_id="keboola.snowflake-transformation",
            branch_id=12345,
            config_path_str="transformation/keboola.snowflake-transformation/01_stage",
            new_id="123456789",
            file_hash="abc123",
            cfg_hash="def456",
        )

        assert len(manifest.configurations) == 1, "must not append a duplicate"
        assert entry.id == "123456789"
        assert entry.branch_id == 12345
        assert entry.metadata["pull_hash"] == "abc123"
        assert entry.metadata["pull_config_hash"] == "def456"
        assert entry.metadata["KBC.configuration.folderName"] == "FI Pipeline", (
            "user-declared KBC.* metadata must survive the writeback"
        )

    def test_writeback_config_appends_when_no_placeholder(self, tmp_config_dir: Path) -> None:
        """If no placeholder exists at the path, append (legacy fallback)."""
        svc = self._make_svc(tmp_config_dir)
        manifest = Manifest.model_construct(
            project={"id": 1, "apiHost": "connection.keboola.com"},  # type: ignore[arg-type]
            naming={"config": "{component_type}/{component_id}/{config_name}"},  # type: ignore[arg-type]
            configurations=[],
        )

        entry = svc._writeback_create_config_in_manifest(
            manifest=manifest,
            component_id="keboola.ex-http",
            branch_id=0,
            config_path_str="extractor/keboola.ex-http/my-new-config",
            new_id="999",
            file_hash="h1",
            cfg_hash="h2",
        )

        assert len(manifest.configurations) == 1
        assert manifest.configurations[0] is entry
        assert entry.id == "999"
        assert entry.metadata == {"pull_hash": "h1", "pull_config_hash": "h2"}

    def test_propagate_kbc_metadata_calls_set_config_metadata(self, tmp_config_dir: Path) -> None:
        """KBC.* keys are POSTed via client.set_config_metadata; bookkeeping
        keys (``pull_hash``, ...) are filtered out."""
        from keboola_agent_cli.sync.manifest import ManifestConfiguration

        svc = self._make_svc(tmp_config_dir)
        entry = ManifestConfiguration(
            branchId=0,
            componentId="keboola.snowflake-transformation",
            id="cfg-123",
            path="x",
            metadata={
                "pull_hash": "h1",
                "pull_config_hash": "h2",
                "KBC.configuration.folderName": "FI Pipeline",
                "KBC.configuration.category": "transformation",
            },
        )
        client = MagicMock()

        svc._propagate_kbc_metadata(client, entry, branch_id=99)

        client.set_config_metadata.assert_called_once()
        call = client.set_config_metadata.call_args
        assert call.kwargs["component_id"] == "keboola.snowflake-transformation"
        assert call.kwargs["config_id"] == "cfg-123"
        assert call.kwargs["branch_id"] == 99
        entries = dict(call.kwargs["entries"])
        assert entries == {
            "KBC.configuration.folderName": "FI Pipeline",
            "KBC.configuration.category": "transformation",
        }, "pull_* bookkeeping keys must not be sent to the metadata API"

    def test_propagate_kbc_metadata_noop_when_no_kbc_keys(self, tmp_config_dir: Path) -> None:
        """No KBC.* keys → no API call (don't waste a round-trip)."""
        from keboola_agent_cli.sync.manifest import ManifestConfiguration

        svc = self._make_svc(tmp_config_dir)
        entry = ManifestConfiguration(
            branchId=0,
            componentId="x",
            id="y",
            path="z",
            metadata={"pull_hash": "h", "pull_config_hash": "h2"},
        )
        client = MagicMock()

        svc._propagate_kbc_metadata(client, entry, branch_id=None)

        client.set_config_metadata.assert_not_called()

    def test_writeback_row_in_place_updates_placeholder(self, tmp_config_dir: Path) -> None:
        """A placeholder row at the same ``path`` is updated in place; parent's
        rows list does not grow."""
        from keboola_agent_cli.sync.manifest import ManifestConfigRow, ManifestConfiguration

        svc = self._make_svc(tmp_config_dir)
        parent = ManifestConfiguration(
            branchId=0,
            componentId="keboola.variables",
            id="vars-001",
            path="other/keboola.variables/shared",
            rows=[
                ManifestConfigRow(
                    id="PLACEHOLDER-ROW",
                    path="rows/default",
                    metadata={},
                )
            ],
        )

        row = svc._writeback_create_row_in_manifest(
            parent=parent,
            row_path_str="rows/default",
            new_row_id="vals-real-id",
            file_hash="rh1",
            cfg_hash="rh2",
        )

        assert len(parent.rows) == 1, "must not append a duplicate row"
        assert row.id == "vals-real-id"
        assert row.metadata == {"pull_hash": "rh1", "pull_config_hash": "rh2"}

    def test_writeback_row_appends_when_no_placeholder(self, tmp_config_dir: Path) -> None:
        """No placeholder row → append (legacy fallback for untracked rows)."""
        from keboola_agent_cli.sync.manifest import ManifestConfiguration

        svc = self._make_svc(tmp_config_dir)
        parent = ManifestConfiguration(
            branchId=0,
            componentId="keboola.ex-http",
            id="cfg-1",
            path="extractor/keboola.ex-http/my-ext",
            rows=[],
        )

        row = svc._writeback_create_row_in_manifest(
            parent=parent,
            row_path_str="rows/new",
            new_row_id="row-001",
            file_hash="rh1",
            cfg_hash="rh2",
        )

        assert len(parent.rows) == 1
        assert parent.rows[0] is row
        assert row.id == "row-001"

    def test_push_create_with_placeholder_is_idempotent_and_propagates_folder(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """End-to-end push: placeholder + KBC.configuration.folderName.

        Round-trip:
          1. Init a project (empty manifest).
          2. Hand-populate a placeholder ManifestConfiguration with a
             ``KBC.configuration.folderName`` metadata key, save it, and write
             a matching ``_config.yml`` file.
          3. Run sync push — assert: created=1, manifest length stays at 1
             (placeholder updated in place to real ULID), client.create_config
             was called, client.set_config_metadata was called with the folder
             metadata.
          4. Run sync push a second time — assert: created=0, errors=0
             (idempotency naturally follows from writeback-in-place).
        """
        from keboola_agent_cli.constants import CONFIG_YML_VERSION
        from keboola_agent_cli.sync.manifest import ManifestConfiguration, save_manifest

        project_root = tmp_path / "project"
        project_root.mkdir()

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

        # Hand-author a placeholder manifest entry + local _config.yml at the
        # corresponding path. This is the FIIA / scaffold emit pattern.
        manifest = load_manifest(project_root)
        placeholder_path = "transformation/keboola.snowflake-transformation/01_stage"
        manifest.configurations.append(
            ManifestConfiguration(
                branchId=12345,
                componentId="keboola.snowflake-transformation",
                id="PLACEHOLDER-TX1",
                path=placeholder_path,
                metadata={"KBC.configuration.folderName": "FI Pipeline"},
            )
        )
        save_manifest(project_root, manifest)

        branch_path = manifest.branches[0].path
        config_dir = project_root / branch_path / placeholder_path
        config_dir.mkdir(parents=True)
        (config_dir / CONFIG_FILENAME).write_text(
            yaml.dump(
                {
                    "version": CONFIG_YML_VERSION,
                    "name": "01 Stage",
                    "description": "Staging transformation",
                    "parameters": {},
                    "_keboola": {
                        "component_id": "keboola.snowflake-transformation",
                        "config_id": "",
                    },
                },
                default_flow_style=False,
            ),
            encoding="utf-8",
        )

        push_client = _make_sync_mock_client(components_response=[])
        push_client.create_config.return_value = {"id": "999000111"}

        push_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: push_client,
        )

        # First push: placeholder → real ULID, KBC.* propagated.
        result = push_svc.push(alias="prod", project_root=project_root)
        assert result["status"] == "pushed"
        assert result["created"] == 1
        assert result["errors"] == []
        push_client.create_config.assert_called_once()
        push_client.set_config_metadata.assert_called_once()
        meta_call = push_client.set_config_metadata.call_args
        assert meta_call.kwargs["component_id"] == "keboola.snowflake-transformation"
        assert meta_call.kwargs["config_id"] == "999000111"
        assert dict(meta_call.kwargs["entries"]) == {
            "KBC.configuration.folderName": "FI Pipeline",
        }

        # Manifest must have updated the placeholder in place — NOT appended.
        post = load_manifest(project_root)
        matching = [
            c
            for c in post.configurations
            if c.component_id == "keboola.snowflake-transformation" and c.path == placeholder_path
        ]
        assert len(matching) == 1, "writeback must update placeholder in place"
        assert matching[0].id == "999000111"
        assert matching[0].metadata.get("KBC.configuration.folderName") == "FI Pipeline"

        # Second push against the now-real manifest must be a no-op.
        # The remote side reports the created config so the diff sees it as
        # present and unchanged.
        push_client2 = _make_sync_mock_client(
            components_response=[
                {
                    "id": "keboola.snowflake-transformation",
                    "type": "transformation",
                    "configurations": [
                        {
                            "id": "999000111",
                            "name": "01 Stage",
                            "description": "Staging transformation",
                            "configuration": {"parameters": {}},
                            "rows": [],
                        }
                    ],
                }
            ],
        )
        push_svc2 = SyncService(
            config_store=store,
            client_factory=lambda url, token: push_client2,
        )
        result2 = push_svc2.push(alias="prod", project_root=project_root)
        # Idempotent re-push: diff sees no changes, so service short-circuits
        # to status="no_changes" without ever entering the create path.
        assert result2["status"] in ("no_changes", "pushed")
        assert result2.get("created", 0) == 0, "re-push must be idempotent"
        push_client2.create_config.assert_not_called()
