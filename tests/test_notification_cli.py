"""Tests for `kbagent notification list` via CliRunner (issue #600).

Mirrors tests/test_billing_cli.py: patch ConfigStore + the service class used
inside `keboola_agent_cli.cli`, invoke through the real Typer app, and assert
on the JSON envelope, human-mode rendering, argument forwarding and exit codes.
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
        patch("keboola_agent_cli.cli.NotificationService") as MockNS,
    ):
        MockStore.return_value = store
        MockNS.return_value = mock_service
        return runner.invoke(app, args)


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "project_alias": "prod",
        "subscription_id": "101",
        "event": "job-failed",
        "scope": "config",
        "component_id": "keboola.flow",
        "config_id": "9001",
        "config_name": "Daily ingest",
        "branch_id": "",
        "channel": "email",
        "address": "ops@example.com",
        "expires_at": "",
        "filters": [],
    }
    row.update(overrides)
    return row


class TestNotificationListCli:
    def test_json_output_emits_envelope_verbatim(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        envelope = {"subscriptions": [_row()], "errors": []}
        service.list_subscriptions.return_value = envelope

        result = _run(["--json", "notification", "list", "--project", "prod"], store, service)

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"] == envelope

    def test_human_mode_shows_flow_name_and_recipient(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_subscriptions.return_value = {"subscriptions": [_row()], "errors": []}

        result = _run(["notification", "list"], store, service)

        assert result.exit_code == 0, result.output
        assert "Daily ingest" in result.output
        assert "ops@example.com" in result.output
        assert "job-failed" in result.output

    def test_project_wide_subscription_is_labelled_not_blank(self, tmp_path: Path) -> None:
        """The catch-all must read as a scope, not as a flow with no name."""
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_subscriptions.return_value = {
            "subscriptions": [
                _row(scope="project-wide", component_id="", config_id="", config_name="")
            ],
            "errors": [],
        }

        result = _run(["notification", "list"], store, service)

        assert "project-wide" in result.output

    def test_dangling_subscription_falls_back_to_config_id(self, tmp_path: Path) -> None:
        """A subscription pointing at a deleted flow is the audit's whole point."""
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_subscriptions.return_value = {
            "subscriptions": [_row(config_name="")],
            "errors": [],
        }

        result = _run(["notification", "list"], store, service)

        assert "9001" in result.output

    def test_production_subscription_renders_as_production(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_subscriptions.return_value = {"subscriptions": [_row()], "errors": []}

        result = _run(["notification", "list"], store, service)

        assert "production" in result.output

    def test_filters_are_forwarded(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"a": {}, "b": {}})
        service = MagicMock()
        service.list_subscriptions.return_value = {"subscriptions": [], "errors": []}

        result = _run(
            [
                "--json",
                "notification",
                "list",
                "--project",
                "a",
                "--project",
                "b",
                "--event",
                "job-failed",
                "--component-id",
                "keboola.flow",
                "--config-id",
                "9001",
            ],
            store,
            service,
        )

        assert result.exit_code == 0, result.output
        service.list_subscriptions.assert_called_once_with(
            aliases=["a", "b"],
            event="job-failed",
            component_id="keboola.flow",
            config_id="9001",
            branch_id=None,
        )

    def test_no_project_flag_forwards_none(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"a": {}})
        service = MagicMock()
        service.list_subscriptions.return_value = {"subscriptions": [], "errors": []}

        result = _run(["--json", "notification", "list"], store, service)

        assert result.exit_code == 0, result.output
        assert service.list_subscriptions.call_args.kwargs["aliases"] is None

    def test_branch_is_passed_through_verbatim(self, tmp_path: Path) -> None:
        """Never inferred from the active branch -- only what the caller typed."""
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_subscriptions.return_value = {"subscriptions": [], "errors": []}

        result = _run(
            ["--json", "notification", "list", "--project", "prod", "--branch", "1234"],
            store,
            service,
        )

        assert result.exit_code == 0, result.output
        assert service.list_subscriptions.call_args.kwargs["branch_id"] == 1234

    def test_branch_without_single_project_is_usage_error(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"a": {}, "b": {}})
        service = MagicMock()
        service.list_subscriptions.return_value = {"subscriptions": [], "errors": []}

        result = _run(["notification", "list", "--branch", "1234"], store, service)

        assert result.exit_code == 2
        service.list_subscriptions.assert_not_called()

    def test_per_project_errors_surface_as_warnings_exit_0(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"a": {}, "b": {}})
        service = MagicMock()
        service.list_subscriptions.return_value = {
            "subscriptions": [_row(project_alias="a")],
            "errors": [
                {
                    "project_alias": "b",
                    "error_code": "INVALID_TOKEN",
                    "message": "Invalid or expired token",
                }
            ],
        }

        result = _run(["notification", "list"], store, service)

        assert result.exit_code == 0, result.output
        assert "b" in result.output

    def test_empty_result_is_stated_not_silent(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_subscriptions.return_value = {"subscriptions": [], "errors": []}

        result = _run(["notification", "list"], store, service)

        assert result.exit_code == 0, result.output
        assert "No notification subscriptions found." in result.output

    def test_unknown_alias_exits_5(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_subscriptions.side_effect = ConfigError("Project 'nope' not found")

        result = _run(["notification", "list", "--project", "nope"], store, service)

        assert result.exit_code == 5


class TestNotificationPermissions:
    def test_read_only_command_survives_deny_writes(self, tmp_path: Path) -> None:
        """An audit must stay available under the write firewall."""
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_subscriptions.return_value = {"subscriptions": [], "errors": []}

        result = _run(["--deny-writes", "--json", "notification", "list"], store, service)

        assert result.exit_code == 0, result.output
