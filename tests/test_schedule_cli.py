"""Tests for schedule CLI commands via CliRunner.

Covers the three new subcommands (list, detail, find) plus the
``flow list --with-schedules`` enrichment flag. Verifies JSON output,
human-mode rendering, and exit-code behavior for ConfigError / usage errors.
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


def _run(args: list[str], store: ConfigStore, mock_service: MagicMock) -> Any:
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ScheduleService") as MockSS,
    ):
        MockStore.return_value = store
        MockSS.return_value = mock_service
        return runner.invoke(app, args)


# ---------------------------------------------------------------------------
# schedule list
# ---------------------------------------------------------------------------


class TestScheduleListCli:
    def test_json_output(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_service = MagicMock()
        mock_service.list_schedules.return_value = {
            "schedules": [
                {
                    "project_alias": "prod",
                    "schedule_id": "sc1",
                    "schedule_name": "Schedule",
                    "parent_component_id": "keboola.orchestrator",
                    "parent_config_id": "111",
                    "parent_name": "Daily",
                    "cron": "0 6 * * *",
                    "timezone": "UTC",
                    "enabled": True,
                }
            ],
            "errors": [],
        }
        result = _run(["--json", "schedule", "list", "--project", "prod"], store, mock_service)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["schedules"][0]["schedule_id"] == "sc1"
        mock_service.list_schedules.assert_called_once_with(
            aliases=["prod"], enabled_only=False, branch_id=None
        )

    def test_enabled_only_flag_propagates(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_service = MagicMock()
        mock_service.list_schedules.return_value = {"schedules": [], "errors": []}
        result = _run(
            ["--json", "schedule", "list", "--project", "prod", "--enabled-only"],
            store,
            mock_service,
        )
        assert result.exit_code == 0
        mock_service.list_schedules.assert_called_once_with(
            aliases=["prod"], enabled_only=True, branch_id=None
        )

    def test_no_project_flag_means_all_projects(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"a": {}, "b": {}})
        mock_service = MagicMock()
        mock_service.list_schedules.return_value = {"schedules": [], "errors": []}
        result = _run(["--json", "schedule", "list"], store, mock_service)
        assert result.exit_code == 0
        mock_service.list_schedules.assert_called_once_with(
            aliases=None, enabled_only=False, branch_id=None
        )

    def test_branch_without_project_fails(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_service = MagicMock()
        result = _run(["--json", "schedule", "list", "--branch", "42"], store, mock_service)
        assert result.exit_code == 2

    def test_config_error_exits_5(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_service = MagicMock()
        mock_service.list_schedules.side_effect = ConfigError("No projects")
        result = _run(["--json", "schedule", "list"], store, mock_service)
        assert result.exit_code == 5

    def test_human_mode_empty(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_service = MagicMock()
        mock_service.list_schedules.return_value = {"schedules": [], "errors": []}
        result = _run(["schedule", "list", "--project", "prod"], store, mock_service)
        assert result.exit_code == 0
        assert "No schedules found" in result.output


# ---------------------------------------------------------------------------
# schedule detail
# ---------------------------------------------------------------------------


class TestScheduleDetailCli:
    def _detail_payload(self) -> dict:
        return {
            "project_alias": "prod",
            "branch_id": None,
            "schedule_id": "sc1",
            "schedule_name": "Schedule",
            "parent_component_id": "keboola.orchestrator",
            "parent_config_id": "111",
            "parent_name": "Daily",
            "cron": "0 6 * * *",
            "timezone": "UTC",
            "enabled": True,
            "configuration": {},
            "version": 1,
            "created": "2026-04-23T15:00:00+0000",
            "change_description": "Created",
        }

    def test_json(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_service = MagicMock()
        mock_service.get_schedule_detail.return_value = self._detail_payload()
        result = _run(
            [
                "--json",
                "schedule",
                "detail",
                "--project",
                "prod",
                "--schedule-id",
                "sc1",
            ],
            store,
            mock_service,
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["schedule_id"] == "sc1"
        assert data["data"]["parent_name"] == "Daily"

    def test_not_found_exits_1(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_service = MagicMock()
        mock_service.get_schedule_detail.side_effect = KeboolaApiError(
            message="not found",
            status_code=404,
            error_code="NOT_FOUND",
            retryable=False,
        )
        result = _run(
            [
                "--json",
                "schedule",
                "detail",
                "--project",
                "prod",
                "--schedule-id",
                "missing",
            ],
            store,
            mock_service,
        )
        assert result.exit_code == 1

    def test_human_output(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_service = MagicMock()
        mock_service.get_schedule_detail.return_value = self._detail_payload()
        result = _run(
            [
                "schedule",
                "detail",
                "--project",
                "prod",
                "--schedule-id",
                "sc1",
            ],
            store,
            mock_service,
        )
        assert result.exit_code == 0
        assert "0 6 * * *" in result.output


# ---------------------------------------------------------------------------
# schedule find
# ---------------------------------------------------------------------------


class TestScheduleFindCli:
    def _find_payload(self, **overrides) -> dict:
        # Shape reflects "no filters" -- both audit columns None. Tests
        # that exercise --cron-window / --not-run-since override the
        # relevant keys via `overrides` or build their own payload.
        payload = {
            "schedules": [
                {
                    "project_alias": "prod",
                    "schedule_id": "sc1",
                    "schedule_name": "Schedule",
                    "parent_component_id": "keboola.orchestrator",
                    "parent_config_id": "111",
                    "parent_name": "Daily",
                    "cron": "0 3 * * *",
                    "timezone": "UTC",
                    "enabled": True,
                    "matches_cron_window": None,
                    "last_run_at": None,
                }
            ],
            "errors": [],
            "filters": {"cron_window": None, "not_run_since_days": None},
        }
        payload.update(overrides)
        return payload

    def test_no_filters(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_service = MagicMock()
        mock_service.find_schedules.return_value = self._find_payload()
        result = _run(
            ["--json", "schedule", "find", "--project", "prod"],
            store,
            mock_service,
        )
        assert result.exit_code == 0
        mock_service.find_schedules.assert_called_once_with(
            aliases=["prod"],
            cron_window=None,
            not_run_since_days=None,
            branch_id=None,
        )

    def test_cron_window_and_not_run_since_combine(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_service = MagicMock()
        mock_service.find_schedules.return_value = self._find_payload(
            filters={"cron_window": "02:00-04:00", "not_run_since_days": 30}
        )
        result = _run(
            [
                "--json",
                "schedule",
                "find",
                "--cron-window",
                "02:00-04:00",
                "--not-run-since",
                "30",
            ],
            store,
            mock_service,
        )
        assert result.exit_code == 0
        mock_service.find_schedules.assert_called_once_with(
            aliases=None,
            cron_window="02:00-04:00",
            not_run_since_days=30,
            branch_id=None,
        )

    def test_config_error_on_invalid_window_exits_5(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_service = MagicMock()
        mock_service.find_schedules.side_effect = ConfigError("Invalid cron-window")
        result = _run(
            ["--json", "schedule", "find", "--cron-window", "garbage"],
            store,
            mock_service,
        )
        assert result.exit_code == 5


# ---------------------------------------------------------------------------
# flow list --with-schedules
# ---------------------------------------------------------------------------


class TestFlowListWithSchedulesCli:
    def test_flag_propagates_to_service(self, tmp_path: Path) -> None:
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
                    "schedules": [
                        {
                            "schedule_id": "sc1",
                            "cron": "0 6 * * *",
                            "timezone": "UTC",
                            "enabled": True,
                        }
                    ],
                }
            ],
            "errors": [],
        }
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlow,
        ):
            MockStore.return_value = store
            MockFlow.return_value = mock_flow
            result = runner.invoke(
                app,
                [
                    "--json",
                    "flow",
                    "list",
                    "--project",
                    "prod",
                    "--with-schedules",
                ],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["flows"][0]["schedules"][0]["cron"] == "0 6 * * *"
        mock_flow.list_flows.assert_called_once_with(
            aliases=["prod"], branch_id=None, with_schedules=True
        )

    def test_without_flag_no_schedules_key(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_flow = MagicMock()
        mock_flow.list_flows.return_value = {
            "flows": [
                {
                    "project_alias": "prod",
                    "component_id": "keboola.orchestrator",
                    "config_id": "111",
                    "name": "Daily",
                    "description": "",
                    "is_disabled": False,
                }
            ],
            "errors": [],
        }
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.FlowService") as MockFlow,
        ):
            MockStore.return_value = store
            MockFlow.return_value = mock_flow
            result = runner.invoke(
                app,
                ["--json", "flow", "list", "--project", "prod"],
            )
        assert result.exit_code == 0
        mock_flow.list_flows.assert_called_once_with(
            aliases=["prod"], branch_id=None, with_schedules=False
        )
