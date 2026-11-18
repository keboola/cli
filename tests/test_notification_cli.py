"""Tests for `kbagent notification` CLI commands via CliRunner -- issue #600.

Covers JSON output, human-mode rendering, the project-wide exclusion warning,
error accumulation, and exit codes for ConfigError / KeboolaApiError.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import typer.rich_utils as typer_rich_utils
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

        # A failed project makes the read exit 1 (E4).
        assert result.exit_code == 1
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


class TestNotificationCreateOvertimeCli:
    """``--overtime-percent`` (issue #989)."""

    BASE = (
        "notification",
        "create",
        "--project",
        "prod",
        "--event",
        "job-processing-long",
        "--channel",
        "email",
        "--address",
        "ops@example.com",
    )

    def test_passes_whole_percent_to_service(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.create_subscription.return_value = _row(
            event="job-processing-long", overtime_percent=50
        )

        result = _run(["--json", *self.BASE, "--overtime-percent", "50"], store, service)

        assert result.exit_code == 0, result.output
        assert service.create_subscription.call_args.kwargs["overtime_percent"] == 50
        assert json.loads(result.output)["data"]["overtime_percent"] == 50

    def test_omitted_passes_none(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.create_subscription.return_value = _row(event="job-processing-long")

        result = _run(["--json", *self.BASE], store, service)

        assert result.exit_code == 0, result.output
        assert service.create_subscription.call_args.kwargs["overtime_percent"] is None

    @pytest.mark.parametrize("bad", ["0", "0.5", "-1", "fifty", "10001"])
    def test_fraction_negative_or_text_exits_2_without_service_call(
        self, tmp_path: Path, bad: str
    ) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()

        result = _run(["--json", *self.BASE, "--overtime-percent", bad], store, service)

        assert result.exit_code == 2, result.output
        assert json.loads(result.output)["error"]["code"] == "INVALID_ARGUMENT"
        service.create_subscription.assert_not_called()

    def test_service_invalid_argument_exits_2(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.create_subscription.side_effect = KeboolaApiError(
            message="--overtime-percent applies only to job-processing-long",
            error_code="INVALID_ARGUMENT",
        )
        args = list(self.BASE)
        args[args.index("job-processing-long")] = "job-failed"

        result = _run(["--json", *args, "--overtime-percent", "50"], store, service)

        assert result.exit_code == 2, result.output
        assert json.loads(result.output)["error"]["code"] == "INVALID_ARGUMENT"

    def test_human_output_shows_threshold(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.create_subscription.return_value = _row(
            event="job-processing-long",
            overtime_percent=50,
            filters=[{"field": "durationOvertimePercentage", "value": 0.5, "operator": ">="}],
        )

        result = _run([*self.BASE, "--overtime-percent", "50"], store, service)

        assert result.exit_code == 0, result.output
        assert "Overtime:" in result.output
        assert "50 %" in result.output


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

    @pytest.mark.usefixtures("interactive_terminal")
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

    @pytest.mark.usefixtures("interactive_terminal")
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


# ---------------------------------------------------------------------------
# --filter and clone (issue #989, rest)
# ---------------------------------------------------------------------------


class TestNotificationCreateFilterCli:
    BASE = (
        "notification",
        "create",
        "--project",
        "prod",
        "--event",
        "job-processing-long",
        "--channel",
        "email",
        "--address",
        "ops@example.com",
    )

    def test_filters_are_parsed_and_passed_to_the_service(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.create_subscription.return_value = _row(event="job-processing-long")

        result = _run(
            [
                "--json",
                *self.BASE,
                "--filter",
                "averageDuration>=60",
                "--filter",
                "x=a:b, c",
            ],
            store,
            service,
        )

        assert result.exit_code == 0, result.output
        assert service.create_subscription.call_args.kwargs["filters"] == [
            {"field": "averageDuration", "operator": ">=", "value": "60"},
            {"field": "x", "operator": "==", "value": "a:b, c"},
        ]

    def test_omitted_filter_passes_none(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.create_subscription.return_value = _row()

        result = _run(["--json", *self.BASE], store, service)

        assert result.exit_code == 0, result.output
        assert service.create_subscription.call_args.kwargs["filters"] is None

    @pytest.mark.parametrize("bad", ["noop", "a..b=1", "=1"])
    def test_malformed_filter_exits_2_naming_the_option(self, tmp_path: Path, bad: str) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()

        result = _run(["--json", *self.BASE, "--filter", bad], store, service)

        assert result.exit_code == 2, result.output
        error = json.loads(result.output)["error"]
        assert error["code"] == "INVALID_ARGUMENT"
        assert "--filter" in error["message"]
        service.create_subscription.assert_not_called()

    def test_help_tells_to_quote_the_expression(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Typer forces a colour terminal when GITHUB_ACTIONS is set (CI) and
        # the ANSI codes split "--filter". Force it here too, so the test sees
        # what CI sees on every host, and compare the text without the codes.
        monkeypatch.setattr(typer_rich_utils, "FORCE_TERMINAL", True)
        result = runner.invoke(app, ["notification", "create", "--help"])
        assert result.exit_code == 0
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "--filter" in plain
        assert "quote" in plain.lower()


class TestNotificationCloneCli:
    BASE = ("notification", "clone", "--project", "prod", "--subscription-id", "500")

    def _clone_result(self, **overrides: Any) -> dict[str, Any]:
        result = {
            "source_subscription_id": "500",
            "new_subscription_id": "900",
            "dry_run": False,
            "filters_replaced": [],
            "filters_added": [],
            "filters_kept": [],
            "warnings": [],
            **_row(subscription_id="900"),
        }
        result.update(overrides)
        return result

    def test_passes_every_override_to_the_service(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.clone_subscription.return_value = self._clone_result()

        result = _run(
            [
                "--json",
                *self.BASE,
                "--component-id",
                "keboola.flow",
                "--config-id",
                "222",
                "--branch",
                "7",
                "--overtime-percent",
                "50",
                "--filter",
                "averageDuration>=60",
                "--address",
                "new@example.com",
                "--channel",
                "email",
                "--expires-at",
                "2030-01-01T00:00:00Z",
            ],
            store,
            service,
        )

        assert result.exit_code == 0, result.output
        kwargs = service.clone_subscription.call_args.kwargs
        assert kwargs == {
            "alias": "prod",
            "subscription_id": "500",
            "component_id": "keboola.flow",
            "config_id": "222",
            "branch_id": 7,
            "overtime_percent": 50,
            "filters": [{"field": "averageDuration", "operator": ">=", "value": "60"}],
            "address": "new@example.com",
            "channel": "email",
            "expires_at": "2030-01-01T00:00:00Z",
            "clear_expires_at": False,
            "dry_run": False,
        }
        assert json.loads(result.output)["data"]["new_subscription_id"] == "900"

    def test_no_override_clone_passes_defaults(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.clone_subscription.return_value = self._clone_result()

        result = _run(["--json", *self.BASE], store, service)

        assert result.exit_code == 0, result.output
        kwargs = service.clone_subscription.call_args.kwargs
        assert kwargs["filters"] is None
        assert kwargs["address"] is None
        assert kwargs["channel"] is None
        assert kwargs["branch_id"] is None
        assert kwargs["clear_expires_at"] is False
        assert kwargs["dry_run"] is False

    def test_dry_run_and_clear_expires_at_are_forwarded(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.clone_subscription.return_value = {
            "source_subscription_id": "500",
            "dry_run": True,
            "planned": {"event": "job-failed", "recipient": {}, "filters": [], "expiresAt": None},
            "filters_replaced": [],
            "filters_added": [],
            "filters_kept": [],
            "warnings": ["The service will add the default branch."],
        }

        result = _run([*self.BASE, "--dry-run", "--clear-expires-at"], store, service)

        assert result.exit_code == 0, result.output
        kwargs = service.clone_subscription.call_args.kwargs
        assert kwargs["dry_run"] is True
        assert kwargs["clear_expires_at"] is True
        assert "dry run" in result.output.lower()
        assert "default branch" in result.output

    def test_invalid_channel_exits_2_without_service_call(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()

        result = _run(
            ["--json", *self.BASE, "--address", "a@example.com", "--channel", "slack"],
            store,
            service,
        )

        assert result.exit_code == 2, result.output
        assert json.loads(result.output)["error"]["code"] == "INVALID_ARGUMENT"
        service.clone_subscription.assert_not_called()

    def test_malformed_filter_exits_2_without_service_call(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()

        result = _run(["--json", *self.BASE, "--filter", "nope"], store, service)

        assert result.exit_code == 2, result.output
        assert "--filter" in json.loads(result.output)["error"]["message"]
        service.clone_subscription.assert_not_called()

    def test_service_invalid_argument_exits_2(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.clone_subscription.side_effect = KeboolaApiError(
            message="--channel needs --address", error_code="INVALID_ARGUMENT"
        )

        result = _run(["--json", *self.BASE, "--channel", "email"], store, service)

        assert result.exit_code == 2, result.output

    def test_human_output_names_both_ids_and_warnings(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.clone_subscription.return_value = self._clone_result(
            warnings=["phase.id may not exist in the new flow"]
        )

        result = _run([*self.BASE, "--config-id", "222"], store, service)

        assert result.exit_code == 0, result.output
        assert "500" in result.output
        assert "900" in result.output
        assert "phase.id" in result.output

    def test_needs_no_confirmation(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.clone_subscription.return_value = self._clone_result()

        result = _run(list(self.BASE), store, service, input="")

        assert result.exit_code == 0, result.output
        service.clone_subscription.assert_called_once()

    @pytest.mark.parametrize("extra", [[], ["--dry-run"]])
    def test_deny_writes_blocks_clone_and_its_dry_run(
        self, tmp_path: Path, extra: list[str]
    ) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()

        result = _run(["--deny-writes", *self.BASE, *extra], store, service)

        assert result.exit_code == 6
        service.clone_subscription.assert_not_called()

    def test_deny_destructive_allows_clone(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.clone_subscription.return_value = self._clone_result()

        result = _run(["--deny-destructive", "--json", *self.BASE], store, service)

        assert result.exit_code == 0, result.output
