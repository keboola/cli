"""Tests for WorkspaceService - workspace lifecycle management.

Tests cover CRUD operations, table loading, SQL query execution,
create-from-transformation workflow, branch resolution, and
multi-project parallel listing.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from helpers import setup_single_project, setup_two_projects
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.workspace_service import WorkspaceService

SAMPLE_WORKSPACE = {
    "id": 42,
    "connection": {
        "backend": "snowflake",
        "host": "account.snowflakecomputing.com",
        "warehouse": "KEBOOLA_PROD",
        "database": "KEBOOLA_258",
        "schema": "WORKSPACE_42",
        "user": "KEBOOLA_WORKSPACE_42",
        "password": "s3cret!Passw0rd",
    },
    "created": "2025-09-10T14:00:00Z",
}

SAMPLE_WORKSPACE_NO_PASSWORD = {
    "id": 42,
    "connection": {
        "backend": "snowflake",
        "host": "account.snowflakecomputing.com",
        "warehouse": "KEBOOLA_PROD",
        "database": "KEBOOLA_258",
        "schema": "WORKSPACE_42",
        "user": "KEBOOLA_WORKSPACE_42",
    },
    "created": "2025-09-10T14:00:00Z",
}

SAMPLE_WORKSPACE_LIST = [
    {
        "id": 42,
        "name": "my-workspace",
        "connection": {
            "backend": "snowflake",
            "host": "account.snowflakecomputing.com",
            "schema": "WORKSPACE_42",
            "user": "KEBOOLA_WORKSPACE_42",
        },
        "created": "2025-09-10T14:00:00Z",
        "component": "keboola.snowflake-transformation",
        "configurationId": "123",
    },
    {
        "id": 99,
        "name": "",
        "connection": {
            "backend": "snowflake",
            "host": "account.snowflakecomputing.com",
            "schema": "WORKSPACE_99",
            "user": "KEBOOLA_WORKSPACE_99",
        },
        "created": "2025-09-11T08:30:00Z",
        "component": None,
        "configurationId": None,
    },
]

SAMPLE_BRANCHES = [
    {"id": 100, "name": "main", "isDefault": True},
    {"id": 200, "name": "feature-x", "isDefault": False},
]


class TestCreateWorkspace:
    """Tests for WorkspaceService.create_workspace()."""

    def test_create_workspace_success(self, tmp_config_dir: Path) -> None:
        """create_workspace returns workspace details including password."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_client.create_sandbox_config.return_value = {
            "id": "cfg-123",
            "name": "test-ws",
        }
        mock_client.create_config_workspace.return_value = SAMPLE_WORKSPACE

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.create_workspace(
            alias="prod", name="test-ws", backend="snowflake", read_only=True
        )

        assert result["project_alias"] == "prod"
        assert result["workspace_id"] == 42
        assert result["name"] == "test-ws"
        assert result["config_id"] == "cfg-123"
        assert result["backend"] == "snowflake"
        assert result["host"] == "account.snowflakecomputing.com"
        assert result["warehouse"] == "KEBOOLA_PROD"
        assert result["database"] == "KEBOOLA_258"
        assert result["schema"] == "WORKSPACE_42"
        assert result["user"] == "KEBOOLA_WORKSPACE_42"
        assert result["password"] == "s3cret!Passw0rd"
        assert result["read_only"] is True
        assert "Save the password" in result["message"]

        mock_client.create_sandbox_config.assert_called_once_with(
            name="test-ws",
            description="Created by kbagent CLI",
            branch_id=123,
        )
        mock_client.create_config_workspace.assert_called_once_with(
            branch_id=123,
            component_id="keboola.sandboxes",
            config_id="cfg-123",
            backend="snowflake",
        )
        # close() called twice: once in _resolve_branch_id, once in create_workspace
        assert mock_client.close.call_count == 2

    def test_create_workspace_unknown_project(self, tmp_config_dir: Path) -> None:
        """create_workspace raises ConfigError for an unknown alias."""
        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(config_store=store)

        with pytest.raises(ConfigError, match="Project 'nonexistent' not found"):
            svc.create_workspace(alias="nonexistent")

    def test_create_workspace_api_error(self, tmp_config_dir: Path) -> None:
        """create_workspace propagates KeboolaApiError from the client."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_client.create_sandbox_config.side_effect = KeboolaApiError(
            message="Quota exceeded",
            error_code="QUOTA_EXCEEDED",
            status_code=403,
            retryable=False,
        )

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.raises(KeboolaApiError, match="Quota exceeded"):
            svc.create_workspace(alias="prod")

    def test_create_workspace_in_dev_branch(self, tmp_config_dir: Path) -> None:
        """create_workspace uses active_branch_id for sandbox config endpoint."""
        mock_client = MagicMock()
        mock_client.create_sandbox_config.return_value = {
            "id": "cfg-456",
            "name": "branch-ws",
        }
        mock_client.create_config_workspace.return_value = SAMPLE_WORKSPACE

        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-xxx",
                project_name="Production",
                project_id=258,
                active_branch_id=200,
            ),
        )
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.create_workspace(alias="prod", name="branch-ws", backend="snowflake")

        assert result["config_id"] == "cfg-456"
        # Sandbox config must be created in the dev branch
        mock_client.create_sandbox_config.assert_called_once_with(
            name="branch-ws",
            description="Created by kbagent CLI",
            branch_id=200,
        )
        # Config workspace must also use the dev branch
        mock_client.create_config_workspace.assert_called_once_with(
            branch_id=200,
            component_id="keboola.sandboxes",
            config_id="cfg-456",
            backend="snowflake",
        )


class TestListWorkspacesSingleProject:
    """Tests for WorkspaceService.list_workspaces() with a single project."""

    def test_list_workspaces_single_project(self, tmp_config_dir: Path) -> None:
        """list_workspaces returns workspaces annotated with project alias."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_client.list_workspaces.return_value = SAMPLE_WORKSPACE_LIST

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.list_workspaces(aliases=["prod"])
        workspaces = result["workspaces"]
        errors = result["errors"]

        assert errors == []
        assert len(workspaces) == 2
        assert workspaces[0]["project_alias"] == "prod"
        assert workspaces[0]["id"] == 42
        assert workspaces[0]["name"] == "my-workspace"
        assert workspaces[0]["backend"] == "snowflake"
        assert workspaces[0]["component_id"] == "keboola.snowflake-transformation"
        assert workspaces[0]["config_id"] == "123"
        assert workspaces[1]["id"] == 99
        assert workspaces[1]["name"] == ""
        assert workspaces[1]["component_id"] == ""
        assert workspaces[1]["config_id"] == ""

    def test_list_workspaces_empty(self, tmp_config_dir: Path) -> None:
        """list_workspaces returns empty list when no workspaces exist."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_client.list_workspaces.return_value = []

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.list_workspaces(aliases=["prod"])
        assert result["workspaces"] == []
        assert result["errors"] == []


class TestListWorkspacesMultiProject:
    """Tests for WorkspaceService.list_workspaces() with multiple projects."""

    def test_list_workspaces_multi_project(self, tmp_config_dir: Path) -> None:
        """list_workspaces aggregates workspaces from all projects."""

        def make_client(url: str, token: str) -> MagicMock:
            mock = MagicMock()
            mock.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
            if token == "901-xxx":
                mock.list_workspaces.return_value = SAMPLE_WORKSPACE_LIST
            else:
                mock.list_workspaces.return_value = [
                    {
                        "id": 200,
                        "connection": {
                            "backend": "snowflake",
                            "host": "dev.snowflakecomputing.com",
                            "schema": "WORKSPACE_200",
                            "user": "KEBOOLA_WORKSPACE_200",
                        },
                        "created": "2025-09-12T12:00:00Z",
                        "configurationId": {},
                    },
                ]
            return mock

        store = setup_two_projects(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=make_client,
        )

        result = svc.list_workspaces()
        workspaces = result["workspaces"]
        errors = result["errors"]

        assert errors == []
        assert len(workspaces) == 3

        dev_workspaces = [w for w in workspaces if w["project_alias"] == "dev"]
        prod_workspaces = [w for w in workspaces if w["project_alias"] == "prod"]
        assert len(dev_workspaces) == 1
        assert len(prod_workspaces) == 2


class TestListWorkspacesWithError:
    """Tests for error handling in WorkspaceService.list_workspaces()."""

    def test_list_workspaces_one_project_fails(self, tmp_config_dir: Path) -> None:
        """When one project fails, the other still returns results."""

        def make_client(url: str, token: str) -> MagicMock:
            mock = MagicMock()
            mock.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
            if token == "901-xxx":
                mock.list_workspaces.return_value = SAMPLE_WORKSPACE_LIST
            else:
                mock.list_workspaces.side_effect = KeboolaApiError(
                    message="Connection refused",
                    error_code="CONNECTION_ERROR",
                    status_code=0,
                    retryable=True,
                )
            return mock

        store = setup_two_projects(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=make_client,
        )

        result = svc.list_workspaces()
        workspaces = result["workspaces"]
        errors = result["errors"]

        assert len(workspaces) == 2
        assert all(w["project_alias"] == "prod" for w in workspaces)
        assert len(errors) == 1
        assert errors[0]["project_alias"] == "dev"
        assert errors[0]["error_code"] == "CONNECTION_ERROR"

    def test_list_workspaces_unexpected_error(self, tmp_config_dir: Path) -> None:
        """Unexpected exceptions are captured as UNEXPECTED_ERROR."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_client.list_workspaces.side_effect = RuntimeError("Something broke")

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.list_workspaces(aliases=["prod"])
        assert result["workspaces"] == []
        assert len(result["errors"]) == 1
        assert result["errors"][0]["error_code"] == "UNEXPECTED_ERROR"
        assert "Something broke" in result["errors"][0]["message"]


class TestGetWorkspace:
    """Tests for WorkspaceService.get_workspace()."""

    def test_get_workspace_success(self, tmp_config_dir: Path) -> None:
        """get_workspace returns workspace details without password."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_client.get_workspace.return_value = SAMPLE_WORKSPACE_NO_PASSWORD

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.get_workspace(alias="prod", workspace_id=42)

        assert result["project_alias"] == "prod"
        assert result["workspace_id"] == 42
        assert result["backend"] == "snowflake"
        assert result["host"] == "account.snowflakecomputing.com"
        assert result["schema"] == "WORKSPACE_42"
        assert result["user"] == "KEBOOLA_WORKSPACE_42"
        assert result["created"] == "2025-09-10T14:00:00Z"
        assert "password" not in result

        mock_client.get_workspace.assert_called_once_with(42, branch_id=123)
        # close() called twice: once in _resolve_branch_id, once in get_workspace
        assert mock_client.close.call_count == 2

    def test_get_workspace_not_found(self, tmp_config_dir: Path) -> None:
        """get_workspace propagates 404 KeboolaApiError."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_client.get_workspace.side_effect = KeboolaApiError(
            message="Workspace not found",
            error_code="NOT_FOUND",
            status_code=404,
            retryable=False,
        )

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.raises(KeboolaApiError, match="Workspace not found"):
            svc.get_workspace(alias="prod", workspace_id=999)


class TestDeleteWorkspace:
    """Tests for WorkspaceService.delete_workspace()."""

    def test_delete_workspace_success(self, tmp_config_dir: Path) -> None:
        """delete_workspace calls the API and returns confirmation."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_client.get_workspace.return_value = {
            "component": "keboola.sandboxes",
            "configurationId": "cfg-123",
        }

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.delete_workspace(alias="prod", workspace_id=42)

        assert result["project_alias"] == "prod"
        assert result["workspace_id"] == 42
        assert "deleted" in result["message"]
        mock_client.get_workspace.assert_called_once_with(42, branch_id=123)
        mock_client.delete_workspace.assert_called_once_with(42, branch_id=123)
        mock_client.delete_config.assert_called_once_with(
            "keboola.sandboxes", "cfg-123", branch_id=123
        )
        # close() called twice: once in _resolve_branch_id, once in delete_workspace
        assert mock_client.close.call_count == 2

    def test_delete_workspace_api_error(self, tmp_config_dir: Path) -> None:
        """delete_workspace propagates KeboolaApiError from delete call."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        # get_workspace fails (workspace lookup), but delete_workspace also fails
        mock_client.get_workspace.side_effect = KeboolaApiError(
            message="Workspace not found",
            error_code="NOT_FOUND",
            status_code=404,
            retryable=False,
        )
        mock_client.delete_workspace.side_effect = KeboolaApiError(
            message="Workspace not found",
            error_code="NOT_FOUND",
            status_code=404,
            retryable=False,
        )

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.raises(KeboolaApiError, match="Workspace not found"):
            svc.delete_workspace(alias="prod", workspace_id=999)

    def test_delete_workspace_in_dev_branch(self, tmp_config_dir: Path) -> None:
        """delete_workspace uses active_branch_id for config deletion."""
        mock_client = MagicMock()
        mock_client.get_workspace.return_value = {
            "component": "keboola.sandboxes",
            "configurationId": "cfg-789",
        }

        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-xxx",
                project_name="Production",
                project_id=258,
                active_branch_id=200,
            ),
        )
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.delete_workspace(alias="prod", workspace_id=42)

        assert result["workspace_id"] == 42
        mock_client.get_workspace.assert_called_once_with(42, branch_id=200)
        mock_client.delete_workspace.assert_called_once_with(42, branch_id=200)
        mock_client.delete_config.assert_called_once_with(
            "keboola.sandboxes", "cfg-789", branch_id=200
        )


class TestResetPassword:
    """Tests for WorkspaceService.reset_password()."""

    def test_reset_password_success(self, tmp_config_dir: Path) -> None:
        """reset_password returns the new password."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_client.reset_workspace_password.return_value = {
            "password": "n3wS3cret!Pwd",
        }

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.reset_password(alias="prod", workspace_id=42)

        assert result["project_alias"] == "prod"
        assert result["workspace_id"] == 42
        assert result["password"] == "n3wS3cret!Pwd"
        assert "Password reset" in result["message"]
        assert "Save the new password" in result["message"]

        mock_client.reset_workspace_password.assert_called_once_with(42, branch_id=123)
        # close() called twice: once in _resolve_branch_id, once in reset_password
        assert mock_client.close.call_count == 2


class TestLoadTables:
    """Tests for WorkspaceService.load_tables()."""

    def test_load_tables_success(self, tmp_config_dir: Path) -> None:
        """load_tables builds table defs and returns job result."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_client.load_workspace_tables.return_value = {
            "id": 777,
            "status": "success",
        }

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        tables = ["in.c-main.orders", "in.c-main.customers"]
        result = svc.load_tables(alias="prod", workspace_id=42, tables=tables)

        assert result["project_alias"] == "prod"
        assert result["workspace_id"] == 42
        assert result["tables_loaded"] == 2
        assert result["table_ids"] == tables
        assert result["job_id"] == 777
        assert result["job_status"] == "success"
        assert "Loaded 2 table(s)" in result["message"]

        # Verify the table defs built from IDs
        call_args = mock_client.load_workspace_tables.call_args
        assert call_args[0][0] == 42  # workspace_id
        table_defs = call_args[0][1]
        assert len(table_defs) == 2
        assert table_defs[0] == {"source": "in.c-main.orders", "destination": "orders"}
        assert table_defs[1] == {"source": "in.c-main.customers", "destination": "customers"}
        assert call_args[1] == {"branch_id": 123, "preserve": False}
        # close() called twice: once in _resolve_branch_id, once in load_tables
        assert mock_client.close.call_count == 2

    def test_load_tables_preserve_false(self, tmp_config_dir: Path) -> None:
        """load_tables passes preserve=False to client by default."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_client.load_workspace_tables.return_value = {
            "id": 778,
            "status": "success",
        }

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        svc.load_tables(alias="prod", workspace_id=42, tables=["in.c-main.orders"])

        call_kwargs = mock_client.load_workspace_tables.call_args[1]
        assert call_kwargs["preserve"] is False

    def test_load_tables_preserve_true(self, tmp_config_dir: Path) -> None:
        """load_tables passes preserve=True to client when requested."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_client.load_workspace_tables.return_value = {
            "id": 779,
            "status": "success",
        }

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.load_tables(
            alias="prod", workspace_id=42, tables=["in.c-main.orders"], preserve=True
        )

        call_kwargs = mock_client.load_workspace_tables.call_args[1]
        assert call_kwargs["preserve"] is True
        assert result["job_id"] == 779

    def test_load_tables_api_error(self, tmp_config_dir: Path) -> None:
        """load_tables propagates KeboolaApiError when job fails."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_client.load_workspace_tables.side_effect = KeboolaApiError(
            message="Storage job failed",
            error_code="STORAGE_JOB_FAILED",
            status_code=500,
            retryable=False,
        )

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.raises(KeboolaApiError, match="Storage job failed"):
            svc.load_tables(alias="prod", workspace_id=42, tables=["in.c-main.orders"])


class TestExecuteQuery:
    """Tests for WorkspaceService.execute_query()."""

    def test_execute_query_success(self, tmp_config_dir: Path) -> None:
        """execute_query submits, polls, and exports CSV results."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES
        mock_client.submit_query.return_value = {"id": "qj-abc123"}
        mock_client.wait_for_query_job.return_value = {
            "status": "completed",
            "statements": [
                {
                    "id": "stmt-1",
                    "status": "completed",
                    "resultRows": 5,
                },
            ],
        }
        mock_client.export_query_results.return_value = "col1,col2\na,b\nc,d\n"

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.execute_query(
            alias="prod",
            workspace_id=42,
            sql="SELECT * FROM orders LIMIT 5",
        )

        assert result["project_alias"] == "prod"
        assert result["workspace_id"] == 42
        assert result["branch_id"] == 100  # main branch from SAMPLE_BRANCHES
        assert result["query_job_id"] == "qj-abc123"
        assert result["status"] == "completed"
        assert len(result["statements"]) == 1
        assert result["statements"][0]["statement_id"] == "stmt-1"
        assert result["statements"][0]["status"] == "completed"
        assert result["statements"][0]["rows_affected"] == 5
        assert result["statements"][0]["csv_data"] == "col1,col2\na,b\nc,d\n"

        mock_client.submit_query.assert_called_once_with(
            branch_id=100,
            workspace_id=42,
            statements=["SELECT * FROM orders LIMIT 5"],
            transactional=False,
        )
        # close() called twice: once in _resolve_branch_id, once in execute_query
        assert mock_client.close.call_count == 2

    def test_execute_query_with_active_branch(self, tmp_config_dir: Path) -> None:
        """execute_query uses active_branch_id when set."""
        mock_client = MagicMock()
        mock_client.submit_query.return_value = {"id": "qj-xyz"}
        mock_client.wait_for_query_job.return_value = {
            "status": "completed",
            "statements": [],
        }

        store = setup_single_project(tmp_config_dir)
        store.set_project_branch("prod", 200)

        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.execute_query(
            alias="prod",
            workspace_id=42,
            sql="SELECT 1",
        )

        assert result["branch_id"] == 200
        # Should NOT call list_dev_branches when active branch is set
        mock_client.list_dev_branches.assert_not_called()
        mock_client.submit_query.assert_called_once_with(
            branch_id=200,
            workspace_id=42,
            statements=["SELECT 1"],
            transactional=False,
        )

    def test_execute_query_failure(self, tmp_config_dir: Path) -> None:
        """execute_query raises when query job fails."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES
        mock_client.submit_query.return_value = {"id": "qj-fail"}
        mock_client.wait_for_query_job.side_effect = KeboolaApiError(
            message="Query job failed: syntax error",
            error_code="QUERY_JOB_FAILED",
            status_code=500,
            retryable=False,
        )

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.raises(KeboolaApiError, match="Query job failed"):
            svc.execute_query(alias="prod", workspace_id=42, sql="INVALID SQL")

    def test_execute_query_no_result_rows(self, tmp_config_dir: Path) -> None:
        """execute_query skips export for statements with zero result rows."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES
        mock_client.submit_query.return_value = {"id": "qj-ddl"}
        mock_client.wait_for_query_job.return_value = {
            "status": "completed",
            "statements": [
                {
                    "id": "stmt-ddl",
                    "status": "completed",
                    "resultRows": 0,
                },
            ],
        }

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.execute_query(
            alias="prod",
            workspace_id=42,
            sql="CREATE TABLE test (id INT)",
        )

        assert result["statements"][0]["rows_affected"] == 0
        assert "csv_data" not in result["statements"][0]
        mock_client.export_query_results.assert_not_called()

    def test_execute_query_export_fails_gracefully(self, tmp_config_dir: Path) -> None:
        """execute_query handles export failure gracefully (no csv_data in result)."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES
        mock_client.submit_query.return_value = {"id": "qj-export-fail"}
        mock_client.wait_for_query_job.return_value = {
            "status": "completed",
            "statements": [
                {
                    "id": "stmt-1",
                    "status": "completed",
                    "resultRows": 10,
                },
            ],
        }
        mock_client.export_query_results.side_effect = KeboolaApiError(
            message="Export unavailable",
            error_code="EXPORT_ERROR",
            status_code=500,
            retryable=False,
        )

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.execute_query(
            alias="prod",
            workspace_id=42,
            sql="SELECT * FROM big_table",
        )

        # Should still succeed, just without csv_data
        assert result["status"] == "completed"
        assert "csv_data" not in result["statements"][0]


class TestCreateFromTransformation:
    """Tests for WorkspaceService.create_from_transformation()."""

    def test_create_from_transformation_success(self, tmp_config_dir: Path) -> None:
        """create_from_transformation reads config, creates workspace, loads tables."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES
        mock_client.get_config_detail.return_value = {
            "id": "456",
            "configuration": {
                "storage": {
                    "input": {
                        "tables": [
                            {"source": "in.c-main.orders", "destination": "orders"},
                            {"source": "in.c-main.products", "destination": "products"},
                        ],
                    },
                },
            },
        }
        mock_client.create_config_workspace.return_value = {
            "id": 55,
            "connection": {
                "backend": "snowflake",
                "host": "account.snowflakecomputing.com",
                "warehouse": "KEBOOLA_PROD",
                "database": "KEBOOLA_258",
                "schema": "WORKSPACE_55",
                "user": "KEBOOLA_WORKSPACE_55",
                "password": "ws-secret-pwd",
            },
        }
        mock_client.load_workspace_tables.return_value = {
            "id": 888,
            "status": "success",
        }

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.create_from_transformation(
            alias="prod",
            component_id="keboola.snowflake-transformation",
            config_id="456",
        )

        assert result["project_alias"] == "prod"
        assert result["workspace_id"] == 55
        assert result["branch_id"] == 100
        assert result["component_id"] == "keboola.snowflake-transformation"
        assert result["config_id"] == "456"
        assert result["row_id"] is None
        assert result["backend"] == "snowflake"
        assert result["password"] == "ws-secret-pwd"
        assert result["tables_loaded"] == ["in.c-main.orders", "in.c-main.products"]
        assert "2 table(s) loaded" in result["message"]

        mock_client.get_config_detail.assert_called_once_with(
            "keboola.snowflake-transformation",
            "456",
        )
        mock_client.create_config_workspace.assert_called_once_with(
            branch_id=100,
            component_id="keboola.snowflake-transformation",
            config_id="456",
            backend="snowflake",
        )
        mock_client.load_workspace_tables.assert_called_once()
        # close() called twice: once in _resolve_branch_id, once in create_from_transformation
        assert mock_client.close.call_count == 2

    def test_create_from_transformation_with_row_id(self, tmp_config_dir: Path) -> None:
        """create_from_transformation extracts input tables from specific row."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES
        mock_client.get_config_detail.return_value = {
            "id": "789",
            "configuration": {
                "storage": {"input": {"tables": []}},
            },
            "rows": [
                {
                    "id": "r1",
                    "configuration": {
                        "storage": {
                            "input": {
                                "tables": [
                                    {"source": "in.c-crm.contacts", "destination": "contacts"},
                                ],
                            },
                        },
                    },
                },
                {
                    "id": "r2",
                    "configuration": {
                        "storage": {
                            "input": {
                                "tables": [
                                    {"source": "in.c-crm.deals", "destination": "deals"},
                                ],
                            },
                        },
                    },
                },
            ],
        }
        mock_client.create_config_workspace.return_value = {
            "id": 66,
            "connection": {
                "backend": "snowflake",
                "host": "h",
                "warehouse": "w",
                "database": "d",
                "schema": "s",
                "user": "u",
                "password": "p",
            },
        }
        mock_client.load_workspace_tables.return_value = {"id": 900, "status": "success"}

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.create_from_transformation(
            alias="prod",
            component_id="keboola.snowflake-transformation",
            config_id="789",
            row_id="r2",
        )

        assert result["row_id"] == "r2"
        assert result["tables_loaded"] == ["in.c-crm.deals"]
        assert "1 table(s) loaded" in result["message"]

    def test_create_from_transformation_row_not_found(self, tmp_config_dir: Path) -> None:
        """create_from_transformation raises ConfigError when row_id not found."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES
        mock_client.get_config_detail.return_value = {
            "id": "789",
            "configuration": {},
            "rows": [
                {"id": "r1", "configuration": {}},
            ],
        }

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.raises(ConfigError, match="Row 'r99' not found"):
            svc.create_from_transformation(
                alias="prod",
                component_id="keboola.snowflake-transformation",
                config_id="789",
                row_id="r99",
            )

    def test_create_from_transformation_no_input_tables(self, tmp_config_dir: Path) -> None:
        """create_from_transformation raises ConfigError when no input tables defined."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES
        mock_client.get_config_detail.return_value = {
            "id": "456",
            "configuration": {
                "storage": {
                    "input": {
                        "tables": [],
                    },
                },
            },
        }

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.raises(ConfigError, match="No input tables found"):
            svc.create_from_transformation(
                alias="prod",
                component_id="keboola.snowflake-transformation",
                config_id="456",
            )

    def test_create_from_transformation_passes_columns_and_where(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """create_from_transformation passes through columns and where filters."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES
        mock_client.get_config_detail.return_value = {
            "id": "456",
            "configuration": {
                "storage": {
                    "input": {
                        "tables": [
                            {
                                "source": "in.c-main.orders",
                                "destination": "orders",
                                "columns": ["id", "amount"],
                                "where_column": "status",
                                "where_values": ["active"],
                            },
                        ],
                    },
                },
            },
        }
        mock_client.create_config_workspace.return_value = {
            "id": 77,
            "connection": {
                "backend": "snowflake",
                "host": "h",
                "warehouse": "w",
                "database": "d",
                "schema": "s",
                "user": "u",
                "password": "p",
            },
        }
        mock_client.load_workspace_tables.return_value = {"id": 901, "status": "success"}

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.create_from_transformation(
            alias="prod",
            component_id="keboola.snowflake-transformation",
            config_id="456",
        )

        # Verify the table defs include columns and where filters
        call_args = mock_client.load_workspace_tables.call_args
        table_defs = call_args[0][1]
        assert len(table_defs) == 1
        assert table_defs[0]["columns"] == ["id", "amount"]
        assert table_defs[0]["where_column"] == "status"
        assert table_defs[0]["where_values"] == ["active"]
        assert result["tables_loaded"] == ["in.c-main.orders"]


class TestResolveBranchId:
    """Tests for WorkspaceService._resolve_branch_id()."""

    def test_resolve_branch_id_uses_active_branch(self, tmp_config_dir: Path) -> None:
        """_resolve_branch_id returns active_branch_id when set."""
        store = setup_single_project(tmp_config_dir)
        store.set_project_branch("prod", 200)

        svc = WorkspaceService(config_store=store)
        project = store.get_project("prod")

        branch_id = svc._resolve_branch_id("prod", project)

        assert branch_id == 200

    def test_resolve_branch_id_fetches_main_branch(self, tmp_config_dir: Path) -> None:
        """_resolve_branch_id fetches main branch when no active branch set."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        project = store.get_project("prod")
        branch_id = svc._resolve_branch_id("prod", project)

        assert branch_id == 100  # main branch
        mock_client.list_dev_branches.assert_called_once()
        mock_client.close.assert_called_once()

    def test_resolve_branch_id_no_default_branch(self, tmp_config_dir: Path) -> None:
        """_resolve_branch_id raises ConfigError when no default branch found."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [
            {"id": 300, "name": "feature-only", "isDefault": False},
        ]

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        project = store.get_project("prod")
        with pytest.raises(ConfigError, match="No default branch found"):
            svc._resolve_branch_id("prod", project)
