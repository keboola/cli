"""Tests for workspace CLI commands via CliRunner.

Tests all workspace subcommands: create, list, detail, delete, password,
load, query, and from-transformation. Follows the existing CLI test pattern
from test_cli.py with patched services in ctx.obj.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"

runner = CliRunner()


def _setup_config(config_dir: Path, projects: dict[str, dict] | None = None) -> ConfigStore:
    """Set up a ConfigStore with given projects for CLI workspace tests."""
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


def _make_workspace_mock() -> MagicMock:
    """Create a fresh MagicMock for WorkspaceService."""
    return MagicMock()


class TestWorkspaceCreate:
    """Tests for `kbagent workspace create` command."""

    def test_workspace_create_success_json(self, tmp_path: Path) -> None:
        """workspace create --json returns structured JSON with credentials."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        mock_ws = _make_workspace_mock()
        mock_ws.create_workspace.return_value = {
            "project_alias": "prod",
            "workspace_id": 42,
            "name": "my-workspace",
            "config_id": "cfg-123",
            "backend": "snowflake",
            "host": "account.snowflakecomputing.com",
            "warehouse": "KEBOOLA_PROD",
            "database": "KEBOOLA_258",
            "schema": "WORKSPACE_42",
            "user": "KEBOOLA_WORKSPACE_42",
            "password": "s3cret!Passw0rd",
            "read_only": True,
            "message": "Workspace 'my-workspace' (42) created in project 'prod'. Save the password!",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                ["--json", "workspace", "create", "--project", "prod"],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["workspace_id"] == 42
        assert output["data"]["password"] == "s3cret!Passw0rd"
        assert output["data"]["backend"] == "snowflake"

    def test_workspace_create_api_error(self, tmp_path: Path) -> None:
        """workspace create with API error returns correct exit code."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_ws = _make_workspace_mock()
        mock_ws.create_workspace.side_effect = KeboolaApiError(
            message="Invalid token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                ["--json", "workspace", "create", "--project", "prod"],
            )

        assert result.exit_code == 3
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_TOKEN"

    def test_workspace_create_config_error(self, tmp_path: Path) -> None:
        """workspace create with ConfigError returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_ws = _make_workspace_mock()
        mock_ws.create_workspace.side_effect = ConfigError("Project 'nonexistent' not found.")

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                ["--json", "workspace", "create", "--project", "nonexistent"],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"


class TestWorkspaceList:
    """Tests for `kbagent workspace list` command."""

    def test_workspace_list_success_json(self, tmp_path: Path) -> None:
        """workspace list --json returns structured JSON with workspaces."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_ws = _make_workspace_mock()
        mock_ws.list_workspaces.return_value = {
            "workspaces": [
                {
                    "project_alias": "prod",
                    "id": 42,
                    "backend": "snowflake",
                    "host": "account.snowflakecomputing.com",
                    "schema": "WORKSPACE_42",
                    "user": "KEBOOLA_WORKSPACE_42",
                    "created": "2025-09-10T14:00:00Z",
                    "component_id": "",
                    "config_id": "",
                },
            ],
            "errors": [],
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                ["--json", "workspace", "list"],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        workspaces = output["data"]["workspaces"]
        assert len(workspaces) == 1
        assert workspaces[0]["id"] == 42
        assert workspaces[0]["backend"] == "snowflake"

    def test_workspace_list_config_error(self, tmp_path: Path) -> None:
        """workspace list with ConfigError returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir)

        mock_ws = _make_workspace_mock()
        mock_ws.list_workspaces.side_effect = ConfigError("Project 'ghost' not found.")

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                ["--json", "workspace", "list", "--project", "ghost"],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"


class TestWorkspaceDetail:
    """Tests for `kbagent workspace detail` command."""

    def test_workspace_detail_success_json(self, tmp_path: Path) -> None:
        """workspace detail --json returns structured JSON with workspace info."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_ws = _make_workspace_mock()
        mock_ws.get_workspace.return_value = {
            "project_alias": "prod",
            "workspace_id": 42,
            "backend": "snowflake",
            "host": "account.snowflakecomputing.com",
            "warehouse": "KEBOOLA_PROD",
            "database": "KEBOOLA_258",
            "schema": "WORKSPACE_42",
            "user": "KEBOOLA_WORKSPACE_42",
            "created": "2025-09-10T14:00:00Z",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                [
                    "--json",
                    "workspace",
                    "detail",
                    "--project",
                    "prod",
                    "--workspace-id",
                    "42",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["workspace_id"] == 42
        assert output["data"]["backend"] == "snowflake"

    def test_workspace_detail_not_found(self, tmp_path: Path) -> None:
        """workspace detail for nonexistent workspace returns exit code 1."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_ws = _make_workspace_mock()
        mock_ws.get_workspace.side_effect = KeboolaApiError(
            message="Workspace not found",
            error_code="NOT_FOUND",
            status_code=404,
            retryable=False,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                [
                    "--json",
                    "workspace",
                    "detail",
                    "--project",
                    "prod",
                    "--workspace-id",
                    "999",
                ],
            )

        assert result.exit_code == 1
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "NOT_FOUND"


class TestWorkspaceDelete:
    """Tests for `kbagent workspace delete` command."""

    def test_workspace_delete_success_json(self, tmp_path: Path) -> None:
        """workspace delete --json returns structured JSON confirmation."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_ws = _make_workspace_mock()
        mock_ws.delete_workspace.return_value = {
            "project_alias": "prod",
            "workspace_id": 42,
            "message": "Workspace 42 deleted from project 'prod'.",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                [
                    "--json",
                    "workspace",
                    "delete",
                    "--project",
                    "prod",
                    "--workspace-id",
                    "42",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["workspace_id"] == 42
        assert "deleted" in output["data"]["message"]


class TestWorkspacePassword:
    """Tests for `kbagent workspace password` command."""

    def test_workspace_password_success_json(self, tmp_path: Path) -> None:
        """workspace password --json returns new password."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_ws = _make_workspace_mock()
        mock_ws.reset_password.return_value = {
            "project_alias": "prod",
            "workspace_id": 42,
            "password": "n3wS3cret!Pwd",
            "message": "Password reset for workspace 42 in project 'prod'.",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                [
                    "--json",
                    "workspace",
                    "password",
                    "--project",
                    "prod",
                    "--workspace-id",
                    "42",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["password"] == "n3wS3cret!Pwd"

    def test_workspace_password_api_error(self, tmp_path: Path) -> None:
        """workspace password with network error returns exit code 4."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_ws = _make_workspace_mock()
        mock_ws.reset_password.side_effect = KeboolaApiError(
            message="Request timed out",
            status_code=0,
            error_code="TIMEOUT",
            retryable=True,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                [
                    "--json",
                    "workspace",
                    "password",
                    "--project",
                    "prod",
                    "--workspace-id",
                    "42",
                ],
            )

        assert result.exit_code == 4
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "TIMEOUT"


class TestWorkspaceLoad:
    """Tests for `kbagent workspace load` command."""

    def test_workspace_load_success_json(self, tmp_path: Path) -> None:
        """workspace load --json returns load job result."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_ws = _make_workspace_mock()
        mock_ws.load_tables.return_value = {
            "project_alias": "prod",
            "workspace_id": 42,
            "tables_loaded": 2,
            "table_ids": ["in.c-main.orders", "in.c-main.customers"],
            "job_id": 777,
            "job_status": "success",
            "message": "Loaded 2 table(s) into workspace 42.",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                [
                    "--json",
                    "workspace",
                    "load",
                    "--project",
                    "prod",
                    "--workspace-id",
                    "42",
                    "--tables",
                    "in.c-main.orders",
                    "--tables",
                    "in.c-main.customers",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["tables_loaded"] == 2
        assert output["data"]["job_status"] == "success"

    def test_workspace_load_with_preserve_flag(self, tmp_path: Path) -> None:
        """workspace load --preserve passes preserve=True to service."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_ws = _make_workspace_mock()
        mock_ws.load_tables.return_value = {
            "project_alias": "prod",
            "workspace_id": 42,
            "tables_loaded": 1,
            "table_ids": ["in.c-main.orders"],
            "job_id": 800,
            "job_status": "success",
            "message": "Loaded 1 table(s) into workspace 42.",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                [
                    "--json",
                    "workspace",
                    "load",
                    "--project",
                    "prod",
                    "--workspace-id",
                    "42",
                    "--tables",
                    "in.c-main.orders",
                    "--preserve",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["tables_loaded"] == 1

        # Verify preserve=True was passed to the service
        mock_ws.load_tables.assert_called_once_with(
            alias="prod", workspace_id=42, tables=["in.c-main.orders"], preserve=True
        )

    def test_workspace_load_without_preserve_flag(self, tmp_path: Path) -> None:
        """workspace load without --preserve passes preserve=False to service."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_ws = _make_workspace_mock()
        mock_ws.load_tables.return_value = {
            "project_alias": "prod",
            "workspace_id": 42,
            "tables_loaded": 1,
            "table_ids": ["in.c-main.orders"],
            "job_id": 801,
            "job_status": "success",
            "message": "Loaded 1 table(s) into workspace 42.",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                [
                    "--json",
                    "workspace",
                    "load",
                    "--project",
                    "prod",
                    "--workspace-id",
                    "42",
                    "--tables",
                    "in.c-main.orders",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"

        # Verify preserve=False was passed to the service (default)
        mock_ws.load_tables.assert_called_once_with(
            alias="prod", workspace_id=42, tables=["in.c-main.orders"], preserve=False
        )


class TestWorkspaceQuery:
    """Tests for `kbagent workspace query` command."""

    def test_workspace_query_sql_success_json(self, tmp_path: Path) -> None:
        """workspace query --sql --json returns query results."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_ws = _make_workspace_mock()
        mock_ws.execute_query.return_value = {
            "project_alias": "prod",
            "workspace_id": 42,
            "branch_id": 100,
            "query_job_id": "qj-abc123",
            "status": "completed",
            "statements": [
                {
                    "statement_id": "stmt-1",
                    "status": "completed",
                    "rows_affected": 5,
                    "csv_data": "col1,col2\na,b\n",
                },
            ],
            "message": "Query executed in workspace 42.",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                [
                    "--json",
                    "workspace",
                    "query",
                    "--project",
                    "prod",
                    "--workspace-id",
                    "42",
                    "--sql",
                    "SELECT * FROM orders LIMIT 5",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["status"] == "completed"
        assert len(output["data"]["statements"]) == 1
        assert output["data"]["statements"][0]["csv_data"] == "col1,col2\na,b\n"

    def test_workspace_query_file_success_json(self, tmp_path: Path) -> None:
        """workspace query --file --json reads SQL from file."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        # Create a .sql file
        sql_file = tmp_path / "query.sql"
        sql_file.write_text("SELECT COUNT(*) FROM products;", encoding="utf-8")

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_ws = _make_workspace_mock()
        mock_ws.execute_query.return_value = {
            "project_alias": "prod",
            "workspace_id": 42,
            "branch_id": 100,
            "query_job_id": "qj-file",
            "status": "completed",
            "statements": [
                {
                    "statement_id": "stmt-1",
                    "status": "completed",
                    "rows_affected": 1,
                    "csv_data": "count\n42\n",
                },
            ],
            "message": "Query executed in workspace 42.",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                [
                    "--json",
                    "workspace",
                    "query",
                    "--project",
                    "prod",
                    "--workspace-id",
                    "42",
                    "--file",
                    str(sql_file),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"

        # Verify the SQL from file was passed to the service
        mock_ws.execute_query.assert_called_once()
        call_kwargs = mock_ws.execute_query.call_args
        assert "SELECT COUNT(*) FROM products;" in (
            call_kwargs.kwargs.get("sql", "") or call_kwargs[1].get("sql", "")
        )

    def test_workspace_query_neither_sql_nor_file(self, tmp_path: Path) -> None:
        """workspace query without --sql or --file returns exit code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_ws = _make_workspace_mock()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                [
                    "--json",
                    "workspace",
                    "query",
                    "--project",
                    "prod",
                    "--workspace-id",
                    "42",
                ],
            )

        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "USAGE_ERROR"
        assert "Specify either --sql or --file" in output["error"]["message"]

    def test_workspace_query_both_sql_and_file(self, tmp_path: Path) -> None:
        """workspace query with both --sql and --file returns exit code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        sql_file = tmp_path / "query.sql"
        sql_file.write_text("SELECT 1;", encoding="utf-8")

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_ws = _make_workspace_mock()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                [
                    "--json",
                    "workspace",
                    "query",
                    "--project",
                    "prod",
                    "--workspace-id",
                    "42",
                    "--sql",
                    "SELECT 1",
                    "--file",
                    str(sql_file),
                ],
            )

        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "USAGE_ERROR"
        assert "not both" in output["error"]["message"]

    def test_workspace_query_api_error(self, tmp_path: Path) -> None:
        """workspace query with query failure returns exit code 1."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_ws = _make_workspace_mock()
        mock_ws.execute_query.side_effect = KeboolaApiError(
            message="Query job failed: syntax error",
            error_code="QUERY_JOB_FAILED",
            status_code=500,
            retryable=False,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                [
                    "--json",
                    "workspace",
                    "query",
                    "--project",
                    "prod",
                    "--workspace-id",
                    "42",
                    "--sql",
                    "INVALID SQL",
                ],
            )

        assert result.exit_code == 1
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "QUERY_JOB_FAILED"


class TestWorkspaceFromTransformation:
    """Tests for `kbagent workspace from-transformation` command."""

    def test_from_transformation_success_json(self, tmp_path: Path) -> None:
        """workspace from-transformation --json returns workspace with tables."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_ws = _make_workspace_mock()
        mock_ws.create_from_transformation.return_value = {
            "project_alias": "prod",
            "workspace_id": 55,
            "branch_id": 100,
            "component_id": "keboola.snowflake-transformation",
            "config_id": "456",
            "row_id": None,
            "backend": "snowflake",
            "host": "account.snowflakecomputing.com",
            "warehouse": "KEBOOLA_PROD",
            "database": "KEBOOLA_258",
            "schema": "WORKSPACE_55",
            "user": "KEBOOLA_WORKSPACE_55",
            "password": "ws-secret-pwd",
            "tables_loaded": ["in.c-main.orders", "in.c-main.products"],
            "message": "Workspace 55 created from transformation '456' with 2 table(s) loaded.",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                [
                    "--json",
                    "workspace",
                    "from-transformation",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.snowflake-transformation",
                    "--config-id",
                    "456",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["workspace_id"] == 55
        assert output["data"]["password"] == "ws-secret-pwd"
        assert output["data"]["tables_loaded"] == ["in.c-main.orders", "in.c-main.products"]

    def test_from_transformation_config_error(self, tmp_path: Path) -> None:
        """workspace from-transformation with no input tables returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_ws = _make_workspace_mock()
        mock_ws.create_from_transformation.side_effect = ConfigError(
            "No input tables found in transformation config '456'."
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                [
                    "--json",
                    "workspace",
                    "from-transformation",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.snowflake-transformation",
                    "--config-id",
                    "456",
                ],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"
        assert "No input tables" in output["error"]["message"]

    def test_from_transformation_api_error(self, tmp_path: Path) -> None:
        """workspace from-transformation with API error returns exit code 1."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_ws = _make_workspace_mock()
        mock_ws.create_from_transformation.side_effect = KeboolaApiError(
            message="Config not found",
            error_code="NOT_FOUND",
            status_code=404,
            retryable=False,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.WorkspaceService") as MockWsService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockWsService.return_value = mock_ws

            result = runner.invoke(
                app,
                [
                    "--json",
                    "workspace",
                    "from-transformation",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.snowflake-transformation",
                    "--config-id",
                    "456",
                ],
            )

        assert result.exit_code == 1
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "NOT_FOUND"
