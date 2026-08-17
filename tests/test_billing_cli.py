"""Tests for `kbagent billing credits` via CliRunner.

Mirrors tests/test_schedule_cli.py's structure: patch ConfigStore + the
service class used inside `keboola_agent_cli.cli`, invoke through the real
Typer app, and assert on JSON envelope / human-mode rendering / exit codes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError
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
        patch("keboola_agent_cli.cli.BillingService") as MockBS,
    ):
        MockStore.return_value = store
        MockBS.return_value = mock_service
        return runner.invoke(app, args)


def _credit_row(alias: str = "prod") -> dict[str, Any]:
    return {
        "project_alias": alias,
        "project_id": 1234,
        "consumed": 100.5,
        "remaining": 25.5,
        "total": 126.0,
        "consumed_minutes": 6030.0,
        "remaining_minutes": 1530.0,
        "component_jobs_consumed": 95.25,
        "workspace_jobs": [
            {"workspace_type": "sandbox-sql", "warehouse_size": "small", "consumed": 5.0},
        ],
    }


class TestBillingCreditsCli:
    def test_json_output_emits_envelope_verbatim(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_service = MagicMock()
        envelope = {"credits": [_credit_row()], "errors": []}
        mock_service.get_credits.return_value = envelope
        result = _run(["--json", "billing", "credits", "--project", "prod"], store, mock_service)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"] == envelope

    def test_human_mode_renders_table_with_balance_and_minutes(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_service = MagicMock()
        mock_service.get_credits.return_value = {"credits": [_credit_row()], "errors": []}
        result = _run(["billing", "credits", "--project", "prod"], store, mock_service)
        assert result.exit_code == 0, result.output
        assert "prod" in result.output
        assert "25.50" in result.output  # remaining
        assert "100.50" in result.output  # consumed
        assert "126.00" in result.output  # total
        assert "1530" in result.output  # remaining minutes

    def test_project_flag_repeatable_forwarded_as_aliases(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"a": {}, "b": {}})
        mock_service = MagicMock()
        mock_service.get_credits.return_value = {"credits": [], "errors": []}
        result = _run(
            ["--json", "billing", "credits", "--project", "a", "--project", "b"],
            store,
            mock_service,
        )
        assert result.exit_code == 0, result.output
        mock_service.get_credits.assert_called_once_with(aliases=["a", "b"])

    def test_no_project_flag_forwards_none(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"a": {}})
        mock_service = MagicMock()
        mock_service.get_credits.return_value = {"credits": [], "errors": []}
        result = _run(["--json", "billing", "credits"], store, mock_service)
        assert result.exit_code == 0, result.output
        mock_service.get_credits.assert_called_once_with(aliases=None)

    def test_per_project_errors_surface_as_warnings_exit_0(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"a": {}, "b": {}})
        mock_service = MagicMock()
        mock_service.get_credits.return_value = {
            "credits": [_credit_row("a")],
            "errors": [
                {
                    "project_alias": "b",
                    "error_code": "PAYG_NOT_AVAILABLE",
                    "message": "Project does not have the 'pay-as-you-go' feature enabled.",
                }
            ],
        }
        result = _run(["billing", "credits"], store, mock_service)
        assert result.exit_code == 0, result.output
        assert "b" in result.output
        assert "PAYG_NOT_AVAILABLE" in result.output or "pay-as-you-go" in result.output

    def test_empty_result_prints_no_payg_projects_line(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_service = MagicMock()
        mock_service.get_credits.return_value = {"credits": [], "errors": []}
        result = _run(["billing", "credits", "--project", "prod"], store, mock_service)
        assert result.exit_code == 0, result.output
        assert "No PAYG projects found." in result.output

    def test_config_error_exits_5(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_service = MagicMock()
        mock_service.get_credits.side_effect = ConfigError("No projects")
        result = _run(["--json", "billing", "credits"], store, mock_service)
        assert result.exit_code == 5

    def test_deny_writes_still_permits_read(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        mock_service = MagicMock()
        mock_service.get_credits.return_value = {"credits": [_credit_row()], "errors": []}
        result = _run(
            ["--deny-writes", "--json", "billing", "credits", "--project", "prod"],
            store,
            mock_service,
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["credits"][0]["project_alias"] == "prod"
