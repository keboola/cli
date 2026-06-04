"""Tests for flow CLI commands via CliRunner (conditional flows).

Tests all flow subcommands: list, detail, schema, validate, new, update,
delete, schedule, schedule-remove. Mock-service tests patch FlowService in
ctx.obj; the offline `validate` / `schema` paths exercise the real app.
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
from keboola_agent_cli.services.flow_service import FlowSchemaFetch

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


def _invoke(store: ConfigStore, mock_flow: MagicMock, args: list[str]) -> Any:
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.FlowService") as MockFlowService,
    ):
        MockStore.return_value = store
        MockFlowService.return_value = mock_flow
        return runner.invoke(app, args)


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
                    "component_id": "keboola.flow",
                    "config_id": "111",
                    "name": "Daily ETL",
                    "description": "",
                    "is_disabled": False,
                }
            ],
            "errors": [],
            "legacy_orchestrator_count": 0,
        }
        result = _invoke(store, mock_flow, ["--json", "flow", "list", "--project", "prod"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["flows"][0]["config_id"] == "111"
        mock_flow.list_flows.assert_called_once_with(
            aliases=["prod"], branch_id=None, with_schedules=False
        )

    def test_list_legacy_count_warns(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.list_flows.return_value = {
            "flows": [],
            "errors": [],
            "legacy_orchestrator_count": 3,
        }
        result = _invoke(store, mock_flow, ["flow", "list", "--project", "prod"])
        assert result.exit_code == 0, result.output
        assert "3 legacy" in result.output and "Conditional Flows" in result.output

    def test_list_empty(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.list_flows.return_value = {
            "flows": [],
            "errors": [],
            "legacy_orchestrator_count": 0,
        }
        result = _invoke(store, mock_flow, ["flow", "list"])
        assert result.exit_code == 0

    def test_list_config_error(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.list_flows.side_effect = ConfigError("No projects")
        result = _invoke(store, mock_flow, ["--json", "flow", "list"])
        assert result.exit_code == 5

    def test_list_all_projects_no_project_flag(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}, "dev": {}})
        mock_flow = MagicMock()
        mock_flow.list_flows.return_value = {
            "flows": [
                {
                    "project_alias": "prod",
                    "component_id": "keboola.flow",
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
            "legacy_orchestrator_count": 0,
        }
        result = _invoke(store, mock_flow, ["--json", "flow", "list"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data["data"]["flows"]) == 2
        mock_flow.list_flows.assert_called_once_with(
            aliases=None, branch_id=None, with_schedules=False
        )

    def test_branch_without_project_fails(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        result = _invoke(store, mock_flow, ["--json", "flow", "list", "--branch", "42"])
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
            "component_id": "keboola.flow",
            "branch_id": None,
            "phases": [{"id": "p1", "name": "P1", "next": [{"id": "n", "goto": None}]}],
            "tasks": [
                {
                    "id": "t1",
                    "name": "T1",
                    "phase": "p1",
                    "task": {"type": "job", "componentId": "c", "configId": "1", "mode": "run"},
                }
            ],
            "phase_count": 1,
            "task_count": 1,
        }

    def test_detail_json(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.get_flow_detail.return_value = self._mock_detail()
        result = _invoke(
            store,
            mock_flow,
            ["--json", "flow", "detail", "--project", "prod", "--flow-id", "flow-1"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["phase_count"] == 1

    def test_detail_human(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.get_flow_detail.return_value = self._mock_detail()
        result = _invoke(
            store, mock_flow, ["flow", "detail", "--project", "prod", "--flow-id", "flow-1"]
        )
        assert result.exit_code == 0, result.output
        mock_flow.get_flow_detail.assert_called_once_with(
            alias="prod", config_id="flow-1", branch_id=None
        )

    def test_detail_not_found(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.get_flow_detail.side_effect = KeboolaApiError(
            message="Not found", status_code=404, error_code="NOT_FOUND", retryable=False
        )
        result = _invoke(
            store, mock_flow, ["--json", "flow", "detail", "--project", "prod", "--flow-id", "bad"]
        )
        assert result.exit_code == 1


def test_component_id_flag_removed(tmp_path: Path) -> None:
    # --component-id is no longer a recognized option on flow detail
    store = _setup_config(tmp_path / "cfg", {"prod": {}})
    mock_flow = MagicMock()
    result = _invoke(
        store,
        mock_flow,
        ["flow", "detail", "--project", "prod", "--flow-id", "1", "--component-id", "keboola.flow"],
    )
    assert result.exit_code == 2
    assert "No such option" in result.output or "no such option" in result.output.lower()


# ---------------------------------------------------------------------------
# flow schema
# ---------------------------------------------------------------------------


_LIVE_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["phases", "tasks"],
    "properties": {
        "phases": {"type": "array"},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["job", "notification", "variable"]}
                        },
                    }
                },
            },
        },
    },
}


class TestFlowSchema:
    def test_schema_default_is_conditional_template(self) -> None:
        result = runner.invoke(app, ["flow", "schema"])
        assert result.exit_code == 0
        assert "next:" in result.output
        assert "goto" in result.output
        assert "dependsOn" not in result.output

    def test_schema_json(self) -> None:
        result = runner.invoke(app, ["--json", "flow", "schema"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "phases" in data["data"]["schema"]

    def test_schema_full_without_project_errors(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path)
        result = _invoke(store, MagicMock(), ["flow", "schema", "--full"])
        assert result.exit_code == 2
        assert "--project" in result.output

    def test_schema_full_with_project_dumps_live_schema(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path, {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.fetch_flow_schema.return_value = FlowSchemaFetch(schema=_LIVE_SCHEMA, reason=None)
        result = _invoke(store, mock_flow, ["flow", "schema", "--full", "--project", "prod"])
        assert result.exit_code == 0
        assert "$schema" in result.output or "draft-07" in result.output
        mock_flow.fetch_flow_schema.assert_called_once_with("prod")

    def test_schema_full_with_project_json_mode(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path, {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.fetch_flow_schema.return_value = FlowSchemaFetch(schema=_LIVE_SCHEMA, reason=None)
        result = _invoke(
            store, mock_flow, ["--json", "flow", "schema", "--full", "--project", "prod"]
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["data"]["schema"]["required"] == ["phases", "tasks"]

    def test_schema_full_fetch_failure_errors(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path, {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.fetch_flow_schema.return_value = FlowSchemaFetch(
            schema=None, reason="network down"
        )
        result = _invoke(store, mock_flow, ["flow", "schema", "--full", "--project", "prod"])
        assert result.exit_code == 4
        assert "network down" in result.output


# ---------------------------------------------------------------------------
# flow validate
# ---------------------------------------------------------------------------


_VALID_FLOW_YAML = """
phases:
  - id: "p1"
    name: "P1"
    next:
      - id: "n"
        goto: null
tasks:
  - id: "t1"
    name: "T1"
    phase: "p1"
    enabled: true
    task:
      type: job
      componentId: "keboola.ex-http"
      configId: "1"
      mode: run
"""


class TestFlowValidate:
    def test_validate_valid_semantic_only(self, tmp_path: Path) -> None:
        f = tmp_path / "flow.yaml"
        f.write_text(_VALID_FLOW_YAML)
        result = runner.invoke(app, ["flow", "validate", "--file", f"@{f}"])
        assert result.exit_code == 0

    def test_validate_no_project_notes_structural_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / "flow.yaml"
        f.write_text(_VALID_FLOW_YAML)
        result = runner.invoke(app, ["--json", "flow", "validate", "--file", f"@{f}"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["data"]["valid"] is True
        assert any("structural schema validation skipped" in n for n in payload["data"]["notes"])

    def test_validate_no_project_note_human(self, tmp_path: Path) -> None:
        f = tmp_path / "flow.yaml"
        f.write_text(_VALID_FLOW_YAML)
        result = runner.invoke(app, ["flow", "validate", "--file", f"@{f}"])
        assert result.exit_code == 0
        assert "structural schema validation skipped" in result.output

    def test_validate_invalid_exit_2(self, tmp_path: Path) -> None:
        bad = _VALID_FLOW_YAML.replace('phase: "p1"', 'phase: "ghost"')
        f = tmp_path / "bad.yaml"
        f.write_text(bad)
        result = runner.invoke(app, ["--json", "flow", "validate", "--file", f"@{f}"])
        assert result.exit_code == 2
        payload = json.loads(result.output)
        assert payload["data"]["valid"] is False
        assert payload["data"]["errors"]

    def test_validate_json_valid_lists_warnings(self, tmp_path: Path) -> None:
        f = tmp_path / "flow.yaml"
        f.write_text(_VALID_FLOW_YAML)
        result = runner.invoke(app, ["--json", "flow", "validate", "--file", f"@{f}"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["data"]["valid"] is True
        assert payload["data"]["errors"] == []
        assert "warnings" in payload["data"]

    def test_validate_with_project_full_validation(self, tmp_path: Path) -> None:
        # Live schema fetched -> bad task type caught structurally (exit 2).
        store = _setup_config(tmp_path, {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.fetch_flow_schema.return_value = FlowSchemaFetch(schema=_LIVE_SCHEMA, reason=None)
        bad = _VALID_FLOW_YAML.replace("type: job", "type: nonsense")
        f = tmp_path / "bad.yaml"
        f.write_text(bad)
        result = _invoke(
            store, mock_flow, ["--json", "flow", "validate", "--file", f"@{f}", "--project", "prod"]
        )
        assert result.exit_code == 2
        payload = json.loads(result.output)
        assert payload["data"]["valid"] is False
        mock_flow.fetch_flow_schema.assert_called_once_with("prod")

    def test_validate_with_project_fetch_failure_degrades(self, tmp_path: Path) -> None:
        # Schema fetch fails -> semantic-only, valid flow still passes + a note.
        store = _setup_config(tmp_path, {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.fetch_flow_schema.return_value = FlowSchemaFetch(
            schema=None, reason="network down"
        )
        f = tmp_path / "flow.yaml"
        f.write_text(_VALID_FLOW_YAML)
        result = _invoke(
            store, mock_flow, ["--json", "flow", "validate", "--file", f"@{f}", "--project", "prod"]
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["data"]["valid"] is True
        assert any("network down" in n for n in payload["data"]["notes"])


# ---------------------------------------------------------------------------
# flow new
# ---------------------------------------------------------------------------


class TestFlowNew:
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
            "warnings": [],
        }
        flow_yaml = tmp_path / "flow.yaml"
        flow_yaml.write_text(_VALID_FLOW_YAML, encoding="utf-8")
        result = _invoke(
            store,
            mock_flow,
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
        assert "component_id" not in call_kwargs

    def test_new_api_error(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.create_flow.side_effect = KeboolaApiError(
            message="Server error", status_code=500, error_code="API_ERROR", retryable=True
        )
        result = _invoke(
            store, mock_flow, ["--json", "flow", "new", "--project", "prod", "--name", "Bad"]
        )
        assert result.exit_code == 1

    def test_new_invalid_yaml_type_exits_2(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("- just a list\n- not a mapping\n", encoding="utf-8")
        result = _invoke(
            store,
            mock_flow,
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
        result = _invoke(
            store,
            mock_flow,
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
        assert "component_id" not in mock_flow.update_flow.call_args.kwargs

    def test_update_without_anything_fails(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        result = _invoke(
            store, mock_flow, ["--json", "flow", "update", "--project", "prod", "--flow-id", "1"]
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
            "component_id": "keboola.flow",
            "config_id": "1",
            "branch_id": None,
        }
        result = _invoke(
            store,
            mock_flow,
            ["--json", "flow", "delete", "--project", "prod", "--flow-id", "1", "--yes"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["status"] == "deleted"
        assert "component_id" not in mock_flow.delete_flow.call_args.kwargs

    def test_delete_dry_run_does_not_call_service(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        result = _invoke(
            store,
            mock_flow,
            ["--json", "flow", "delete", "--project", "prod", "--flow-id", "1", "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["would_delete"]["config_id"] == "1"
        assert data["data"]["would_delete"]["component_id"] == "keboola.flow"
        mock_flow.delete_flow.assert_not_called()


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
            "component_id": "keboola.flow",
            "config_id": "flow-1",
            "cron_tab": "0 6 * * *",
            "timezone": "UTC",
            "state": "enabled",
            "branch_id": None,
        }
        result = _invoke(
            store,
            mock_flow,
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
        assert "component_id" not in mock_flow.set_flow_schedule.call_args.kwargs

    def test_schedule_with_timezone_and_disabled(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.set_flow_schedule.return_value = {
            "status": "created",
            "project_alias": "prod",
            "schedule_id": "sched-tz",
            "schedule_name": "Flow (Schedule)",
            "component_id": "keboola.flow",
            "config_id": "flow-1",
            "cron_tab": "0 8 * * 1-5",
            "timezone": "Europe/Prague",
            "state": "disabled",
            "branch_id": None,
        }
        result = _invoke(
            store,
            mock_flow,
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
            "component_id": "keboola.flow",
            "config_id": "flow-1",
            "deleted_schedule_ids": ["sched-1"],
            "deleted_count": 1,
            "branch_id": None,
        }
        result = _invoke(
            store,
            mock_flow,
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
        assert "component_id" not in mock_flow.remove_flow_schedule.call_args.kwargs

    def test_schedule_remove_dry_run_lists_schedules(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.list_flow_schedules.return_value = {
            "project_alias": "prod",
            "component_id": "keboola.flow",
            "config_id": "flow-1",
            "schedules": [
                {
                    "schedule_id": "sched-1",
                    "name": "Flow (Schedule)",
                    "cron_tab": "0 6 * * *",
                    "timezone": "UTC",
                    "state": "enabled",
                }
            ],
        }
        result = _invoke(
            store,
            mock_flow,
            [
                "--json",
                "flow",
                "schedule-remove",
                "--project",
                "prod",
                "--flow-id",
                "flow-1",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["would_delete"]["count"] == 1
        assert data["data"]["would_delete"]["schedules"][0]["cron_tab"] == "0 6 * * *"
        mock_flow.remove_flow_schedule.assert_not_called()

    def test_schedule_remove_dry_run_no_schedules(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.list_flow_schedules.return_value = {
            "project_alias": "prod",
            "component_id": "keboola.flow",
            "config_id": "flow-1",
            "schedules": [],
        }
        result = _invoke(
            store,
            mock_flow,
            [
                "--json",
                "flow",
                "schedule-remove",
                "--project",
                "prod",
                "--flow-id",
                "flow-1",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["would_delete"]["count"] == 0


# ---------------------------------------------------------------------------
# detail rendering (pure formatter unit)
# ---------------------------------------------------------------------------


def test_format_flow_detail_renders_transitions_and_badges(capsys) -> None:
    from keboola_agent_cli.commands.flow import _format_flow_detail
    from keboola_agent_cli.output import OutputFormatter

    formatter = OutputFormatter(json_mode=False)
    detail = {
        "name": "My CF",
        "id": "100",
        "phases": [
            {
                "id": "p1",
                "name": "Extract",
                "next": [
                    {
                        "id": "c",
                        "goto": "p2",
                        "condition": {
                            "type": "operator",
                            "operator": "ANY_TASKS_IN_PHASE",
                            "phase": "p1",
                            "operands": [],
                        },
                    },
                    {"id": "d", "goto": None},
                ],
            },
            {"id": "p2", "name": "Transform"},
        ],
        "tasks": [
            {
                "id": "t1",
                "name": "Run",
                "phase": "p1",
                "enabled": True,
                "task": {
                    "type": "job",
                    "componentId": "keboola.ex-http",
                    "configId": "9",
                    "mode": "run",
                },
            },
            {
                "id": "t2",
                "name": "Notify",
                "phase": "p2",
                "task": {"type": "notification", "title": "x", "recipients": []},
            },
        ],
    }
    _format_flow_detail(formatter, detail)
    out = capsys.readouterr().out
    assert "Extract" in out and "Transform" in out
    assert "→" in out  # transition arrow
    assert "default" in out.lower()  # condition-less transition labeled
    assert "job" in out and "notification" in out  # task type badges
