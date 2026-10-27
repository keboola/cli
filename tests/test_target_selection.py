"""Contract cases of target selection (keboola/cli#791, CLI-45).

- ``errors.fanout-exit`` (spec E4): a fan-out read in which a project failed
  exits 1 after its full output; with no registered project it exits 5.
- ``target.targets-reported`` (spec T6, T12): every record in ``targets``
  names where its project came from (``project_source``); a destructive
  command under ``--json`` whose project or branch is the saved active choice
  adds a ``TARGET_FROM_ACTIVE_CHOICE`` warning.

The fan-out reads run with their service replaced by a mock, and the other
commands with their body replaced, so no case makes an HTTP request.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.commands._helpers import check_cli_operation
from keboola_agent_cli.commands._project_ref import (
    NOT_A_TARGET_COMMANDS,
    OWN_PROJECT_DEFAULT_COMMANDS,
)
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import ENV_AUTO_UPDATE, ENV_KBAGENT_PROJECT
from keboola_agent_cli.effective_branch import (
    record_branch,
    record_project,
    record_targets,
    recorded_targets,
    resolve_branch,
)
from keboola_agent_cli.models import AppConfig, ProjectConfig
from keboola_agent_cli.output import TARGET_FROM_ACTIVE_CHOICE_CODE
from keboola_agent_cli.root_command import _click
from keboola_agent_cli.services.base import BaseService
from tests.test_all_projects_and_warnings import FAN_OUT_READS
from tests.test_project_ref import EXPECTED_EXCLUDED, _argv_for, _commands_with_project_option

runner = CliRunner()
# `click.globals` or the one Typer >= 0.25 vendors (see root_command._click).
_click_globals = importlib.import_module(f"{_click.__name__}.globals")


@pytest.fixture(autouse=True)
def _no_auto_update(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_AUTO_UPDATE, "false")
    monkeypatch.delenv(ENV_KBAGENT_PROJECT, raising=False)


def _project(project_id: int, branch: int | None = None) -> ProjectConfig:
    return ProjectConfig(
        stack_url="https://connection.keboola.com",
        token=f"{project_id}-token",
        project_name=f"project {project_id}",
        project_id=project_id,
        active_branch_id=branch,
        active_branch_name="feature-x" if branch else None,
    )


def _store(
    config_dir: Path, *aliases: str, active: str = "", branch: int | None = None
) -> ConfigStore:
    """A config with ``aliases`` (project IDs 1, 2, ...); ``branch`` is the active branch of each."""
    store = ConfigStore(config_dir=config_dir)
    projects = {alias: _project(n, branch) for n, alias in enumerate(aliases, start=1)}
    store.save(AppConfig(projects=projects, default_project=active))
    return store


def _failure_payload(method: str) -> Any:
    """A service result in which project ``dev`` failed."""
    if method == "get_status":
        return [
            {"alias": "prod", "status": "ok"},
            {
                "alias": "dev",
                "status": "error",
                "error": "Invalid token",
                "error_code": "INVALID_TOKEN",
            },
        ]
    return {
        "errors": [
            {"project_alias": "dev", "error_code": "INVALID_TOKEN", "message": "Invalid token"}
        ]
    }


def _empty_payload(method: str) -> Any:
    return [] if method == "get_status" else {"errors": []}


def _run_read(
    store: ConfigStore, args: list[str], service_class: str, method: str, payload: Any
) -> tuple[Any, MagicMock]:
    """Run ``kbagent --json ARGS`` with the read's service replaced by a mock."""
    service = MagicMock()
    getattr(service, method).return_value = payload
    with (
        patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
        patch(f"keboola_agent_cli.cli.{service_class}", return_value=service),
    ):
        result = runner.invoke(app, ["--json", *args])
    return result, getattr(service, method)


def _targets(result: Any) -> list[dict[str, Any]]:
    return json.loads(result.stdout).get("targets", [])


# --------------------------------------------------------------------------- #
# errors.fanout-exit (E4)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("args", "service_class", "method"), FAN_OUT_READS)
def test_a_failed_project_exits_1_after_the_output(
    tmp_path: Path, args: list[str], service_class: str, method: str
) -> None:
    store = _store(tmp_path, "prod", "dev", active="prod")
    result, _call = _run_read(
        store, [*args, "--all-projects"], service_class, method, _failure_payload(method)
    )

    assert result.exit_code == 1, result.output
    document = json.loads(result.stdout)
    assert document["status"] == "ok"
    assert document["exit_code"] == 1
    assert document["data"] == _failure_payload(method)


@pytest.mark.parametrize(("args", "service_class", "method"), FAN_OUT_READS)
@pytest.mark.parametrize("all_projects", [False, True], ids=["omitted", "all-projects"])
def test_no_registered_project_exits_5(
    tmp_path: Path, args: list[str], service_class: str, method: str, all_projects: bool
) -> None:
    store = _store(tmp_path)
    argv = [*args, "--all-projects"] if all_projects else args
    result, call = _run_read(store, argv, service_class, method, _empty_payload(method))

    assert result.exit_code == 5, result.output
    assert json.loads(result.stdout)["error"]["code"] == "CONFIG_ERROR"
    call.assert_not_called()


def test_several_projects_and_no_active_one_exit_5(tmp_path: Path) -> None:
    store = _store(tmp_path, "prod", "dev")
    result, call = _run_read(
        store, ["config", "list"], "ConfigService", "list_configs", _empty_payload("")
    )

    assert result.exit_code == 5, result.output
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "CONFIG_ERROR"
    assert "--project" in error["message"]
    call.assert_not_called()


def test_an_answer_that_is_not_a_failure_exits_0(tmp_path: Path) -> None:
    store = _store(tmp_path, "prod", "dev", active="prod")
    payload = {
        "credits": [],
        "errors": [
            {"project_alias": "dev", "error_code": "PAYG_NOT_AVAILABLE", "message": "No PAYG."}
        ],
    }
    result, _call = _run_read(
        store, ["billing", "credits", "--all-projects"], "BillingService", "get_credits", payload
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["exit_code"] == 0


def test_human_mode_exits_1_after_the_output(tmp_path: Path) -> None:
    store = _store(tmp_path, "prod", "dev", active="prod")
    service = MagicMock()
    service.list_jobs.return_value = {"jobs": [], **_failure_payload("list_jobs")}
    with (
        patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
        patch("keboola_agent_cli.cli.JobService", return_value=service),
    ):
        result = runner.invoke(app, ["job", "list", "--all-projects"])

    assert result.exit_code == 1, result.output
    # A failed project is an `Error:` line on stdout, before the headline (HO2).
    assert "Error: project 'dev': Invalid token" in result.stdout
    assert result.stdout.strip().splitlines()[-1].startswith("Failed: 1 of 2 projects.")


# --------------------------------------------------------------------------- #
# target.targets-reported (T6): project_source
# --------------------------------------------------------------------------- #


def _source_of(result: Any) -> dict[str, tuple[str | None, str]]:
    return {t["project_alias"]: (t["project_source"], t["branch_source"]) for t in _targets(result)}


@pytest.mark.parametrize(("args", "service_class", "method"), FAN_OUT_READS)
def test_fan_out_read_reports_the_active_project(
    tmp_path: Path, args: list[str], service_class: str, method: str
) -> None:
    store = _store(tmp_path, "prod", "dev", active="prod")
    result, call = _run_read(store, args, service_class, method, _empty_payload(method))

    assert result.exit_code == 0, result.output
    assert call.call_args.kwargs["aliases"] == ["prod"]
    # The mocked service chose no branch, so the record has branch_source "none",
    # unless the command itself chose production (data-app list, storage reads).
    assert _source_of(result) in (
        {"prod": ("active_project", "none")},
        {"prod": ("active_project", "production")},
    )


@pytest.mark.parametrize(("args", "service_class", "method"), FAN_OUT_READS)
def test_fan_out_read_with_all_projects_reports_each_project(
    tmp_path: Path, args: list[str], service_class: str, method: str
) -> None:
    store = _store(tmp_path, "prod", "dev", active="prod")
    result, _call = _run_read(
        store, [*args, "--all-projects"], service_class, method, _empty_payload(method)
    )

    assert result.exit_code == 0, result.output
    assert _source_of(result) == {"prod": ("all", "none"), "dev": ("all", "none")}


@pytest.mark.parametrize(
    ("env", "active", "aliases", "argv", "expected"),
    [
        pytest.param(None, "prod", ("prod", "dev"), [], ("prod", "active_project"), id="active"),
        pytest.param("dev", "prod", ("prod", "dev"), [], ("dev", "env"), id="env"),
        pytest.param(None, "", ("prod",), [], ("prod", "sole"), id="sole"),
        pytest.param(
            "dev", "prod", ("prod", "dev"), ["--project", "2"], ("dev", "explicit"), id="explicit"
        ),
    ],
)
def test_project_source_follows_the_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: str | None,
    active: str,
    aliases: tuple[str, ...],
    argv: list[str],
    expected: tuple[str, str],
) -> None:
    if env:
        monkeypatch.setenv(ENV_KBAGENT_PROJECT, env)
    store = _store(tmp_path, *aliases, active=active)
    result, call = _run_read(
        store, ["config", "list", *argv], "ConfigService", "list_configs", _empty_payload("")
    )

    alias, source = expected
    assert result.exit_code == 0, result.output
    assert call.call_args.kwargs["aliases"] == [alias]
    assert _source_of(result) == {alias: (source, "none")}


def _replace_body(root: Any, path: tuple[str, ...], body: Any) -> None:
    node = root
    for name in path:
        node = node.get_command(typer.Context(node), name)
    node.callback = body


def _invoke_root(root: Any, args: list[str], capsys: pytest.CaptureFixture[str]) -> Any:
    """Run the Click tree ``root`` (typer's CliRunner takes only a Typer app)."""
    exit_code = root.main(args=args, prog_name="kbagent", standalone_mode=False)
    captured = capsys.readouterr()
    return SimpleNamespace(
        exit_code=exit_code or 0, stdout=captured.out, output=captured.out + captured.err
    )


def _formatter_and_store() -> tuple[Any, ConfigStore]:
    obj = _click_globals.get_current_context().find_root().obj
    return obj["formatter"], obj["config_store"]


def _output_body(**_params: Any) -> None:
    formatter, _config_store = _formatter_and_store()
    formatter.output({})


def test_every_project_option_reports_an_explicit_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every command with a registry --project names that project in `targets` (T6).

    The command bodies are replaced by one that only writes the envelope, so
    the record comes from the --project option itself, before the body runs.
    """
    config_dir = tmp_path / "config"
    store = _store(config_dir, "prod", "other", active="other")
    existing_file = tmp_path / "input.txt"
    existing_file.write_text("x")
    root = typer.main.get_command(app)
    commands = [
        (path, command)
        for path, command in _commands_with_project_option(root)
        if path not in EXPECTED_EXCLUDED
    ]
    assert len(commands) > 100

    missing = []
    for path, command in commands:
        _replace_body(root, path, _output_body)
        param = next(p for p in command.params if "--project" in p.opts)
        # _argv_for gives --project the ID of `prod` (4242 in test_project_ref).
        argv = [
            a if a != "4242" else "1" for a in _argv_for(command, param, "--project", existing_file)
        ]
        result = _invoke_root(
            root, ["--config-dir", str(store.config_dir), "--json", *path, *argv], capsys
        )
        records = (
            [t for t in _targets(result) if t["project_alias"] == "prod"] if result.stdout else []
        )
        if result.exit_code != 0 or not records or records[0]["project_source"] != "explicit":
            missing.append(f"{' '.join(path)} (exit {result.exit_code})")
    assert missing == []


def _single_project_commands(root: Any) -> list[tuple[tuple[str, ...], Any]]:
    """The commands that take one project and use the active project without --project (T17)."""
    found = []
    for path, command in _commands_with_project_option(root):
        param = next(p for p in command.params if "--project" in p.opts)
        if (
            not param.multiple
            and not any(p.name == "all_projects" for p in command.params)
            and path not in EXPECTED_EXCLUDED | OWN_PROJECT_DEFAULT_COMMANDS | NOT_A_TARGET_COMMANDS
        ):
            found.append((path, command))
    return found


def _body_that_outputs(command: Any) -> Any:
    """A command body that writes the value of the command's --project parameter."""
    project_param = next(p.name for p in command.params if "--project" in p.opts)

    def body(**params: Any) -> None:
        _formatter_and_store()[0].output(params[project_param])

    return body


def test_every_single_project_command_uses_the_active_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A command that takes one project runs on the active project when --project is omitted (T17).

    The command bodies are replaced by one that writes the project it received,
    so the value and its `targets` record come from the root command class.
    """
    store = _store(tmp_path / "config", "prod", "other", active="prod")
    existing_file = tmp_path / "input.txt"
    existing_file.write_text("x")
    root = typer.main.get_command(app)
    commands = _single_project_commands(root)
    # Every command with one --project but the 12 that resolve it in their
    # body or take a new alias, plus the `sl` / `mr` copies.
    assert len(commands) > 190

    missing = []
    for path, command in commands:
        _replace_body(root, path, _body_that_outputs(command))
        argv = _argv_for(command, None, "--project", existing_file)
        result = _invoke_root(
            root, ["--config-dir", str(store.config_dir), "--json", *path, *argv], capsys
        )
        envelope = json.loads(result.stdout) if result.stdout else {}
        if (
            result.exit_code != 0
            or envelope.get("data") != "prod"
            or _source_of(result) != {"prod": ("active_project", "none")}
        ):
            missing.append(f"{' '.join(path)} (exit {result.exit_code})")
    assert missing == []


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["job", "detail", "--job-id", "1"], id="read"),
        pytest.param(["storage", "delete-table", "--table-id", "in.c-a.t"], id="destructive"),
        # The commands that resolve an omitted --project in their body.
        pytest.param(["config", "detail", "--component-id", "keboola.ex-x"], id="config-detail"),
        pytest.param(["sync", "pull"], id="sync-pull"),
        pytest.param(["sync", "diff"], id="sync-diff"),
        pytest.param(["sync", "push"], id="sync-push"),
        pytest.param(["workspace", "gc"], id="workspace-gc"),
        pytest.param(["project", "refresh"], id="project-refresh"),
        pytest.param(
            ["project", "invite", "--email", "a@b.com", "--role", "guest"], id="project-invite"
        ),
    ],
)
def test_command_without_a_resolvable_project_is_config_error(
    tmp_path: Path, args: list[str]
) -> None:
    """Several projects and no active one: CONFIG_ERROR (exit 5), never a demand for --project."""
    store = _store(tmp_path, "prod", "dev")
    with patch("keboola_agent_cli.cli.ConfigStore", return_value=store):
        result = runner.invoke(app, ["--json", *args])

    assert result.exit_code == 5, result.output
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "CONFIG_ERROR"
    assert "project use" in error["message"]


def test_missing_required_option_wins_over_a_missing_project(tmp_path: Path) -> None:
    store = _store(tmp_path, "prod", "dev")
    with patch("keboola_agent_cli.cli.ConfigStore", return_value=store):
        result = runner.invoke(app, ["--json", "job", "detail"])

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["error"]["code"] == "USAGE_ERROR"


def test_branch_record_carries_the_project_source() -> None:
    store = MagicMock()
    store.get_project.return_value = _project(1, branch=456)
    with record_targets(lambda _target: None):
        resolve_branch(store, "prod", None)
        record_project("prod", "active_project", _project(1, branch=456))
        record_project("prod", "explicit", _project(1, branch=456))  # the first source wins
        record_project("dev", "all", _project(2))
        record_branch(store, "prod", None, "production", role="source")
        records = [t.to_dict() for t in recorded_targets()]

    assert [
        (r["role"], r["project_alias"], r["project_source"], r["branch_source"]) for r in records
    ] == [
        ("source", "prod", "active_project", "production"),
        ("target", "dev", "all", "none"),
        ("target", "prod", "active_project", "active_branch"),
    ]
    assert list(records[2]) == [
        "role",
        "project_alias",
        "project_source",
        "branch_id",
        "branch_name",
        "branch_source",
        "active_branch",
    ]


def test_nothing_is_recorded_without_a_command() -> None:
    record_project("prod", "explicit", _project(1))
    assert recorded_targets() == []


# --------------------------------------------------------------------------- #
# target.targets-reported (T12): TARGET_FROM_ACTIVE_CHOICE
# --------------------------------------------------------------------------- #


def _active_choice_body(**params: Any) -> None:
    """Resolve the project and the branch as a command on the T1 chain does."""
    formatter, store = _formatter_and_store()
    alias, _source = BaseService(store).resolve_pinned_alias(params.get("project"))
    branch = params.get("branch")
    resolve_branch(store, alias, int(branch) if branch else None)
    formatter.output({})


def _run_with_body(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    path: tuple[str, ...],
    argv: list[str],
    *flags: str,
) -> Any:
    store = _store(tmp_path / "config", "prod", "dev", active="prod", branch=456)
    root = typer.main.get_command(app)
    _replace_body(root, path, _active_choice_body)
    return _invoke_root(root, ["--config-dir", str(store.config_dir), *flags, *path, *argv], capsys)


def test_destructive_command_warns_about_the_active_choice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _run_with_body(
        tmp_path, capsys, ("merge-request", "merge"), ["--merge-request-id", "1"], "--json"
    )

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    warnings = [w for w in document["warnings"] if w["code"] == TARGET_FROM_ACTIVE_CHOICE_CODE]
    assert [w["hint"] for w in warnings] == ["--project prod", "--branch 456"]
    assert "kbagent project use" in warnings[0]["message"]
    assert "kbagent branch use" in warnings[1]["message"]
    assert _source_of(result) == {"prod": ("active_project", "active_branch")}


@pytest.mark.parametrize(
    ("path", "argv"),
    [
        pytest.param(("storage", "delete-table"), ["--table-id", "in.c-a.t"], id="destructive"),
        pytest.param(("branch", "delete"), ["--branch", "456"], id="destructive-branch"),
        pytest.param(("project", "edit"), ["--new-alias", "renamed"], id="admin"),
        pytest.param(("project", "remove"), [], id="admin-remove"),
    ],
)
def test_destructive_or_admin_command_without_project_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], path: tuple[str, ...], argv: list[str]
) -> None:
    """A destructive or admin command whose --project was required before 1.0.0 (T17) warns (T12)."""
    result = _run_with_body(tmp_path, capsys, path, argv, "--json")

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    hints = [w["hint"] for w in document["warnings"] if w["code"] == TARGET_FROM_ACTIVE_CHOICE_CODE]
    assert "--project prod" in hints


def test_explicit_flags_give_no_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = _run_with_body(
        tmp_path,
        capsys,
        ("merge-request", "merge"),
        ["--project", "prod", "--branch", "456"],
        "--json",
    )

    assert result.exit_code == 0, result.output
    assert "warnings" not in json.loads(result.stdout)
    assert _source_of(result) == {"prod": ("explicit", "explicit")}


def test_read_command_gives_no_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = _run_with_body(tmp_path, capsys, ("merge-request", "list"), [], "--json")

    assert result.exit_code == 0, result.output
    assert "warnings" not in json.loads(result.stdout)


def test_human_mode_gives_no_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = _run_with_body(
        tmp_path, capsys, ("merge-request", "merge"), ["--merge-request-id", "1"]
    )

    assert result.exit_code == 0, result.output
    assert "comes from" not in result.output


def _sync_push_body(**params: Any) -> None:
    """Gate --force as `sync push` does, then use the project and its active branch."""
    ctx = _click_globals.get_current_context()
    if params.get("force"):
        check_cli_operation(ctx, "sync.push --force")
    _active_choice_body(project=params.get("project"))


@pytest.mark.parametrize(("flags", "warned"), [([], False), (["--force"], True)])
def test_flag_escalation_to_destructive_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flags: list[str], warned: bool
) -> None:
    """`sync push` is a write; `sync push --force` is destructive (FLAG_ESCALATIONS)."""
    store = _store(tmp_path / "config", "prod", "dev", active="prod", branch=456)
    root = typer.main.get_command(app)
    _replace_body(root, ("sync", "push"), _sync_push_body)
    result = _invoke_root(
        root,
        [
            "--config-dir",
            str(store.config_dir),
            "--json",
            "sync",
            "push",
            "--project",
            "prod",
            *flags,
        ],
        capsys,
    )

    assert result.exit_code == 0, result.output
    hints = [
        w["hint"]
        for w in json.loads(result.stdout).get("warnings", [])
        if w["code"] == TARGET_FROM_ACTIVE_CHOICE_CODE
    ]
    # --project is explicit, so only the active branch is reported.
    assert hints == (["--branch 456"] if warned else [])


def test_argument_errors_come_before_project_resolution(tmp_path: Path) -> None:
    """A bad option value is a usage error (exit 2) even when no project resolves."""
    store = _store(tmp_path, "prod", "dev")
    result, call = _run_read(
        store,
        ["config", "list", "--component-type", "bogus"],
        "ConfigService",
        "list_configs",
        _empty_payload(""),
    )

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["error"]["code"] == "INVALID_ARGUMENT"
    call.assert_not_called()
