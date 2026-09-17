"""Tests for `kbagent rls` CLI commands via CliRunner -- CLI-17.

Covers JSON output, human-mode rendering, exit codes for ConfigError /
KeboolaApiError / invalid arguments, --dry-run never triggering a write, and
`rls setup`'s non-TTY refusal (CliRunner's captured stdout is never a real
terminal, so it naturally exercises that path).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
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
    storage_service: MagicMock | None = None,
) -> Any:
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.RlsService") as MockRS,
        patch("keboola_agent_cli.cli.StorageService") as MockSS,
    ):
        MockStore.return_value = store
        MockRS.return_value = mock_service
        MockSS.return_value = storage_service or MagicMock()
        return runner.invoke(app, args, input=input)


def _policy_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "p-1",
        "table": "in.c-crm.invoices",
        "dialect": "snowflake",
        "rule_count": 1,
        "scope": "organization",
        "source_project_id": "5725",
        "target_project_ids": [],
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# rls list
# ---------------------------------------------------------------------------


class TestRlsListCli:
    def test_json_output(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_policies.return_value = {"project": "prod", "policies": [_policy_row()]}

        result = _run(["--json", "rls", "list", "--project", "prod"], store, service)

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)["data"]
        assert payload["policies"][0]["table"] == "in.c-crm.invoices"

    def test_human_output_shows_table(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_policies.return_value = {"project": "prod", "policies": [_policy_row()]}

        result = _run(["rls", "list", "--project", "prod"], store, service)

        assert result.exit_code == 0
        assert "in.c-crm.invoices" in result.output

    def test_empty_list(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_policies.return_value = {"project": "prod", "policies": []}

        result = _run(["rls", "list", "--project", "prod"], store, service)

        assert result.exit_code == 0
        assert "No RLS policies found" in result.output

    def test_config_error_exit_5(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {})
        service = MagicMock()
        service.list_policies.side_effect = ConfigError("unknown alias 'prod'")

        result = _run(["rls", "list", "--project", "prod"], store, service)

        assert result.exit_code == 5

    def test_api_error_mapped(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.list_policies.side_effect = KeboolaApiError(
            message="boom", status_code=401, error_code=ErrorCode.INVALID_TOKEN
        )

        result = _run(["rls", "list", "--project", "prod"], store, service)

        assert result.exit_code == 3


# ---------------------------------------------------------------------------
# rls detail
# ---------------------------------------------------------------------------


class TestRlsDetailCli:
    def test_json_output(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.get_policy.return_value = {
            **_policy_row(),
            "rules": [{"principal": "a@x.com", "condition": {"true": True}}],
        }

        result = _run(
            ["--json", "rls", "detail", "--project", "prod", "--policy-id", "p-1"], store, service
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)["data"]
        assert payload["rules"][0]["principal"] == "a@x.com"
        service.get_policy.assert_called_once_with(alias="prod", policy_id="p-1")


# ---------------------------------------------------------------------------
# rls schema
# ---------------------------------------------------------------------------


class TestRlsSchemaCli:
    def test_success(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        fetch = MagicMock(schema={"type": "object"}, reason=None)
        service.fetch_schema.return_value = fetch

        result = _run(["--json", "rls", "schema", "--project", "prod"], store, service)

        assert result.exit_code == 0
        payload = json.loads(result.output)["data"]
        assert payload["schema"] == {"type": "object"}
        assert payload["source"] == "live"

    def test_fetch_failure_is_exit_4_not_a_crash(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        fetch = MagicMock(schema=None, reason="404 not found -- backend not registered yet")
        service.fetch_schema.return_value = fetch

        result = _run(["rls", "schema", "--project", "prod"], store, service)

        assert result.exit_code == 4
        assert "not registered yet" in result.output or "Could not fetch" in result.output


# ---------------------------------------------------------------------------
# rls create
# ---------------------------------------------------------------------------


class TestRlsCreateCli:
    _RULES_JSON = '[{"principal": "a@x.com", "condition": {"true": true}}]'

    def test_json_output_no_confirm_needed(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.create_policy.return_value = {**_policy_row(), "preview": []}

        result = _run(
            [
                "--json",
                "rls",
                "create",
                "--project",
                "prod",
                "--table",
                "in.c-crm.invoices",
                "--dialect",
                "snowflake",
                "--rules",
                self._RULES_JSON,
            ],
            store,
            service,
        )

        assert result.exit_code == 0, result.output
        service.create_policy.assert_called_once()

    def test_invalid_dialect_exit_2_before_service_call(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()

        result = _run(
            [
                "--json",
                "rls",
                "create",
                "--project",
                "prod",
                "--table",
                "t",
                "--dialect",
                "postgres",
                "--rules",
                self._RULES_JSON,
            ],
            store,
            service,
        )

        assert result.exit_code == 2
        service.create_policy.assert_not_called()

    def test_invalid_rules_json_exit_2(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()

        result = _run(
            [
                "--json",
                "rls",
                "create",
                "--project",
                "prod",
                "--table",
                "t",
                "--dialect",
                "snowflake",
                "--rules",
                "not-json",
            ],
            store,
            service,
        )

        assert result.exit_code == 2
        service.create_policy.assert_not_called()

    def test_dry_run_uses_dry_run_flag_and_needs_no_confirm(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.create_policy.return_value = {**_policy_row(), "dry_run": True, "preview": []}

        result = _run(
            [
                "rls",
                "create",
                "--project",
                "prod",
                "--table",
                "t",
                "--dialect",
                "snowflake",
                "--rules",
                self._RULES_JSON,
                "--dry-run",
            ],
            store,
            service,
        )

        assert result.exit_code == 0, result.output
        _, kwargs = service.create_policy.call_args
        assert kwargs["dry_run"] is True

    def test_human_mode_without_yes_aborts_on_no(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.create_policy.return_value = {**_policy_row(), "preview": []}

        result = _run(
            [
                "rls",
                "create",
                "--project",
                "prod",
                "--table",
                "t",
                "--dialect",
                "snowflake",
                "--rules",
                self._RULES_JSON,
            ],
            store,
            service,
            input="n\n",
        )

        assert result.exit_code == 0
        assert "Aborted" in result.output
        # Only the pre-confirm preview call (dry_run=True) may have happened;
        # the real write must never fire after a "no".
        for call in service.create_policy.call_args_list:
            assert call.kwargs.get("dry_run") is True

    def test_config_error_exit_5(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {})
        service = MagicMock()
        service.create_policy.side_effect = ConfigError("unknown alias")

        result = _run(
            [
                "--json",
                "rls",
                "create",
                "--project",
                "prod",
                "--table",
                "t",
                "--dialect",
                "snowflake",
                "--rules",
                self._RULES_JSON,
            ],
            store,
            service,
        )

        assert result.exit_code == 5


# ---------------------------------------------------------------------------
# rls update
# ---------------------------------------------------------------------------


class TestRlsUpdateCli:
    def test_json_output(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.update_policy.return_value = {**_policy_row(), "preview": []}

        result = _run(
            [
                "--json",
                "rls",
                "update",
                "--project",
                "prod",
                "--policy-id",
                "p-1",
                "--table",
                "in.c-crm.invoices",
            ],
            store,
            service,
        )

        assert result.exit_code == 0, result.output
        _, kwargs = service.update_policy.call_args
        assert kwargs["policy_id"] == "p-1"
        assert kwargs["rules"] is None  # untouched flags stay None, not overwritten


# ---------------------------------------------------------------------------
# rls delete
# ---------------------------------------------------------------------------


class TestRlsDeleteCli:
    def test_yes_flag_skips_confirm(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.delete_policy.return_value = {
            "project": "prod",
            "policy_id": "p-1",
            "deleted": True,
        }

        result = _run(
            ["rls", "delete", "--project", "prod", "--policy-id", "p-1", "--yes"], store, service
        )

        assert result.exit_code == 0, result.output
        service.delete_policy.assert_called_once_with(alias="prod", policy_id="p-1")

    def test_human_mode_without_yes_aborts_on_no(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()

        result = _run(
            ["rls", "delete", "--project", "prod", "--policy-id", "p-1"],
            store,
            service,
            input="n\n",
        )

        assert result.exit_code == 0
        assert "Aborted" in result.output
        service.delete_policy.assert_not_called()

    def test_json_mode_needs_no_confirm(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.delete_policy.return_value = {
            "project": "prod",
            "policy_id": "p-1",
            "deleted": True,
        }

        result = _run(
            ["--json", "rls", "delete", "--project", "prod", "--policy-id", "p-1"], store, service
        )

        assert result.exit_code == 0
        service.delete_policy.assert_called_once()


# ---------------------------------------------------------------------------
# rls setup (non-TTY refusal -- CliRunner's captured stdout is never a real
# terminal, so this exercises the exact same path a piped/non-interactive
# invocation would)
# ---------------------------------------------------------------------------


class TestRlsSetupCli:
    def test_non_tty_refuses_and_hints_at_create(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        storage_service = MagicMock()

        result = _run(
            ["rls", "setup", "--project", "prod"],
            store,
            service,
            storage_service=storage_service,
        )

        assert result.exit_code == 2
        assert "rls create" in result.output
        storage_service.list_tables.assert_not_called()
        service.create_policy.assert_not_called()

    def test_json_mode_refuses_without_launching_picker(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        storage_service = MagicMock()

        result = _run(
            ["--json", "rls", "setup", "--project", "prod"],
            store,
            service,
            storage_service=storage_service,
        )

        assert result.exit_code == 2
        storage_service.list_tables.assert_not_called()
        service.create_policy.assert_not_called()
