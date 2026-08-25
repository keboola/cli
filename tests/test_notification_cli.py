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


def _run(
    args: list[str],
    store: ConfigStore,
    mock_service: MagicMock,
    input: str | None = None,
) -> Any:
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.NotificationService") as MockNS,
    ):
        MockStore.return_value = store
        MockNS.return_value = mock_service
        return runner.invoke(app, args, input=input)


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
# notification create
# ---------------------------------------------------------------------------


class TestNotificationCreateCli:
    def test_json_output_calls_service_with_all_args(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.create_subscription.return_value = _row()

        result = _run(
            [
                "--json",
                "notification",
                "create",
                "--project",
                "prod",
                "--event",
                "job-failed",
                "--channel",
                "email",
                "--address",
                "ops@example.com",
                "--component-id",
                "keboola.flow",
                "--config-id",
                "98765",
                "--branch",
                "123",
                "--expires-at",
                "2027-01-01T00:00:00Z",
            ],
            store,
            service,
        )

        assert result.exit_code == 0, result.output
        kwargs = service.create_subscription.call_args.kwargs
        assert kwargs["alias"] == "prod"
        assert kwargs["event"] == "job-failed"
        assert kwargs["channel"] == "email"
        assert kwargs["address"] == "ops@example.com"
        assert kwargs["component_id"] == "keboola.flow"
        assert kwargs["config_id"] == "98765"
        assert kwargs["branch_id"] == 123
        assert kwargs["expires_at"] == "2027-01-01T00:00:00Z"
        assert json.loads(result.output)["data"]["subscription_id"] == "1234"

    def test_invalid_channel_exits_2_and_service_not_called(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()

        result = _run(
            [
                "notification",
                "create",
                "--project",
                "prod",
                "--event",
                "job-failed",
                "--channel",
                "slack",
                "--address",
                "ops@example.com",
            ],
            store,
            service,
        )

        assert result.exit_code == 2
        service.create_subscription.assert_not_called()

    def test_human_output_renders_recipient(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.create_subscription.return_value = _row()

        result = _run(
            [
                "notification",
                "create",
                "--project",
                "prod",
                "--event",
                "job-failed",
                "--channel",
                "email",
                "--address",
                "ops@example.com",
            ],
            store,
            service,
        )

        assert result.exit_code == 0, result.output
        assert "ops@example.com" in result.output

    def test_config_error_exits_5(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.create_subscription.side_effect = ConfigError("Project 'nope' not found")

        result = _run(
            [
                "notification",
                "create",
                "--project",
                "nope",
                "--event",
                "job-failed",
                "--channel",
                "email",
                "--address",
                "ops@example.com",
            ],
            store,
            service,
        )

        assert result.exit_code == 5


# ---------------------------------------------------------------------------
# notification delete
# ---------------------------------------------------------------------------


class TestNotificationDeleteCli:
    def test_yes_flag_calls_service(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.delete_subscription.return_value = {
            "project_alias": "prod",
            "subscription_id": "1234",
            "deleted": True,
        }

        result = _run(
            [
                "notification",
                "delete",
                "--project",
                "prod",
                "--subscription-id",
                "1234",
                "--yes",
            ],
            store,
            service,
        )

        assert result.exit_code == 0, result.output
        service.delete_subscription.assert_called_once_with(alias="prod", subscription_id="1234")

    def test_human_mode_without_yes_aborts_on_no(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()

        result = _run(
            ["notification", "delete", "--project", "prod", "--subscription-id", "1234"],
            store,
            service,
            input="n\n",
        )

        assert result.exit_code == 0
        assert "Aborted" in result.output
        service.delete_subscription.assert_not_called()

    def test_json_mode_needs_no_confirm(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.delete_subscription.return_value = {
            "project_alias": "prod",
            "subscription_id": "1234",
            "deleted": True,
        }

        result = _run(
            [
                "--json",
                "notification",
                "delete",
                "--project",
                "prod",
                "--subscription-id",
                "1234",
            ],
            store,
            service,
        )

        assert result.exit_code == 0, result.output
        service.delete_subscription.assert_called_once()

    def test_api_error_maps_to_exit_code(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.delete_subscription.side_effect = KeboolaApiError(
            message="Not found", error_code="NOT_FOUND", status_code=404
        )

        result = _run(
            [
                "notification",
                "delete",
                "--project",
                "prod",
                "--subscription-id",
                "9",
                "--yes",
            ],
            store,
            service,
        )

        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# notification replace-recipient
# ---------------------------------------------------------------------------


class TestNotificationReplaceRecipientCli:
    def test_passes_address_and_channel_through(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.replace_subscription_recipient.return_value = {
            "old_subscription_id": "1234",
            "new_subscription_id": "5678",
            "old_address": "old@example.com",
            "old_deleted": True,
            "warnings": [],
            **_row(subscription_id="5678", address="new@example.com"),
        }

        result = _run(
            [
                "--json",
                "notification",
                "replace-recipient",
                "--project",
                "prod",
                "--subscription-id",
                "1234",
                "--address",
                "new@example.com",
                "--channel",
                "webhook",
            ],
            store,
            service,
        )

        assert result.exit_code == 0, result.output
        kwargs = service.replace_subscription_recipient.call_args.kwargs
        assert kwargs["alias"] == "prod"
        assert kwargs["subscription_id"] == "1234"
        assert kwargs["new_address"] == "new@example.com"
        assert kwargs["new_channel"] == "webhook"

    def test_human_output_prints_ids_and_warnings(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.replace_subscription_recipient.return_value = {
            "old_subscription_id": "1234",
            "new_subscription_id": "5678",
            "old_address": "old@example.com",
            "old_deleted": False,
            "warnings": ["Old subscription 1234 was not deleted (a duplicate now exists)."],
            **_row(subscription_id="5678", address="new@example.com"),
        }

        result = _run(
            [
                "notification",
                "replace-recipient",
                "--project",
                "prod",
                "--subscription-id",
                "1234",
                "--address",
                "new@example.com",
                "--yes",
            ],
            store,
            service,
        )

        assert result.exit_code == 0, result.output
        assert "1234" in result.output
        assert "5678" in result.output
        assert "was not deleted" in result.output

    def test_confirm_behavior_same_as_delete(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()

        result = _run(
            [
                "notification",
                "replace-recipient",
                "--project",
                "prod",
                "--subscription-id",
                "1234",
                "--address",
                "new@example.com",
            ],
            store,
            service,
            input="n\n",
        )

        assert result.exit_code == 0
        assert "Aborted" in result.output
        service.replace_subscription_recipient.assert_not_called()

    def test_invalid_channel_exits_2_and_service_not_called(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()

        result = _run(
            [
                "notification",
                "replace-recipient",
                "--project",
                "prod",
                "--subscription-id",
                "1234",
                "--address",
                "new@example.com",
                "--channel",
                "slack",
            ],
            store,
            service,
        )

        assert result.exit_code == 2
        service.replace_subscription_recipient.assert_not_called()


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

    def test_deny_writes_blocks_create(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()

        result = _run(
            [
                "--deny-writes",
                "notification",
                "create",
                "--project",
                "prod",
                "--event",
                "job-failed",
                "--channel",
                "email",
                "--address",
                "ops@example.com",
            ],
            store,
            service,
        )

        assert result.exit_code == 6
        service.create_subscription.assert_not_called()

    def test_deny_writes_blocks_replace_recipient(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()

        result = _run(
            [
                "--deny-writes",
                "notification",
                "replace-recipient",
                "--project",
                "prod",
                "--subscription-id",
                "1234",
                "--address",
                "new@example.com",
                "--yes",
            ],
            store,
            service,
        )

        assert result.exit_code == 6
        service.replace_subscription_recipient.assert_not_called()

    def test_deny_writes_blocks_delete(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()

        result = _run(
            [
                "--deny-writes",
                "notification",
                "delete",
                "--project",
                "prod",
                "--subscription-id",
                "1234",
                "--yes",
            ],
            store,
            service,
        )

        assert result.exit_code == 6
        service.delete_subscription.assert_not_called()

    def test_deny_destructive_blocks_delete_but_allows_create(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()

        delete_result = _run(
            [
                "--deny-destructive",
                "notification",
                "delete",
                "--project",
                "prod",
                "--subscription-id",
                "1234",
                "--yes",
            ],
            store,
            service,
        )
        assert delete_result.exit_code == 6
        service.delete_subscription.assert_not_called()

        service.create_subscription.return_value = _row()
        create_result = _run(
            [
                "--deny-destructive",
                "notification",
                "create",
                "--project",
                "prod",
                "--event",
                "job-failed",
                "--channel",
                "email",
                "--address",
                "ops@example.com",
            ],
            store,
            service,
        )
        assert create_result.exit_code == 0, create_result.output
        service.create_subscription.assert_called_once()
