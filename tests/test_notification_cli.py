"""Tests for `kbagent notification` CLI commands via CliRunner -- issue #600.

Covers JSON output, human-mode rendering, the project-wide exclusion warning,
error accumulation, and exit codes for ConfigError / KeboolaApiError.
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
    for alias, info in (projects or {}).items():
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
        "subscription_id": "1234",
        "event": "job-failed",
        "component_id": "keboola.flow",
        "config_id": "98765",
        "config_name": "Daily ETL",
        "branch_id": "",
        "phase_id": "",
        "channel": "email",
        "address": "ops@example.com",
        "expires_at": "",
        "filters": [],
        "scope": "config",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# notification list
# ---------------------------------------------------------------------------


class TestNotificationListCli:
    def test_json_output(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_subscriptions.return_value = {
            "subscriptions": [_row()],
            "errors": [],
            "project_wide_excluded": 0,
        }

        result = _run(["--json", "notification", "list"], store, service)

        assert result.exit_code == 0
        payload = json.loads(result.output)["data"]
        assert payload["subscriptions"][0]["address"] == "ops@example.com"
        assert payload["subscriptions"][0]["event"] == "job-failed"

    def test_human_output_shows_recipient_and_flow(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_subscriptions.return_value = {
            "subscriptions": [_row()],
            "errors": [],
            "project_wide_excluded": 0,
        }

        result = _run(["notification", "list"], store, service)

        assert result.exit_code == 0
        assert "ops@example.com" in result.output
        assert "Daily ETL" in result.output

    def test_project_wide_row_renders_without_a_config(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_subscriptions.return_value = {
            "subscriptions": [
                _row(
                    subscription_id="1235",
                    component_id="",
                    config_id="",
                    config_name="",
                    scope="project-wide",
                    address="catchall@example.com",
                )
            ],
            "errors": [],
            "project_wide_excluded": 0,
        }

        result = _run(["notification", "list"], store, service)

        assert result.exit_code == 0
        assert "catchall@example.com" in result.output
        assert "project-wide" in result.output

    def test_component_column_shows_any_not_raw_markup(self, tmp_path: Path) -> None:
        """A config-scoped row with no component filter renders a dim "any".

        Escaping the fallback would turn `[dim]` into a literal, so the cell
        would read `[dim]any[/dim]` on screen.
        """
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_subscriptions.return_value = {
            "subscriptions": [_row(component_id="")],
            "errors": [],
            "project_wide_excluded": 0,
        }

        result = _run(["notification", "list"], store, service)

        assert result.exit_code == 0
        assert "any" in result.output
        assert "[dim]" not in result.output

    def test_filters_are_passed_to_the_service(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_subscriptions.return_value = {
            "subscriptions": [],
            "errors": [],
            "project_wide_excluded": 0,
        }

        result = _run(
            [
                "notification",
                "list",
                "--project",
                "prod",
                "--event",
                "job-failed",
                "--component-id",
                "keboola.flow",
                "--config-id",
                "98765",
            ],
            store,
            service,
        )

        assert result.exit_code == 0
        kwargs = service.list_subscriptions.call_args.kwargs
        assert kwargs["aliases"] == ["prod"]
        assert kwargs["event"] == "job-failed"
        assert kwargs["component_id"] == "keboola.flow"
        assert kwargs["config_id"] == "98765"

    def test_excluded_catchalls_are_warned_about(self, tmp_path: Path) -> None:
        """Silently dropping a catch-all would answer 'who gets paged' wrongly."""
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_subscriptions.return_value = {
            "subscriptions": [_row()],
            "errors": [],
            "project_wide_excluded": 2,
        }

        result = _run(
            ["notification", "list", "--config-id", "98765"],
            store,
            service,
        )

        assert result.exit_code == 0
        assert "2" in result.output
        assert "project-wide" in result.output

    def test_empty_result_is_reported(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_subscriptions.return_value = {
            "subscriptions": [],
            "errors": [],
            "project_wide_excluded": 0,
        }

        result = _run(["notification", "list"], store, service)

        assert result.exit_code == 0
        assert "No notification subscriptions" in result.output

    def test_per_project_errors_are_surfaced(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_subscriptions.return_value = {
            "subscriptions": [],
            "errors": [
                {
                    "project_alias": "broken",
                    "error_code": "AUTH_ERROR",
                    "message": "Access denied",
                }
            ],
            "project_wide_excluded": 0,
        }

        result = _run(["notification", "list"], store, service)

        assert result.exit_code == 0
        assert "broken" in result.output

    def test_config_error_exits_5(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_subscriptions.side_effect = ConfigError("Project 'nope' not found")

        result = _run(["notification", "list", "--project", "nope"], store, service)

        assert result.exit_code == 5


# ---------------------------------------------------------------------------
# notification detail
# ---------------------------------------------------------------------------


class TestNotificationDetailCli:
    def test_json_output(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.get_subscription_detail.return_value = _row(
            filters=[{"field": "job.configuration.id", "value": "98765"}]
        )

        result = _run(
            ["--json", "notification", "detail", "--project", "prod", "--subscription-id", "1234"],
            store,
            service,
        )

        assert result.exit_code == 0
        assert json.loads(result.output)["data"]["subscription_id"] == "1234"

    def test_human_output_lists_raw_filters(self, tmp_path: Path) -> None:
        """Threshold filters have no column -- detail must still show them."""
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.get_subscription_detail.return_value = _row(
            event="job-processing-long",
            filters=[
                {"field": "durationOvertimePercentage", "operator": ">=", "value": 0.75},
            ],
        )

        result = _run(
            ["notification", "detail", "--project", "prod", "--subscription-id", "1234"],
            store,
            service,
        )

        assert result.exit_code == 0
        assert "durationOvertimePercentage" in result.output
        assert "0.75" in result.output

    def test_api_error_maps_to_exit_code(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.get_subscription_detail.side_effect = KeboolaApiError(
            message="Not found", error_code="NOT_FOUND", status_code=404
        )

        result = _run(
            ["notification", "detail", "--project", "prod", "--subscription-id", "9"],
            store,
            service,
        )

        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# permissions
# ---------------------------------------------------------------------------


class TestNotificationPermissions:
    def test_list_is_read_only_and_survives_deny_writes(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_subscriptions.return_value = {
            "subscriptions": [],
            "errors": [],
            "project_wide_excluded": 0,
        }

        result = _run(["--deny-writes", "notification", "list"], store, service)

        assert result.exit_code == 0
