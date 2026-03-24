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
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.project_service import ProjectService


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text for assertion matching."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


TEST_TOKEN = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"

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
