"""The two additive forms that 1.0.0 needs (#791 step 1, CLI-36).

1. ``--all-projects`` on the fan-out reads. Until 1.0.0 it does the same as an
   omitted ``--project``: the same service call and the same output. With
   ``--project`` it is a usage error (exit 2), like ``workspace gc``.
2. The optional top-level ``warnings`` key of the ``--json`` envelope.
   ``OutputFormatter.warning()`` adds to it in JSON mode; the key is present
   only when not empty, in the success and in the error envelope. Human mode
   is unchanged (stderr).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from rich.markup import escape
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.commands._helpers import emit_project_warnings
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.output import OutputFormatter, _plain_text

runner = CliRunner()

# The reads whose omitted --project reads all registered projects:
# (command line without --project, service class in cli.py, service method).
# `config detail` is not here: it requires --project. `project status` is: it
# takes one --project, and moves to the default-project chain in 1.0.0 too.
FAN_OUT_READS = [
    pytest.param(["search", "orders"], "SearchService", "search", id="search"),
    pytest.param(["billing", "credits"], "BillingService", "get_credits", id="billing-credits"),
    pytest.param(["component", "list"], "ComponentService", "list_components", id="component-list"),
    pytest.param(["config", "list"], "ConfigService", "list_configs", id="config-list"),
    pytest.param(
        ["config", "search", "--query", "x"],
        "ConfigService",
        "search_configs",
        id="config-search",
    ),
    pytest.param(["data-app", "list"], "DataAppService", "list_data_apps", id="data-app-list"),
    pytest.param(["job", "list"], "JobService", "list_jobs", id="job-list"),
    pytest.param(["storage", "buckets"], "StorageService", "list_buckets", id="storage-buckets"),
    pytest.param(["storage", "tables"], "StorageService", "list_tables", id="storage-tables"),
    pytest.param(["sharing", "list"], "SharingService", "list_shared", id="sharing-list"),
    pytest.param(["sharing", "edges"], "LineageService", "get_lineage", id="sharing-edges"),
    pytest.param(["flow", "list"], "FlowService", "list_flows", id="flow-list"),
    pytest.param(["schedule", "list"], "ScheduleService", "list_schedules", id="schedule-list"),
    pytest.param(["schedule", "find"], "ScheduleService", "find_schedules", id="schedule-find"),
    pytest.param(
        ["notification", "list"],
        "NotificationService",
        "list_subscriptions",
        id="notification-list",
    ),
    pytest.param(["branch", "list"], "BranchService", "list_branches", id="branch-list"),
    pytest.param(["workspace", "list"], "WorkspaceService", "list_workspaces", id="workspace-list"),
    pytest.param(["project", "status"], "ProjectService", "get_status", id="project-status"),
]


def _store(config_dir: Path) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    if store.load().projects:
        return store
    for alias, project_id in (("prod", 1), ("dev", 2)):
        store.add_project(
            alias,
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token=f"{project_id}-token",
                project_name=alias,
                project_id=project_id,
            ),
        )
    return store


def _invoke(
    tmp_path: Path, args: list[str], service_class: str, method: str
) -> tuple[Any, MagicMock]:
    """Run ``kbagent --json ARGS`` with the command's service replaced by a mock."""
    service = MagicMock()
    getattr(service, method).return_value = {"errors": []}
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as store_class,
        patch(f"keboola_agent_cli.cli.{service_class}") as service_factory,
    ):
        store_class.return_value = _store(tmp_path / "config")
        service_factory.return_value = service
        result = runner.invoke(app, ["--json", *args])
    return result, getattr(service, method)


class TestAllProjects:
    @pytest.mark.parametrize(("args", "service_class", "method"), FAN_OUT_READS)
    def test_same_result_as_omitted_project(
        self, tmp_path: Path, args: list[str], service_class: str, method: str
    ) -> None:
        omitted, omitted_call = _invoke(tmp_path, args, service_class, method)
        explicit, explicit_call = _invoke(
            tmp_path, [*args, "--all-projects"], service_class, method
        )

        assert omitted.exit_code == 0, omitted.output
        assert explicit.exit_code == 0, explicit.output
        assert explicit.stdout == omitted.stdout
        assert explicit_call.call_args == omitted_call.call_args
        # The omitted --project fans out: the service gets no project list.
        assert not omitted_call.call_args.kwargs["aliases"]

    @pytest.mark.parametrize(("args", "service_class", "method"), FAN_OUT_READS)
    def test_with_project_is_a_usage_error(
        self, tmp_path: Path, args: list[str], service_class: str, method: str
    ) -> None:
        result, call = _invoke(
            tmp_path, [*args, "--all-projects", "--project", "prod"], service_class, method
        )

        assert result.exit_code == 2, result.output
        error = json.loads(result.stdout)["error"]
        assert error["code"] == "USAGE_ERROR"
        assert error["message"] == "Cannot use --project with --all-projects"
        call.assert_not_called()


def _json_document(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


class TestWarningsInEnvelope:
    def test_success_envelope_carries_warnings(self, capsys: pytest.CaptureFixture[str]) -> None:
        formatter = OutputFormatter(json_mode=True)
        formatter.warning("Project '[bold]prod[/bold]': token \\[expired]")
        formatter.output({"value": 1})

        document = _json_document(capsys)
        assert document["warnings"] == [
            {"code": "WARNING", "message": "Project 'prod': token [expired]"}
        ]
        assert document["data"] == {"value": 1}
        assert list(document) == ["status", "schema_version", "exit_code", "warnings", "data"]

    def test_error_envelope_carries_warnings(self, capsys: pytest.CaptureFixture[str]) -> None:
        formatter = OutputFormatter(json_mode=True)
        formatter.warning("first")
        formatter.warning("second")
        formatter.error("it failed", error_code="CONFIG_ERROR")

        document = _json_document(capsys)
        assert document["warnings"] == [
            {"code": "WARNING", "message": "first"},
            {"code": "WARNING", "message": "second"},
        ]
        assert document["error"]["message"] == "it failed"
        assert list(document) == ["status", "schema_version", "warnings", "error"]

    def test_success_message_carries_warnings(self, capsys: pytest.CaptureFixture[str]) -> None:
        formatter = OutputFormatter(json_mode=True)
        formatter.warning("careful")
        formatter.success("done")

        assert _json_document(capsys)["warnings"] == [{"code": "WARNING", "message": "careful"}]

    def test_no_warnings_key_without_a_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        OutputFormatter(json_mode=True).output({"value": 1})
        assert "warnings" not in _json_document(capsys)

        OutputFormatter(json_mode=True).error("it failed", error_code="CONFIG_ERROR")
        assert "warnings" not in _json_document(capsys)

    def test_warning_after_the_envelope_is_dropped(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        formatter = OutputFormatter(json_mode=True)
        formatter.output({"value": 1})
        formatter.warning("too late")

        captured = capsys.readouterr()
        assert captured.err == ""
        assert json.loads(captured.out) == {
            "status": "ok",
            "schema_version": "1.0",
            "exit_code": 0,
            "data": {"value": 1},
        }

    def test_unbalanced_markup_is_kept_as_text(self, capsys: pytest.CaptureFixture[str]) -> None:
        formatter = OutputFormatter(json_mode=True)
        formatter.warning("closing tag [/x] without an opening tag")
        formatter.output({})

        assert _json_document(capsys)["warnings"][0]["message"] == (
            "closing tag [/x] without an opening tag"
        )

    def test_human_mode_prints_to_stderr_only(self, capsys: pytest.CaptureFixture[str]) -> None:
        formatter = OutputFormatter(json_mode=False, no_color=True)
        formatter.warning("careful")
        formatter.output("result")

        captured = capsys.readouterr()
        assert captured.err == "Warning: careful\n"
        assert captured.out == "result\n"


class TestWarningsFromACommand:
    """`flow examples --component-id keboola.orchestrator` warns before its output, offline."""

    ARGS = ("flow", "examples", "--component-id", "keboola.orchestrator")

    def test_json_mode_puts_the_warning_into_the_envelope(self) -> None:
        result = runner.invoke(app, ["--json", *self.ARGS])

        assert result.exit_code == 0, result.output
        document = json.loads(result.stdout)
        assert [entry["code"] for entry in document["warnings"]] == ["WARNING"]
        assert "keboola.orchestrator" in document["warnings"][0]["message"]
        assert "Warning:" not in result.stderr

    def test_human_mode_prints_the_warning_on_stderr(self) -> None:
        result = runner.invoke(app, self.ARGS)

        assert result.exit_code == 0, result.output
        assert "Warning:" in result.stderr
        assert "Warning:" not in result.stdout


class TestWarningEntryShape:
    def test_code_hint_and_extra_fields(self, capsys: pytest.CaptureFixture[str]) -> None:
        formatter = OutputFormatter(json_mode=True)
        formatter.warning(
            "A new major version is out.",
            code="EXAMPLE_CODE",
            hint="kbagent update --major",
            days_left=28,
            released_at=None,
        )
        formatter.output({})

        assert _json_document(capsys)["warnings"] == [
            {
                "code": "EXAMPLE_CODE",
                "message": "A new major version is out.",
                "hint": "kbagent update --major",
                "days_left": 28,
                "released_at": None,
            }
        ]

    def test_escaped_table_id_reaches_the_envelope_unchanged(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        formatter = OutputFormatter(json_mode=True)
        formatter.warning(f"Table {escape('[in.c-main.x]')} missing, list {escape('[a, b]')}")
        formatter.output({})

        assert _json_document(capsys)["warnings"][0]["message"] == (
            "Table [in.c-main.x] missing, list [a, b]"
        )


class TestCallersEscapeVariableText:
    def test_project_edit_warning_keeps_a_bracketed_table_id(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.edit_project.return_value = {
            "alias": "prod",
            "warnings": ["Table [in.c-main.x] is not readable with the new token."],
        }
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as store_class,
            patch("keboola_agent_cli.cli.ProjectService") as service_factory,
        ):
            store_class.return_value = _store(tmp_path / "config")
            service_factory.return_value = service
            result = runner.invoke(
                app, ["--json", "project", "edit", "--project", "prod", "--new-alias", "main"]
            )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["warnings"] == [
            {
                "code": "WARNING",
                "message": "Table [in.c-main.x] is not readable with the new token.",
            }
        ]


class TestComponentListQueryWithAllProjects:
    def test_warns_that_all_projects_has_no_effect(self, tmp_path: Path) -> None:
        result, call = _invoke(
            tmp_path,
            ["component", "list", "--query", "snowflake", "--all-projects"],
            "ComponentService",
            "list_components",
        )

        assert result.exit_code == 0, result.output
        assert call.call_args.kwargs["aliases"] is None
        warnings = json.loads(result.stdout)["warnings"]
        assert [entry["code"] for entry in warnings] == ["WARNING"]
        assert "--all-projects has no effect" in warnings[0]["message"]

    def test_no_warning_without_all_projects(self, tmp_path: Path) -> None:
        result, _call = _invoke(
            tmp_path,
            ["component", "list", "--query", "snowflake"],
            "ComponentService",
            "list_components",
        )

        assert result.exit_code == 0, result.output
        assert "warnings" not in json.loads(result.stdout)


class TestPlainTextKeepsBrackets:
    """The JSON path removes only real Rich markup; other bracketed text stays."""

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("[bold]x[/bold]", "x"),
            ("[bold red]x[/] y", "x y"),
            ("[link=https://example.com]docs[/link]", "docs"),
            ("Bucket [in.c-main.x] failed", "Bucket [in.c-main.x] failed"),
            ("list [a, b]", "list [a, b]"),
            ("item [0]", "item [0]"),
            ("mode [sync]", "mode [sync]"),
            ("escaped \\[x]", "escaped [x]"),
            ("[bold]Table[/bold] [in.c-main.x]", "Table [in.c-main.x]"),
            ("closing tag [/x] without an opening tag", "closing tag [/x] without an opening tag"),
        ],
    )
    def test_plain_text(self, message: str, expected: str) -> None:
        assert _plain_text(message) == expected

    def test_escaped_text_comes_back_unchanged(self) -> None:
        assert _plain_text(escape("[bold] and [in.c-main.x]")) == "[bold] and [in.c-main.x]"

    def test_emit_project_warnings_keeps_a_bracketed_backend_message(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The commands call this helper only in human mode; this runs it in JSON mode."""
        formatter = OutputFormatter(json_mode=True)
        emit_project_warnings(
            formatter,
            {"errors": [{"project_alias": "prod", "message": "Bucket [in.c-main.x] failed"}]},
        )
        formatter.output({})

        assert _json_document(capsys)["warnings"] == [
            {"code": "WARNING", "message": "Project 'prod': Bucket [in.c-main.x] failed"}
        ]
