"""Tests for `kbagent component sync-action` (issue #395, MCP run_sync_action port).

Covers three layers:
- L3 KeboolaClient.run_sync_action: sync-actions host derivation, camelCase
  body keys, branchId omission for production, token header inheritance.
- L2 ComponentService.run_sync_action: override path, root-only configData,
  root+row SHALLOW top-level merge semantics (no deep merge), branch
  pass-through, config_id validation.
- L1 CLI command: --json envelope, --config-data JSON|@file|- parsing,
  usage validation, human render, API error mapping.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from helpers import setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.component_service import ComponentService
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
STACK_URL = "https://connection.keboola.com"
SYNC_ACTIONS_URL = "https://sync-actions.keboola.com"

COMPONENT_ID = "keboola.ex-db-mysql"
CONFIG_ID = "cfg-001"
ROW_ID = "row-001"

runner = CliRunner()


# ---------------------------------------------------------------------------
# L3 client tests
# ---------------------------------------------------------------------------


class TestClientRunSyncAction:
    """Tests for KeboolaClient.run_sync_action (sync-actions sub-client)."""

    def test_derive_sync_actions_url(self) -> None:
        """Host derivation: connection.<region> -> sync-actions.<region>."""
        result = KeboolaClient._derive_service_url(
            "https://connection.eu-central-1.keboola.com", "sync-actions"
        )
        assert result == "https://sync-actions.eu-central-1.keboola.com"

    def test_run_sync_action_posts_camelcase_body(self, httpx_mock) -> None:
        """POST /actions carries configData/componentId/action (camelCase)."""
        httpx_mock.add_response(
            url=f"{SYNC_ACTIONS_URL}/actions",
            method="POST",
            json={"status": "success"},
            status_code=200,
        )

        config_data = {"parameters": {"db": {"host": "example.com"}}, "storage": {}}
        with KeboolaClient(stack_url=STACK_URL, token=TEST_TOKEN) as client:
            result = client.run_sync_action(COMPONENT_ID, "testConnection", config_data)

        assert result == {"status": "success"}
        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        body = json.loads(requests[0].content)
        assert body == {
            "configData": config_data,
            "componentId": COMPONENT_ID,
            "action": "testConnection",
        }
        # branchId must be OMITTED entirely for production
        assert "branchId" not in body
        # Sub-client inherits the Storage API token header
        assert requests[0].headers["X-StorageApi-Token"] == TEST_TOKEN

    def test_run_sync_action_sends_branch_id_when_set(self, httpx_mock) -> None:
        """branchId is included in the body when branch_id is given."""
        httpx_mock.add_response(
            url=f"{SYNC_ACTIONS_URL}/actions",
            method="POST",
            json=[{"name": "table1"}],
            status_code=200,
        )

        with KeboolaClient(stack_url=STACK_URL, token=TEST_TOKEN) as client:
            result = client.run_sync_action(
                COMPONENT_ID, "getTables", {"parameters": {}}, branch_id=456
            )

        # Opaque pass-through: list results survive verbatim
        assert result == [{"name": "table1"}]
        body = json.loads(httpx_mock.get_requests()[0].content)
        assert body["branchId"] == 456

    def test_run_sync_action_api_error(self, httpx_mock) -> None:
        """Non-retryable HTTP errors raise KeboolaApiError."""
        httpx_mock.add_response(
            url=f"{SYNC_ACTIONS_URL}/actions",
            method="POST",
            json={"error": "Action 'nope' not found"},
            status_code=404,
        )

        with (
            KeboolaClient(stack_url=STACK_URL, token=TEST_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.run_sync_action(COMPONENT_ID, "nope", {"parameters": {}})

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# L2 service tests
# ---------------------------------------------------------------------------


def _root_config_response(
    parameters: dict[str, Any] | None = None,
    storage: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
    authorization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    configuration: dict[str, Any] = {
        "parameters": parameters if parameters is not None else {},
        "storage": storage if storage is not None else {},
    }
    if runtime is not None:
        configuration["runtime"] = runtime
    if authorization is not None:
        configuration["authorization"] = authorization
    return {
        "id": CONFIG_ID,
        "name": "MySQL extractor",
        "configuration": configuration,
    }


def _row_config_response(
    parameters: dict[str, Any] | None = None,
    storage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": ROW_ID,
        "configuration": {
            "parameters": parameters if parameters is not None else {},
            "storage": storage if storage is not None else {},
        },
    }


def _make_service(tmp_config_dir: Path, client: MagicMock) -> ComponentService:
    store = setup_single_project(tmp_config_dir)
    return ComponentService(
        config_store=store,
        client_factory=lambda url, token: client,
    )


class TestRunSyncActionService:
    """Tests for ComponentService.run_sync_action."""

    def test_override_path_sends_verbatim(self, tmp_config_dir: Path) -> None:
        """config_data_override is sent as-is; no config fetch happens."""
        client = MagicMock()
        client.run_sync_action.return_value = {"status": "success"}
        service = _make_service(tmp_config_dir, client)

        override = {"parameters": {"db": {"host": "explicit.example.com"}}}
        result = service.run_sync_action(
            alias="prod",
            component_id=COMPONENT_ID,
            action="testConnection",
            config_data_override=override,
        )

        assert result == {
            "component_id": COMPONENT_ID,
            "action": "testConnection",
            "result": {"status": "success"},
        }
        client.get_config_detail.assert_not_called()
        client.get_config_row.assert_not_called()
        client.run_sync_action.assert_called_once_with(
            COMPONENT_ID,
            "testConnection",
            override,
            branch_id=None,
            timeout=None,
        )
        client.close.assert_called_once()

    def test_root_only_builds_config_data_from_root(self, tmp_config_dir: Path) -> None:
        """Without row_id, configData is the root parameters + storage."""
        client = MagicMock()
        client.get_config_detail.return_value = _root_config_response(
            parameters={"db": {"host": "root.example.com", "port": 3306}},
            storage={"input": {"tables": [{"source": "in.c-main.a"}]}},
        )
        client.run_sync_action.return_value = {"status": "success"}
        service = _make_service(tmp_config_dir, client)

        service.run_sync_action(
            alias="prod",
            component_id=COMPONENT_ID,
            action="testConnection",
            config_id=CONFIG_ID,
        )

        client.get_config_detail.assert_called_once_with(COMPONENT_ID, CONFIG_ID, branch_id=None)
        client.get_config_row.assert_not_called()
        sent_config_data = client.run_sync_action.call_args.args[2]
        assert sent_config_data == {
            "parameters": {"db": {"host": "root.example.com", "port": 3306}},
            "storage": {"input": {"tables": [{"source": "in.c-main.a"}]}},
        }

    def test_root_row_shallow_merge_not_deep(self, tmp_config_dir: Path) -> None:
        """Row keys REPLACE root keys at the top level -- never deep-merged.

        The root has parameters.db = {host, port}; the row overrides
        parameters.db = {host}. A deep merge would keep root's port; the MCP
        parity semantics require the row dict to replace root's wholesale.
        """
        client = MagicMock()
        client.get_config_detail.return_value = _root_config_response(
            parameters={
                "db": {"host": "root.example.com", "port": 3306},
                "rootOnly": "kept",
            },
            storage={"input": {"tables": [{"source": "in.c-main.root"}]}},
        )
        client.get_config_row.return_value = _row_config_response(
            parameters={"db": {"host": "row.example.com"}},
            storage={"input": {"tables": [{"source": "in.c-main.row"}]}},
        )
        client.run_sync_action.return_value = {"status": "success"}
        service = _make_service(tmp_config_dir, client)

        service.run_sync_action(
            alias="prod",
            component_id=COMPONENT_ID,
            action="getTables",
            config_id=CONFIG_ID,
            row_id=ROW_ID,
        )

        client.get_config_row.assert_called_once_with(
            COMPONENT_ID, CONFIG_ID, ROW_ID, branch_id=None
        )
        sent_config_data = client.run_sync_action.call_args.args[2]
        # Top-level key from row replaces root's dict wholesale:
        assert sent_config_data["parameters"]["db"] == {"host": "row.example.com"}
        assert "port" not in sent_config_data["parameters"]["db"], (
            "root's nested 'port' must NOT survive -- shallow merge, not deep"
        )
        # Root-only top-level keys survive the shallow merge:
        assert sent_config_data["parameters"]["rootOnly"] == "kept"
        # storage merged independently with the same semantics:
        assert sent_config_data["storage"] == {"input": {"tables": [{"source": "in.c-main.row"}]}}

    def test_authorization_and_runtime_forwarded_from_root(self, tmp_config_dir: Path) -> None:
        """OAuth/Service-Account components need root authorization+runtime forwarded (AI-3757).

        Without this, e.g. keboola.ex-linkedin-ads never receives its OAuth
        broker reference and crashes before its own error handling runs,
        surfacing as an opaque empty-body 400.
        """
        client = MagicMock()
        client.get_config_detail.return_value = _root_config_response(
            parameters={"ad_account_id": "123"},
            runtime={"parallelism": "5"},
            authorization={"oauth_api": {"id": "linkedin-ads"}},
        )
        client.get_config_row.return_value = _row_config_response(
            parameters={"ad_account_id": "456"}
        )
        client.run_sync_action.return_value = {"accounts": []}
        service = _make_service(tmp_config_dir, client)

        service.run_sync_action(
            alias="prod",
            component_id="keboola.ex-linkedin-ads",
            action="list_accounts",
            config_id=CONFIG_ID,
            row_id=ROW_ID,
        )

        sent_config_data = client.run_sync_action.call_args.args[2]
        assert sent_config_data["runtime"] == {"parallelism": "5"}
        assert sent_config_data["authorization"] == {"oauth_api": {"id": "linkedin-ads"}}

    def test_authorization_and_runtime_omitted_when_absent(self, tmp_config_dir: Path) -> None:
        """No authorization/runtime key at all when the root config has none."""
        client = MagicMock()
        client.get_config_detail.return_value = _root_config_response(
            parameters={"host": "example.com"}
        )
        client.run_sync_action.return_value = {"status": "success"}
        service = _make_service(tmp_config_dir, client)

        service.run_sync_action(
            alias="prod",
            component_id=COMPONENT_ID,
            action="testConnection",
            config_id=CONFIG_ID,
        )

        sent_config_data = client.run_sync_action.call_args.args[2]
        assert "runtime" not in sent_config_data
        assert "authorization" not in sent_config_data

    def test_branch_pass_through(self, tmp_config_dir: Path) -> None:
        """branch_id flows to config fetch, row fetch, and the action call."""
        client = MagicMock()
        client.get_config_detail.return_value = _root_config_response()
        client.get_config_row.return_value = _row_config_response()
        client.run_sync_action.return_value = {"status": "success"}
        service = _make_service(tmp_config_dir, client)

        service.run_sync_action(
            alias="prod",
            component_id=COMPONENT_ID,
            action="testConnection",
            config_id=CONFIG_ID,
            row_id=ROW_ID,
            branch_id=456,
        )

        client.get_config_detail.assert_called_once_with(COMPONENT_ID, CONFIG_ID, branch_id=456)
        client.get_config_row.assert_called_once_with(
            COMPONENT_ID, CONFIG_ID, ROW_ID, branch_id=456
        )
        assert client.run_sync_action.call_args.kwargs["branch_id"] == 456

    def test_timeout_pass_through(self, tmp_config_dir: Path) -> None:
        """timeout is forwarded to the client action call."""
        client = MagicMock()
        client.run_sync_action.return_value = {"status": "success"}
        service = _make_service(tmp_config_dir, client)

        service.run_sync_action(
            alias="prod",
            component_id=COMPONENT_ID,
            action="testConnection",
            config_data_override={"parameters": {}},
            timeout=120,
        )

        assert client.run_sync_action.call_args.kwargs["timeout"] == 120

    def test_missing_config_id_without_override_raises(self, tmp_config_dir: Path) -> None:
        """Neither config_id nor override -> ConfigError; client still closed."""
        client = MagicMock()
        service = _make_service(tmp_config_dir, client)

        with pytest.raises(ConfigError, match="configuration ID"):
            service.run_sync_action(
                alias="prod",
                component_id=COMPONENT_ID,
                action="testConnection",
            )

        client.run_sync_action.assert_not_called()
        client.close.assert_called_once()

    def test_api_error_propagates_and_closes_client(self, tmp_config_dir: Path) -> None:
        """Errors from the action call bubble up; client is closed."""
        client = MagicMock()
        client.get_config_detail.return_value = _root_config_response()
        client.run_sync_action.side_effect = KeboolaApiError(
            message="Action failed",
            status_code=400,
            error_code="API_ERROR",
            retryable=False,
        )
        service = _make_service(tmp_config_dir, client)

        with pytest.raises(KeboolaApiError):
            service.run_sync_action(
                alias="prod",
                component_id=COMPONENT_ID,
                action="testConnection",
                config_id=CONFIG_ID,
            )

        client.close.assert_called_once()


# ---------------------------------------------------------------------------
# L1 CLI tests
# ---------------------------------------------------------------------------


def _invoke(tmp_path: Path, mock_svc: MagicMock, args: list[str]):
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url=STACK_URL,
            token=TEST_TOKEN,
            project_name="prod",
            project_id=1234,
        ),
    )

    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
        patch("keboola_agent_cli.cli.ComponentService") as MockCompService,
    ):
        MockStore.return_value = store
        MockProjService.return_value = ProjectService(config_store=store)
        MockCompService.return_value = mock_svc

        return runner.invoke(app, args)


def _sync_action_result() -> dict[str, Any]:
    return {
        "component_id": COMPONENT_ID,
        "action": "testConnection",
        "result": {"status": "success"},
    }


class TestComponentSyncActionCli:
    """Tests for `kbagent component sync-action` command."""

    def test_sync_action_json(self, tmp_path: Path) -> None:
        """--json emits the {component_id, action, result} envelope."""
        mock_svc = MagicMock()
        mock_svc.run_sync_action.return_value = _sync_action_result()

        result = _invoke(
            tmp_path,
            mock_svc,
            [
                "--json",
                "component",
                "sync-action",
                "testConnection",
                "--component-id",
                COMPONENT_ID,
                "--config-id",
                CONFIG_ID,
                "--project",
                "prod",
            ],
        )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"] == _sync_action_result()
        mock_svc.run_sync_action.assert_called_once_with(
            alias="prod",
            component_id=COMPONENT_ID,
            action="testConnection",
            config_id=CONFIG_ID,
            row_id=None,
            branch_id=None,
            config_data_override=None,
            timeout=None,
        )

    def test_sync_action_config_data_inline(self, tmp_path: Path) -> None:
        """--config-data inline JSON is parsed and passed as the override."""
        mock_svc = MagicMock()
        mock_svc.run_sync_action.return_value = _sync_action_result()

        result = _invoke(
            tmp_path,
            mock_svc,
            [
                "--json",
                "component",
                "sync-action",
                "testConnection",
                "--component-id",
                COMPONENT_ID,
                "--project",
                "prod",
                "--config-data",
                '{"parameters": {"db": {"host": "explicit.example.com"}}}',
            ],
        )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        call_kwargs = mock_svc.run_sync_action.call_args.kwargs
        assert call_kwargs["config_data_override"] == {
            "parameters": {"db": {"host": "explicit.example.com"}}
        }
        assert call_kwargs["config_id"] is None

    def test_sync_action_config_data_from_file(self, tmp_path: Path) -> None:
        """--config-data @file.json reads the payload from disk."""
        payload = {"parameters": {"token": "from-file"}}
        payload_file = tmp_path / "payload.json"
        payload_file.write_text(json.dumps(payload), encoding="utf-8")

        mock_svc = MagicMock()
        mock_svc.run_sync_action.return_value = _sync_action_result()

        result = _invoke(
            tmp_path,
            mock_svc,
            [
                "--json",
                "component",
                "sync-action",
                "testConnection",
                "--component-id",
                COMPONENT_ID,
                "--project",
                "prod",
                "--config-data",
                f"@{payload_file}",
            ],
        )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert mock_svc.run_sync_action.call_args.kwargs["config_data_override"] == payload

    def test_sync_action_invalid_config_data(self, tmp_path: Path) -> None:
        """Malformed --config-data JSON is a validation error (exit 2)."""
        mock_svc = MagicMock()

        result = _invoke(
            tmp_path,
            mock_svc,
            [
                "--json",
                "component",
                "sync-action",
                "testConnection",
                "--component-id",
                COMPONENT_ID,
                "--project",
                "prod",
                "--config-data",
                "{not json",
            ],
        )

        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "VALIDATION_ERROR" in output["error"]["code"]
        mock_svc.run_sync_action.assert_not_called()

    def test_sync_action_missing_config_id_and_data(self, tmp_path: Path) -> None:
        """Neither --config-id nor --config-data -> usage error (exit 2)."""
        mock_svc = MagicMock()

        result = _invoke(
            tmp_path,
            mock_svc,
            [
                "--json",
                "component",
                "sync-action",
                "testConnection",
                "--component-id",
                COMPONENT_ID,
                "--project",
                "prod",
            ],
        )

        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "MISSING_PARAMETER" in output["error"]["code"]
        mock_svc.run_sync_action.assert_not_called()

    def test_sync_action_row_id_requires_config_id(self, tmp_path: Path) -> None:
        """--row-id without --config-id is rejected (exit 2)."""
        mock_svc = MagicMock()

        result = _invoke(
            tmp_path,
            mock_svc,
            [
                "--json",
                "component",
                "sync-action",
                "testConnection",
                "--component-id",
                COMPONENT_ID,
                "--project",
                "prod",
                "--row-id",
                ROW_ID,
                "--config-data",
                "{}",
            ],
        )

        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "INVALID_ARGUMENT" in output["error"]["code"]
        mock_svc.run_sync_action.assert_not_called()

    def test_sync_action_branch_pass_through(self, tmp_path: Path) -> None:
        """Explicit --branch is forwarded to the service as branch_id."""
        mock_svc = MagicMock()
        mock_svc.run_sync_action.return_value = _sync_action_result()

        result = _invoke(
            tmp_path,
            mock_svc,
            [
                "--json",
                "component",
                "sync-action",
                "getTables",
                "--component-id",
                COMPONENT_ID,
                "--config-id",
                CONFIG_ID,
                "--row-id",
                ROW_ID,
                "--project",
                "prod",
                "--branch",
                "456",
                "--timeout",
                "120",
            ],
        )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        mock_svc.run_sync_action.assert_called_once_with(
            alias="prod",
            component_id=COMPONENT_ID,
            action="getTables",
            config_id=CONFIG_ID,
            row_id=ROW_ID,
            branch_id=456,
            config_data_override=None,
            timeout=120,
        )

    def test_sync_action_human(self, tmp_path: Path) -> None:
        """Human mode renders a JSON syntax panel with the action title."""
        mock_svc = MagicMock()
        mock_svc.run_sync_action.return_value = _sync_action_result()

        result = _invoke(
            tmp_path,
            mock_svc,
            [
                "component",
                "sync-action",
                "testConnection",
                "--component-id",
                COMPONENT_ID,
                "--config-id",
                CONFIG_ID,
                "--project",
                "prod",
            ],
        )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "testConnection" in result.output
        assert "status" in result.output
        assert "success" in result.output

    def test_sync_action_api_error_auth(self, tmp_path: Path) -> None:
        """INVALID_TOKEN maps to exit code 3 (auth error)."""
        mock_svc = MagicMock()
        mock_svc.run_sync_action.side_effect = KeboolaApiError(
            message="Invalid access token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        result = _invoke(
            tmp_path,
            mock_svc,
            [
                "--json",
                "component",
                "sync-action",
                "testConnection",
                "--component-id",
                COMPONENT_ID,
                "--config-id",
                CONFIG_ID,
                "--project",
                "prod",
            ],
        )

        assert result.exit_code == 3
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "INVALID_TOKEN" in output["error"]["code"]

    def test_sync_action_api_error_general(self, tmp_path: Path) -> None:
        """Generic API errors map to exit code 1."""
        mock_svc = MagicMock()
        mock_svc.run_sync_action.side_effect = KeboolaApiError(
            message="Action 'nope' not found",
            status_code=404,
            error_code="NOT_FOUND",
            retryable=False,
        )

        result = _invoke(
            tmp_path,
            mock_svc,
            [
                "--json",
                "component",
                "sync-action",
                "nope",
                "--component-id",
                COMPONENT_ID,
                "--config-id",
                CONFIG_ID,
                "--project",
                "prod",
            ],
        )

        assert result.exit_code == 1
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "NOT_FOUND" in output["error"]["code"]

    def test_sync_action_config_error(self, tmp_path: Path) -> None:
        """ConfigError from the service maps to exit code 5."""
        mock_svc = MagicMock()
        mock_svc.run_sync_action.side_effect = ConfigError("Project 'nope' not found")

        result = _invoke(
            tmp_path,
            mock_svc,
            [
                "--json",
                "component",
                "sync-action",
                "testConnection",
                "--component-id",
                COMPONENT_ID,
                "--config-id",
                CONFIG_ID,
                "--project",
                "prod",
            ],
        )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "CONFIG_ERROR" in output["error"]["code"]
