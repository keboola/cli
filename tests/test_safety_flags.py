"""Safety flags of the CLI interface spec (S2-S5, C3; keboola/cli#791, CLI-46).

- `_confirm_or_abort`: --yes and --json skip the prompt; a terminal is asked on
  stderr; without a terminal the command refuses with CONFIRMATION_REQUIRED.
- `branch delete` and `workspace delete`: --yes, a read-only --dry-run and the
  confirmation; the REST routes pass `dry_run` and never prompt.
- A form that 1.0.0 removed (`stream delete --force`, `project add --project`,
  `branch merge`, ...) is a usage error whose message names the replacement.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from helpers import TerminalRunner, setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.commands import _helpers
from keboola_agent_cli.commands._helpers import (
    DEPRECATIONS,
    _confirm_or_abort,
    deprecated_option,
)
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.output import OutputFormatter
from keboola_agent_cli.services.branch_service import BranchService
from keboola_agent_cli.services.workspace_service import WorkspaceService

ALIAS = "prod"
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()


def _seed(config_dir: Path) -> None:
    config_dir.mkdir(exist_ok=True)
    ConfigStore(config_dir=config_dir).add_project(
        ALIAS,
        ProjectConfig(stack_url="https://connection.keboola.com", token=TOKEN, project_id=1),
    )


def _invoke(
    tmp_path: Path,
    service_attr: str,
    service: MagicMock,
    args: list[str],
    *,
    user_input: str | None = None,
    cli_runner: CliRunner = runner,
) -> Any:
    config_dir = tmp_path / "config"
    _seed(config_dir)
    with patch(f"keboola_agent_cli.cli.{service_attr}", return_value=service):
        return cli_runner.invoke(app, ["--config-dir", str(config_dir), *args], input=user_input)


# --------------------------------------------------------------------------- #
# The shared helper
# --------------------------------------------------------------------------- #


def _no_prompt(*_args: Any, **_kwargs: Any) -> bool:
    raise AssertionError("the helper must not prompt here")


class TestConfirmOrAbort:
    def test_yes_skips_the_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(typer, "confirm", _no_prompt)
        _confirm_or_abort(OutputFormatter(json_mode=False), True, "Delete?")

    def test_json_skips_the_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(typer, "confirm", _no_prompt)
        monkeypatch.setattr(_helpers, "is_interactive_terminal", lambda: False)
        _confirm_or_abort(OutputFormatter(json_mode=True), False, "Delete?")

    def test_no_terminal_refuses_with_exit_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(typer, "confirm", _no_prompt)
        monkeypatch.setattr(_helpers, "is_interactive_terminal", lambda: False)
        with pytest.raises(typer.Exit) as exc:
            _confirm_or_abort(OutputFormatter(json_mode=False), False, "Delete it?")
        assert exc.value.exit_code == 2
        err = " ".join(capsys.readouterr().err.split())
        assert "CONFIRMATION_REQUIRED: Delete it?" in err
        assert "--yes" in err
        assert "AI agents: ask the user before you retry with --yes." in err

    def test_terminal_decline_aborts_with_exit_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        asked: list[tuple[str, bool]] = []

        def _decline(question: str, err: bool = False) -> bool:
            asked.append((question, err))
            return False

        monkeypatch.setattr(typer, "confirm", _decline)
        monkeypatch.setattr(_helpers, "is_interactive_terminal", lambda: True)
        with pytest.raises(typer.Exit) as exc:
            _confirm_or_abort(OutputFormatter(json_mode=False), False, "Delete it?")
        assert exc.value.exit_code == 0
        assert asked == [("Delete it?", True)]  # the prompt goes to stderr

    def test_terminal_accept_returns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(typer, "confirm", lambda question, err=False: True)
        monkeypatch.setattr(_helpers, "is_interactive_terminal", lambda: True)
        _confirm_or_abort(OutputFormatter(json_mode=False), False, "Delete it?")

    @pytest.mark.parametrize(("stdin_tty", "stderr_tty"), [(True, False), (False, True)])
    def test_one_stream_without_terminal_is_no_terminal(
        self, tmp_path: Path, stdin_tty: bool, stderr_tty: bool
    ) -> None:
        service = MagicMock()
        result = _invoke(
            tmp_path,
            "TokenService",
            service,
            ["token", "delete", "--project", ALIAS, "--token-id", "1"],
            cli_runner=TerminalRunner(stdin_tty, stderr_tty),
        )
        assert result.exit_code == 2, result.output
        service.delete_token.assert_not_called()

    def test_confirmation_required_maps_to_exit_2(self) -> None:
        exc = KeboolaApiError("x", error_code=ErrorCode.CONFIRMATION_REQUIRED)
        assert _helpers.map_error_to_exit_code(exc) == 2


# --------------------------------------------------------------------------- #
# branch delete / workspace delete (S2, S5)
# --------------------------------------------------------------------------- #

DELETE_COMMANDS: dict[str, tuple[str, str, list[str], dict[str, Any]]] = {
    "branch delete": (
        "BranchService",
        "delete_branch",
        ["branch", "delete", "--project", ALIAS, "--branch", "5"],
        {"alias": ALIAS, "branch_id": 5},
    ),
    "workspace delete": (
        "WorkspaceService",
        "delete_workspace",
        ["workspace", "delete", "--project", ALIAS, "--workspace-id", "7"],
        {"alias": ALIAS, "workspace_id": 7},
    ),
}


def _run_delete(
    tmp_path: Path,
    command: str,
    extra: list[str],
    *,
    user_input: str | None = None,
    cli_runner: CliRunner = runner,
) -> tuple[Any, MagicMock, dict[str, Any]]:
    service_attr, method, args, kwargs = DELETE_COMMANDS[command]
    service = MagicMock()
    dry_run = "--dry-run" in extra
    getattr(service, method).return_value = {
        "project_alias": ALIAS,
        "dry_run": dry_run,
        "message": "Would delete it." if dry_run else "Deleted it.",
    }
    json_flag = ["--json"] if "--json" in extra else []
    rest = [a for a in extra if a != "--json"]
    result = _invoke(
        tmp_path,
        service_attr,
        service,
        [*json_flag, *args, *rest],
        user_input=user_input,
        cli_runner=cli_runner,
    )
    return result, getattr(service, method), kwargs


@pytest.mark.parametrize("command", sorted(DELETE_COMMANDS))
class TestDeleteConfirmation:
    def test_no_terminal_without_yes_refuses(self, tmp_path: Path, command: str) -> None:
        result, method, _ = _run_delete(tmp_path, command, [], user_input="y\n")
        assert result.exit_code == 2, result.output
        assert "--yes" in result.stderr
        method.assert_not_called()

    def test_yes_deletes_without_a_prompt(self, tmp_path: Path, command: str) -> None:
        result, method, kwargs = _run_delete(tmp_path, command, ["--yes"])
        assert result.exit_code == 0, result.output
        method.assert_called_once_with(**kwargs, dry_run=False)

    def test_short_y_deletes(self, tmp_path: Path, command: str) -> None:
        result, method, _ = _run_delete(tmp_path, command, ["-y"])
        assert result.exit_code == 0, result.output
        method.assert_called_once()

    def test_json_is_consent(self, tmp_path: Path, command: str) -> None:
        result, method, kwargs = _run_delete(tmp_path, command, ["--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["status"] == "ok"
        method.assert_called_once_with(**kwargs, dry_run=False)

    def test_terminal_decline_deletes_nothing(self, tmp_path: Path, command: str) -> None:
        result, method, _ = _run_delete(
            tmp_path, command, [], user_input="n\n", cli_runner=TerminalRunner()
        )
        assert result.exit_code == 0, result.output
        assert "[y/N]" in result.stderr
        assert "Aborted." in result.output
        method.assert_not_called()

    def test_terminal_accept_deletes(self, tmp_path: Path, command: str) -> None:
        result, method, kwargs = _run_delete(
            tmp_path, command, [], user_input="y\n", cli_runner=TerminalRunner()
        )
        assert result.exit_code == 0, result.output
        method.assert_called_once_with(**kwargs, dry_run=False)

    def test_dry_run_does_not_ask(self, tmp_path: Path, command: str) -> None:
        result, method, kwargs = _run_delete(tmp_path, command, ["--dry-run"])
        assert result.exit_code == 0, result.output
        assert "Dry run:" in result.stdout
        method.assert_called_once_with(**kwargs, dry_run=True)


class TestBranchDeleteDryRunService:
    def test_dry_run_reads_and_deletes_nothing(self, tmp_config_dir: Path) -> None:
        client = MagicMock()
        client.list_dev_branches.return_value = [
            {"id": 1, "name": "main", "isDefault": True},
            {"id": 5, "name": "feature", "isDefault": False},
        ]
        store = setup_single_project(tmp_config_dir)
        store.set_project_branch(ALIAS, 5)
        service = BranchService(config_store=store, client_factory=lambda url, token: client)

        result = service.delete_branch(alias=ALIAS, branch_id=5, dry_run=True)

        assert result["dry_run"] is True
        assert result["branch_name"] == "feature"
        assert result["was_active"] is True
        client.delete_dev_branch.assert_not_called()
        project = store.get_project(ALIAS)
        assert project is not None
        assert project.active_branch_id == 5  # the active branch is not reset

    def test_dry_run_of_a_missing_branch_is_not_found(self, tmp_config_dir: Path) -> None:
        client = MagicMock()
        client.list_dev_branches.return_value = [{"id": 1, "name": "main", "isDefault": True}]
        store = setup_single_project(tmp_config_dir)
        service = BranchService(config_store=store, client_factory=lambda url, token: client)

        with pytest.raises(KeboolaApiError) as exc:
            service.delete_branch(alias=ALIAS, branch_id=5, dry_run=True)
        assert exc.value.error_code == ErrorCode.NOT_FOUND
        assert "kbagent branch list --project prod" in exc.value.message
        client.delete_dev_branch.assert_not_called()


class TestWorkspaceDeleteDryRunService:
    def test_dry_run_reads_and_deletes_nothing(self, tmp_config_dir: Path) -> None:
        client = MagicMock()
        client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        client.get_workspace.return_value = {
            "component": "keboola.sandboxes",
            "configurationId": "cfg-1",
            "connection": {"user": "u", "password": "do-not-print"},
        }
        store = setup_single_project(tmp_config_dir)
        service = WorkspaceService(config_store=store, client_factory=lambda url, token: client)

        result = service.delete_workspace(alias=ALIAS, workspace_id=42, dry_run=True)

        assert result["dry_run"] is True
        assert result["would_delete_sandbox_config"] == "cfg-1"
        assert "do-not-print" not in json.dumps(result)
        client.get_workspace.assert_called_once_with(42, branch_id=123)
        client.delete_workspace.assert_not_called()
        client.delete_config.assert_not_called()

    def test_dry_run_of_a_missing_workspace_fails(self, tmp_config_dir: Path) -> None:
        client = MagicMock()
        client.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        client.get_workspace.side_effect = KeboolaApiError(
            "Workspace not found", status_code=404, error_code=ErrorCode.NOT_FOUND
        )
        store = setup_single_project(tmp_config_dir)
        service = WorkspaceService(config_store=store, client_factory=lambda url, token: client)

        with pytest.raises(KeboolaApiError):
            service.delete_workspace(alias=ALIAS, workspace_id=42, dry_run=True)
        client.delete_workspace.assert_not_called()


fastapi_missing = importlib.util.find_spec("fastapi") is None


@pytest.mark.skipif(fastapi_missing, reason="FastAPI not installed")
@pytest.mark.parametrize(
    ("path", "service_name", "method", "kwargs"),
    [
        ("/branches/prod/5", "branch", "delete_branch", {"alias": ALIAS, "branch_id": 5}),
        (
            "/workspaces/prod/7",
            "workspace",
            "delete_workspace",
            {"alias": ALIAS, "workspace_id": 7},
        ),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_rest_delete_route_passes_dry_run_and_never_prompts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    service_name: str,
    method: str,
    kwargs: dict[str, Any],
    dry_run: bool,
) -> None:
    from fastapi.testclient import TestClient

    from keboola_agent_cli.server import create_app
    from keboola_agent_cli.server.dependencies import ServiceRegistry, get_registry

    monkeypatch.setattr(typer, "confirm", _no_prompt)
    service = MagicMock()
    getattr(service, method).return_value = {"dry_run": dry_run}
    registry = ServiceRegistry.__new__(ServiceRegistry)
    setattr(registry, service_name, service)
    server = create_app(config_dir=str(tmp_path), auth_token="t")
    server.dependency_overrides[get_registry] = lambda: registry

    query = "?dry_run=true" if dry_run else ""
    response = TestClient(server).delete(f"{path}{query}", headers={"Authorization": "Bearer t"})

    assert response.status_code == 200, response.text
    getattr(service, method).assert_called_once_with(**kwargs, dry_run=dry_run)


# --------------------------------------------------------------------------- #
# Removed forms (C3): 1.0.0 removed the forms that 0.x kept for compatibility
# --------------------------------------------------------------------------- #

REMOVED_CLI_FORMS = [
    (["stream", "delete", "s1", "--force"], "Use --yes"),
    (["semantic-layer", "import", "--file", "f.json", "--yes"], "omit it"),
    (["sl", "import", "--file", "f.json", "-y"], "omit it"),
    (["project", "add", "--project", "new", "--url", "https://x"], "Use --alias"),
    (["project", "add", "-p", "new", "--url", "https://x"], "Use --alias"),
    (["project", "create", "--url", "https://x", "--project", "new"], "Use --alias"),
    (["project", "refresh", "--all"], "Use --all-projects"),
    (["merge-request", "detail", "--id", "7"], "Use --merge-request-id"),
    (["mr", "approve", "--id", "7"], "Use --merge-request-id"),
    (["agent", "show", "--id", "abc"], "Use --task-id"),
    (["branch", "merge", "--branch", "1"], "kbagent merge-request"),
]


@pytest.mark.parametrize(("args", "replacement"), REMOVED_CLI_FORMS)
def test_removed_form_is_a_usage_error_that_names_the_replacement(
    tmp_path: Path, args: list[str], replacement: str
) -> None:
    result = runner.invoke(app, ["--config-dir", str(tmp_path), "--json", *args])

    assert result.exit_code == 2, result.output
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "USAGE_ERROR"
    assert "was removed in 1.0.0" in error["message"]
    assert replacement in error["message"]


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_BOX = re.compile(r"[\u2500-\u257f]")


def test_removed_form_in_human_mode_names_the_replacement(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config-dir", str(tmp_path), "project", "refresh", "--all"])

    assert result.exit_code == 2
    # Without color codes, Rich box lines and line wrapping (CI forces color).
    stderr = " ".join(_BOX.sub(" ", _ANSI.sub("", result.stderr)).split())
    assert "project refresh --all was removed in 1.0.0. Use --all-projects." in stderr


def test_unknown_option_without_a_removed_form_keeps_the_click_message(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["--config-dir", str(tmp_path), "--json", "stream", "delete", "s1", "--nope"]
    )

    assert result.exit_code == 2
    assert "was removed" not in json.loads(result.stdout)["error"]["message"]


def test_deprecated_option_needs_a_registry_entry() -> None:
    with pytest.raises(KeyError):
        deprecated_option("stream delete --nope", "--nope")


def test_deprecated_option_refuses_a_removed_form() -> None:
    with pytest.raises(KeyError):
        deprecated_option("stream delete --force", "--force")


def _leaves(command: Any) -> list[Any]:
    if hasattr(command, "commands"):
        return [leaf for sub in command.commands.values() for leaf in _leaves(sub)]
    return [command]


def test_every_cli_form_in_the_registry_names_a_live_command() -> None:
    """A deprecated form is a hidden option; a removed one is gone from every command it names."""
    import typer.main

    root: Any = typer.main.get_command(app)
    # A REST form ("PUT /path") is checked in tests/test_server_response_meta.py.
    for form, entry in ((f, e) for f, e in DEPRECATIONS.items() if not f.split()[0].isupper()):
        *path, last = form.split()
        command: Any = root
        for name in path:
            command = command.commands[name]
        if not last.startswith("-"):
            assert entry.removed_in and last not in command.commands, form
            continue
        names = {last, *entry.other_names}
        params = [p for leaf in _leaves(command) for p in leaf.params]
        found = [p for p in params if names & set(getattr(p, "opts", []))]
        if entry.removed_in:
            assert found == [], form
        else:
            assert found and all(p.hidden for p in found), form


# --------------------------------------------------------------------------- #
# Preview-then-confirm flows: the preview runs, then the shared helper asks
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("module", "service_attr", "method", "args", "preview"),
    [
        (
            "org",
            "OrgService",
            "setup_organization",
            ["org", "setup", "--org-id", "42", "--url", "https://connection.keboola.com"],
            {
                "projects_added": [{"project_id": 1, "project_name": "P", "alias": "p"}],
                "projects_skipped": [],
                "projects_failed": [],
                "dry_run": True,
            },
        ),
        (
            "project",
            "OrgService",
            "refresh_tokens",
            ["project", "refresh", "--project", ALIAS],
            {
                "projects_refreshed": [{"alias": ALIAS, "project_id": 1, "project_name": "P"}],
                "projects_failed": [],
                "dry_run": True,
            },
        ),
    ],
)
def test_preview_then_confirm_refuses_without_terminal(
    tmp_path: Path,
    module: str,
    service_attr: str,
    method: str,
    args: list[str],
    preview: dict[str, Any],
) -> None:
    service = MagicMock()
    getattr(service, method).return_value = preview
    with patch(
        f"keboola_agent_cli.commands.{module}.resolve_manage_token", return_value="manage-token"
    ):
        result = _invoke(tmp_path, service_attr, service, args, user_input="y\n")
    assert result.exit_code == 2, result.output
    assert "CONFIRMATION_REQUIRED" in result.stderr
    # Only the dry-run preview ran, never the real call.
    assert [c.kwargs["dry_run"] for c in getattr(service, method).call_args_list] == [True]
