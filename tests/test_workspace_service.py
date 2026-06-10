"""Tests for WorkspaceService - workspace lifecycle management.

Tests cover CRUD operations, table loading, SQL query execution,
create-from-transformation workflow, branch resolution, and
multi-project parallel listing.
"""

from pathlib import Path
from unittest.mock import ANY, MagicMock

import pytest

from helpers import setup_single_project, setup_two_projects
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig, TokenVerifyResponse
from keboola_agent_cli.services import workspace_service as workspace_service_module
from keboola_agent_cli.services.workspace_service import WorkspaceService

SAMPLE_TOKEN_VERIFY = TokenVerifyResponse(
    token_id="12345",
    token_description="Test Token",
    project_id=258,
    project_name="Production",
    owner_name="Production",
    default_backend="snowflake",
)

SAMPLE_TOKEN_VERIFY_BIGQUERY = TokenVerifyResponse(
    token_id="12345",
    token_description="Test Token",
    project_id=258,
    project_name="Production",
    owner_name="Production",
    default_backend="bigquery",
)

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
        """create_workspace returns workspace details including Snowflake key-pair credentials."""
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
        assert result["private_key"].startswith("-----BEGIN PRIVATE KEY-----")
        assert result["read_only"] is True
        assert "Save the private key" in result["message"]

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
            login_type="snowflake-person-keypair",
            public_key=ANY,
        )
        public_key = mock_client.create_config_workspace.call_args.kwargs["public_key"]
        assert public_key.startswith("-----BEGIN PUBLIC KEY-----")
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
            login_type="snowflake-person-keypair",
            public_key=ANY,
        )
        public_key = mock_client.create_config_workspace.call_args.kwargs["public_key"]
        assert public_key.startswith("-----BEGIN PUBLIC KEY-----")


class TestAutoDetectBackend:
    """Tests for automatic backend detection when --backend is omitted."""

    def test_create_workspace_snowflake_uses_person_keypair_login_type(
        self, tmp_config_dir: Path
    ) -> None:
        """Snowflake sandbox workspaces request the Query-Service-compatible login type."""
        mock_client = MagicMock()
        mock_client.verify_token.return_value = SAMPLE_TOKEN_VERIFY
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_client.create_sandbox_config.return_value = {"id": "cfg-1", "name": "ws"}
        mock_client.create_config_workspace.return_value = SAMPLE_WORKSPACE

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.create_workspace(alias="prod", name="ws")

        assert result["backend"] == "snowflake"
        assert result["private_key"].startswith("-----BEGIN PRIVATE KEY-----")
        call_kwargs = mock_client.create_config_workspace.call_args.kwargs
        assert call_kwargs["branch_id"] == 123
        assert call_kwargs["component_id"] == "keboola.sandboxes"
        assert call_kwargs["config_id"] == "cfg-1"
        assert call_kwargs["backend"] == "snowflake"
        assert call_kwargs["login_type"] == "snowflake-person-keypair"
        assert call_kwargs["public_key"].startswith("-----BEGIN PUBLIC KEY-----")

    def test_create_workspace_bigquery_requests_default_login_type(
        self, tmp_config_dir: Path
    ) -> None:
        """BigQuery sandbox workspaces request loginType ``default`` -- the only
        BigQuery loginType and the one the Query Service accepts (since v0.58.0,
        matching keboola-mcp-server). No key pair: BigQuery uses service-account
        credentials, not RSA keys."""
        mock_client = MagicMock()
        mock_client.verify_token.return_value = SAMPLE_TOKEN_VERIFY_BIGQUERY
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_client.create_sandbox_config.return_value = {"id": "cfg-1", "name": "ws"}
        mock_client.create_config_workspace.return_value = {
            "id": 42,
            "connection": {
                "backend": "bigquery",
                "schema": "WORKSPACE_42",
            },
        }

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.create_workspace(alias="prod", name="ws")

        assert result["backend"] == "bigquery"
        mock_client.create_config_workspace.assert_called_once_with(
            branch_id=123,
            component_id="keboola.sandboxes",
            config_id="cfg-1",
            backend="bigquery",
            login_type="default",
            public_key=None,
        )

    def test_create_workspace_auto_detects_snowflake(self, tmp_config_dir: Path) -> None:
        """create_workspace auto-detects snowflake backend from project."""
        mock_client = MagicMock()
        mock_client.verify_token.return_value = SAMPLE_TOKEN_VERIFY
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_client.create_sandbox_config.return_value = {"id": "cfg-1", "name": "ws"}
        mock_client.create_config_workspace.return_value = SAMPLE_WORKSPACE

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.create_workspace(alias="prod", name="ws")

        assert result["backend"] == "snowflake"
        mock_client.verify_token.assert_called_once()
        mock_client.create_config_workspace.assert_called_once_with(
            branch_id=123,
            component_id="keboola.sandboxes",
            config_id="cfg-1",
            backend="snowflake",
            login_type="snowflake-person-keypair",
            public_key=ANY,
        )
        public_key = mock_client.create_config_workspace.call_args.kwargs["public_key"]
        assert public_key.startswith("-----BEGIN PUBLIC KEY-----")

    def test_create_workspace_auto_detects_bigquery(self, tmp_config_dir: Path) -> None:
        """create_workspace auto-detects bigquery backend from project."""
        mock_client = MagicMock()
        mock_client.verify_token.return_value = SAMPLE_TOKEN_VERIFY_BIGQUERY
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_client.create_sandbox_config.return_value = {"id": "cfg-1", "name": "ws"}
        bq_workspace = {
            "id": 42,
            "connection": {
                "backend": "bigquery",
                "schema": "WORKSPACE_42",
            },
        }
        mock_client.create_config_workspace.return_value = bq_workspace

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.create_workspace(alias="prod", name="ws")

        assert result["backend"] == "bigquery"
        mock_client.verify_token.assert_called_once()
        mock_client.create_config_workspace.assert_called_once_with(
            branch_id=123,
            component_id="keboola.sandboxes",
            config_id="cfg-1",
            backend="bigquery",
            login_type="default",
            public_key=None,
        )

    def test_explicit_backend_skips_auto_detect(self, tmp_config_dir: Path) -> None:
        """When --backend is passed explicitly, verify_token is NOT called."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_client.create_sandbox_config.return_value = {"id": "cfg-1", "name": "ws"}
        mock_client.create_config_workspace.return_value = SAMPLE_WORKSPACE

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.create_workspace(alias="prod", name="ws", backend="snowflake")

        assert result["backend"] == "snowflake"
        mock_client.verify_token.assert_not_called()

    def test_from_transformation_auto_detects_bigquery(self, tmp_config_dir: Path) -> None:
        """create_from_transformation auto-detects bigquery backend."""
        mock_client = MagicMock()
        mock_client.verify_token.return_value = SAMPLE_TOKEN_VERIFY_BIGQUERY
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES
        mock_client.get_config_detail.return_value = {
            "id": "456",
            "configuration": {
                "storage": {
                    "input": {
                        "tables": [
                            {"source": "in.c-main.orders", "destination": "orders"},
                        ],
                    },
                },
            },
        }
        mock_client.create_config_workspace.return_value = {
            "id": 55,
            "connection": {
                "backend": "bigquery",
                "schema": "WORKSPACE_55",
                "password": "secret",
            },
        }
        mock_client.load_workspace_tables.return_value = {"id": 888, "status": "success"}

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

        assert result["backend"] == "bigquery"
        mock_client.create_config_workspace.assert_called_once_with(
            branch_id=100,
            component_id="keboola.snowflake-transformation",
            config_id="456",
            backend="bigquery",
            login_type="default",
            public_key=None,
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


class TestListWorkspacesNameResolution:
    """Tests for workspace name resolution from sandbox configs."""

    def test_list_workspaces_resolves_user_given_names(self, tmp_config_dir: Path) -> None:
        """list_workspaces shows user-given name from sandbox config, not internal name."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_client.list_workspaces.return_value = [
            {
                "id": 42,
                "name": "WORKSPACE_42",
                "connection": {"backend": "snowflake", "schema": "WORKSPACE_42"},
                "created": "2025-09-10T14:00:00Z",
                "component": "keboola.sandboxes",
                "configurationId": "cfg-100",
            },
            {
                "id": 99,
                "name": "WORKSPACE_99",
                "connection": {"backend": "snowflake", "schema": "WORKSPACE_99"},
                "created": "2025-09-11T08:30:00Z",
                "component": None,
                "configurationId": None,
            },
        ]
        mock_client.list_component_configs.return_value = [
            {"id": "cfg-100", "name": "my-debug-workspace"},
            {"id": "cfg-200", "name": "another-workspace"},
        ]

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.list_workspaces(aliases=["prod"])
        workspaces = result["workspaces"]

        # Workspace with configurationId gets the user-given name
        assert workspaces[0]["name"] == "my-debug-workspace"
        # Workspace without configurationId falls back to internal name
        assert workspaces[1]["name"] == "WORKSPACE_99"

    def test_list_workspaces_falls_back_on_config_fetch_error(self, tmp_config_dir: Path) -> None:
        """list_workspaces shows internal name when sandbox config fetch fails."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_client.list_workspaces.return_value = [
            {
                "id": 42,
                "name": "WORKSPACE_42",
                "connection": {"backend": "snowflake", "schema": "WORKSPACE_42"},
                "created": "2025-09-10T14:00:00Z",
                "component": "keboola.sandboxes",
                "configurationId": "cfg-100",
            },
        ]
        mock_client.list_component_configs.side_effect = KeboolaApiError(
            message="Forbidden", error_code="ACCESS_DENIED", status_code=403
        )

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.list_workspaces(aliases=["prod"])
        # Falls back to internal name gracefully
        assert result["workspaces"][0]["name"] == "WORKSPACE_42"
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
        """Default path reads inline /results: structured columns+rows + csv_data."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES
        mock_client.submit_query.return_value = {"id": "qj-abc123"}
        mock_client.wait_for_query_job.return_value = {
            "status": "completed",
            "statements": [
                {
                    "id": "stmt-1",
                    "status": "completed",
                    "numberOfRows": 2,
                },
            ],
        }
        mock_client.get_query_results.return_value = {
            "status": "completed",
            "columns": [
                {"name": "col1", "type": "VARCHAR", "nullable": True},
                {"name": "col2", "type": "VARCHAR", "nullable": True},
            ],
            "data": [["a", "b"], ["c", "d"]],
            "numberOfRows": 2,
        }

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
        stmt = result["statements"][0]
        assert stmt["statement_id"] == "stmt-1"
        assert stmt["status"] == "completed"
        assert stmt["rows_affected"] == 2
        assert stmt["columns"] == [
            {"name": "col1", "type": "VARCHAR", "nullable": True},
            {"name": "col2", "type": "VARCHAR", "nullable": True},
        ]
        assert stmt["rows"] == [["a", "b"], ["c", "d"]]
        assert stmt["row_count"] == 2
        assert stmt["total_rows"] == 2
        assert stmt["truncated"] is False
        # csv_data synthesized from columns+rows for legacy consumers.
        assert stmt["csv_data"] == "col1,col2\na,b\nc,d\n"
        # Default path uses the fast inline endpoint, not the CSV export.
        # page_size = min(QUERY_RESULTS_PAGE_SIZE, limit) with the default limit.
        mock_client.get_query_results.assert_called_once_with(
            "qj-abc123",
            "stmt-1",
            offset=0,
            page_size=workspace_service_module.QUERY_RESULTS_PAGE_SIZE,
        )
        mock_client.export_query_results.assert_not_called()

        mock_client.submit_query.assert_called_once_with(
            branch_id=100,
            workspace_id=42,
            statements=["SELECT * FROM orders LIMIT 5"],
            transactional=False,
        )
        # close() called twice: once in _resolve_branch_id, once in execute_query
        assert mock_client.close.call_count == 2

    def test_execute_query_full_uses_csv_export(self, tmp_config_dir: Path) -> None:
        """full=True takes the legacy CSV export path (complete result set)."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES
        mock_client.submit_query.return_value = {"id": "qj-full"}
        mock_client.wait_for_query_job.return_value = {
            "status": "completed",
            "statements": [{"id": "stmt-1", "status": "completed", "numberOfRows": 5}],
        }
        mock_client.export_query_results.return_value = "col1,col2\na,b\nc,d\n"

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.execute_query(
            alias="prod", workspace_id=42, sql="SELECT * FROM orders", full=True
        )

        stmt = result["statements"][0]
        assert stmt["csv_data"] == "col1,col2\na,b\nc,d\n"
        # Full export path carries no structured columns/rows.
        assert "columns" not in stmt
        assert "rows" not in stmt
        mock_client.export_query_results.assert_called_once_with("qj-full", "stmt-1")
        mock_client.get_query_results.assert_not_called()
        # close() twice: once in _resolve_branch_id, once in execute_query.
        assert mock_client.close.call_count == 2

    def test_execute_query_inline_pagination(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A --limit larger than one page walks offset until the limit is reached."""
        # Shrink the page size so a 4-row limit needs two /results calls.
        monkeypatch.setattr(workspace_service_module, "QUERY_RESULTS_PAGE_SIZE", 2)
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES
        mock_client.submit_query.return_value = {"id": "qj-page"}
        mock_client.wait_for_query_job.return_value = {
            "status": "completed",
            "statements": [{"id": "stmt-1", "status": "completed", "numberOfRows": 10}],
        }
        cols = [{"name": "id", "type": "INTEGER", "nullable": False}]
        mock_client.get_query_results.side_effect = [
            {"status": "completed", "columns": cols, "data": [[1], [2]], "numberOfRows": 10},
            {"status": "completed", "columns": cols, "data": [[3], [4]], "numberOfRows": 10},
        ]

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.execute_query(alias="prod", workspace_id=42, sql="SELECT id FROM big", limit=4)

        stmt = result["statements"][0]
        assert stmt["rows"] == [[1], [2], [3], [4]]
        assert stmt["row_count"] == 4
        assert stmt["total_rows"] == 10
        assert stmt["truncated"] is True  # 4 fetched < 10 total
        assert mock_client.get_query_results.call_count == 2
        mock_client.get_query_results.assert_any_call("qj-page", "stmt-1", offset=0, page_size=2)
        mock_client.get_query_results.assert_any_call("qj-page", "stmt-1", offset=2, page_size=2)
        assert mock_client.close.call_count == 2

    def test_execute_query_small_limit_keeps_valid_page_size(self, tmp_config_dir: Path) -> None:
        """A small --limit must NOT shrink pageSize below the API floor (100..100000).

        Regression: deriving pageSize from --limit (e.g. 5) made the Query Service
        reject the /results call with 400 'Invalid pageSize parameter, must be
        between 100 and 100000'. pageSize is now a fixed valid value; --limit only
        trims the accumulated rows locally.
        """
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES
        mock_client.submit_query.return_value = {"id": "qj-small"}
        mock_client.wait_for_query_job.return_value = {
            "status": "completed",
            "statements": [{"id": "stmt-1", "status": "completed", "numberOfRows": 25}],
        }
        # One full page returns all 25 rows; the service must trim to --limit.
        mock_client.get_query_results.return_value = {
            "status": "completed",
            "columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
            "data": [[i] for i in range(25)],
            "numberOfRows": 25,
        }

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.execute_query(alias="prod", workspace_id=42, sql="SELECT id FROM t", limit=5)

        stmt = result["statements"][0]
        assert stmt["row_count"] == 5  # trimmed locally
        assert stmt["total_rows"] == 25
        assert stmt["truncated"] is True
        # pageSize stays at the fixed valid value, NOT the small --limit.
        mock_client.get_query_results.assert_called_once_with(
            "qj-small",
            "stmt-1",
            offset=0,
            page_size=workspace_service_module.QUERY_RESULTS_PAGE_SIZE,
        )
        assert workspace_service_module.QUERY_RESULTS_PAGE_SIZE >= 100
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
        mock_client.get_query_results.assert_not_called()
        mock_client.export_query_results.assert_not_called()

    def test_execute_query_inline_fetch_fails_gracefully(self, tmp_config_dir: Path) -> None:
        """A failed inline /results fetch must not sink the whole query."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES
        mock_client.submit_query.return_value = {"id": "qj-fetch-fail"}
        mock_client.wait_for_query_job.return_value = {
            "status": "completed",
            "statements": [{"id": "stmt-1", "status": "completed", "numberOfRows": 10}],
        }
        mock_client.get_query_results.side_effect = KeboolaApiError(
            message="Results unavailable",
            error_code="QUERY_JOB_FAILED",
            status_code=500,
            retryable=False,
        )

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.execute_query(alias="prod", workspace_id=42, sql="SELECT * FROM big")

        # Statement still reports status + row count, just without a data payload.
        stmt = result["statements"][0]
        assert result["status"] == "completed"
        assert stmt["rows_affected"] == 10
        assert "csv_data" not in stmt
        assert "rows" not in stmt
        assert mock_client.close.call_count == 2

    def test_execute_query_full_export_fails_gracefully(self, tmp_config_dir: Path) -> None:
        """A failed CSV export (full=True) must not sink the whole query."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES
        mock_client.submit_query.return_value = {"id": "qj-export-fail"}
        mock_client.wait_for_query_job.return_value = {
            "status": "completed",
            "statements": [{"id": "stmt-1", "status": "completed", "numberOfRows": 10}],
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
            alias="prod", workspace_id=42, sql="SELECT * FROM big_table", full=True
        )

        # Should still succeed, just without csv_data
        assert result["status"] == "completed"
        assert "csv_data" not in result["statements"][0]
        assert mock_client.close.call_count == 2

    def test_execute_query_truncated_when_number_of_rows_missing(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the Query Service omits numberOfRows, fall back to how the loop ended.

        Stopping at the --limit cap with a full last page (not exhausted) means
        there may be more rows, so `truncated` must be True even without a count.
        """
        monkeypatch.setattr(workspace_service_module, "QUERY_RESULTS_PAGE_SIZE", 2)
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES
        mock_client.submit_query.return_value = {"id": "qj-nocount"}
        mock_client.wait_for_query_job.return_value = {
            "status": "completed",
            "statements": [{"id": "stmt-1", "status": "completed", "numberOfRows": 2}],
        }
        # A full page (== page_size) with NO numberOfRows in the payload.
        mock_client.get_query_results.return_value = {
            "status": "completed",
            "columns": [{"name": "id"}],
            "data": [[1], [2]],
        }

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.execute_query(alias="prod", workspace_id=42, sql="SELECT id FROM t", limit=2)

        stmt = result["statements"][0]
        assert stmt["total_rows"] is None
        assert stmt["truncated"] is True  # full last page, capped at limit -> maybe more


class TestRowsToCsv:
    """Tests for the module-level _rows_to_csv / _csv_cell helpers."""

    def test_none_becomes_empty_field(self) -> None:
        csv_str = workspace_service_module._rows_to_csv(
            [{"name": "a"}, {"name": "b"}], [["x", None]]
        )
        assert csv_str == "a,b\nx,\n"

    def test_dict_and_list_cells_serialize_as_compact_json(self) -> None:
        """VARIANT/ARRAY/OBJECT cells arrive as Python dict/list -- emit compact
        JSON (``{"k":"v"}``), not Python repr (``{'k': 'v'}``), to match the
        warehouse CSV export.
        """
        csv_str = workspace_service_module._rows_to_csv(
            [{"name": "payload"}, {"name": "tags"}],
            [[{"k": "v", "n": 1}, [1, 2, 3]]],
        )
        # csv.writer quotes fields containing commas.
        assert '"{""k"":""v"",""n"":1}"' in csv_str
        assert '"[1,2,3]"' in csv_str
        # No Python-repr artifacts (single quotes / spaces after colon).
        assert "'k'" not in csv_str
        assert "{'" not in csv_str


class TestCreateFromTransformation:
    """Tests for WorkspaceService.create_from_transformation()."""

    def test_create_from_transformation_success(self, tmp_config_dir: Path) -> None:
        """create_from_transformation reads config, creates workspace, loads tables."""
        mock_client = MagicMock()
        mock_client.verify_token.return_value = SAMPLE_TOKEN_VERIFY
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
        assert result["private_key"].startswith("-----BEGIN PRIVATE KEY-----")
        assert result["tables_loaded"] == ["in.c-main.orders", "in.c-main.products"]
        assert "2 table(s) loaded" in result["message"]
        assert "Save the private key" in result["message"]

        mock_client.get_config_detail.assert_called_once_with(
            "keboola.snowflake-transformation",
            "456",
        )
        mock_client.create_config_workspace.assert_called_once_with(
            branch_id=100,
            component_id="keboola.snowflake-transformation",
            config_id="456",
            backend="snowflake",
            login_type="snowflake-person-keypair",
            public_key=ANY,
        )
        public_key = mock_client.create_config_workspace.call_args.kwargs["public_key"]
        assert public_key.startswith("-----BEGIN PUBLIC KEY-----")
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
        assert project is not None

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
        assert project is not None
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
        assert project is not None
        with pytest.raises(ConfigError, match="No default branch found"):
            svc._resolve_branch_id("prod", project)


class TestIssue304WorkspaceListEnrichment:
    """Tests for the fields added to `list_workspaces` to close issue #304."""

    def test_list_exposes_login_type_and_qs_compatible(self, tmp_config_dir: Path) -> None:
        """Each workspace entry carries login_type, read_only, qs_compatible."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 1, "isDefault": True}]
        mock_client.list_workspaces.return_value = [
            {
                "id": 1,
                "name": "compat",
                "connection": {
                    "backend": "snowflake",
                    "host": "h",
                    "schema": "S1",
                    "user": "U1",
                    "loginType": "snowflake-service-keypair",
                },
                "readOnlyStorageAccess": True,
                "created": "2026-05-18T00:00:00Z",
                "component": "keboola.sandboxes",
                "configurationId": "cfg-1",
            },
            {
                "id": 2,
                "name": "legacy-rw",
                "connection": {
                    "backend": "snowflake",
                    "host": "h",
                    "schema": "S2",
                    "user": "U2",
                    "loginType": "default",
                },
                "readOnlyStorageAccess": False,
                "created": "2026-05-18T00:00:00Z",
                "component": "keboola.snowflake-transformation",
                "configurationId": "cfg-2",
            },
            {
                "id": 3,
                "name": "person-keypair",
                "connection": {
                    "backend": "snowflake",
                    "host": "h",
                    "schema": "S3",
                    "user": "U3",
                    "loginType": "snowflake-person-keypair",
                },
                "readOnlyStorageAccess": True,
                "created": "2026-05-18T00:00:00Z",
                "component": "keboola.sandboxes",
                "configurationId": "cfg-3",
            },
        ]
        mock_client.list_component_configs.return_value = []

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.list_workspaces(aliases=["prod"])

        workspaces = result["workspaces"]
        assert len(workspaces) == 3
        compat = next(w for w in workspaces if w["id"] == 1)
        legacy = next(w for w in workspaces if w["id"] == 2)
        person_keypair = next(w for w in workspaces if w["id"] == 3)

        assert compat["login_type"] == "snowflake-service-keypair"
        assert compat["read_only"] is True
        assert compat["qs_compatible"] is True

        assert person_keypair["login_type"] == "snowflake-person-keypair"
        assert person_keypair["read_only"] is True
        assert person_keypair["qs_compatible"] is True

        assert legacy["login_type"] == "default"
        assert legacy["read_only"] is False
        # ``default`` is intentionally OFF the whitelist (legacy 2016 ws,
        # confirmed broken with Query Service via "JWT token is invalid")
        assert legacy["qs_compatible"] is False

    def test_qs_compatible_filter_requires_both_compat_and_ro(self, tmp_config_dir: Path) -> None:
        """`qs_compatible_only=True` drops workspaces that are compat-but-RW or RO-but-unknown."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 1, "isDefault": True}]
        mock_client.list_workspaces.return_value = [
            # Hit: compat + RO
            {
                "id": 1,
                "name": "data-app-ready",
                "connection": {
                    "backend": "snowflake",
                    "loginType": "snowflake-service-keypair",
                },
                "readOnlyStorageAccess": True,
            },
            # Miss: compat but RW
            {
                "id": 2,
                "name": "compat-but-rw",
                "connection": {
                    "backend": "snowflake",
                    "loginType": "snowflake-person-sso",
                },
                "readOnlyStorageAccess": False,
            },
            # Miss: RO but legacy loginType
            {
                "id": 3,
                "name": "ro-but-legacy",
                "connection": {
                    "backend": "snowflake",
                    "loginType": "default",
                },
                "readOnlyStorageAccess": True,
            },
        ]
        mock_client.list_component_configs.return_value = []

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.list_workspaces(aliases=["prod"], qs_compatible_only=True)

        ids = [w["id"] for w in result["workspaces"]]
        assert ids == [1]

    def test_explicit_branch_id_propagated_to_client(self, tmp_config_dir: Path) -> None:
        """When the command layer passes branch_id, the client call uses it verbatim."""
        mock_client = MagicMock()
        mock_client.list_workspaces.return_value = []
        mock_client.list_component_configs.return_value = []

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        svc.list_workspaces(aliases=["prod"], branch_id=42)

        mock_client.list_workspaces.assert_called_once_with(branch_id=42)
        # `_resolve_branch_id` MUST NOT be invoked when caller supplied a branch
        mock_client.list_dev_branches.assert_not_called()

    def test_explicit_branch_with_multi_project_raises(self, tmp_config_dir: Path) -> None:
        """A branch ID is per-project; multi-project + branch_id is a usage bug."""
        store = setup_two_projects(tmp_config_dir)
        svc = WorkspaceService(config_store=store)

        with pytest.raises(ConfigError, match="branch_id requires exactly one alias"):
            svc.list_workspaces(branch_id=99)


class TestIssue304ResolveSandboxWorkspaceId:
    """Tests for ``resolve_sandbox_workspace_id`` (issue #304 bod #3)."""

    def test_resolves_matching_workspace(self, tmp_config_dir: Path) -> None:
        """A keboola.sandboxes config ID maps back to the workspace pointing at it."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 1, "isDefault": True}]
        mock_client.list_workspaces.return_value = [
            {
                "id": 7777,
                "component": "keboola.snowflake-transformation",
                "configurationId": "cfg-other",
            },
            {
                "id": 9999,
                "component": "keboola.sandboxes",
                "configurationId": "cfg-target",
            },
        ]

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        assert svc.resolve_sandbox_workspace_id("prod", "cfg-target") == 9999

    def test_returns_none_for_orphan_config(self, tmp_config_dir: Path) -> None:
        """No workspace currently backs the config -> None (not raise)."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 1, "isDefault": True}]
        mock_client.list_workspaces.return_value = [
            {
                "id": 7777,
                "component": "keboola.sandboxes",
                "configurationId": "cfg-other",
            },
        ]

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        assert svc.resolve_sandbox_workspace_id("prod", "cfg-orphan") is None

    def test_uses_explicit_branch(self, tmp_config_dir: Path) -> None:
        """Caller-supplied branch_id is passed to list_workspaces."""
        mock_client = MagicMock()
        mock_client.list_workspaces.return_value = []

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        svc.resolve_sandbox_workspace_id("prod", "cfg-1", branch_id=555)

        mock_client.list_workspaces.assert_called_once_with(branch_id=555)
        mock_client.list_dev_branches.assert_not_called()


class TestIssue304GetWorkspaceEnrichment:
    """Tests for the fields added to `get_workspace` to close issue #304."""

    def test_detail_exposes_login_type_and_qs_compatible(self, tmp_config_dir: Path) -> None:
        """`get_workspace` returns login_type / read_only / qs_compatible."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 1, "isDefault": True}]
        mock_client.get_workspace.return_value = {
            "id": 42,
            "component": "keboola.sandboxes",
            "configurationId": "cfg-42",
            "connection": {
                "backend": "snowflake",
                "host": "h",
                "schema": "S",
                "user": "U",
                "warehouse": "W",
                "database": "D",
                "loginType": "snowflake-person-sso",
            },
            "readOnlyStorageAccess": True,
            "created": "2026-05-18T00:00:00Z",
        }

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.get_workspace(alias="prod", workspace_id=42)

        assert result["login_type"] == "snowflake-person-sso"
        assert result["read_only"] is True
        assert result["qs_compatible"] is True
        assert result["component_id"] == "keboola.sandboxes"
        assert result["config_id"] == "cfg-42"


class TestBigQueryQueryServiceSupport:
    """BigQuery Query-Service compatibility added in v0.58.0.

    Verified live against project 9621 on connection.keboola.com: BigQuery
    workspaces carry loginType ``default`` and the Query Service runs SELECTs
    against them. The fix makes qs_compatibility backend-aware so BigQuery's
    ``default`` is whitelisted while Snowflake's legacy ``default`` stays off.
    """

    def test_classify_bigquery_default_is_compatible(self) -> None:
        from keboola_agent_cli.services.workspace_service import _classify_qs_compatibility

        assert _classify_qs_compatibility("default", "bigquery") is True
        # Backend match is case-insensitive.
        assert _classify_qs_compatibility("default", "BigQuery") is True

    def test_classify_snowflake_default_stays_incompatible(self) -> None:
        """Regression guard: Snowflake legacy ``default`` must NOT inherit the
        BigQuery whitelist -- it is rejected with 'JWT token is invalid'."""
        from keboola_agent_cli.services.workspace_service import _classify_qs_compatibility

        assert _classify_qs_compatibility("default", "snowflake") is False

    def test_classify_bigquery_rejects_snowflake_login_types(self) -> None:
        """Backends do not share login types: a Snowflake loginType is not on
        the BigQuery whitelist."""
        from keboola_agent_cli.services.workspace_service import _classify_qs_compatibility

        assert _classify_qs_compatibility("snowflake-person-sso", "bigquery") is False

    def test_login_type_for_bigquery_backend_is_default(self) -> None:
        from keboola_agent_cli.services.workspace_service import (
            _workspace_login_type_for_backend,
        )

        assert _workspace_login_type_for_backend("bigquery") == "default"
        assert _workspace_login_type_for_backend("BigQuery") == "default"

    def test_login_type_for_snowflake_and_unknown_backend(self) -> None:
        from keboola_agent_cli.services.workspace_service import (
            _workspace_login_type_for_backend,
        )

        assert _workspace_login_type_for_backend("snowflake") == "snowflake-person-keypair"
        assert _workspace_login_type_for_backend("exasol") is None

    def test_list_marks_bigquery_default_workspace_compatible(self, tmp_config_dir: Path) -> None:
        """End-to-end through the service: a BigQuery ``default`` RO workspace
        is qs_compatible (mirrors the real project-9621 shape)."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = [{"id": 1, "isDefault": True}]
        mock_client.list_workspaces.return_value = [
            {
                "id": 7,
                "name": "bq-ro",
                "connection": {
                    "backend": "bigquery",
                    "schema": "WORKSPACE_7",
                    "user": '{"type":"service_account","project_id":"sapi-9621"}',
                    "loginType": "default",
                },
                "readOnlyStorageAccess": True,
                "component": "keboola.sandboxes",
                "configurationId": "cfg-7",
            },
        ]
        mock_client.list_component_configs.return_value = []

        store = setup_single_project(tmp_config_dir)
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.list_workspaces(aliases=["prod"], qs_compatible_only=True)

        ids = [w["id"] for w in result["workspaces"]]
        assert ids == [7]
        assert result["workspaces"][0]["qs_compatible"] is True
