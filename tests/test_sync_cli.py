"""Tests for sync CLI commands via CliRunner.

Tests init, pull, and status subcommands. Follows the existing CLI test
pattern from test_cli.py and test_workspace_cli.py with patched services
in ctx.obj.
"""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError, SyncConflictError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.project_service import ProjectService


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text for assertion matching."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()


def _setup_config(config_dir: Path, projects: dict[str, dict] | None = None) -> ConfigStore:
    """Set up a ConfigStore with given projects for CLI sync tests."""
    store = ConfigStore(config_dir=config_dir)
    if projects:
        for alias, info in projects.items():
            store.add_project(
                alias,
                ProjectConfig(
                    stack_url=info.get("stack_url", "https://connection.keboola.com"),
                    token=info["token"],
                    project_name=info.get("project_name", alias),
                    project_id=info.get("project_id", 1234),
                ),
            )
    return store


def _make_sync_service_mock() -> MagicMock:
    """Create a fresh MagicMock for SyncService."""
    return MagicMock()


# ===================================================================
# Help text tests
# ===================================================================


class TestSyncHelp:
    """Tests for sync subcommand help output."""

    def test_sync_init_help(self) -> None:
        """sync init --help shows usage text."""
        result = runner.invoke(app, ["sync", "init", "--help"])
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "Initialize" in output or "init" in output
        assert "--project" in output
        assert "--directory" in output
        assert "--git-branching" in output

    def test_sync_pull_help(self) -> None:
        """sync pull --help shows usage text."""
        result = runner.invoke(app, ["sync", "pull", "--help"])
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "Download" in output or "pull" in output
        assert "--project" in output
        assert "--directory" in output
        assert "--force" in output

    def test_sync_status_help(self) -> None:
        """sync status --help shows usage text."""
        result = runner.invoke(app, ["sync", "status", "--help"])
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "status" in output.lower()
        assert "--directory" in output


# ===================================================================
# sync init CLI tests
# ===================================================================


class TestSyncInitCli:
    """Tests for `kbagent sync init` command."""

    def test_sync_init_json_output(self, tmp_path: Path) -> None:
        """sync init --json returns structured JSON with init result."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN, "project_id": 258}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.init_sync.return_value = {
            "status": "initialized",
            "project_id": 258,
            "project_alias": "prod",
            "api_host": "connection.keboola.com",
            "git_branching": False,
            "default_branch": "main",
            "files_created": ["/tmp/project/.keboola/manifest.json"],
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    "init",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["status"] == "initialized"
        assert output["data"]["project_id"] == 258
        assert output["data"]["api_host"] == "connection.keboola.com"
        assert output["data"]["git_branching"] is False

    def test_sync_init_human_output(self, tmp_path: Path) -> None:
        """sync init in human mode shows success message."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN, "project_id": 258}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.init_sync.return_value = {
            "status": "initialized",
            "project_id": 258,
            "project_alias": "prod",
            "api_host": "connection.keboola.com",
            "git_branching": False,
            "default_branch": "main",
            "files_created": ["/tmp/project/.keboola/manifest.json"],
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "sync",
                    "init",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "prod" in result.output
        assert "258" in result.output

    def test_sync_init_config_error(self, tmp_path: Path) -> None:
        """sync init returns exit code 5 when project alias is not found."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir)

        mock_sync = _make_sync_service_mock()
        mock_sync.init_sync.side_effect = ConfigError("Project 'missing' not found.")

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    "init",
                    "--project",
                    "missing",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 5

    def test_sync_init_already_exists_error(self, tmp_path: Path) -> None:
        """sync init returns exit code 1 when manifest already exists."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.init_sync.side_effect = FileExistsError(
            "Manifest already exists. Use 'sync pull' to update."
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    "init",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 1


# ===================================================================
# sync pull CLI tests
# ===================================================================


class TestSyncPullCli:
    """Tests for `kbagent sync pull` command."""

    def test_sync_pull_json_output(self, tmp_path: Path) -> None:
        """sync pull --json returns structured JSON with pull stats."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.pull.return_value = {
            "status": "pulled",
            "project_alias": "prod",
            "branch_id": 12345,
            "branch_dir": "main",
            "configs_pulled": 5,
            "rows_pulled": 3,
            "files_written": 8,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    "pull",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["configs_pulled"] == 5
        assert output["data"]["rows_pulled"] == 3
        assert output["data"]["files_written"] == 8

    def test_sync_pull_human_output(self, tmp_path: Path) -> None:
        """sync pull in human mode shows pulled summary."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.pull.return_value = {
            "status": "pulled",
            "project_alias": "prod",
            "branch_id": 12345,
            "branch_dir": "main",
            "configs_pulled": 3,
            "rows_pulled": 1,
            "files_written": 4,
            "details": [
                {
                    "action": "new",
                    "component_id": "keboola.ex-db-snowflake",
                    "config_name": "My Extractor",
                    "path": "extractor/keboola.ex-db-snowflake/my-extractor",
                },
                {
                    "action": "updated",
                    "component_id": "keboola.snowflake-transformation",
                    "config_name": "Main Transform",
                    "path": "transformation/keboola.snowflake-transformation/main",
                },
            ],
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "sync",
                    "pull",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "3" in result.output  # configs_pulled
        assert "1" in result.output  # rows_pulled
        assert "main" in result.output  # branch_dir

    def test_sync_pull_not_initialized_error(self, tmp_path: Path) -> None:
        """sync pull returns exit code 1 when project not initialized."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.pull.side_effect = FileNotFoundError(
            "Manifest not found. Is this a Keboola project directory?"
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    "pull",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 1

    def test_sync_pull_api_error(self, tmp_path: Path) -> None:
        """sync pull returns appropriate exit code on API error."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.pull.side_effect = KeboolaApiError(
            message="Invalid token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    "pull",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 3  # auth error

    def _conflict_error(self) -> SyncConflictError:
        return SyncConflictError(
            [
                {
                    "scope": "config",
                    "component_id": "keboola.ex-http",
                    "config_id": "cfg-001",
                    "config_name": "My HTTP Extractor",
                    "path": "extractor/keboola.ex-http/my-http-extractor",
                }
            ]
        )

    def test_sync_pull_force_conflict_human(self, tmp_path: Path) -> None:
        """sync pull --force aborts with exit 1 and lists the conflict (human)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_sync = _make_sync_service_mock()
        mock_sync.pull.side_effect = self._conflict_error()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                ["sync", "pull", "--project", "prod", "--force", "--directory", str(tmp_path)],
            )

        assert result.exit_code == 1
        out = _strip_ansi(result.output)
        assert "conflict" in out.lower()
        assert "keboola.ex-http/cfg-001" in out

    def test_sync_pull_force_conflict_json(self, tmp_path: Path) -> None:
        """sync pull --force conflict emits a SYNC_CONFLICT error envelope (JSON)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_sync = _make_sync_service_mock()
        mock_sync.pull.side_effect = self._conflict_error()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    "pull",
                    "--project",
                    "prod",
                    "--force",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "SYNC_CONFLICT"
        assert payload["error"]["details"]["conflicts"][0]["config_id"] == "cfg-001"


# ===================================================================
# sync status CLI tests
# ===================================================================


class TestSyncStatusCli:
    """Tests for `kbagent sync status` command."""

    def test_sync_status_no_changes(self, tmp_path: Path) -> None:
        """sync status shows 'No changes detected' when nothing is modified."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.status.return_value = {
            "modified": [],
            "added": [],
            "deleted": [],
            "unchanged": 5,
            "total_tracked": 5,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "sync",
                    "status",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "No changes detected" in result.output
        assert "5" in result.output  # number of tracked configs

    def test_sync_status_json_output(self, tmp_path: Path) -> None:
        """sync status --json returns structured JSON with change lists."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.status.return_value = {
            "modified": [
                {
                    "component_id": "keboola.ex-http",
                    "config_id": "cfg-001",
                    "path": "extractor/keboola.ex-http/my-config",
                }
            ],
            "added": [],
            "deleted": [
                {
                    "component_id": "keboola.snowflake-transformation",
                    "config_id": "cfg-002",
                    "path": "transformation/keboola.snowflake-transformation/clean-data",
                }
            ],
            "unchanged": 3,
            "total_tracked": 5,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    "status",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        data = output["data"]
        assert len(data["modified"]) == 1
        assert len(data["deleted"]) == 1
        assert data["unchanged"] == 3
        assert data["total_tracked"] == 5

    def test_sync_status_with_changes_human(self, tmp_path: Path) -> None:
        """sync status in human mode shows M/A/D prefixed entries."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.status.return_value = {
            "modified": [
                {
                    "component_id": "keboola.ex-http",
                    "config_id": "cfg-001",
                    "path": "extractor/keboola.ex-http/my-config",
                }
            ],
            "added": [
                {
                    "component_id": "keboola.ex-db",
                    "config_id": "cfg-new",
                    "path": "extractor/keboola.ex-db/new-config",
                }
            ],
            "deleted": [
                {
                    "component_id": "keboola.snowflake-transformation",
                    "config_id": "cfg-002",
                    "path": "transformation/keboola.snowflake-transformation/clean-data",
                }
            ],
            "unchanged": 2,
            "total_tracked": 4,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "sync",
                    "status",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        # Human output should show M, A, D prefixes
        assert "M " in result.output  # Modified
        assert "A " in result.output  # Added
        assert "D " in result.output  # Deleted
        # Should contain summary line
        assert "1 modified" in result.output
        assert "1 added" in result.output
        assert "1 deleted" in result.output

    def test_sync_status_not_initialized_error(self, tmp_path: Path) -> None:
        """sync status returns exit code 1 when project not initialized."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir)

        mock_sync = _make_sync_service_mock()
        mock_sync.status.side_effect = FileNotFoundError(
            "Manifest not found. Is this a Keboola project directory?"
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    "status",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 1


# ===================================================================
# sync diff CLI tests
# ===================================================================


class TestSyncDiffCli:
    """Tests for `kbagent sync diff` command."""

    def test_sync_diff_help(self) -> None:
        """sync diff --help shows usage text."""
        result = runner.invoke(app, ["sync", "diff", "--help"])
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "--project" in output
        assert "--directory" in output

    def test_sync_diff_json_output(self, tmp_path: Path) -> None:
        """sync diff --json returns structured JSON with changes and summary."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.diff.return_value = {
            "changes": [
                {
                    "change_type": "modified",
                    "component_id": "keboola.ex-http",
                    "config_id": "cfg-001",
                    "config_name": "My Config",
                    "path": "extractor/keboola.ex-http/my-config",
                    "details": ["parameters.url changed: 'old' -> 'new'"],
                },
                {
                    "change_type": "added",
                    "component_id": "keboola.wr-snowflake",
                    "config_id": "",
                    "config_name": "New Writer",
                    "path": "writer/keboola.wr-snowflake/new-writer",
                    "details": [],
                },
            ],
            "summary": {
                "added": 1,
                "modified": 1,
                "deleted": 0,
                "unchanged": 3,
            },
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    "diff",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        data = output["data"]
        assert len(data["changes"]) == 2
        assert data["summary"]["added"] == 1
        assert data["summary"]["modified"] == 1
        assert data["summary"]["deleted"] == 0
        assert data["summary"]["unchanged"] == 3

    def test_sync_diff_no_changes_human(self, tmp_path: Path) -> None:
        """sync diff in human mode shows 'No differences found' when no changes."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.diff.return_value = {
            "changes": [],
            "summary": {
                "added": 0,
                "modified": 0,
                "deleted": 0,
                "unchanged": 5,
            },
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "sync",
                    "diff",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "No differences found" in result.output


# ===================================================================
# sync push CLI tests
# ===================================================================


class TestSyncPushCli:
    """Tests for `kbagent sync push` command."""

    def test_sync_push_help(self) -> None:
        """sync push --help shows usage text."""
        result = runner.invoke(app, ["sync", "push", "--help"])
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "--project" in output
        assert "--directory" in output
        assert "--dry-run" in output
        assert "--force" in output

    def test_sync_push_json_output(self, tmp_path: Path) -> None:
        """sync push --json returns structured JSON with push results."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.push.return_value = {
            "status": "pushed",
            "created": 1,
            "updated": 2,
            "deleted": 0,
            "errors": [],
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    "push",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        data = output["data"]
        assert data["status"] == "pushed"
        assert data["created"] == 1
        assert data["updated"] == 2
        assert data["deleted"] == 0
        assert data["errors"] == []

    def test_sync_push_dry_run_human(self, tmp_path: Path) -> None:
        """sync push --dry-run in human mode shows dry run output."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.push.return_value = {
            "status": "dry_run",
            "changes": [
                {
                    "change_type": "modified",
                    "component_id": "keboola.ex-http",
                    "config_id": "cfg-001",
                    "config_name": "My Config",
                    "path": "extractor/keboola.ex-http/my-config",
                    "details": [],
                },
            ],
            "summary": {
                "added": 0,
                "modified": 1,
                "deleted": 0,
                "unchanged": 4,
            },
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "sync",
                    "push",
                    "--project",
                    "prod",
                    "--dry-run",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "Dry run" in result.output or "dry run" in result.output.lower()
        assert "MODIFIED" in result.output

    def test_sync_push_no_changes_human(self, tmp_path: Path) -> None:
        """sync push in human mode shows 'No changes to push' when nothing changed."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.push.return_value = {
            "status": "no_changes",
            "created": 0,
            "updated": 0,
            "deleted": 0,
            "errors": [],
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "sync",
                    "push",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "No changes to push" in result.output

    def test_sync_push_with_row_changes_json(self, tmp_path: Path) -> None:
        """JSON output reflects row-level push results (P0-1).

        Mocks a push that deploys one new row, one updated row, one deleted
        row; asserts the ``pushed_details`` entries preserve ``is_row`` +
        ``parent_config_id`` so ``--json`` consumers can distinguish row ops
        from parent-config ops.
        """
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.push.return_value = {
            "status": "pushed",
            "created": 1,
            "updated": 1,
            "deleted": 1,
            "errors": [],
            "pushed_details": [
                {
                    "change_type": "added",
                    "component_id": "keboola.variables",
                    "config_id": "row-new",
                    "config_name": "new-row",
                    "path": "values/new",
                    "is_row": True,
                    "parent_config_id": "vars-1",
                },
                {
                    "change_type": "modified",
                    "component_id": "keboola.variables",
                    "config_id": "row-1",
                    "config_name": "main",
                    "path": "values/main",
                    "is_row": True,
                    "parent_config_id": "vars-1",
                },
                {
                    "change_type": "deleted",
                    "component_id": "keboola.variables",
                    "config_id": "row-old",
                    "config_name": "old",
                    "path": "values/old",
                    "is_row": True,
                    "parent_config_id": "vars-1",
                },
            ],
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    "push",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        data = output["data"]
        assert data["status"] == "pushed"
        assert data["created"] == 1
        assert data["updated"] == 1
        assert data["deleted"] == 1
        # Row changes carry is_row + parent_config_id so downstream agents can
        # distinguish row ops from parent config ops in the response.
        row_details = [d for d in data["pushed_details"] if d.get("is_row")]
        assert len(row_details) == 3
        assert all(d["parent_config_id"] == "vars-1" for d in row_details)

    def test_sync_push_row_encryption_failure_exits_nonzero(self, tmp_path: Path) -> None:
        """Encryption failure surfaced by the service exits non-zero (exit 1).

        Bundled P1-5 contract: if the service raises ``KeboolaApiError`` with
        ``ENCRYPTION_FAILED`` (e.g. Encryption API unreachable and
        ``--allow-plaintext-on-encrypt-failure`` not set), the CLI maps it to
        exit 1 and emits the error code in the JSON response.
        """
        from keboola_agent_cli.errors import KeboolaApiError

        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.push.side_effect = KeboolaApiError(
            message="Encryption failed for keboola.variables: network error",
            status_code=0,
            error_code="ENCRYPTION_FAILED",
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    "push",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                ],
            )

        # ENCRYPTION_FAILED is an "everything else" error_code in
        # map_error_to_exit_code -> exit 1 (general). Lock that contract.
        assert result.exit_code == 1
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "ENCRYPTION_FAILED"

    def test_sync_push_with_row_changes_human(self, tmp_path: Path) -> None:
        """Human-mode row push prints per-row action labels + summary counts.

        Complements :meth:`test_sync_push_with_row_changes_json` so the CLI
        layer has both output modes covered per best_practices.md §5. Strips
        ANSI so the assertion is stable on CI.
        """
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.push.return_value = {
            "status": "pushed",
            "created": 1,
            "updated": 1,
            "deleted": 0,
            "errors": [],
            "pushed_details": [
                {
                    "change_type": "added",
                    "component_id": "keboola.variables",
                    "config_id": "row-new",
                    "config_name": "new-row",
                    "path": "values/new",
                    "is_row": True,
                    "parent_config_id": "vars-1",
                },
                {
                    "change_type": "modified",
                    "component_id": "keboola.variables",
                    "config_id": "row-1",
                    "config_name": "main",
                    "path": "values/main",
                    "is_row": True,
                    "parent_config_id": "vars-1",
                },
            ],
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "sync",
                    "push",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        text = _strip_ansi(result.output)
        assert "Pushed: 1 created, 1 updated" in text
        assert "ADDED keboola.variables/values/new" in text
        assert "MODIFIED keboola.variables/values/main" in text


# ===================================================================
# sync branch-link / branch-unlink / branch-status CLI tests
# ===================================================================


class TestSyncBranchLinkCli:
    """Tests for `kbagent sync branch-link` command."""

    def test_sync_branch_link_help(self) -> None:
        """sync branch-link --help shows usage text."""
        result = runner.invoke(app, ["sync", "branch-link", "--help"])
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "--project" in output
        assert "--directory" in output
        assert "--branch-id" in output
        assert "--branch-name" in output

    def test_sync_branch_link_json_output(self, tmp_path: Path) -> None:
        """sync branch-link --json returns structured JSON."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.branch_link.return_value = {
            "status": "linked",
            "git_branch": "feature/auth",
            "keboola_branch_id": "99999",
            "keboola_branch_name": "feature/auth",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    "branch-link",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["status"] == "linked"
        assert output["data"]["git_branch"] == "feature/auth"
        assert output["data"]["keboola_branch_id"] == "99999"

    def test_sync_branch_link_config_error(self, tmp_path: Path) -> None:
        """sync branch-link returns exit code 5 on ConfigError."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.branch_link.side_effect = ConfigError("Git-branching mode is not enabled.")

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    "branch-link",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 5

    def test_sync_branch_link_already_linked_human(self, tmp_path: Path) -> None:
        """sync branch-link in human mode shows 'Already linked' for existing mapping."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.branch_link.return_value = {
            "status": "already_linked",
            "git_branch": "feature/auth",
            "keboola_branch_id": "99999",
            "keboola_branch_name": "feature/auth",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "sync",
                    "branch-link",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "Already linked" in result.output


class TestSyncBranchUnlinkCli:
    """Tests for `kbagent sync branch-unlink` command."""

    def test_sync_branch_unlink_help(self) -> None:
        """sync branch-unlink --help shows usage text."""
        result = runner.invoke(app, ["sync", "branch-unlink", "--help"])
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "--directory" in output

    def test_sync_branch_unlink_json_output(self, tmp_path: Path) -> None:
        """sync branch-unlink --json returns structured JSON."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.branch_unlink.return_value = {
            "status": "unlinked",
            "git_branch": "feature/auth",
            "keboola_branch_id": "99999",
            "keboola_branch_name": "feature/auth",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    "branch-unlink",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["status"] == "unlinked"

    def test_sync_branch_unlink_not_linked_human(self, tmp_path: Path) -> None:
        """sync branch-unlink in human mode shows 'not linked' message."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.branch_unlink.return_value = {
            "status": "not_linked",
            "git_branch": "feature/auth",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "sync",
                    "branch-unlink",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "not linked" in result.output


class TestSyncBranchStatusCli:
    """Tests for `kbagent sync branch-status` command."""

    def test_sync_branch_status_help(self) -> None:
        """sync branch-status --help shows usage text."""
        result = runner.invoke(app, ["sync", "branch-status", "--help"])
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "--directory" in output

    def test_sync_branch_status_json_output(self, tmp_path: Path) -> None:
        """sync branch-status --json returns structured JSON."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.branch_status.return_value = {
            "git_branching": True,
            "git_branch": "feature/auth",
            "linked": True,
            "keboola_branch_id": "99999",
            "keboola_branch_name": "feature/auth",
            "is_production": False,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    "branch-status",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["linked"] is True
        assert output["data"]["keboola_branch_id"] == "99999"

    def test_sync_branch_status_not_linked_human(self, tmp_path: Path) -> None:
        """sync branch-status in human mode shows 'Not linked' and hint."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.branch_status.return_value = {
            "git_branching": True,
            "git_branch": "feature/auth",
            "linked": False,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "sync",
                    "branch-status",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "Not linked" in result.output
        assert "branch-link" in result.output

    def test_sync_branch_status_disabled_human(self, tmp_path: Path) -> None:
        """sync branch-status shows 'not enabled' when git branching is off."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.branch_status.return_value = {"git_branching": False}

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "sync",
                    "branch-status",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "not enabled" in result.output

    def test_sync_branch_status_corrupted_mapping_clean_envelope(self, tmp_path: Path) -> None:
        """A corrupted .keboola/branch-mapping.json must produce a clean
        JSON error envelope (exit 5, CONFIG_ERROR), not a Python traceback
        (issue #269 sec-20 follow-up + #273 reviewer feedback)."""
        from keboola_agent_cli.errors import ConfigError

        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_sync = _make_sync_service_mock()
        # Service raises ConfigError as load_branch_mapping now does for
        # malformed mappings. CLI must catch it.
        mock_sync.branch_status.side_effect = ConfigError(
            "Failed to parse /tmp/.keboola/branch-mapping.json: "
            "Invalid branch ID in branch-mapping.json: 'not-a-number'. "
            "Expected null or an integer; got str."
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                ["--json", "sync", "branch-status", "--directory", str(tmp_path)],
            )

        assert result.exit_code == 5, f"expected exit 5, got {result.exit_code}: {result.output}"
        assert "Traceback" not in result.output
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["error"]["code"] == "CONFIG_ERROR"
        assert "Invalid branch ID" in data["error"]["message"]


class TestSyncInitAdoptExistingCli:
    """Tests for `kbagent sync init --adopt-existing`."""

    def test_adopt_existing_flag_present_in_help(self) -> None:
        """sync init --help shows --adopt-existing flag."""
        result = runner.invoke(app, ["sync", "init", "--help"])
        assert result.exit_code == 0
        assert "--adopt-existing" in _strip_ansi(result.output)

    def test_adopt_existing_json_output(self, tmp_path: Path) -> None:
        """sync init --adopt-existing --json returns adopted status."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN, "project_id": 258}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.init_sync.return_value = {
            "status": "adopted",
            "project_id": 258,
            "project_alias": "prod",
            "api_host": "connection.keboola.com",
            "git_branching": False,
            "default_branch": "main",
            "files_created": [],
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    "init",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                    "--adopt-existing",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["status"] == "adopted"
        assert output["data"]["files_created"] == []

        # Verify adopt_existing=True was passed through to the service
        call_kwargs = mock_sync.init_sync.call_args
        assert call_kwargs.kwargs.get("adopt_existing") is True

    def test_adopt_existing_human_output_shows_adopted(self, tmp_path: Path) -> None:
        """sync init --adopt-existing shows 'Adopted' instead of 'Initialized'."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN, "project_id": 258}},
        )

        mock_sync = _make_sync_service_mock()
        mock_sync.init_sync.return_value = {
            "status": "adopted",
            "project_id": 258,
            "project_alias": "prod",
            "api_host": "connection.europe-west3.gcp.keboola.com",
            "git_branching": False,
            "default_branch": "main",
            "files_created": [],
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "sync",
                    "init",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                    "--adopt-existing",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = _strip_ansi(result.output)
        assert "Adopted" in output or "adopted" in output.lower()

    def test_adopt_existing_config_error_exits_5(self, tmp_path: Path) -> None:
        """sync init --adopt-existing returns exit code 5 on project_id mismatch."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN, "project_id": 258}},
        )

        from keboola_agent_cli.errors import ConfigError

        mock_sync = _make_sync_service_mock()
        mock_sync.init_sync.side_effect = ConfigError(
            "Manifest project_id=999 does not match alias 'prod' project_id=258"
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = mock_sync

            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    "init",
                    "--project",
                    "prod",
                    "--adopt-existing",
                    "--directory",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "project_id" in output["error"]["message"]
