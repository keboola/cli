"""Tests for `kbagent cls` CLI commands via CliRunner.

Covers JSON + human output, exit codes (ConfigError 5, API error mapping,
invalid arguments 2), confirmation prompts, and ``--dry-run`` /
``--yes`` / ``--json`` skipping the prompt. Mirrors ``test_rls_cli.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig

runner = CliRunner()
RULES_JSON = json.dumps([{"principal": "a@x.com", "visible_columns": ["id", "region"]}])
CREATE_ARGS = [
    "cls",
    "create",
    "--project",
    "prod",
    "--table-id",
    "in.c-crm.invoices",
    "--dialect",
    "snowflake",
    "--rules",
    RULES_JSON,
]


def _store(tmp_path: Path) -> ConfigStore:
    store = ConfigStore(config_dir=tmp_path / "cfg")
    if "prod" not in store.load().projects:  # a test may invoke `_run` more than once
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="999-token-abc",
                project_name="prod",
                project_id=1234,
            ),
        )
    return store


def _run(args: list[str], tmp_path: Path, service: MagicMock, input: str | None = None) -> Any:
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ClsService") as MockCls,
    ):
        MockStore.return_value = _store(tmp_path)
        MockCls.return_value = service
        return runner.invoke(app, args, input=input)


def _row(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "p-1",
        "table": "in.c-crm.invoices",
        "dialect": "snowflake",
        "rule_count": 1,
        "scope": "organization",
        "source_project_id": "5725",
        "target_project_ids": [],
        **overrides,
    }


def _preview(**overrides: Any) -> dict[str, Any]:
    return {
        "table": "in.c-crm.invoices",
        "dialect": "snowflake",
        "scope": "organization",
        "preview": [{"principal": "a@x.com", "visible_columns": ["id", "region"]}],
        **overrides,
    }


class TestList:
    def test_json(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.list_policies.return_value = {"project": "prod", "policies": [_row()]}

        result = _run(["--json", "cls", "list", "--project", "prod"], tmp_path, service)

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["policies"][0]["table"] == "in.c-crm.invoices"
        service.list_policies.assert_called_once_with(alias="prod")

    def test_human_table_and_empty(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.list_policies.return_value = {"project": "prod", "policies": [_row()]}
        assert (
            "in.c-crm.invoices"
            in _run(["cls", "list", "--project", "prod"], tmp_path, service).output
        )

        service.list_policies.return_value = {"project": "prod", "policies": []}
        assert (
            "No CLS policies found"
            in _run(["cls", "list", "--project", "prod"], tmp_path, service).output
        )

    def test_config_error_exits_5(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.list_policies.side_effect = ConfigError("unknown alias")

        assert _run(["cls", "list", "--project", "prod"], tmp_path, service).exit_code == 5

    def test_api_error_is_mapped(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.list_policies.side_effect = KeboolaApiError(
            message="nope", status_code=403, error_code=ErrorCode.ACCESS_DENIED
        )

        result = _run(["--json", "cls", "list", "--project", "prod"], tmp_path, service)

        assert result.exit_code != 0
        assert "nope" in result.output


class TestDetail:
    def test_json_and_human(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.get_policy.return_value = _row(
            rules=[{"principal": "a@x.com", "visible_columns": ["id", "region"]}], revision=3
        )

        as_json = _run(
            ["--json", "cls", "detail", "--project", "prod", "--policy-id", "p-1"],
            tmp_path,
            service,
        )
        human = _run(
            ["cls", "detail", "--project", "prod", "--policy-id", "p-1"], tmp_path, service
        )

        assert json.loads(as_json.output)["data"]["revision"] == 3
        assert "a@x.com: id, region" in human.output
        service.get_policy.assert_called_with(alias="prod", policy_id="p-1")


class TestSchema:
    def test_success(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.fetch_schema.return_value = MagicMock(schema={"type": "object"}, reason=None)

        result = _run(["--json", "cls", "schema", "--project", "prod"], tmp_path, service)

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["schema"] == {"type": "object"}

    def test_human_prints_json(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.fetch_schema.return_value = MagicMock(schema={"title": "cls-policy"}, reason=None)

        assert (
            "cls-policy" in _run(["cls", "schema", "--project", "prod"], tmp_path, service).output
        )

    def test_fetch_failure_exits_4_with_cls_message(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.fetch_schema.return_value = MagicMock(schema=None, reason="not registered")

        result = _run(["--json", "cls", "schema", "--project", "prod"], tmp_path, service)

        assert result.exit_code == 4
        assert "cls-policy schema" in result.output

    def test_config_error_exits_5(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.fetch_schema.side_effect = ConfigError("unknown alias")

        assert _run(["cls", "schema", "--project", "prod"], tmp_path, service).exit_code == 5


class TestCreate:
    def test_json_skips_prompt_and_writes_once(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.create_policy.return_value = {**_row(), "preview": _preview()["preview"]}

        result = _run(["--json", *CREATE_ARGS], tmp_path, service)

        assert result.exit_code == 0, result.output
        service.create_policy.assert_called_once()
        kwargs = service.create_policy.call_args.kwargs
        assert kwargs["rules"][0]["visible_columns"] == ["id", "region"]
        assert kwargs["dry_run"] is False

    def test_dry_run_prints_projection_without_prompt(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.create_policy.return_value = _preview(dry_run=True)

        result = _run([*CREATE_ARGS, "--dry-run"], tmp_path, service)

        assert result.exit_code == 0, result.output
        assert "a@x.com: SELECT id, region" in result.output
        assert service.create_policy.call_args.kwargs["dry_run"] is True

    def test_confirmed_prompt_previews_then_writes(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.create_policy.return_value = {**_row(), "preview": _preview()["preview"]}

        result = _run(CREATE_ARGS, tmp_path, service, input="y\n")

        assert result.exit_code == 0, result.output
        assert "Created CLS policy p-1" in result.output
        assert [c.kwargs["dry_run"] for c in service.create_policy.call_args_list] == [True, False]

    def test_declined_prompt_aborts_without_writing(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.create_policy.return_value = _preview(dry_run=True)

        result = _run(CREATE_ARGS, tmp_path, service, input="n\n")

        assert result.exit_code == 0
        assert "Aborted" in result.output
        assert [c.kwargs["dry_run"] for c in service.create_policy.call_args_list] == [True]

    def test_yes_skips_prompt_and_passes_target_projects(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.create_policy.return_value = {**_row(), "preview": _preview()["preview"]}

        result = _run(
            [*CREATE_ARGS, "--yes", "--target-project", "7", "--target-project", "8"],
            tmp_path,
            service,
        )

        assert result.exit_code == 0, result.output
        service.create_policy.assert_called_once()
        assert service.create_policy.call_args.kwargs["target_projects"] == ["7", "8"]

    def test_invalid_dialect_exits_2_before_any_call(self, tmp_path: Path) -> None:
        service = MagicMock()
        args = [a if a != "snowflake" else "oracle" for a in CREATE_ARGS]

        result = _run(["--json", *args], tmp_path, service)

        assert result.exit_code == 2
        service.create_policy.assert_not_called()

    @pytest.mark.parametrize("bad_rules", ["not json", '{"principal": "a@x.com"}'])
    def test_invalid_rules_exit_2(self, tmp_path: Path, bad_rules: str) -> None:
        service = MagicMock()
        args = [*CREATE_ARGS[:-1], bad_rules]

        result = _run(["--json", *args], tmp_path, service)

        assert result.exit_code == 2
        if bad_rules.startswith("{"):
            assert "{principal|principals, visible_columns}" in result.output
        service.create_policy.assert_not_called()

    def test_validation_error_surfaces_cls_code(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.create_policy.side_effect = KeboolaApiError(
            message="CLS policy is invalid: x",
            status_code=400,
            error_code=ErrorCode.INVALID_CLS_POLICY,
        )

        result = _run(["--json", *CREATE_ARGS], tmp_path, service)

        assert result.exit_code != 0
        assert "INVALID_CLS_POLICY" in result.output


class TestUpdate:
    def test_passes_only_given_flags(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.update_policy.return_value = {**_row(), "preview": _preview()["preview"]}

        result = _run(
            [
                "--json",
                "cls",
                "update",
                "--project",
                "prod",
                "--policy-id",
                "p-1",
                "--rules",
                RULES_JSON,
            ],
            tmp_path,
            service,
        )

        assert result.exit_code == 0, result.output
        kwargs = service.update_policy.call_args.kwargs
        assert kwargs["table"] is None
        assert kwargs["dialect"] is None
        assert kwargs["rules"][0]["principal"] == "a@x.com"

    def test_declined_prompt_aborts(self, tmp_path: Path) -> None:
        service = MagicMock()

        result = _run(
            ["cls", "update", "--project", "prod", "--policy-id", "p-1"],
            tmp_path,
            service,
            input="n\n",
        )

        assert "Aborted" in result.output
        service.update_policy.assert_not_called()

    def test_confirmed_human_output(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.update_policy.return_value = {**_row(), "preview": _preview()["preview"]}

        result = _run(
            ["cls", "update", "--project", "prod", "--policy-id", "p-1"],
            tmp_path,
            service,
            input="y\n",
        )

        assert "Updated CLS policy p-1" in result.output

    def test_dry_run_skips_prompt(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.update_policy.return_value = _preview(dry_run=True)

        result = _run(
            [
                "cls",
                "update",
                "--project",
                "prod",
                "--policy-id",
                "p-1",
                "--dialect",
                "bigquery",
                "--dry-run",
            ],
            tmp_path,
            service,
        )

        assert result.exit_code == 0, result.output
        assert service.update_policy.call_args.kwargs["dry_run"] is True

    def test_invalid_dialect_exits_2(self, tmp_path: Path) -> None:
        service = MagicMock()

        result = _run(
            [
                "--json",
                "cls",
                "update",
                "--project",
                "prod",
                "--policy-id",
                "p-1",
                "--dialect",
                "oracle",
            ],
            tmp_path,
            service,
        )

        assert result.exit_code == 2
        service.update_policy.assert_not_called()


class TestDelete:
    def test_json_skips_prompt(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.delete_policy.return_value = {
            "project": "prod",
            "policy_id": "p-1",
            "deleted": True,
        }

        result = _run(
            ["--json", "cls", "delete", "--project", "prod", "--policy-id", "p-1"],
            tmp_path,
            service,
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["deleted"] is True

    def test_declined_prompt_does_not_delete(self, tmp_path: Path) -> None:
        service = MagicMock()

        result = _run(
            ["cls", "delete", "--project", "prod", "--policy-id", "p-1"],
            tmp_path,
            service,
            input="n\n",
        )

        assert "Aborted" in result.output
        service.delete_policy.assert_not_called()

    def test_yes_deletes(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.delete_policy.return_value = {"deleted": True}

        result = _run(
            ["cls", "delete", "--project", "prod", "--policy-id", "p-1", "--yes"], tmp_path, service
        )

        assert "Deleted CLS policy p-1" in result.output
        service.delete_policy.assert_called_once_with(alias="prod", policy_id="p-1", dry_run=False)


def test_permission_classes_of_the_cls_operations(tmp_path: Path) -> None:
    """`cls.*` in OPERATION_REGISTRY: create/update=admin, delete=destructive, reads=read."""
    from keboola_agent_cli.permissions import OPERATION_REGISTRY

    assert {op: OPERATION_REGISTRY[f"cls.{op}"] for op in ("list", "detail", "schema")} == {
        "list": "read",
        "detail": "read",
        "schema": "read",
    }
    assert all(OPERATION_REGISTRY[f"cls.{op}"] == "admin" for op in ("create", "update"))
    assert OPERATION_REGISTRY["cls.delete"] == "destructive"


class TestClsReviewFixes:
    def test_schema_auth_failure_is_an_error_not_a_missing_schema(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.fetch_schema.side_effect = KeboolaApiError(
            message="bad token", status_code=401, error_code=ErrorCode.INVALID_TOKEN
        )

        result = _run(["--json", "cls", "schema", "--project", "prod"], tmp_path, service)

        assert result.exit_code == 3
        assert "INVALID_TOKEN" in result.output

    def test_update_clear_target_projects_passes_an_empty_list(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.update_policy.return_value = {**_row(), "preview": _preview()["preview"]}

        result = _run(
            [
                "--json",
                "cls",
                "update",
                "--project",
                "prod",
                "--policy-id",
                "p-1",
                "--clear-target-projects",
            ],
            tmp_path,
            service,
        )

        assert result.exit_code == 0, result.output
        assert service.update_policy.call_args.kwargs["target_projects"] == []

    def test_update_clear_and_target_project_are_mutually_exclusive(self, tmp_path: Path) -> None:
        service = MagicMock()

        result = _run(
            [
                "--json", "cls", "update", "--project", "prod", "--policy-id", "p-1",
                "--clear-target-projects", "--target-project", "7",
            ],
            tmp_path,
            service,
        )  # fmt: skip

        assert result.exit_code == 2
        service.update_policy.assert_not_called()

    def test_create_prints_the_validation_warning_in_human_mode(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.create_policy.return_value = {
            **_row(),
            "preview": _preview()["preview"],
            "warnings": ["Live schema validation was skipped: no schema"],
        }

        result = _run([*CREATE_ARGS, "--yes"], tmp_path, service)

        assert result.exit_code == 0, result.output
        assert "Live schema validation was skipped" in result.output


class TestDestructiveGates:
    """`--scope organization` and `delete` are destructive-class: `--deny-destructive` blocks them."""

    @pytest.mark.parametrize(("extra", "exit_code"), [([], 0), (["--scope", "organization"], 6)])
    def test_deny_destructive_blocks_only_organization_scope(
        self, tmp_path: Path, extra: list[str], exit_code: int
    ) -> None:
        service = MagicMock()
        service.create_policy.return_value = {**_row(), "preview": _preview()["preview"]}

        result = _run(["--json", "--deny-destructive", *CREATE_ARGS, *extra], tmp_path, service)

        assert result.exit_code == exit_code, result.output
        assert service.create_policy.called is (exit_code == 0)

    def test_deny_destructive_blocks_delete(self, tmp_path: Path) -> None:
        service = MagicMock()

        result = _run(
            [
                "--json",
                "--deny-destructive",
                "cls",
                "delete",
                "--project",
                "prod",
                "--policy-id",
                "p-1",
            ],
            tmp_path,
            service,
        )

        assert result.exit_code == 6
        service.delete_policy.assert_not_called()

    def test_delete_dry_run_needs_no_confirm(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.delete_policy.return_value = {"project": "prod", "policy": _row(), "dry_run": True}

        result = _run(
            ["cls", "delete", "--project", "prod", "--policy-id", "p-1", "--dry-run"],
            tmp_path,
            service,
        )

        assert result.exit_code == 0, result.output
        assert "Would delete" in result.output
        service.delete_policy.assert_called_once_with(alias="prod", policy_id="p-1", dry_run=True)
