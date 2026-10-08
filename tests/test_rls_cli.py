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

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.commands._checkbox_select import _stdio_is_tty
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.output import OutputFormatter

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


class TestRlsDefaultOption:
    """`--default` (schema 1.1.0): a JSON condition passed through; anything else is a usage error."""

    @pytest.mark.parametrize(
        "command",
        [["create", "--table-id", "t.x", "--rules", "[]"], ["update", "--policy-id", "p-1"]],
    )
    def test_default_is_parsed_and_passed_to_the_service(
        self, tmp_path: Path, command: list[str]
    ) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.create_policy.return_value = {**_policy_row(), "preview": []}
        service.update_policy.return_value = {**_policy_row(), "preview": []}

        result = _run(
            ["--json", "rls", *command, "--project", "prod", "--default", '{"false": true}'],
            store,
            service,
        )

        assert result.exit_code == 0, result.output
        method = service.create_policy if command[0] == "create" else service.update_policy
        assert method.call_args.kwargs["default"] == {"false": True}

    @pytest.mark.parametrize("raw", ["[1]", "not json"])
    def test_a_non_object_default_is_a_usage_error(self, tmp_path: Path, raw: str) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()

        result = _run(
            [
                "--json",
                "rls",
                "create",
                "--project",
                "prod",
                "--table-id",
                "t.x",
                "--rules",
                "[]",
                "--default",
                raw,
            ],
            store,
            service,
        )

        assert result.exit_code == 2, result.output
        service.create_policy.assert_not_called()


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
                "--table-id",
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
                "--table-id",
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
                "--table-id",
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
                "--table-id",
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
                "--table-id",
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
                "--table-id",
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
                "--table-id",
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
        service.delete_policy.assert_called_once_with(alias="prod", policy_id="p-1", dry_run=False)

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


class _Stream:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty", "expected"),
    [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
)
def test_setup_is_interactive_only_when_stdin_and_stdout_are_terminals(
    monkeypatch: pytest.MonkeyPatch, stdin_tty: bool, stdout_tty: bool, expected: bool
) -> None:
    """A TTY stdout with a piped stdin used to pass, so `rls setup` hit the API before the picker failed."""
    monkeypatch.setattr("sys.stdin", _Stream(stdin_tty))
    monkeypatch.setattr("sys.stdout", _Stream(stdout_tty))

    assert _stdio_is_tty() is expected


class TestRlsReviewFixes:
    def test_schema_auth_failure_is_an_error_not_a_missing_schema(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.fetch_schema.side_effect = KeboolaApiError(
            message="bad token", status_code=401, error_code=ErrorCode.INVALID_TOKEN
        )

        result = _run(["--json", "rls", "schema", "--project", "prod"], store, service)

        assert result.exit_code == 3  # authentication, not 4 (NOT_FOUND)
        assert "INVALID_TOKEN" in result.output

    def test_update_clear_target_projects_passes_an_empty_list(self, tmp_path: Path) -> None:
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
                "--clear-target-projects",
            ],
            store,
            service,
        )

        assert result.exit_code == 0, result.output
        assert service.update_policy.call_args.kwargs["target_projects"] == []

    def test_update_clear_and_target_project_are_mutually_exclusive(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()

        result = _run(
            [
                "--json", "rls", "update", "--project", "prod", "--policy-id", "p-1",
                "--clear-target-projects", "--target-project", "7",
            ],
            store,
            service,
        )  # fmt: skip

        assert result.exit_code == 2
        service.update_policy.assert_not_called()

    def test_update_without_the_flag_keeps_grants_unchanged(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.update_policy.return_value = {**_policy_row(), "preview": []}

        _run(
            ["--json", "rls", "update", "--project", "prod", "--policy-id", "p-1", "--yes"],
            store,
            service,
        )

        assert service.update_policy.call_args.kwargs["target_projects"] is None

    def test_create_prints_the_validation_warning_in_human_mode(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.create_policy.return_value = {
            **_policy_row(),
            "preview": [],
            "warnings": ["Live schema validation was skipped: no schema"],
        }

        result = _run(
            [
                "rls", "create", "--project", "prod", "--table-id", "in.c-crm.invoices", "--dialect", "snowflake",
                "--rules", '[{"principal":"a@x.com","condition":{"true":true}}]', "--yes",
            ],
            store,
            service,
        )  # fmt: skip

        assert result.exit_code == 0, result.output
        assert "Live schema validation was skipped" in result.output


class TestRlsSetupReviewFixes:
    """`rls setup` needs a terminal, so the interactive gate and the picker are patched out."""

    @staticmethod
    def _setup_run(
        args: list[str], store: ConfigStore, service: MagicMock, storage: MagicMock
    ) -> Any:
        with (
            patch("keboola_agent_cli.commands.rls._stdio_is_tty", return_value=True),
            patch("keboola_agent_cli.commands.rls.checkbox_select", return_value=[0, 1]),
        ):
            return _run(args, store, service, storage_service=storage)

    @staticmethod
    def _storage() -> MagicMock:
        storage = MagicMock()
        storage.list_tables.return_value = {
            "tables": [{"id": "in.c-crm.a", "rows_count": 1}, {"id": "in.c-crm.b", "rows_count": 2}]
        }
        return storage

    RULES = '[{"principal":"a@x.com","condition":{"true":true}}]'

    def test_an_invalid_dialect_is_rejected_before_any_api_call(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        storage = self._storage()

        result = self._setup_run(
            [
                "rls",
                "setup",
                "--project",
                "prod",
                "--dialect",
                "postgres",
                "--rules",
                self.RULES,
                "--yes",
            ],
            store,
            MagicMock(),
            storage,
        )

        assert result.exit_code == 2
        storage.list_tables.assert_not_called()

    def test_an_exact_create_denial_also_blocks_setup(self, tmp_path: Path) -> None:
        from keboola_agent_cli.models import PermissionPolicy

        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        cfg = store.load()
        cfg.permissions = PermissionPolicy(mode="allow", deny=["rls.create"])
        store.save(cfg)
        service = MagicMock()
        storage = self._storage()

        result = self._setup_run(
            [
                "rls",
                "setup",
                "--project",
                "prod",
                "--dialect",
                "snowflake",
                "--rules",
                self.RULES,
                "--yes",
            ],
            store,
            service,
            storage,
        )

        assert result.exit_code == 6, result.output
        storage.list_tables.assert_not_called()
        service.create_policy.assert_not_called()

    @pytest.mark.parametrize(
        ("error", "exit_code", "error_code"),
        [
            (
                KeboolaApiError(message="x", status_code=401, error_code=ErrorCode.INVALID_TOKEN),
                3,
                "INVALID_TOKEN",
            ),
            (
                KeboolaApiError(message="x", status_code=500, error_code=ErrorCode.API_ERROR),
                1,
                "API_ERROR",
            ),
            (ConfigError("no such project"), 5, "CONFIG_ERROR"),
        ],
    )
    def test_every_table_failing_keeps_the_error_class_in_the_exit_code(
        self, tmp_path: Path, error: Exception, exit_code: int, error_code: str
    ) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.create_policy.side_effect = error

        with patch("keboola_agent_cli.commands.rls.get_formatter") as get_formatter_mock:
            real = OutputFormatter(json_mode=False)
            get_formatter_mock.return_value = real
            with patch.object(real, "error", wraps=real.error) as error_spy:
                result = self._setup_run(
                    [
                        "rls",
                        "setup",
                        "--project",
                        "prod",
                        "--dialect",
                        "snowflake",
                        "--rules",
                        self.RULES,
                        "--yes",
                    ],
                    store,
                    service,
                    self._storage(),
                )

        assert result.exit_code == exit_code
        assert error_spy.call_args.kwargs["error_code"] == error_code  # the failures' own code

    def test_every_table_failing_is_a_non_zero_exit(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.create_policy.side_effect = KeboolaApiError(
            message="needs a master token",
            status_code=403,
            error_code=ErrorCode.MISSING_MASTER_TOKEN,
        )

        result = self._setup_run(
            [
                "rls",
                "setup",
                "--project",
                "prod",
                "--dialect",
                "snowflake",
                "--rules",
                self.RULES,
                "--yes",
            ],
            store,
            service,
            self._storage(),
        )

        assert result.exit_code == 3  # an auth-class failure keeps the authentication exit code
        assert "2 of 2 RLS policies could not be created" in result.output

    def test_a_partial_failure_is_a_non_zero_exit_and_names_the_failed_table(
        self, tmp_path: Path
    ) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        ok = {**_policy_row(), "preview": []}
        failure = KeboolaApiError(message="boom", status_code=500, error_code=ErrorCode.API_ERROR)
        # The wizard previews each table (dry-run) first, then writes: only the second write fails.
        service.create_policy.side_effect = [ok, ok, ok, failure]

        result = self._setup_run(
            [
                "rls",
                "setup",
                "--project",
                "prod",
                "--dialect",
                "snowflake",
                "--rules",
                self.RULES,
                "--yes",
            ],
            store,
            service,
            self._storage(),
        )

        assert result.exit_code == 1
        assert "1 of 2 RLS policies could not be created: in.c-crm.b" in result.output

    def test_all_tables_created_exits_zero(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.create_policy.return_value = {**_policy_row(), "preview": []}

        result = self._setup_run(
            [
                "rls",
                "setup",
                "--project",
                "prod",
                "--dialect",
                "snowflake",
                "--rules",
                self.RULES,
                "--yes",
            ],
            store,
            service,
            self._storage(),
        )

        assert result.exit_code == 0, result.output


class TestScopeAndDestructiveGates:
    """`--scope organization` and `delete` are destructive-class: `--deny-destructive` blocks them."""

    RULES = '[{"principal":"a@x.com","condition":{"true":true}}]'

    @pytest.mark.parametrize(("extra", "exit_code"), [([], 0), (["--scope", "organization"], 6)])
    def test_deny_destructive_blocks_only_organization_scope(
        self, tmp_path: Path, extra: list[str], exit_code: int
    ) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.create_policy.return_value = {**_policy_row(), "preview": []}

        result = _run(
            [
                "--json", "--deny-destructive", "rls", "create", "--project", "prod",
                "--table-id", "in.c-crm.t", "--rules", self.RULES, *extra,
            ],
            store,
            service,
        )  # fmt: skip

        assert result.exit_code == exit_code, result.output
        assert service.create_policy.called is (exit_code == 0)

    def test_scope_is_passed_to_the_service(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.create_policy.return_value = {**_policy_row(), "preview": []}

        _run(
            [
                "--json", "rls", "create", "--project", "prod", "--table-id", "in.c-crm.t",
                "--rules", self.RULES, "--scope", "organization",
            ],
            store,
            service,
        )  # fmt: skip

        kwargs = service.create_policy.call_args.kwargs
        assert kwargs["scope"] == "organization"
        assert kwargs["dialect"] is None  # omitted: the service uses the project backend

    def test_deny_destructive_blocks_delete(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()

        result = _run(
            [
                "--json",
                "--deny-destructive",
                "rls",
                "delete",
                "--project",
                "prod",
                "--policy-id",
                "p-1",
            ],
            store,
            service,
        )

        assert result.exit_code == 6
        service.delete_policy.assert_not_called()

    def test_delete_dry_run_needs_no_confirm_and_deletes_nothing(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.delete_policy.return_value = {
            "project": "prod",
            "policy": _policy_row(),
            "dry_run": True,
        }

        result = _run(
            ["rls", "delete", "--project", "prod", "--policy-id", "p-1", "--dry-run"],
            store,
            service,
        )

        assert result.exit_code == 0, result.output
        assert "Would delete" in result.output
        service.delete_policy.assert_called_once_with(alias="prod", policy_id="p-1", dry_run=True)

    def test_a_service_usage_error_exits_2(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service = MagicMock()
        service.create_policy.side_effect = KeboolaApiError(
            message="--target-project 'ghost' is neither ...",
            status_code=400,
            error_code=ErrorCode.INVALID_ARGUMENT,
        )

        result = _run(
            [
                "--json", "rls", "create", "--project", "prod", "--table-id", "t.x",
                "--rules", self.RULES, "--target-project", "ghost",
            ],
            store,
            service,
        )  # fmt: skip

        assert result.exit_code == 2

    @pytest.mark.parametrize(
        "command", [["rls", "setup"], ["rls", "create", "--table-id", "t.x", "--rules", "[]"]]
    )
    def test_organization_scope_with_target_project_fails_before_any_call(
        self, tmp_path: Path, command: list[str]
    ) -> None:
        """No grants on organization scope: a usage error before the table listing or any write."""
        store = _setup_config(tmp_path / "cfg", {"prod": {}})
        service, storage = MagicMock(), MagicMock()
        args = [*command, "--project", "prod", "--scope", "organization", "--target-project", "7"]

        result = _run(args, store, service, storage_service=storage)

        assert result.exit_code == 2, result.output
        assert "--target-project requires --scope targeted" in result.output
        storage.list_tables.assert_not_called()
        service.create_policy.assert_not_called()

    def test_setup_json_prints_a_json_error_envelope(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg", {"prod": {}})

        result = _run(["--json", "rls", "setup", "--project", "prod"], store, MagicMock())

        assert result.exit_code == 2
        envelope = json.loads(result.output)
        assert envelope["status"] == "error"
        assert envelope["error"]["code"] == "INVALID_ARGUMENT"
        assert "rls create" in envelope["error"]["message"]
