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
from keboola_agent_cli.services._sync_push_ops import push_update_row
from keboola_agent_cli.services._sync_writeback import (
    propagate_kbc_metadata,
    writeback_create_config_in_manifest,
    writeback_create_row_in_manifest,
)
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

        push_update_row(
            push_svc,
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
            push_update_row(
                push_svc,
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
        branch is the resolved *source* tree for this op (Bug B fix; the
        walker takes the source branch path since issue #482)."""
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
        # diff/push resolve the source tree first (the scaffold materializes
        # branch-99999) and pass its path to the walker.
        added = svc._find_untracked_configs(
            project_root,
            manifest,
            only_branch_path="branch-99999",
        )

        assert len(added) == 1
        assert added[0]["path"].endswith("my-test-config")

    def test_walker_skips_orphaned_branch_dirs(self, tmp_path: Path) -> None:
        """Phantom-add protection still holds: a directory under a branch
        other than the resolved source tree is ignored. Guards against
        regression of Bug B in the wrong direction."""
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
        # Nothing on disk under the resolved branch 99999, so diff/push
        # promote the default tree ("main") as the source -- branch-orphan
        # must stay out of scope.
        added = svc._find_untracked_configs(
            project_root,
            manifest,
            only_branch_path="main",
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


# ===================================================================
# Issue #482 regression tests
# ===================================================================


class TestIssue482BranchSwitchDuplicates:
    """Regression coverage for issue #482.

    ``sync pull`` replaces ``manifest.configurations`` with the pulled
    branch's entries, so after ``branch use <dev>`` + ``pull`` the previously
    pulled ``main/`` tree is orphaned on disk. The untracked-config walker
    used to keep the default branch tree in scope unconditionally, so every
    orphaned file surfaced as ``added`` (with empty ``config_id``) and
    ``sync push`` created a duplicate config on the dev branch for each of
    them -- 100 duplicates from a single push in the report.

    The fix scopes the walker to the resolved *source* branch tree (the one
    push reads from) and adds an adopt-by-id guard: an untracked file whose
    ``_keboola.config_id`` resolves on the target branch and is unclaimed by
    the manifest diffs against the existing remote config instead of
    creating a duplicate.
    """

    def _init_and_pull_main(
        self,
        tmp_config_dir: Path,
        project_root: Path,
        components: list,
    ) -> ConfigStore:
        """Helper: init + pull production so ``main/`` is materialized."""
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

        pull_client = _make_sync_mock_client(components_response=components)
        pull_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )
        pull_svc.pull(alias="prod", project_root=project_root)
        return store

    def test_push_on_dev_branch_after_pull_is_noop(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """The issue's exact repro: pull main, pull a dev branch that inherits
        the same configs, then push with zero local edits -- must be a no-op
        instead of duplicating every config orphaned under ``main/``."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init_and_pull_main(tmp_config_dir, project_root, SAMPLE_COMPONENTS)

        # Pull the dev branch: it inherits the same configs from main.
        dev_client = _make_sync_mock_client(
            components_response=SAMPLE_COMPONENTS,
            branches_response=SAMPLE_BRANCHES_WITH_DEV,
        )
        dev_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: dev_client,
        )
        dev_svc.pull(alias="prod", project_root=project_root, branch_override=99999)

        # Bug precondition: the manifest now tracks only the dev branch while
        # the previously pulled main/ tree is still on disk.
        manifest = load_manifest(project_root)
        assert {cfg.branch_id for cfg in manifest.configurations} == {99999}
        assert (project_root / "main").is_dir()
        assert (project_root / "feature-x").is_dir()

        diff_result = dev_svc.diff(alias="prod", project_root=project_root, branch_override=99999)
        assert diff_result["changes"] == []
        assert diff_result["summary"]["added"] == 0

        push_result = dev_svc.push(alias="prod", project_root=project_root, branch_override=99999)
        assert push_result["status"] == "no_changes"
        assert push_result["created"] == 0
        dev_client.create_config.assert_not_called()

    def test_untracked_file_with_known_remote_id_updates_instead_of_creating(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Adopt-by-id guard: an untracked ``_config.yml`` whose
        ``_keboola.config_id`` exists on the target branch and is not claimed
        by any manifest entry is diffed as ``modified`` (an update), never
        ``added`` (a duplicate create)."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init_and_pull_main(tmp_config_dir, project_root, SAMPLE_COMPONENTS_NO_ROWS)

        # Hand-drop an untracked config dir carrying the id of a remote
        # config the manifest does not track (e.g. rebuilt/lost manifest).
        adopted_dir = project_root / "main" / "extractor" / "keboola.ex-http" / "orphaned-extractor"
        adopted_dir.mkdir(parents=True)
        (adopted_dir / CONFIG_FILENAME).write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "name": "Orphaned Extractor",
                    "parameters": {"baseUrl": "https://changed.example.com"},
                    "_keboola": {
                        "component_id": "keboola.ex-http",
                        "config_id": "cfg-777",
                    },
                }
            )
        )

        remote = [
            {
                **SAMPLE_COMPONENTS_NO_ROWS[0],
                "configurations": [
                    *SAMPLE_COMPONENTS_NO_ROWS[0]["configurations"],
                    {
                        "id": "cfg-777",
                        "name": "Orphaned Extractor",
                        "description": "",
                        "configuration": {
                            "parameters": {"baseUrl": "https://original.example.com"},
                        },
                        "rows": [],
                    },
                ],
            }
        ]
        client = _make_sync_mock_client(components_response=remote)
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: client,
        )

        diff_result = svc.diff(alias="prod", project_root=project_root)
        assert diff_result["summary"]["added"] == 0
        assert diff_result["summary"]["modified"] == 1
        modified = [c for c in diff_result["changes"] if c["change_type"] == "modified"]
        assert modified[0]["config_id"] == "cfg-777"

        push_result = svc.push(alias="prod", project_root=project_root)
        assert push_result["created"] == 0
        assert push_result["updated"] == 1
        client.create_config.assert_not_called()
        client.update_config.assert_called_once()
        assert client.update_config.call_args.kwargs["config_id"] == "cfg-777"

    def test_untracked_copy_of_tracked_config_still_creates_fresh(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Forking by copying a tracked config dir keeps the CREATE semantics:
        the copy carries the original's ``_keboola.config_id``, but that id is
        claimed by a manifest entry, so the copy must not be adopted (which
        would overwrite the original remote config)."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init_and_pull_main(tmp_config_dir, project_root, SAMPLE_COMPONENTS_NO_ROWS)

        manifest = load_manifest(project_root)
        entry = next(c for c in manifest.configurations if c.id == "cfg-001")
        src_dir = project_root / "main" / entry.path
        copy_dir = src_dir.parent / f"{src_dir.name}-copy"
        copy_dir.mkdir()
        data = yaml.safe_load((src_dir / CONFIG_FILENAME).read_text(encoding="utf-8"))
        data["name"] = "Forked Extractor"
        (copy_dir / CONFIG_FILENAME).write_text(yaml.safe_dump(data))

        client = _make_sync_mock_client(components_response=SAMPLE_COMPONENTS_NO_ROWS)
        client.create_config.return_value = {"id": "cfg-fresh-001"}
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: client,
        )

        diff_result = svc.diff(alias="prod", project_root=project_root)
        added = [c for c in diff_result["changes"] if c["change_type"] == "added"]
        assert len(added) == 1
        assert added[0]["config_id"] == ""  # fresh create, id not reused
        assert diff_result["summary"]["modified"] == 0

        push_result = svc.push(alias="prod", project_root=project_root)
        assert push_result["created"] == 1
        client.create_config.assert_called_once()
        client.update_config.assert_not_called()


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
        """A placeholder entry at the same (branch_id, component_id, path) is
        updated in place; the manifest does not grow."""
        from keboola_agent_cli.sync.manifest import ManifestConfiguration

        manifest = Manifest.model_construct(
            project={"id": 1, "apiHost": "connection.keboola.com"},  # type: ignore[arg-type]
            naming={"config": "{component_type}/{component_id}/{config_name}"},  # type: ignore[arg-type]
            configurations=[
                ManifestConfiguration(
                    branchId=12345,
                    componentId="keboola.snowflake-transformation",
                    id="PLACEHOLDER-TX1",
                    path="transformation/keboola.snowflake-transformation/01_stage",
                    metadata={"KBC.configuration.folderName": "FI Pipeline"},
                )
            ],
        )

        writeback = writeback_create_config_in_manifest(
            manifest=manifest,
            component_id="keboola.snowflake-transformation",
            branch_id=12345,
            config_path_str="transformation/keboola.snowflake-transformation/01_stage",
            new_id="123456789",
            file_hash="abc123",
            cfg_hash="def456",
        )

        entry = writeback.entry
        assert writeback.previous_id == "PLACEHOLDER-TX1", (
            "previous_id must capture the pre-overwrite placeholder for remapping"
        )
        assert len(manifest.configurations) == 1, "must not append a duplicate"
        assert entry.id == "123456789"
        assert entry.branch_id == 12345
        assert entry.metadata["pull_hash"] == "abc123"
        assert entry.metadata["pull_config_hash"] == "def456"
        assert entry.metadata["KBC.configuration.folderName"] == "FI Pipeline", (
            "user-declared KBC.* metadata must survive the writeback"
        )

    def test_writeback_config_does_not_match_across_branches(self, tmp_config_dir: Path) -> None:
        """A placeholder at the same (component_id, path) but a different
        branch must NOT be matched. The new entry is appended; the other
        branch's entry is left untouched."""
        from keboola_agent_cli.sync.manifest import ManifestConfiguration

        # Placeholder for branch 12345 (e.g. main); we push to dev branch 99999.
        manifest = Manifest.model_construct(
            project={"id": 1, "apiHost": "connection.keboola.com"},  # type: ignore[arg-type]
            naming={"config": "{component_type}/{component_id}/{config_name}"},  # type: ignore[arg-type]
            configurations=[
                ManifestConfiguration(
                    branchId=12345,
                    componentId="keboola.snowflake-transformation",
                    id="main-id-001",
                    path="transformation/keboola.snowflake-transformation/01_stage",
                    metadata={"KBC.configuration.folderName": "Main FI"},
                )
            ],
        )

        writeback = writeback_create_config_in_manifest(
            manifest=manifest,
            component_id="keboola.snowflake-transformation",
            branch_id=99999,
            config_path_str="transformation/keboola.snowflake-transformation/01_stage",
            new_id="dev-id-002",
            file_hash="h1",
            cfg_hash="h2",
        )

        entry = writeback.entry
        # A brand-new entry was appended: no placeholder to remap.
        assert writeback.previous_id == ""
        # Two entries: the main-branch one untouched, plus the new dev-branch
        # one we just appended.
        assert len(manifest.configurations) == 2
        main_entry = next(c for c in manifest.configurations if c.branch_id == 12345)
        assert main_entry.id == "main-id-001"
        assert main_entry.metadata == {"KBC.configuration.folderName": "Main FI"}
        # The returned entry is the newly-appended dev-branch one.
        assert entry.branch_id == 99999
        assert entry.id == "dev-id-002"

    def test_writeback_config_appends_when_no_placeholder(self, tmp_config_dir: Path) -> None:
        """If no placeholder exists at the path, append (legacy fallback)."""
        manifest = Manifest.model_construct(
            project={"id": 1, "apiHost": "connection.keboola.com"},  # type: ignore[arg-type]
            naming={"config": "{component_type}/{component_id}/{config_name}"},  # type: ignore[arg-type]
            configurations=[],
        )

        writeback = writeback_create_config_in_manifest(
            manifest=manifest,
            component_id="keboola.ex-http",
            branch_id=0,
            config_path_str="extractor/keboola.ex-http/my-new-config",
            new_id="999",
            file_hash="h1",
            cfg_hash="h2",
        )

        entry = writeback.entry
        assert writeback.previous_id == ""
        assert len(manifest.configurations) == 1
        assert manifest.configurations[0] is entry
        assert entry.id == "999"
        assert entry.metadata == {"pull_hash": "h1", "pull_config_hash": "h2"}

    def test_propagate_kbc_metadata_calls_set_config_metadata(self, tmp_config_dir: Path) -> None:
        """KBC.* keys are POSTed via client.set_config_metadata; bookkeeping
        keys (``pull_hash``, ...) are filtered out."""
        from keboola_agent_cli.sync.manifest import ManifestConfiguration

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

        propagate_kbc_metadata(client, entry, branch_id=99)

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

        entry = ManifestConfiguration(
            branchId=0,
            componentId="x",
            id="y",
            path="z",
            metadata={"pull_hash": "h", "pull_config_hash": "h2"},
        )
        client = MagicMock()

        result = propagate_kbc_metadata(client, entry, branch_id=None)

        client.set_config_metadata.assert_not_called()
        assert result is None

    def test_propagate_kbc_metadata_returns_error_message_on_api_failure(
        self, tmp_config_dir: Path
    ) -> None:
        """A failed metadata POST returns the error message (caller accumulates
        into the push errors list) instead of aborting the push mid-loop."""
        from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
        from keboola_agent_cli.sync.manifest import ManifestConfiguration

        entry = ManifestConfiguration(
            branchId=0,
            componentId="keboola.snowflake-transformation",
            id="cfg-123",
            path="x",
            metadata={"KBC.configuration.folderName": "FI Pipeline"},
        )
        client = MagicMock()
        client.set_config_metadata.side_effect = KeboolaApiError(
            message="metastore 500",
            status_code=500,
            error_code=ErrorCode.API_ERROR,
        )

        result = propagate_kbc_metadata(client, entry, branch_id=None)

        assert result == "metastore 500", (
            "non-fatal metadata failure must return the message for the caller "
            "to accumulate into the push error list"
        )
        client.set_config_metadata.assert_called_once()

    def test_writeback_row_in_place_updates_placeholder(self, tmp_config_dir: Path) -> None:
        """A placeholder row at the same ``path`` is updated in place; parent's
        rows list does not grow."""
        from keboola_agent_cli.sync.manifest import ManifestConfigRow, ManifestConfiguration

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

        row = writeback_create_row_in_manifest(
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

        parent = ManifestConfiguration(
            branchId=0,
            componentId="keboola.ex-http",
            id="cfg-1",
            path="extractor/keboola.ex-http/my-ext",
            rows=[],
        )

        row = writeback_create_row_in_manifest(
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


class TestFreshCreateVariableBinding:
    """Fresh-CREATE variable-link resolution (KFR-03 / KFR-04 / KFR-05).

    A FIIA / scaffold tree emits a ``keboola.variables`` config + its default
    values row + a transformation that cross-references both by placeholder id.
    One ``sync push`` must create all three, remap the row's parent placeholder
    to the assigned ULID, hoist the row ``values``, and rebind the
    transformation's ``variables_id`` / ``variables_values_id`` to ULIDs.
    """

    TX_COMPONENT = "keboola.snowflake-transformation"
    VARS_COMPONENT = "keboola.variables"

    @staticmethod
    def _init(tmp_config_dir: Path, project_root: Path) -> ConfigStore:
        init_client = _make_sync_mock_client(
            verify_token_response=SAMPLE_VERIFY_TOKEN,
            branches_response=SAMPLE_BRANCHES,
        )
        store = setup_single_project(tmp_config_dir)
        SyncService(
            config_store=store,
            client_factory=lambda url, token: init_client,
        ).init_sync(alias="prod", project_root=project_root)
        return store

    def _author_tree(
        self,
        project_root: Path,
        *,
        tx_vars_placeholder: str = "PH-VARS",
        tx_vals_placeholder: str = "PH-VALS",
        vars_manifest_id: str = "PH-VARS",
        vals_manifest_id: str = "PH-VALS",
        with_values_row: bool = True,
    ) -> None:
        """Write the placeholder manifest entries + local files on disk."""
        from keboola_agent_cli.constants import CONFIG_YML_VERSION
        from keboola_agent_cli.sync.manifest import (
            ManifestConfigRow,
            ManifestConfiguration,
            save_manifest,
        )

        manifest = load_manifest(project_root)
        branch_path = manifest.branches[0].path

        vars_path = "variable/keboola.variables/my_vars"
        tx_path = "transformation/keboola.snowflake-transformation/01_stage"

        vars_entry = ManifestConfiguration(
            branchId=12345,
            componentId=self.VARS_COMPONENT,
            id=vars_manifest_id,
            path=vars_path,
        )
        if with_values_row:
            vars_entry.rows.append(
                ManifestConfigRow(id=vals_manifest_id, path="rows/default", metadata={})
            )
        manifest.configurations.append(vars_entry)
        manifest.configurations.append(
            ManifestConfiguration(
                branchId=12345,
                componentId=self.TX_COMPONENT,
                id="PH-TX",
                path=tx_path,
            )
        )
        save_manifest(project_root, manifest)

        vars_dir = project_root / branch_path / vars_path
        vars_dir.mkdir(parents=True)
        (vars_dir / CONFIG_FILENAME).write_text(
            yaml.dump(
                {
                    "version": CONFIG_YML_VERSION,
                    "name": "My Vars",
                    "description": "",
                    "_keboola": {"component_id": self.VARS_COMPONENT, "config_id": ""},
                },
                default_flow_style=False,
            ),
            encoding="utf-8",
        )

        if with_values_row:
            row_dir = vars_dir / "rows" / "default"
            row_dir.mkdir(parents=True)
            (row_dir / CONFIG_FILENAME).write_text(
                yaml.dump(
                    {
                        "version": CONFIG_YML_VERSION,
                        "name": "default",
                        "description": "",
                        # Top-level hoisted values (KFR-04): no _keboola block so
                        # only the explicit component_id can drive the hoist.
                        "values": [{"name": "year", "value": "2016", "type": "string"}],
                    },
                    default_flow_style=False,
                ),
                encoding="utf-8",
            )

        tx_dir = project_root / branch_path / tx_path
        tx_dir.mkdir(parents=True)
        (tx_dir / CONFIG_FILENAME).write_text(
            yaml.dump(
                {
                    "version": CONFIG_YML_VERSION,
                    "name": "01 Stage",
                    "description": "",
                    "parameters": {},
                    "_configuration_extra": {
                        "variables_id": tx_vars_placeholder,
                        "variables_values_id": tx_vals_placeholder,
                    },
                    "_keboola": {"component_id": self.TX_COMPONENT, "config_id": ""},
                },
                default_flow_style=False,
            ),
            encoding="utf-8",
        )

    def _make_create_client(self) -> MagicMock:
        client = _make_sync_mock_client(components_response=[])
        client.verify_token.return_value = SAMPLE_VERIFY_TOKEN

        def fake_create_config(**kwargs: Any) -> dict[str, str]:
            cid = kwargs["component_id"]
            return {"id": "VARS-9" if cid == self.VARS_COMPONENT else "TX-9"}

        client.create_config.side_effect = fake_create_config
        client.create_config_row.return_value = {"id": "VALS-9"}
        client.update_config.return_value = {"id": "TX-9"}
        return client

    def _remote_after_create(self) -> list[dict[str, Any]]:
        """Remote state mirroring the post-push tree (for idempotency)."""
        return [
            {
                "id": self.VARS_COMPONENT,
                "type": "other",
                "configurations": [
                    {
                        "id": "VARS-9",
                        "name": "My Vars",
                        "description": "",
                        "configuration": {},
                        "rows": [
                            {
                                "id": "VALS-9",
                                "name": "default",
                                "description": "",
                                "configuration": {
                                    "values": [{"name": "year", "value": "2016", "type": "string"}]
                                },
                            }
                        ],
                    }
                ],
            },
            {
                "id": self.TX_COMPONENT,
                "type": "transformation",
                "configurations": [
                    {
                        "id": "TX-9",
                        "name": "01 Stage",
                        "description": "",
                        "configuration": {
                            "parameters": {},
                            "variables_id": "VARS-9",
                            "variables_values_id": "VALS-9",
                        },
                        "rows": [],
                    }
                ],
            },
        ]

    def test_push_resolves_bindings_end_to_end(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """One push: 3 creates, row parent remapped, values hoisted, links rebound."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init(tmp_config_dir, project_root)
        self._author_tree(project_root)

        client = self._make_create_client()
        svc = SyncService(config_store=store, client_factory=lambda url, token: client)
        result = svc.push(alias="prod", project_root=project_root)

        assert result["status"] == "pushed"
        assert result["created"] == 3, result
        assert result["errors"] == [], result["errors"]

        # KFR-05: row POSTed against the freshly-assigned parent ULID, not the
        # placeholder. KFR-04: the hoisted values reached the API body.
        client.create_config_row.assert_called_once()
        row_kwargs = client.create_config_row.call_args.kwargs
        assert row_kwargs["config_id"] == "VARS-9", "row parent must be remapped to ULID"
        assert row_kwargs["configuration"].get("values"), "row values must be hoisted"

        # KFR-03: a single update_config PUT rebinds BOTH ids to ULIDs.
        client.update_config.assert_called_once()
        upd = client.update_config.call_args.kwargs
        assert upd["component_id"] == self.TX_COMPONENT
        assert upd["config_id"] == "TX-9"
        assert upd["configuration"]["variables_id"] == "VARS-9"
        assert upd["configuration"]["variables_values_id"] == "VALS-9"
        assert "Resolve variables link" in upd["change_description"]
        # MUST NOT call set_variables (would create a 2nd variables config).
        client.set_variables.assert_not_called()

        # Local file rewritten to ULIDs.
        manifest = load_manifest(project_root)
        tx_entry = next(c for c in manifest.configurations if c.component_id == self.TX_COMPONENT)
        assert tx_entry.id == "TX-9"
        branch_path = manifest.branches[0].path
        tx_local = yaml.safe_load(
            (project_root / branch_path / tx_entry.path / CONFIG_FILENAME).read_text("utf-8")
        )
        assert tx_local["_configuration_extra"]["variables_id"] == "VARS-9"
        assert tx_local["_configuration_extra"]["variables_values_id"] == "VALS-9"

        # Manifest hashes refreshed from the post-rewrite (ULID) state so a
        # re-push is clean: pull_config_hash must equal config_hash(local).
        from keboola_agent_cli.sync.diff_engine import config_hash

        assert tx_entry.metadata["pull_config_hash"] == config_hash(tx_local)

    def test_push_resolves_bindings_idempotent_repush(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A second push over the mutated tree is a no-op (created==0, errors==0)."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init(tmp_config_dir, project_root)
        self._author_tree(project_root)

        svc = SyncService(
            config_store=store, client_factory=lambda url, token: self._make_create_client()
        )
        svc.push(alias="prod", project_root=project_root)

        repush_client = _make_sync_mock_client(components_response=self._remote_after_create())
        repush_svc = SyncService(
            config_store=store, client_factory=lambda url, token: repush_client
        )
        result2 = repush_svc.push(alias="prod", project_root=project_root)

        assert result2.get("created", 0) == 0, result2
        assert result2.get("errors", []) == [], result2
        repush_client.create_config.assert_not_called()
        repush_client.create_config_row.assert_not_called()
        repush_client.update_config.assert_not_called()

    def test_fallback_single_variables_config_binds_with_warning(
        self, tmp_config_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Placeholder mismatch + exactly one created variables config → bind + warn."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init(tmp_config_dir, project_root)
        # Transformation references a placeholder that does NOT match the
        # variables manifest entry's placeholder id.
        self._author_tree(
            project_root,
            tx_vars_placeholder="WRONG-VARS",
            tx_vals_placeholder="WRONG-VALS",
        )

        client = self._make_create_client()
        svc = SyncService(config_store=store, client_factory=lambda url, token: client)
        with caplog.at_level("WARNING"):
            result = svc.push(alias="prod", project_root=project_root)

        assert result["created"] == 3
        assert result["errors"] == []
        client.update_config.assert_called_once()
        upd = client.update_config.call_args.kwargs
        assert upd["configuration"]["variables_id"] == "VARS-9"
        assert upd["configuration"]["variables_values_id"] == "VALS-9"
        assert any("did not match" in r.message for r in caplog.records)

    def test_ambiguous_variables_configs_error_no_broken_link(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """>1 created variables configs + no placeholder match → error, no PUT."""
        from keboola_agent_cli.constants import CONFIG_YML_VERSION
        from keboola_agent_cli.sync.manifest import ManifestConfiguration, save_manifest

        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init(tmp_config_dir, project_root)
        # Two variables configs, transformation points at a non-matching id.
        self._author_tree(
            project_root,
            tx_vars_placeholder="WRONG-VARS",
            tx_vals_placeholder="WRONG-VALS",
        )
        manifest = load_manifest(project_root)
        branch_path = manifest.branches[0].path
        second_vars_path = "variable/keboola.variables/other_vars"
        manifest.configurations.append(
            ManifestConfiguration(
                branchId=12345,
                componentId=self.VARS_COMPONENT,
                id="PH-VARS-2",
                path=second_vars_path,
            )
        )
        save_manifest(project_root, manifest)
        other_dir = project_root / branch_path / second_vars_path
        other_dir.mkdir(parents=True)
        (other_dir / CONFIG_FILENAME).write_text(
            yaml.dump(
                {
                    "version": CONFIG_YML_VERSION,
                    "name": "Other Vars",
                    "description": "",
                    "_keboola": {"component_id": self.VARS_COMPONENT, "config_id": ""},
                },
                default_flow_style=False,
            ),
            encoding="utf-8",
        )

        client = self._make_create_client()
        svc = SyncService(config_store=store, client_factory=lambda url, token: client)
        result = svc.push(alias="prod", project_root=project_root)

        # No variables link PUT happened (the create-pass update_config for the
        # backfill is the only update_config we guard against).
        client.update_config.assert_not_called()
        assert any(e.get("change_type") == "variable_link" for e in result["errors"]), result[
            "errors"
        ]

    def test_resolve_source_branch_path_promotes_default_tree(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """KFR-07: missing target-branch subtree → read from the default tree."""
        from keboola_agent_cli.constants import CONFIG_YML_VERSION
        from keboola_agent_cli.sync.manifest import ManifestBranch, save_manifest

        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init(tmp_config_dir, project_root)
        svc = SyncService(config_store=store, client_factory=lambda url, token: MagicMock())

        manifest = load_manifest(project_root)
        default_path = manifest.branches[0].path
        # Register a dev branch WITHOUT a materialized subtree on disk.
        manifest.branches.append(ManifestBranch(id=99999, path="feature-x"))
        save_manifest(project_root, manifest)

        # Default tree has at least one config on disk.
        cfg_dir = project_root / default_path / "extractor/keboola.ex-http/c"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / CONFIG_FILENAME).write_text(
            yaml.dump({"version": CONFIG_YML_VERSION, "name": "c"}, default_flow_style=False),
            encoding="utf-8",
        )

        # No feature-x/ subtree → falls back to the default tree.
        assert svc._resolve_source_branch_path(manifest, project_root, 99999) == default_path

        # Materialize the dev-branch subtree with a config → it becomes source.
        dev_cfg_dir = project_root / "feature-x" / "extractor/keboola.ex-http/c"
        dev_cfg_dir.mkdir(parents=True)
        (dev_cfg_dir / CONFIG_FILENAME).write_text(
            yaml.dump({"version": CONFIG_YML_VERSION, "name": "c"}, default_flow_style=False),
            encoding="utf-8",
        )
        assert svc._resolve_source_branch_path(manifest, project_root, 99999) == "feature-x"


# ---------------------------------------------------------------------------
# Ergonomics: --branch override + --no-name-drift-warnings (v0.47.0)
# ---------------------------------------------------------------------------


class TestBranchOverrideAndNameDriftFlag:
    """Cover the `--branch` override (push / pull / diff) and the
    `--no-name-drift-warnings` opt-out at the service boundary."""

    def test_resolve_branch_id_override_wins(self, tmp_path: Path) -> None:
        from keboola_agent_cli.sync.manifest import (
            ManifestBranch,
            ManifestNaming,
            ManifestProject,
        )

        project = MagicMock()
        project.active_branch_id = 12345
        manifest = Manifest.model_construct(
            project=ManifestProject(id=1, apiHost="connection.keboola.com"),
            naming=ManifestNaming(),
            branches=[ManifestBranch(id=999, path="main", metadata={})],
        )

        # Without override -> falls back to active_branch_id (priority 2).
        assert (
            SyncService._resolve_branch_id(project, manifest, tmp_path, branch_override=None)
            == 12345
        )
        # Override wins (priority 0).
        assert (
            SyncService._resolve_branch_id(project, manifest, tmp_path, branch_override=388071)
            == 388071
        )

    def test_push_branch_override_reaches_client(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """push(branch_override=X) must thread X into list_components_with_configs."""
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

        push_client = _make_sync_mock_client(components_response=[])
        push_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: push_client,
        )

        push_svc.push(alias="prod", project_root=project_root, branch_override=99999)

        push_client.list_components_with_configs.assert_called()
        call = push_client.list_components_with_configs.call_args
        assert call.kwargs.get("branch_id") == 99999

    def test_diff_branch_override_reaches_client(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
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

        diff_client = _make_sync_mock_client(components_response=[])
        diff_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: diff_client,
        )

        diff_svc.diff(alias="prod", project_root=project_root, branch_override=77777)

        diff_client.list_components_with_configs.assert_called()
        call = diff_client.list_components_with_configs.call_args
        assert call.kwargs.get("branch_id") == 77777

    def test_no_name_drift_warnings_flag_suppresses_field(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """When name drift is detected, the suppression flag drops the
        ``name_drift_warnings`` array from the result envelope."""
        from keboola_agent_cli.constants import CONFIG_YML_VERSION
        from keboola_agent_cli.sync.manifest import (
            ManifestConfiguration,
            save_manifest,
        )

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

        # Author a tracked manifest entry whose dirname does NOT match the
        # config name -> name-drift detector will surface a warning.
        manifest = load_manifest(project_root)
        cfg_path = "transformation/keboola.snowflake-transformation/dir-name-NEQ-config-name"
        manifest.configurations.append(
            ManifestConfiguration(
                branchId=12345,
                componentId="keboola.snowflake-transformation",
                id="01abc",
                path=cfg_path,
                # No pull_hash -> diff falls into 2-way mode and any
                # difference is classified "modified" (a pushable change),
                # so the name-drift detector actually runs end-to-end.
                metadata={},
            )
        )
        save_manifest(project_root, manifest)
        branch_path = manifest.branches[0].path
        config_dir = project_root / branch_path / cfg_path
        config_dir.mkdir(parents=True)
        (config_dir / CONFIG_FILENAME).write_text(
            yaml.dump(
                {
                    "version": CONFIG_YML_VERSION,
                    "name": "Some Pretty Config Name",
                    "description": "",
                    "parameters": {"x": "y_new"},
                    "_keboola": {
                        "component_id": "keboola.snowflake-transformation",
                        "config_id": "01abc",
                    },
                },
                default_flow_style=False,
            ),
            encoding="utf-8",
        )

        # Remote returns a stale param value so the diff sees a "modified"
        # change and the push actually enters the warning-emitting path.
        push_client = _make_sync_mock_client(
            components_response=[
                {
                    "id": "keboola.snowflake-transformation",
                    "type": "transformation",
                    "configurations": [
                        {
                            "id": "01abc",
                            "name": "Some Pretty Config Name",
                            "description": "",
                            "configuration": {"parameters": {"x": "y_old"}},
                            "rows": [],
                        }
                    ],
                }
            ],
        )
        push_client.update_config.return_value = {"id": "01abc"}
        push_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: push_client,
        )

        default_result = push_svc.push(alias="prod", project_root=project_root)
        assert "name_drift_warnings" in default_result, (
            "control: the warning must surface without the suppression flag"
        )

        suppressed_result = push_svc.push(
            alias="prod",
            project_root=project_root,
            no_name_drift_warnings=True,
        )
        assert "name_drift_warnings" not in suppressed_result, (
            "--no-name-drift-warnings must drop the field from the envelope"
        )


# ===================================================================
# Issue #649 regression tests
# ===================================================================


DEV_ONLY_COMPONENTS = [
    {
        "id": "keboola.ex-http",
        "type": "extractor",
        "configurations": [
            # Inherited from production -- same id, same content.
            {
                "id": "cfg-001",
                "name": "My HTTP Extractor",
                "description": "Fetches data",
                "configuration": {
                    "parameters": {"baseUrl": "https://api.example.com"},
                },
                "rows": [],
            },
            # Created ON the dev branch -- exists nowhere in production.
            {
                "id": "cfg-900",
                "name": "Branch Only Extractor",
                "description": "",
                "configuration": {
                    "parameters": {"baseUrl": "https://branch.example.com"},
                },
                "rows": [],
            },
        ],
    },
]


class TestIssue649ProductionDiffAfterBranchPull:
    """Regression coverage for issue #649.

    ``sync pull --branch <dev>`` re-targets every ``manifest.configurations``
    entry to the dev branch and materializes a ``<branch>/`` subtree, leaving
    the default ``main/`` tree orphaned on disk. A subsequent **production**
    diff/push then hit two independent bugs:

    1. every file in the orphaned ``main/`` tree surfaced as ``added`` with an
       empty ``config_id`` -- the adopt-by-id guard (#482) refused to adopt
       because the id was "claimed" by a manifest entry, without noticing the
       claim came from a *different branch*;
    2. dev-only configs (tracked on the dev branch, absent from production)
       surfaced as ``added`` **with** an id, so push would recreate them in
       production.

    Both made ``sync push`` a mass-duplicate factory against production.
    """

    def _init_and_pull_main(
        self,
        tmp_config_dir: Path,
        project_root: Path,
        components: list,
    ) -> ConfigStore:
        """init + pull production so ``main/`` is materialized and tracked."""
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

        pull_client = _make_sync_mock_client(components_response=components)
        pull_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )
        pull_svc.pull(alias="prod", project_root=project_root)
        return store

    def _pull_dev_branch(
        self,
        store: ConfigStore,
        project_root: Path,
        components: list,
    ) -> None:
        """Pull the dev branch, re-targeting the manifest and orphaning main/."""
        dev_client = _make_sync_mock_client(
            components_response=components,
            branches_response=SAMPLE_BRANCHES_WITH_DEV,
        )
        dev_svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: dev_client,
        )
        dev_svc.pull(alias="prod", project_root=project_root, branch_override=99999)

    def _reproduce(self, tmp_config_dir: Path, tmp_path: Path) -> tuple[ConfigStore, Path]:
        """Replay the issue's repro up to the point of the production diff."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init_and_pull_main(tmp_config_dir, project_root, SAMPLE_COMPONENTS_NO_ROWS)
        self._pull_dev_branch(store, project_root, DEV_ONLY_COMPONENTS)

        # Bug precondition: the manifest tracks ONLY the dev branch while both
        # trees sit on disk.
        manifest = load_manifest(project_root)
        assert {cfg.branch_id for cfg in manifest.configurations} == {99999}
        assert (project_root / "main").is_dir()
        assert (project_root / "feature-x").is_dir()
        return store, project_root

    def test_production_diff_plans_no_creates(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """The issue's exact repro: a production diff after a dev pull must not
        classify the orphaned ``main/`` tree (or the dev-only configs) as
        ``added``."""
        store, project_root = self._reproduce(tmp_config_dir, tmp_path)

        client = _make_sync_mock_client(components_response=SAMPLE_COMPONENTS_NO_ROWS)
        svc = SyncService(config_store=store, client_factory=lambda url, token: client)
        diff_result = svc.diff(alias="prod", project_root=project_root)

        assert diff_result["summary"]["added"] == 0
        assert [c for c in diff_result["changes"] if c["change_type"] == "added"] == []
        # The orphaned main/ file carries the production id -> adopted and
        # compared against the production remote instead of duplicated.
        assert diff_result["summary"]["unchanged"] == 1
        assert diff_result["summary"]["deleted"] == 0

    def test_production_diff_reports_orphans(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """Both manifest entries land in the new ``orphaned`` bucket: they are
        tracked on the dev branch, not on the branch being diffed."""
        store, project_root = self._reproduce(tmp_config_dir, tmp_path)

        client = _make_sync_mock_client(components_response=SAMPLE_COMPONENTS_NO_ROWS)
        svc = SyncService(config_store=store, client_factory=lambda url, token: client)
        diff_result = svc.diff(alias="prod", project_root=project_root)

        orphaned = diff_result["orphaned"]
        assert diff_result["summary"]["orphaned"] == len(orphaned) == 2
        by_id = {item["config_id"]: item for item in orphaned}
        assert set(by_id) == {"cfg-001", "cfg-900"}
        for item in orphaned:
            assert item["branch_id"] == 99999
            assert item["reason"] == "tracked_on_other_branch"
            assert item["component_id"] == "keboola.ex-http"
            assert item["path"]
        # cfg-900 lives only on the dev branch -> the hint points at branch merge.
        assert by_id["cfg-900"]["exists_on_target"] is False
        assert "branch merge" in by_id["cfg-900"]["hint"]
        assert by_id["cfg-001"]["exists_on_target"] is True
        assert "sync pull" in by_id["cfg-001"]["hint"]

    def test_production_push_creates_nothing(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """push inherits the diff's classification: nothing is POSTed, and the
        orphan warning rides along on the result envelope."""
        store, project_root = self._reproduce(tmp_config_dir, tmp_path)

        client = _make_sync_mock_client(components_response=SAMPLE_COMPONENTS_NO_ROWS)
        svc = SyncService(config_store=store, client_factory=lambda url, token: client)
        push_result = svc.push(alias="prod", project_root=project_root)

        assert push_result["status"] == "no_changes"
        assert push_result["created"] == 0
        client.create_config.assert_not_called()
        client.delete_config.assert_not_called()
        assert len(push_result["orphaned"]) == 2

    def test_production_push_dry_run_plans_nothing(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """``sync push --dry-run`` is the surface a user checks before the real
        push -- it must agree with the diff."""
        store, project_root = self._reproduce(tmp_config_dir, tmp_path)

        client = _make_sync_mock_client(components_response=SAMPLE_COMPONENTS_NO_ROWS)
        svc = SyncService(config_store=store, client_factory=lambda url, token: client)
        push_result = svc.push(alias="prod", project_root=project_root, dry_run=True)

        assert push_result["status"] == "no_changes"
        assert push_result.get("changes", []) == []

    def test_stale_tree_file_gone_from_remote_is_orphaned_not_added(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A file in the orphaned tree whose id no longer resolves on the target
        branch cannot be adopted -- it must be reported, never re-created."""
        store, project_root = self._reproduce(tmp_config_dir, tmp_path)

        # Production no longer has cfg-001 (deleted remotely in the meantime),
        # so the orphaned main/ file has nothing to adopt.
        client = _make_sync_mock_client(
            components_response=[{**SAMPLE_COMPONENTS_NO_ROWS[0], "configurations": []}],
        )
        svc = SyncService(config_store=store, client_factory=lambda url, token: client)
        diff_result = svc.diff(alias="prod", project_root=project_root)

        assert diff_result["summary"]["added"] == 0
        stale = [
            item
            for item in diff_result["orphaned"]
            if item["reason"] == "stale_branch_tree" and item["config_id"] == "cfg-001"
        ]
        assert len(stale) == 1
        assert stale[0]["claimed_branch_ids"] == [99999]
        assert stale[0]["path"]

        push_result = svc.push(alias="prod", project_root=project_root)
        assert push_result["created"] == 0
        client.create_config.assert_not_called()

    def test_dev_branch_diff_is_unaffected(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """Control: the dev-branch side of the same working tree keeps behaving
        exactly as it did before (issue #482's acceptance)."""
        store, project_root = self._reproduce(tmp_config_dir, tmp_path)

        client = _make_sync_mock_client(
            components_response=DEV_ONLY_COMPONENTS,
            branches_response=SAMPLE_BRANCHES_WITH_DEV,
        )
        svc = SyncService(config_store=store, client_factory=lambda url, token: client)
        diff_result = svc.diff(alias="prod", project_root=project_root, branch_override=99999)

        assert diff_result["changes"] == []
        assert diff_result["summary"]["added"] == 0
        assert diff_result["orphaned"] == []

    def test_kfr07_promote_default_tree_to_dev_branch_still_creates(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """KFR-07 must survive the fix: with no ``<branch>/`` subtree on disk the
        production tree is the source for a ``--branch`` push, and a config
        missing on the target branch is still CREATED there."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init_and_pull_main(tmp_config_dir, project_root, SAMPLE_COMPONENTS_NO_ROWS)

        # The dev branch does not carry cfg-001 yet -> promoting the default
        # tree must create it there.
        client = _make_sync_mock_client(
            components_response=[{**SAMPLE_COMPONENTS_NO_ROWS[0], "configurations": []}],
            branches_response=SAMPLE_BRANCHES_WITH_DEV,
        )
        client.create_config.return_value = {"id": "cfg-new-on-branch"}
        svc = SyncService(config_store=store, client_factory=lambda url, token: client)

        diff_result = svc.diff(alias="prod", project_root=project_root, branch_override=99999)
        assert diff_result["summary"]["added"] == 1
        assert diff_result["orphaned"] == []

        push_result = svc.push(alias="prod", project_root=project_root, branch_override=99999)
        assert push_result["created"] == 1
        client.create_config.assert_called_once()

    def test_legacy_zero_branch_id_is_not_an_orphan(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Production is spelled three ways in the wild -- ``None`` (CLI), ``0``
        (git-branching pull: ``branchId=branch_id or 0``) and the default
        branch's numeric id (plain pull). All three name the SAME tree, so a
        legacy ``branchId: 0`` entry must stay in the diff, not be reported as
        tracked on another branch."""
        from keboola_agent_cli.sync.manifest import save_manifest

        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init_and_pull_main(tmp_config_dir, project_root, SAMPLE_COMPONENTS_NO_ROWS)

        manifest = load_manifest(project_root)
        for cfg in manifest.configurations:
            cfg.branch_id = 0
        save_manifest(project_root, manifest)

        client = _make_sync_mock_client(components_response=SAMPLE_COMPONENTS_NO_ROWS)
        svc = SyncService(config_store=store, client_factory=lambda url, token: client)
        diff_result = svc.diff(alias="prod", project_root=project_root)

        assert diff_result["orphaned"] == []
        assert diff_result["summary"]["added"] == 0
        assert diff_result["summary"]["unchanged"] == 1

    def test_branch_tree_path_normalizes_every_spelling_of_production(self, tmp_path: Path) -> None:
        """Unit-level lock on the normalizer the whole fix compares with."""
        from keboola_agent_cli.sync.branch_scope import branch_tree_path
        from keboola_agent_cli.sync.manifest import (
            ManifestBranch,
            ManifestNaming,
            ManifestProject,
        )

        manifest = Manifest.model_construct(
            project=ManifestProject(id=1, apiHost="connection.keboola.com"),
            naming=ManifestNaming(),
            branches=[
                ManifestBranch(id=12345, path="main", metadata={}),
                ManifestBranch(id=99999, path="feature-x", metadata={}),
            ],
        )

        assert branch_tree_path(manifest, None) == "main"
        assert branch_tree_path(manifest, 0) == "main"
        assert branch_tree_path(manifest, 12345) == "main"
        assert branch_tree_path(manifest, 99999) == "feature-x"
        # Unregistered id -> default tree (same fallback _find_branch_path had).
        assert branch_tree_path(manifest, 4242) == "main"

    def test_orphan_bucket_stays_empty_on_a_healthy_tree(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """No orphans on an ordinary single-branch working tree -- the new keys
        are additive and stay empty."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init_and_pull_main(tmp_config_dir, project_root, SAMPLE_COMPONENTS_NO_ROWS)

        client = _make_sync_mock_client(components_response=SAMPLE_COMPONENTS_NO_ROWS)
        svc = SyncService(config_store=store, client_factory=lambda url, token: client)
        diff_result = svc.diff(alias="prod", project_root=project_root)

        assert diff_result["orphaned"] == []
        assert diff_result["summary"]["orphaned"] == 0
        assert diff_result["summary"]["added"] == 0

        push_result = svc.push(alias="prod", project_root=project_root)
        assert "orphaned" not in push_result

    def test_config_new_push_scaffold_is_adopted_not_recreated(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Interaction with issue #644: ``config new --push --output-dir``
        stamps the created config's id into the scaffold and places it in the
        target branch's subtree. That file is untracked and unclaimed, so the
        adopt-by-id path must still adopt it -- the branch-scoped claim check
        must not turn "created via the API, mirrored to disk" into a duplicate
        create. The manifest entries left on the production tree are reported,
        never diffed against the dev branch."""
        from keboola_agent_cli.sync.manifest import ManifestBranch, save_manifest

        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init_and_pull_main(tmp_config_dir, project_root, SAMPLE_COMPONENTS_NO_ROWS)

        # #644 placement: branch subtree registered + scaffold written there,
        # carrying the id the API assigned on create.
        manifest = load_manifest(project_root)
        manifest.branches.append(ManifestBranch(id=99999, path="feature-x"))
        save_manifest(project_root, manifest)
        scaffold_dir = project_root / "feature-x" / "extractor/keboola.ex-http/branch-scaffold"
        scaffold_dir.mkdir(parents=True)
        (scaffold_dir / CONFIG_FILENAME).write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "name": "Branch Scaffold",
                    "parameters": {"baseUrl": "https://scaffold.example.com"},
                    "_keboola": {
                        "component_id": "keboola.ex-http",
                        "config_id": "cfg-644",
                    },
                }
            )
        )

        # The dev branch inherits cfg-001 and carries the freshly created
        # cfg-644 (identical content -> the scaffold is in sync with it).
        client = _make_sync_mock_client(
            components_response=[
                {
                    **SAMPLE_COMPONENTS_NO_ROWS[0],
                    "configurations": [
                        *SAMPLE_COMPONENTS_NO_ROWS[0]["configurations"],
                        {
                            "id": "cfg-644",
                            "name": "Branch Scaffold",
                            "description": "",
                            "configuration": {
                                "parameters": {"baseUrl": "https://scaffold.example.com"},
                            },
                            "rows": [],
                        },
                    ],
                }
            ],
            branches_response=SAMPLE_BRANCHES_WITH_DEV,
        )
        svc = SyncService(config_store=store, client_factory=lambda url, token: client)
        diff_result = svc.diff(alias="prod", project_root=project_root, branch_override=99999)

        # Adopted by id -> no create planned for the scaffold.
        assert diff_result["summary"]["added"] == 0
        assert [c for c in diff_result["changes"] if c["change_type"] == "added"] == []
        # cfg-001 is tracked on the production tree, which is NOT the tree this
        # diff reads -> reported, not diffed against the dev branch.
        assert [item["config_id"] for item in diff_result["orphaned"]] == ["cfg-001"]

        push_result = svc.push(alias="prod", project_root=project_root, branch_override=99999)
        client.create_config.assert_not_called()
        assert push_result["created"] == 0


# ===================================================================
# Issue #689: ignored components (hardcoded + manifest-declared)
# ===================================================================

# Component id used as the "user adds this to ignoredComponents" subject. It is
# deliberately NOT in ALWAYS_IGNORED_COMPONENTS, so every assertion below about
# it proves the manifest field is honored rather than the hardcoded set.
IGNORED_CANDIDATE = "custom.ignore-me"

MCP_TOOL_COMPONENT: dict[str, Any] = {
    "id": "keboola.mcp-server-tool",
    "type": "application",
    "configurations": [
        {
            "id": "mcp-001",
            "name": "MCP Server Tool",
            "description": "",
            "configuration": {},
            "rows": [],
        }
    ],
}


def _candidate_component(base_url: str = "https://api.example.com") -> dict[str, Any]:
    """A normal, syncable component -- until the manifest says otherwise."""
    return {
        "id": IGNORED_CANDIDATE,
        "type": "extractor",
        "configurations": [
            {
                "id": "cfg-777",
                "name": "Ignore Me",
                "description": "",
                "configuration": {"parameters": {"baseUrl": base_url}},
                "rows": [],
            }
        ],
    }


def _set_manifest_ignored(project_root: Path, component_ids: list[str]) -> None:
    """Write ``ignoredComponents`` straight into the manifest on disk."""
    manifest_path = project_root / KEBOOLA_DIR_NAME / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["ignoredComponents"] = component_ids
    manifest_path.write_text(json.dumps(data, indent=4), encoding="utf-8")


_STALE_ENTRY_PATH = "application/keboola.mcp-server-tool/mcp-server-tool"


def _inject_stale_entry(project_root: Path) -> None:
    """Recreate what a pre-#689 kbagent left in a synced tree.

    Manifest entry AND materialized directory for a component that today's
    fetch filters out. Hand-built on purpose: no current code path can produce
    it, which is exactly why it must be tested -- every tree pulled before the
    fix carries one per project.
    """
    manifest_path = project_root / KEBOOLA_DIR_NAME / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    branch_id = data["branches"][0]["id"]
    data["configurations"].append(
        {
            "branchId": branch_id,
            "componentId": MCP_TOOL_COMPONENT["id"],
            "id": "mcp-001",
            "path": _STALE_ENTRY_PATH,
            "metadata": {},
            "rows": [],
        }
    )
    manifest_path.write_text(json.dumps(data, indent=4), encoding="utf-8")

    config_dir = project_root / data["branches"][0]["path"] / _STALE_ENTRY_PATH
    config_dir.mkdir(parents=True)
    (config_dir / CONFIG_FILENAME).write_text(
        yaml.dump(
            {
                "name": "MCP Server Tool",
                "_keboola": {
                    "component_id": MCP_TOOL_COMPONENT["id"],
                    "config_id": "mcp-001",
                },
                "parameters": {},
            }
        ),
        encoding="utf-8",
    )


class TestIssue689IgnoredComponents:
    """Regression coverage for issue #689.

    Two gaps, one shared failure mode. ``keboola.mcp-server-tool`` workspace
    records were pulled into every tree as noise, and the manifest's
    ``ignoredComponents`` field -- declared in the schema since v3 -- was read
    by nothing. Both leave the local and remote sides of ``diff`` disagreeing
    about what exists: a manifest entry whose remote counterpart is filtered
    out classifies as ``deleted``, and ``sync push`` then deletes a live
    production configuration.
    """

    def _init(self, tmp_config_dir: Path, project_root: Path) -> ConfigStore:
        init_client = _make_sync_mock_client(
            verify_token_response=SAMPLE_VERIFY_TOKEN,
            branches_response=SAMPLE_BRANCHES,
        )
        store = setup_single_project(tmp_config_dir)
        SyncService(
            config_store=store,
            client_factory=lambda url, token: init_client,
        ).init_sync(alias="prod", project_root=project_root)
        return store

    def _svc(self, store: ConfigStore, components: list) -> SyncService:
        client = _make_sync_mock_client(components_response=components)
        svc = SyncService(config_store=store, client_factory=lambda url, token: client)
        # Tests assert on the mock, so keep it reachable from the service.
        svc._test_client = client  # type: ignore[attr-defined]
        return svc

    def _tracked_candidate(
        self,
        tmp_config_dir: Path,
        project_root: Path,
    ) -> ConfigStore:
        """init + pull with the candidate still syncable, then ignore it."""
        store = self._init(tmp_config_dir, project_root)
        components = [*SAMPLE_COMPONENTS_NO_ROWS, _candidate_component()]
        self._svc(store, components).pull(alias="prod", project_root=project_root)

        manifest = load_manifest(project_root)
        assert IGNORED_CANDIDATE in {cfg.component_id for cfg in manifest.configurations}

        _set_manifest_ignored(project_root, [IGNORED_CANDIDATE])
        return store

    # -- pull ---------------------------------------------------------

    def test_pull_skips_mcp_server_tool(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """The MCP server's workspace record is never materialized or tracked."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init(tmp_config_dir, project_root)

        result = self._svc(store, [*SAMPLE_COMPONENTS_NO_ROWS, MCP_TOOL_COMPONENT]).pull(
            alias="prod", project_root=project_root
        )

        assert result["configs_pulled"] == 1
        manifest = load_manifest(project_root)
        assert {cfg.component_id for cfg in manifest.configurations} == {"keboola.ex-http"}
        assert not list(project_root.rglob("*mcp-server-tool*"))

    def test_pull_honors_manifest_ignored_components(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A component named in ``ignoredComponents`` is skipped like a hardcoded one."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init(tmp_config_dir, project_root)
        _set_manifest_ignored(project_root, [IGNORED_CANDIDATE])

        result = self._svc(store, [*SAMPLE_COMPONENTS_NO_ROWS, _candidate_component()]).pull(
            alias="prod", project_root=project_root
        )

        assert result["configs_pulled"] == 1
        manifest = load_manifest(project_root)
        assert {cfg.component_id for cfg in manifest.configurations} == {"keboola.ex-http"}
        assert manifest.ignored_components == [IGNORED_CANDIDATE]

    def test_pull_reports_newly_ignored_entry_as_ignored_not_removed(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A tracked entry dropped because its component became ignored is
        reported as ``ignored`` -- ``removed`` would claim the remote deleted a
        config that is still very much there."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._tracked_candidate(tmp_config_dir, project_root)
        tracked_path = next(
            cfg.path
            for cfg in load_manifest(project_root).configurations
            if cfg.component_id == IGNORED_CANDIDATE
        )
        assert (project_root / "main" / tracked_path).is_dir()

        result = self._svc(store, [*SAMPLE_COMPONENTS_NO_ROWS, _candidate_component()]).pull(
            alias="prod", project_root=project_root
        )

        actions = {d["action"] for d in result["details"] if d["component_id"] == IGNORED_CANDIDATE}
        assert actions == {"ignored"}
        assert not [d for d in result["details"] if d["action"] == "removed"]
        # Manifest entry gone and the directory cleaned up, exactly as for a
        # genuine removal -- git keeps the deletion reviewable.
        manifest = load_manifest(project_root)
        assert IGNORED_CANDIDATE not in {cfg.component_id for cfg in manifest.configurations}
        assert not (project_root / "main" / tracked_path).exists()

    def test_force_pull_conflict_guard_skips_ignored(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A locally-modified ignored config whose remote also changed is not a
        conflict: ``--force`` must not abort over a config nobody syncs."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._tracked_candidate(tmp_config_dir, project_root)

        tracked_path = next(
            cfg.path
            for cfg in load_manifest(project_root).configurations
            if cfg.component_id == IGNORED_CANDIDATE
        )
        config_file = project_root / "main" / tracked_path / CONFIG_FILENAME
        local_data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        local_data["parameters"]["baseUrl"] = "https://local-edit.example.com"
        config_file.write_text(yaml.dump(local_data, default_flow_style=False), encoding="utf-8")

        # Remote changed too -> a 3-way conflict, were the component synced.
        remote = [*SAMPLE_COMPONENTS_NO_ROWS, _candidate_component("https://remote-edit.example")]
        result = self._svc(store, remote).pull(alias="prod", project_root=project_root, force=True)

        assert result["status"] == "pulled"

    # -- diff / push --------------------------------------------------

    def test_diff_excludes_stale_ignored_entry(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """A stale manifest entry for an ignored component contributes nothing
        to the changeset and is not reported as an orphan.

        This is the dangerous half of #689: the entry is left behind by an
        older kbagent that still pulled the component, so the LOCAL side knows
        it while the REMOTE side now filters it out. The diff engine flags a
        local entry with no remote counterpart as ``added`` -- with its
        existing config id -- and push then CREATES a duplicate of a live
        config, once per push.
        """
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init(tmp_config_dir, project_root)
        self._svc(store, SAMPLE_COMPONENTS_NO_ROWS).pull(alias="prod", project_root=project_root)
        _inject_stale_entry(project_root)

        diff_result = self._svc(store, [*SAMPLE_COMPONENTS_NO_ROWS, MCP_TOOL_COMPONENT]).diff(
            alias="prod", project_root=project_root
        )

        assert diff_result["summary"]["added"] == 0
        assert diff_result["summary"]["deleted"] == 0
        assert diff_result["summary"]["orphaned"] == 0
        assert diff_result["orphaned"] == []
        assert all(c["component_id"] != MCP_TOOL_COMPONENT["id"] for c in diff_result["changes"])

    def test_diff_excludes_remote_side_of_ignored_component(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Remote configs of an ignored component are not reported as
        ``remote_only`` -- ``sync diff`` must not nag the user to pull configs
        that ``sync pull`` is contractually going to skip."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init(tmp_config_dir, project_root)
        _set_manifest_ignored(project_root, [IGNORED_CANDIDATE])
        # Pull a remote that carries only the syncable component, so nothing
        # local can account for the ignored ones on the next diff.
        self._svc(store, SAMPLE_COMPONENTS_NO_ROWS).pull(alias="prod", project_root=project_root)

        diff_result = self._svc(
            store, [*SAMPLE_COMPONENTS_NO_ROWS, _candidate_component(), MCP_TOOL_COMPONENT]
        ).diff(alias="prod", project_root=project_root)

        assert diff_result["summary"]["added"] == 0
        assert diff_result["summary"]["remote_only"] == 0
        assert diff_result["remote_only"] == []

    def test_push_touches_nothing_for_stale_ignored_entry(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """push builds its changeset from diff, so a stale ignored entry
        reaches neither ``create_config`` nor ``delete_config``."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init(tmp_config_dir, project_root)
        self._svc(store, SAMPLE_COMPONENTS_NO_ROWS).pull(alias="prod", project_root=project_root)
        _inject_stale_entry(project_root)

        svc = self._svc(store, [*SAMPLE_COMPONENTS_NO_ROWS, MCP_TOOL_COMPONENT])
        push_result = svc.push(alias="prod", project_root=project_root, force=True)

        assert push_result["status"] == "no_changes"
        assert push_result["deleted"] == 0
        assert push_result["created"] == 0
        svc._test_client.create_config.assert_not_called()  # type: ignore[attr-defined]
        svc._test_client.delete_config.assert_not_called()  # type: ignore[attr-defined]

    def test_diff_ignores_leftover_untracked_directory(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A directory left on disk for an ignored component (no manifest entry)
        must not be planned as a create -- push would re-add what pull refuses
        to fetch."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        store = self._init(tmp_config_dir, project_root)
        _set_manifest_ignored(project_root, [IGNORED_CANDIDATE])
        self._svc(store, SAMPLE_COMPONENTS_NO_ROWS).pull(alias="prod", project_root=project_root)

        leftover = project_root / "main" / "extractor" / "custom.ignore-me" / "ignore-me"
        leftover.mkdir(parents=True)
        (leftover / CONFIG_FILENAME).write_text(
            yaml.dump(
                {
                    "name": "Ignore Me",
                    "_keboola": {"component_id": IGNORED_CANDIDATE, "config_id": ""},
                    "parameters": {},
                }
            ),
            encoding="utf-8",
        )

        diff_result = self._svc(store, SAMPLE_COMPONENTS_NO_ROWS).diff(
            alias="prod", project_root=project_root
        )

        assert diff_result["summary"]["added"] == 0
        assert all(c["component_id"] != IGNORED_CANDIDATE for c in diff_result["changes"])
