"""Tests for flow CLI commands via CliRunner.

Tests all flow subcommands: list, detail, schema, new, update, delete,
schedule, schedule-remove. Follows the existing CLI test pattern with
patched services in ctx.obj.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig

runner = CliRunner()
TEST_TOKEN = "999-token-abc"


def _setup_config(config_dir: Path, projects: dict[str, dict] | None = None) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    if projects:
        for alias, info in projects.items():
            store.add_project(
                alias,
                ProjectConfig(
                    stack_url=info.get("stack_url", "https://connection.keboola.com"),
                    token=info.get("token", TEST_TOKEN),
                    project_name=info.get("project_name", alias),
                    project_id=info.get("project_id", 1234),
                ),
            )
    return store


def _run(args: list[str], store: ConfigStore) -> Any:
    """Run CLI with the given args and a fresh mock flow_service."""
    mock_flow = MagicMock()
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
    ):
        MockStore.return_value = store
        MockFlowService.return_value = mock_flow
        result = runner.invoke(app, args)
        return result, mock_flow


# ---------------------------------------------------------------------------
# flow list
# ---------------------------------------------------------------------------


class TestFlowList:
    def test_list_json(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.list_flows.return_value = {
            "flows": [
                {
                    "project_alias": "prod",
                    "component_id": "keboola.orchestrator",
                    "config_id": "111",
                    "name": "Daily ETL",
                    "description": "",
                    "is_disabled": False,
                }
            ],
            "errors": [],
        }
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
        ):
            MockStore.return_value = store
            MockFlowService.return_value = mock_flow
            result = runner.invoke(app, ["--json", "flow", "list", "--project", "prod"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["flows"][0]["config_id"] == "111"
        mock_flow.list_flows.assert_called_once_with(aliases=["prod"], branch_id=None)

    def test_list_empty(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.list_flows.return_value = {"flows": [], "errors": []}
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
        ):
            MockStore.return_value = store
            MockFlowService.return_value = mock_flow
            result = runner.invoke(app, ["flow", "list"])

        assert result.exit_code == 0

    def test_list_config_error(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.list_flows.side_effect = ConfigError("No projects")
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
        ):
            MockStore.return_value = store
            MockFlowService.return_value = mock_flow
            result = runner.invoke(app, ["--json", "flow", "list"])

        assert result.exit_code == 5

    def test_list_all_projects_no_project_flag(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}, "dev": {}})
        mock_flow = MagicMock()
        mock_flow.list_flows.return_value = {
            "flows": [
                {
                    "project_alias": "prod",
                    "component_id": "keboola.orchestrator",
                    "config_id": "111",
                    "name": "Flow A",
                    "description": "",
                    "is_disabled": False,
                },
                {
                    "project_alias": "dev",
                    "component_id": "keboola.flow",
                    "config_id": "222",
                    "name": "Flow B",
                    "description": "",
                    "is_disabled": False,
                },
            ],
            "errors": [],
        }
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
        ):
            MockStore.return_value = store
            MockFlowService.return_value = mock_flow
            result = runner.invoke(app, ["--json", "flow", "list"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data["data"]["flows"]) == 2
        # aliases=None means all projects
        mock_flow.list_flows.assert_called_once_with(aliases=None, branch_id=None)

    def test_branch_without_project_fails(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
        ):
            MockStore.return_value = store
            MockFlowService.return_value = mock_flow
            result = runner.invoke(app, ["--json", "flow", "list", "--branch", "42"])

        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# flow detail
# ---------------------------------------------------------------------------


class TestFlowDetail:
    def _mock_detail(self) -> dict:
        return {
            "id": "flow-1",
            "name": "My Flow",
            "description": "",
            "configuration": {},
            "project_alias": "prod",
            "branch_id": None,
            "phases": [{"id": 1, "name": "P1", "dependsOn": []}],
            "tasks": [{"id": 1, "name": "T1", "phase": 1, "task": {}}],
            "phase_count": 1,
            "task_count": 1,
        }

    def test_detail_json(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.get_flow_detail.return_value = self._mock_detail()
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
        ):
            MockStore.return_value = store
            MockFlowService.return_value = mock_flow
            result = runner.invoke(
                app, ["--json", "flow", "detail", "--project", "prod", "--flow-id", "flow-1"]
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["phase_count"] == 1

    def test_detail_explicit_component_id(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.get_flow_detail.return_value = self._mock_detail()
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
        ):
            MockStore.return_value = store
            MockFlowService.return_value = mock_flow
            result = runner.invoke(
                app,
                [
                    "--json",
                    "flow",
                    "detail",
                    "--project",
                    "prod",
                    "--flow-id",
                    "flow-1",
                    "--component-id",
                    "keboola.flow",
                ],
            )

        assert result.exit_code == 0, result.output
        mock_flow.get_flow_detail.assert_called_once_with(
            alias="prod", component_id="keboola.flow", config_id="flow-1", branch_id=None
        )

    def test_detail_not_found(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.get_flow_detail.side_effect = KeboolaApiError(
            message="Not found", status_code=404, error_code="NOT_FOUND", retryable=False
        )
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
        ):
            MockStore.return_value = store
            MockFlowService.return_value = mock_flow
            result = runner.invoke(
                app, ["--json", "flow", "detail", "--project", "prod", "--flow-id", "bad"]
            )

        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# flow schema
# ---------------------------------------------------------------------------


class TestFlowSchema:
    def test_schema_human(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg")
        mock_flow = MagicMock()
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
        ):
            MockStore.return_value = store
            MockFlowService.return_value = mock_flow
            result = runner.invoke(app, ["flow", "schema"])

        assert result.exit_code == 0
        assert "phases" in result.output

    def test_schema_json(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg")
        mock_flow = MagicMock()
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
        ):
            MockStore.return_value = store
            MockFlowService.return_value = mock_flow
            result = runner.invoke(app, ["--json", "flow", "schema"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "phases" in data["data"]["schema"]


# ---------------------------------------------------------------------------
# flow new
# ---------------------------------------------------------------------------


class TestFlowNew:
    def test_new_success_json(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.create_flow.return_value = {
            "id": "new-123",
            "name": "My Flow",
            "project_alias": "prod",
            "branch_id": None,
            "phase_count": 0,
            "task_count": 0,
        }
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
        ):
            MockStore.return_value = store
            MockFlowService.return_value = mock_flow
            result = runner.invoke(
                app, ["--json", "flow", "new", "--project", "prod", "--name", "My Flow"]
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["id"] == "new-123"

    def test_new_api_error(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.create_flow.side_effect = KeboolaApiError(
            message="Server error", status_code=500, error_code="API_ERROR", retryable=True
        )
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
        ):
            MockStore.return_value = store
            MockFlowService.return_value = mock_flow
            result = runner.invoke(
                app, ["--json", "flow", "new", "--project", "prod", "--name", "Bad"]
            )

        assert result.exit_code == 1

    def test_new_from_yaml_file(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.create_flow.return_value = {
            "id": "yf-1",
            "name": "YAML Flow",
            "project_alias": "prod",
            "branch_id": None,
            "phase_count": 1,
            "task_count": 1,
        }
        flow_yaml = tmp_path / "flow.yaml"
        flow_yaml.write_text(
            "phases:\n  - id: 1\n    dependsOn: []\ntasks:\n  - id: 1\n    phase: 1\n",
            encoding="utf-8",
        )
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
        ):
            MockStore.return_value = store
            MockFlowService.return_value = mock_flow
            result = runner.invoke(
                app,
                [
                    "--json",
                    "flow",
                    "new",
                    "--project",
                    "prod",
                    "--name",
                    "YAML Flow",
                    "--file",
                    f"@{flow_yaml}",
                ],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = mock_flow.create_flow.call_args.kwargs
        assert len(call_kwargs["phases"]) == 1

    def test_new_invalid_yaml_type_exits_2(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("- just a list\n- not a mapping\n", encoding="utf-8")
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
        ):
            MockStore.return_value = store
            MockFlowService.return_value = mock_flow
            result = runner.invoke(
                app,
                [
                    "--json",
                    "flow",
                    "new",
                    "--project",
                    "prod",
                    "--name",
                    "Bad",
                    "--file",
                    f"@{bad_yaml}",
                ],
            )

        assert result.exit_code == 2
        mock_flow.create_flow.assert_not_called()


# ---------------------------------------------------------------------------
# flow update
# ---------------------------------------------------------------------------


class TestFlowUpdate:
    def test_update_name(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.update_flow.return_value = {
            "id": "1",
            "name": "New Name",
            "project_alias": "prod",
            "branch_id": None,
        }
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
        ):
            MockStore.return_value = store
            MockFlowService.return_value = mock_flow
            result = runner.invoke(
                app,
                [
                    "--json",
                    "flow",
                    "update",
                    "--project",
                    "prod",
                    "--flow-id",
                    "1",
                    "--name",
                    "New Name",
                ],
            )

        assert result.exit_code == 0, result.output

    def test_update_without_anything_fails(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
        ):
            MockStore.return_value = store
            MockFlowService.return_value = mock_flow
            result = runner.invoke(
                app, ["--json", "flow", "update", "--project", "prod", "--flow-id", "1"]
            )

        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# flow delete
# ---------------------------------------------------------------------------


class TestFlowDelete:
    def test_delete_with_yes(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.delete_flow.return_value = {
            "status": "deleted",
            "project_alias": "prod",
            "component_id": "keboola.orchestrator",
            "config_id": "1",
            "branch_id": None,
        }
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
        ):
            MockStore.return_value = store
            MockFlowService.return_value = mock_flow
            result = runner.invoke(
                app,
                [
                    "--json",
                    "flow",
                    "delete",
                    "--project",
                    "prod",
                    "--flow-id",
                    "1",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["status"] == "deleted"


# ---------------------------------------------------------------------------
# flow schedule
# ---------------------------------------------------------------------------


class TestFlowSchedule:
    def test_schedule_success_json(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.set_flow_schedule.return_value = {
            "status": "created",
            "project_alias": "prod",
            "schedule_id": "sched-99",
            "schedule_name": "Daily Run (Schedule)",
            "component_id": "keboola.orchestrator",
            "config_id": "flow-1",
            "cron_tab": "0 6 * * *",
            "timezone": "UTC",
            "state": "enabled",
            "branch_id": None,
        }
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
        ):
            MockStore.return_value = store
            MockFlowService.return_value = mock_flow
            result = runner.invoke(
                app,
                [
                    "--json",
                    "flow",
                    "schedule",
                    "--project",
                    "prod",
                    "--flow-id",
                    "flow-1",
                    "--cron",
                    "0 6 * * *",
                ],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["schedule_id"] == "sched-99"
        mock_flow.set_flow_schedule.assert_called_once()

    def test_schedule_with_timezone_and_disabled(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.set_flow_schedule.return_value = {
            "status": "created",
            "project_alias": "prod",
            "schedule_id": "sched-tz",
            "schedule_name": "Flow (Schedule)",
            "component_id": "keboola.orchestrator",
            "config_id": "flow-1",
            "cron_tab": "0 8 * * 1-5",
            "timezone": "Europe/Prague",
            "state": "disabled",
            "branch_id": None,
        }
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
        ):
            MockStore.return_value = store
            MockFlowService.return_value = mock_flow
            result = runner.invoke(
                app,
                [
                    "--json",
                    "flow",
                    "schedule",
                    "--project",
                    "prod",
                    "--flow-id",
                    "flow-1",
                    "--cron",
                    "0 8 * * 1-5",
                    "--timezone",
                    "Europe/Prague",
                    "--disabled",
                ],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = mock_flow.set_flow_schedule.call_args.kwargs
        assert call_kwargs["timezone"] == "Europe/Prague"
        assert call_kwargs["enabled"] is False


# ---------------------------------------------------------------------------
# flow schedule-remove
# ---------------------------------------------------------------------------


class TestFlowScheduleRemove:
    def test_remove_with_yes(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.remove_flow_schedule.return_value = {
            "status": "removed",
            "project_alias": "prod",
            "component_id": "keboola.orchestrator",
            "config_id": "flow-1",
            "deleted_schedule_ids": ["sched-1"],
            "deleted_count": 1,
            "branch_id": None,
        }
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
        ):
            MockStore.return_value = store
            MockFlowService.return_value = mock_flow
            result = runner.invoke(
                app,
                [
                    "--json",
                    "flow",
                    "schedule-remove",
                    "--project",
                    "prod",
                    "--flow-id",
                    "flow-1",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["deleted_count"] == 1
