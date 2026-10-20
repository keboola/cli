"""Offline contract cases of the CLI interface spec (keboola/cli#791).

Each case pins a behavior that holds on today's code, for every command or
route it applies to, with an empty config directory and no network. A case is
named after its rule id in ``scripts/check_cli_spec.py`` (``CONTRACT_RULES``),
which also lists the contract rules that land later and why.

A contract case lands in the pull request of the fix it checks, so it has no
baseline: it passes for every subject or the test fails.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from keboola_agent_cli import root_command
from keboola_agent_cli.cli import app
from keboola_agent_cli.constants import ENV_AUTO_UPDATE
from keboola_agent_cli.permissions import OPERATION_REGISTRY

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_auto_update(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_AUTO_UPDATE, "false")


# --------------------------------------------------------------------------- #
# INV-3, today's form: with --json, stdout holds one JSON document
# --------------------------------------------------------------------------- #


def _json_document(stdout: str) -> dict[str, Any]:
    """Parse stdout as exactly one JSON document (json.loads rejects trailing data)."""
    return json.loads(stdout)


def test_json_success_is_one_document(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config-dir", str(tmp_path), "--json", "project", "list"])
    assert result.exit_code == 0, result.output
    document = _json_document(result.stdout)
    assert document["status"] == "ok"
    assert "data" in document


def test_json_handled_error_is_one_document(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["--config-dir", str(tmp_path), "--json", "config", "list", "--project", "missing"],
    )
    assert result.exit_code == 5, result.output
    document = _json_document(result.stdout)
    assert document["status"] == "error"
    assert document["error"]["code"] == "CONFIG_ERROR"
    assert document["error"]["message"]


# --------------------------------------------------------------------------- #
# serve.route-permission: a destructive route under --deny-destructive is 403
# --------------------------------------------------------------------------- #

fastapi_missing = importlib.util.find_spec("fastapi") is None
TOKEN = "cli-contract-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _destructive_routes() -> list[tuple[str, str]]:
    if fastapi_missing:  # pragma: no cover
        return []
    from keboola_agent_cli.server.route_permissions import ROUTE_OPERATIONS

    return sorted(
        (method, path)
        for (method, path), operation in ROUTE_OPERATIONS.items()
        if OPERATION_REGISTRY.get(operation) == "destructive"
    )


def _concrete(path: str) -> str:
    """A request URL for a route template: every path parameter becomes "1"."""
    return re.sub(r"\{[^}]+\}", "1", path)


@pytest.mark.skipif(fastapi_missing, reason="FastAPI not installed (server extra)")
def test_there_are_destructive_routes_to_check() -> None:
    assert len(_destructive_routes()) > 20


@pytest.fixture(scope="module")
def deny_destructive_client(tmp_path_factory: pytest.TempPathFactory) -> Any:
    from fastapi.testclient import TestClient

    from keboola_agent_cli.server import create_app

    config_dir = tmp_path_factory.mktemp("serve")
    return TestClient(
        create_app(config_dir=str(config_dir), auth_token=TOKEN, deny_destructive=True)
    )


@pytest.mark.skipif(fastapi_missing, reason="FastAPI not installed (server extra)")
@pytest.mark.parametrize(("method", "path"), _destructive_routes())
def test_destructive_route_is_denied_under_deny_destructive(
    deny_destructive_client: Any, httpx_mock: Any, method: str, path: str
) -> None:
    """The policy answers before the route runs: 403 and no outgoing HTTP call."""
    response = deny_destructive_client.request(method, _concrete(path), headers=AUTH, json={})
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"
    assert httpx_mock.get_requests() == []


# --------------------------------------------------------------------------- #
# The 1.0.0 root command class (CLI-44): O1, O2, O4, O7, A6, H5
#
# One Click tree is built for the module and reused by every invocation, so a
# case over all commands stays fast. No case runs a command body: each one
# stops at a parse error, a permission denial, or a patched command class.
# --------------------------------------------------------------------------- #

BAD_FLAG = "--no-such-option-cli-44"


@pytest.fixture(scope="module")
def root() -> Any:
    import typer.main

    return typer.main.get_command(app)


@pytest.fixture(autouse=True)
def _reuse_root(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if "root" in request.fixturenames:
        import typer.testing

        built = request.getfixturevalue("root")
        monkeypatch.setattr(typer.testing, "_get_command", lambda _app: built)


def _canonical_tree() -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
    """(group paths, command paths) of the visible tree; alias groups (mr, sl) are skipped."""
    import typer.main
    from typer.core import TyperGroup

    groups: list[tuple[str, ...]] = []
    leaves: list[tuple[str, ...]] = []

    def walk(node: Any, prefix: tuple[str, ...]) -> None:
        for name, sub in node.commands.items():
            if getattr(sub, "hidden", False):
                continue
            path = (*prefix, name)
            if isinstance(sub, TyperGroup):
                groups.append(path)
                walk(sub, path)
            else:
                leaves.append(path)

    walk(typer.main.get_command(app), ())
    return groups, leaves


GROUPS, LEAVES = _canonical_tree()


def _ids(paths: list[tuple[str, ...]]) -> list[str]:
    return [" ".join(p) for p in paths]


def _category(path: tuple[str, ...]) -> str:
    return OPERATION_REGISTRY.get(".".join(path), "write")


def _command(root: Any, path: tuple[str, ...]) -> Any:
    node = root
    for name in path:
        node = node.commands[name]
    return node


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_BOX = re.compile(r"[\u2500-\u257f]")


def _plain(text: str) -> str:
    """stderr without color codes, Rich box lines and line wrapping, for substring checks."""
    return " ".join(_BOX.sub(" ", _ANSI.sub("", text)).split())


def test_the_tree_has_commands_and_groups_to_check() -> None:
    assert len(LEAVES) > 250
    assert len(GROUPS) > 25


def _usage_error_document(result: Any) -> dict[str, Any]:
    assert result.exit_code == 2, result.output
    document = _json_document(result.stdout)
    assert document["status"] == "error"
    assert document["error"]["code"] == "USAGE_ERROR"
    return document


# -- output.one-json-document ------------------------------------------------


@pytest.mark.parametrize("path", LEAVES, ids=_ids(LEAVES))
def test_one_json_document_for_a_parse_error(
    root: Any, tmp_path: Path, httpx_mock: Any, path: tuple[str, ...]
) -> None:
    result = runner.invoke(app, ["--config-dir", str(tmp_path), "--json", *path, BAD_FLAG])
    document = _usage_error_document(result)
    assert BAD_FLAG in document["error"]["message"]


def test_a_bad_option_value_is_invalid_argument(root: Any, tmp_path: Path, httpx_mock: Any) -> None:
    """I4: a value that fails Click's type check is INVALID_ARGUMENT and names the option."""
    args = ["agent", "cron-preview", "--cron", "0 6 * * 1", "--count", "abc"]
    result = runner.invoke(app, ["--config-dir", str(tmp_path), "--json", *args])
    assert result.exit_code == 2, result.output
    document = _json_document(result.stdout)
    assert document["error"]["code"] == "INVALID_ARGUMENT"
    assert "--count" in document["error"]["message"]


def test_a_missing_required_option_stays_usage_error(
    root: Any, tmp_path: Path, httpx_mock: Any
) -> None:
    result = runner.invoke(
        app, ["--config-dir", str(tmp_path), "--json", "agent", "cron-preview", "--count", "2"]
    )
    document = _usage_error_document(result)
    assert "--cron" in document["error"]["message"]


def test_click_error_codes() -> None:
    """A bad value is INVALID_ARGUMENT; a missing option or a malformed call is USAGE_ERROR."""
    click = root_command._click.exceptions
    assert root_command.click_error_code(click.BadParameter("bad")) == "INVALID_ARGUMENT"
    assert root_command.click_error_code(click.MissingParameter()) == "USAGE_ERROR"
    assert root_command.click_error_code(click.UsageError("bad call")) == "USAGE_ERROR"
    assert root_command.click_error_code(click.NoSuchOption("--x")) == "USAGE_ERROR"
    assert root_command.click_error_code(click.ClickException("other")) == "UNKNOWN_ERROR"


@pytest.mark.parametrize("path", LEAVES, ids=_ids(LEAVES))
def test_one_json_document_for_a_crash(
    root: Any,
    tmp_path: Path,
    httpx_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
    path: tuple[str, ...],
) -> None:
    from keboola_agent_cli.root_command import LeafCommand

    def crash(self: Any, ctx: Any, args: list[str]) -> list[str]:
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(LeafCommand, "parse_args", crash)
    result = runner.invoke(app, ["--config-dir", str(tmp_path), "--json", *path])
    assert result.exit_code == 1, result.output
    document = _json_document(result.stdout)
    assert document["status"] == "error"
    assert document["error"]["code"] == "INTERNAL_ERROR"
    assert "simulated crash" in document["error"]["message"]
    assert "Traceback" in _plain(result.stderr)


def _exit_with_code(code: int) -> Any:
    def stop(self: Any, ctx: Any, args: list[str]) -> list[str]:
        raise typer.Exit(code=code)

    return stop


def _sys_exit(self: Any, ctx: Any, args: list[str]) -> list[str]:
    raise SystemExit(3)


def _ctx_exit(self: Any, ctx: Any, args: list[str]) -> list[str]:
    ctx.exit(4)
    return args


def _abort(self: Any, ctx: Any, args: list[str]) -> list[str]:
    raise typer.Abort()


def _interrupt(self: Any, ctx: Any, args: list[str]) -> list[str]:
    raise KeyboardInterrupt


@pytest.mark.parametrize(
    ("stop", "exit_code", "error_code"),
    [
        (_exit_with_code(1), 1, "UNKNOWN_ERROR"),
        (_sys_exit, 3, "UNKNOWN_ERROR"),
        (_ctx_exit, 4, "UNKNOWN_ERROR"),
        (_abort, 1, "INTERRUPTED"),
        (_interrupt, 130, "INTERRUPTED"),
    ],
    ids=["typer-exit", "sys-exit", "ctx-exit", "abort", "keyboard-interrupt"],
)
def test_an_exit_without_a_document_writes_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: Any, exit_code: int, error_code: str
) -> None:
    from keboola_agent_cli.root_command import LeafCommand

    monkeypatch.setattr(LeafCommand, "parse_args", stop)
    result = runner.invoke(app, ["--config-dir", str(tmp_path), "--json", "project", "list"])
    assert result.exit_code == exit_code, result.output
    document = _json_document(result.stdout)
    assert document["status"] == "error"
    assert document["error"]["code"] == error_code


def test_an_exit_zero_writes_no_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from keboola_agent_cli.root_command import LeafCommand

    monkeypatch.setattr(LeafCommand, "parse_args", _exit_with_code(0))
    result = runner.invoke(app, ["--config-dir", str(tmp_path), "--json", "project", "list"])
    assert (result.exit_code, result.stdout) == (0, "")


@pytest.mark.parametrize("attribute", ["_protected_args", "protected_args"])
def test_protected_args_are_read_on_click_8_1_and_8_2(attribute: str) -> None:
    """Click >= 8.2 has `ctx._protected_args`, Click 8.1 `ctx.protected_args`."""
    from types import SimpleNamespace

    from keboola_agent_cli.root_command import protected_args

    ctx = SimpleNamespace(**{attribute: ["storage"]})
    assert protected_args(ctx) == ["storage"]


def test_a_custom_command_class_is_not_replaced() -> None:
    """The tree walk refuses a command whose class it would silently drop."""
    from typer.core import TyperCommand, TyperGroup

    from keboola_agent_cli.root_command import apply_command_classes

    class CustomCommand(TyperCommand):
        pass

    group = TyperGroup(name="demo")
    group.add_command(CustomCommand(name="leaf"))
    root = TyperGroup(name="kbagent", commands={"demo": group})
    with pytest.raises(TypeError, match="demo leaf"):
        apply_command_classes(root)


def test_a_bad_root_option_under_json_is_one_document(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config-dir", str(tmp_path), "--json", BAD_FLAG])
    _usage_error_document(result)


def test_human_mode_parse_error_keeps_stdout_empty(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config-dir", str(tmp_path), "project", "list", BAD_FLAG])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert BAD_FLAG in _plain(result.stderr)


# -- output.bare-group-exits-2 -----------------------------------------------


@pytest.mark.parametrize("path", GROUPS, ids=_ids(GROUPS))
def test_bare_group_exits_2_with_help_on_stderr(
    root: Any, tmp_path: Path, path: tuple[str, ...]
) -> None:
    for args in (["--json", *path], [*path, "--json"]):
        result = runner.invoke(app, ["--config-dir", str(tmp_path), *args])
        document = _usage_error_document(result)
        assert "Missing command" in document["error"]["message"]
        assert "Usage" in _plain(result.stderr)

    human = runner.invoke(app, ["--config-dir", str(tmp_path), *path])
    assert human.exit_code == 2
    assert human.stdout == ""
    assert "Usage" in _plain(human.stderr)


def test_json_without_a_command_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config-dir", str(tmp_path), "--json"])
    document = _usage_error_document(result)
    assert "Missing command" in document["error"]["message"]
    assert "Usage" in _plain(result.stderr)


# -- output.root-flags-anywhere ----------------------------------------------


@pytest.mark.parametrize("path", LEAVES, ids=_ids(LEAVES))
def test_json_after_the_command_gives_the_same_document(
    root: Any, tmp_path: Path, httpx_mock: Any, path: tuple[str, ...]
) -> None:
    before = runner.invoke(app, ["--config-dir", str(tmp_path), "--json", *path, BAD_FLAG])
    after = runner.invoke(app, ["--config-dir", str(tmp_path), *path, BAD_FLAG, "--json"])
    assert (after.exit_code, after.stdout) == (before.exit_code, before.stdout)


DENIABLE = [p for p in LEAVES if _category(p) != "read" and p[0] != "permissions"]


@pytest.mark.parametrize("path", DENIABLE, ids=_ids(DENIABLE))
@pytest.mark.parametrize("flag", ["--deny-writes", "--deny-destructive"])
def test_deny_flags_after_the_command_apply(
    root: Any, tmp_path: Path, httpx_mock: Any, flag: str, path: tuple[str, ...]
) -> None:
    """A write command after `--deny-writes` is refused before it runs, in either position."""
    if flag == "--deny-destructive" and _category(path) != "destructive":
        pytest.skip("--deny-destructive refuses destructive commands only")
    before = runner.invoke(app, ["--config-dir", str(tmp_path), "--json", flag, *path])
    after = runner.invoke(app, ["--config-dir", str(tmp_path), *path, flag, "--json"])
    assert before.exit_code == 6, before.output
    assert (after.exit_code, after.stdout) == (before.exit_code, before.stdout)
    assert _json_document(after.stdout)["error"]["code"] == "PERMISSION_DENIED"


def test_json_after_the_command_runs_it_in_json_mode(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config-dir", str(tmp_path), "project", "list", "-j"])
    assert result.exit_code == 0, result.output
    assert _json_document(result.stdout)["status"] == "ok"


def test_an_option_value_equal_to_json_stays_a_value(tmp_path: Path) -> None:
    """argv is not rewritten: `--description --json` sets the description (O7)."""
    args = ["config", "update", "--project", "missing", "--component-id", "c"]
    args += ["--config-id", "1", "--description", "--json"]
    result = runner.invoke(app, ["--config-dir", str(tmp_path), *args])
    assert result.exit_code == 5, result.output
    assert result.stdout == ""
    assert "Error" in _plain(result.stderr)


# -- output.envelope-schema --------------------------------------------------

SUCCESS_KEYS = {"status", "schema_version", "exit_code", "targets", "warnings", "data"}
ERROR_KEYS = {"status", "schema_version", "targets", "warnings", "error"}


def test_success_envelope_schema(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config-dir", str(tmp_path), "--json", "project", "list"])
    document = _json_document(result.stdout)
    assert set(document) <= SUCCESS_KEYS
    assert {"status", "schema_version", "exit_code", "data"} <= set(document)
    assert document["status"] == "ok"
    assert re.fullmatch(r"\d+\.\d+", document["schema_version"])
    assert document["exit_code"] == result.exit_code == 0


def test_error_envelope_schema(tmp_path: Path) -> None:
    args = ["--config-dir", str(tmp_path), "--json", "config", "list", "--project", "missing"]
    result = runner.invoke(app, args)
    document = _json_document(result.stdout)
    assert set(document) <= ERROR_KEYS
    assert {"status", "schema_version", "error"} <= set(document)
    assert "exit_code" not in document
    assert {"code", "message"} <= set(document["error"])


def test_ok_with_a_non_zero_exit_declares_it(tmp_path: Path) -> None:
    """O4: `status: ok` and the process exit code are both in the envelope."""
    args = ["permissions", "check", "storage.delete-bucket", "--deny-destructive", "--json"]
    result = runner.invoke(app, ["--config-dir", str(tmp_path), *args])
    document = _json_document(result.stdout)
    assert document["status"] == "ok"
    assert document["exit_code"] == result.exit_code == 6


def test_warning_entries_have_code_and_message(tmp_path: Path) -> None:
    args = ["permissions", "list", "--category", "read", "--category", "write", "--json"]
    result = runner.invoke(app, ["--config-dir", str(tmp_path), *args])
    assert result.exit_code == 0, result.output
    document = _json_document(result.stdout)
    assert set(document) <= SUCCESS_KEYS
    for entry in document["warnings"]:
        assert {"code", "message"} <= set(entry)
        assert isinstance(entry.get("hint", ""), str)


# -- help.dash-h -------------------------------------------------------------


@pytest.mark.parametrize("path", [(), *GROUPS, *LEAVES], ids=["root", *_ids(GROUPS + LEAVES)])
def test_dash_h_prints_the_help(root: Any, path: tuple[str, ...]) -> None:
    long_form = runner.invoke(app, [*path, "--help"])
    for args in ([*path, "-h"], ["--json", *path, "-h"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        assert result.stdout == long_form.stdout
        assert "Usage" in result.stdout


# -- args.repeated-single-value ----------------------------------------------


def _repeatable_case(root: Any, path: tuple[str, ...]) -> tuple[str, list[str]] | None:
    """A single-value option of the command and two occurrences of it, or None."""
    from keboola_agent_cli.root_command import _single_value

    for param in _command(root, path).params:
        if not _single_value(param):
            continue
        name = next((o for o in param.opts if o.startswith("--")), param.opts[0])
        if getattr(param, "is_flag", False):
            return name, [param.opts[0], param.secondary_opts[0]]
        choices = getattr(param.type, "choices", None)
        value = str(getattr(choices[0], "value", choices[0])) if choices else "1"
        values = [value] * max(int(getattr(param, "nargs", 1) or 1), 1)
        return name, [param.opts[0], *values, param.opts[0], *values]
    return None


@pytest.mark.parametrize("path", LEAVES, ids=_ids(LEAVES))
def test_repeated_single_value_option(
    root: Any,
    tmp_path: Path,
    httpx_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
    path: tuple[str, ...],
) -> None:
    """Refused on write, destructive and admin commands, a warning on read commands (A6)."""
    case = _repeatable_case(root, path)
    if case is None:
        pytest.skip("the command has no single-value option")
    name, args = case
    from keboola_agent_cli.root_command import REPEAT_REFUSED_CATEGORIES

    if _category(path) in REPEAT_REFUSED_CATEGORIES:
        result = runner.invoke(app, ["--config-dir", str(tmp_path), "--json", *path, *args])
        document = _usage_error_document(result)
        assert name in document["error"]["message"]
        return

    from keboola_agent_cli.root_command import LeafCommand

    # The command body must not run: only the parse is under test.
    monkeypatch.setattr(LeafCommand, "invoke", lambda self, ctx: None)
    once = args[: len(args) // 2]
    single = runner.invoke(app, ["--config-dir", str(tmp_path), *path, *once])
    twice = runner.invoke(app, ["--config-dir", str(tmp_path), *path, *args])
    assert twice.exit_code == single.exit_code
    assert f"{name} given more than once" in _plain(twice.stderr)
    assert "given more than once" not in _plain(single.stderr)


def test_most_commands_have_a_single_value_option(root: Any) -> None:
    assert sum(_repeatable_case(root, p) is not None for p in LEAVES) > 200


def test_repeated_project_on_a_destructive_command_names_the_option(tmp_path: Path) -> None:
    args = ["storage", "delete-bucket", "--project", "x", "--bucket-id", "in.c-a"]
    result = runner.invoke(app, ["--config-dir", str(tmp_path), *args, "--project", "test"])
    assert result.exit_code == 2
    assert "--project given more than once" in _plain(result.stderr)


def test_repeated_project_on_an_admin_command_is_refused(tmp_path: Path) -> None:
    args = ["project", "remove", "--project", "a", "--project", "b", "--json"]
    result = runner.invoke(app, ["--config-dir", str(tmp_path), *args])
    document = _usage_error_document(result)
    assert "--project given more than once" in document["error"]["message"]


def test_repeated_option_on_a_read_command_is_a_warning_entry(tmp_path: Path) -> None:
    args = ["permissions", "list", "--category", "read", "--category", "write", "--json"]
    result = runner.invoke(app, ["--config-dir", str(tmp_path), *args])
    assert result.exit_code == 0, result.output
    [entry] = [w for w in _json_document(result.stdout)["warnings"] if w["code"] != "WARNING"]
    assert entry["code"] == "REPEATED_OPTION"
    assert entry["options"] == ["--category"]


# -- output.one-json-document: a --stream command keeps stdout NDJSON --------


def _stream_then(failure: BaseException) -> Any:
    async def stream_run(self: Any, task_id: str, runtime_input: Any = None) -> Any:
        yield {"event": "init", "data": {"task_id": task_id}}
        yield {"event": "stdout", "data": {"line": "working"}}
        raise failure

    return stream_run


@pytest.mark.parametrize(
    ("failure", "exit_code", "error_code"),
    [(KeyboardInterrupt(), 130, "INTERRUPTED"), (RuntimeError("boom"), 1, "INTERNAL_ERROR")],
    ids=["interrupt", "crash"],
)
def test_a_stream_that_fails_stays_ndjson(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    exit_code: int,
    error_code: str,
) -> None:
    """After the first NDJSON line the error is one more line, not a JSON document."""
    from keboola_agent_cli.services.agent_service import AgentService

    monkeypatch.setattr(AgentService, "stream_run", _stream_then(failure))
    args = ["--config-dir", str(tmp_path), "--json", "agent", "run", "abc123", "--stream"]
    result = runner.invoke(app, args)
    assert result.exit_code == exit_code, result.output
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [e["event"] for e in events] == ["init", "stdout", "error"]
    assert events[-1]["data"]["code"] == error_code


# --------------------------------------------------------------------------- #
# safety.no-tty-confirmation: without a terminal and without --yes, a
# confirming command refuses (CONFIRMATION_REQUIRED, exit 2). An open stdin
# pipe that answers "y" does not confirm (D6).
# --------------------------------------------------------------------------- #

# Command lines that reach the safety prompt with an empty config directory and
# no network: the prompt comes before any read.
CONFIRMING_COMMANDS: dict[str, str] = {
    "agent delete": "agent delete t1",
    "auth logout": "auth logout",
    "branch delete": "branch delete --project p --branch 1",
    "branch metadata-delete": "branch metadata-delete --project p --metadata-id 1",
    "config delete-metadata": (
        "config delete-metadata --project p --component-id c --config-id 1 --metadata-id 1"
    ),
    "config row-delete": "config row-delete --project p --component-id c --config-id 1 --row-id 1",
    "config state-set": "config state-set --project p --component-id c --config-id 1 --state {}",
    "config variables-clear": "config variables-clear --project p --component-id c --config-id 1",
    "data-app delete": "data-app delete --project p --app-id 1",
    "data-app git-credentials-create": (
        "data-app git-credentials-create --project p --app-id 1 --type http_token "
        "--permissions readOnly"
    ),
    "data-app secrets-remove": "data-app secrets-remove --project p --app-id 1 --key K",
    "feature project-add": "feature project-add --project p --feature f",
    "feature project-remove": "feature project-remove --project p --feature f",
    "feature user-add": "feature user-add --project p --email a@example.com --feature f",
    "feature user-remove": "feature user-remove --project p --email a@example.com --feature f",
    "flow delete": "flow delete --project p --flow-id 1",
    "job terminate": "job terminate --project p --job-id 1",
    "notification delete": "notification delete --project p --subscription-id 1",
    "notification replace-recipient": (
        "notification replace-recipient --project p --subscription-id 1 --address a@example.com"
    ),
    "project invitation-cancel": "project invitation-cancel --project p --email a@example.com",
    "project member-remove": "project member-remove --project p --email a@example.com",
    "semantic-layer edit glossary": "semantic-layer edit glossary --project p --term a --new-term b",
    "semantic-layer model delete": "semantic-layer model delete --project p --model m",
    "semantic-layer promote": "semantic-layer promote --from-project a --to-project b",
    "semantic-layer reference-data delete": "semantic-layer reference-data delete --project p --id x",
    "semantic-layer scope set": (
        "semantic-layer scope set --project p --type model --context-id x --scope organization"
    ),
    "storage delete-bucket": "storage delete-bucket --project p --bucket-id in.c-a",
    "storage delete-column": "storage delete-column --project p --table-id in.c-a.b --column c",
    "storage delete-table": "storage delete-table --project p --table-id in.c-a.b",
    "storage file-delete": "storage file-delete --project p --file-id 1",
    "storage snapshot-delete": "storage snapshot-delete --project p --snapshot-id 1",
    "storage swap-tables": (
        "storage swap-tables --project p --table-id in.c-a.b --target-table-id in.c-a.c --branch 1"
    ),
    "storage truncate-table": "storage truncate-table --project p --table-id in.c-a.b",
    "stream delete": "stream delete s1 --project p",
    "token delete": "token delete --project p --token-id 1",
    "token refresh": "token refresh --project p --token-id 1",
    "workspace delete": "workspace delete --project p --workspace-id 1",
    "workspace gc": "workspace gc --project p",
}

# Commands with --yes whose prompt comes after a read (they need a registered
# project), or whose --yes answers another question. The unit tests of each
# command cover their prompt.
YES_WITHOUT_OFFLINE_CASE: dict[str, str] = {
    "auth register-projects": "non-safety question: register projects (S4 exception)",
    "flow schedule-remove": "lists the schedules first, then asks",
    "merge-request auto-merge": "resolves the merge request first, then asks",
    "merge-request merge": "resolves the merge request first, then asks",
    "org setup": "needs the manage token first, then previews and asks",
    "project refresh": "needs the manage token first, then previews and asks",
    "semantic-layer add metric": "dataset guard in the service; refuses without a terminal",
    "semantic-layer edit metric": "rename-cascade guard in the service; refuses without a terminal",
    "semantic-layer import": "hidden deprecated --yes with no effect (C3)",
    "semantic-layer remove constraint": "reads the orphaned constraints first, then asks",
    "semantic-layer remove dataset": "reads the orphaned constraints first, then asks",
    "semantic-layer remove glossary": "reads the orphaned constraints first, then asks",
    "semantic-layer remove metric": "reads the orphaned constraints first, then asks",
    "semantic-layer remove relationship": "reads the orphaned constraints first, then asks",
    "storage describe-migrate": "scans the tables first, then asks",
}


def _leaves_with_yes() -> set[str]:
    import typer.main

    found: set[str] = set()

    def walk(command: Any, path: list[str]) -> None:
        children = getattr(command, "commands", None)
        if children is None:
            if any("--yes" in getattr(p, "opts", []) for p in command.params):
                found.add(" ".join(path))
            return
        for name, child in children.items():
            if not getattr(child, "hidden", False):
                walk(child, [*path, name])

    walk(typer.main.get_command(app), [])
    return found


def test_every_command_with_yes_is_classified() -> None:
    """A new command with --yes gets an offline case or a reason here."""
    classified = set(CONFIRMING_COMMANDS) | set(YES_WITHOUT_OFFLINE_CASE)
    assert _leaves_with_yes() == classified


@pytest.mark.parametrize("command", sorted(CONFIRMING_COMMANDS))
def test_no_terminal_confirmation_refuses(tmp_path: Path, command: str) -> None:
    args = CONFIRMING_COMMANDS[command].split()
    result = runner.invoke(app, ["--config-dir", str(tmp_path), *args], input="y\n")
    assert result.exit_code == 2, result.output
    assert "CONFIRMATION_REQUIRED" in result.stderr
    assert "--yes" in result.stderr
    assert "AI agents: ask the user" in " ".join(result.stderr.split())
    assert result.stdout == ""
