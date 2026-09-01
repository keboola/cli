"""Targeted tests for per-invocation usage telemetry (``telemetry.py``).

Three properties, one test each: the event is posted with the right shape on
success; a failure is reported as ``type=error`` and a failing events endpoint
never breaks the command (best-effort); and either kill-switch env var suppresses
the event entirely.
"""

import json
from pathlib import Path

import pytest

from helpers import setup_single_project
from keboola_agent_cli import telemetry

_EVENTS_URL = "https://connection.keboola.com/v2/storage/events"


def test_success_posts_expected_cli_event(tmp_config_dir: Path, httpx_mock) -> None:
    """A successful command posts one ``ext.keboola.cli.`` event with the command path."""
    telemetry.reset()
    setup_single_project(tmp_config_dir)  # sole project -> resolves without --project
    httpx_mock.add_response(url=_EVENTS_URL, method="POST", json={"id": "evt-1"})

    telemetry.emit_cli_invocation(
        ["kbagent", "config", "list", "--config-dir", str(tmp_config_dir)],
        exit_code=0,
        error=None,
        duration_s=0.4,
    )

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["component"] == "keboola.cli"
    assert "configurationId" not in body  # CLI -> ext.keboola.cli. (serve adds "serve")
    assert body["type"] == "info"
    assert body["params"] == {"command": "config list"}
    assert body["results"]["projectId"] == 258
    assert body["duration"] == 1  # ceil(0.4s)
    # The acting client identifies kbagent to Connection by its User-Agent, which
    # is what makes the server-side audit event attributable to kbagent too.
    assert requests[0].headers["User-Agent"].startswith(("keboola-cli/", "keboola-agent-cli/"))


def test_failed_command_reports_error_and_is_best_effort(tmp_config_dir: Path, httpx_mock) -> None:
    """A failed command posts ``type=error`` + message; a failing endpoint never raises."""
    telemetry.reset()
    setup_single_project(tmp_config_dir)
    telemetry.note_command_error("bucket in.c-foo not found")
    # The events endpoint itself fails (500) -- the firewall/blocked case must be swallowed.
    httpx_mock.add_response(url=_EVENTS_URL, method="POST", status_code=500, json={"error": "x"})

    # No exception may escape: telemetry is best-effort.
    telemetry.emit_cli_invocation(
        ["kbagent", "storage", "buckets", "--config-dir", str(tmp_config_dir)],
        exit_code=1,
        error=None,
        duration_s=0.1,
    )

    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["type"] == "error"
    assert body["params"]["command"] == "storage buckets"
    assert body["results"]["error"] == "bucket in.c-foo not found"


@pytest.mark.parametrize("env_var", ["KBAGENT_DISABLE_TELEMETRY", "DO_NOT_TRACK"])
def test_opt_out_env_suppresses_event(
    tmp_config_dir: Path, httpx_mock, monkeypatch: pytest.MonkeyPatch, env_var: str
) -> None:
    """Either kill-switch env var stops any event from being posted."""
    telemetry.reset()
    setup_single_project(tmp_config_dir)
    monkeypatch.setenv(env_var, "1")

    telemetry.emit_cli_invocation(
        ["kbagent", "config", "list", "--config-dir", str(tmp_config_dir)],
        exit_code=0,
        error=None,
        duration_s=0.4,
    )

    assert httpx_mock.get_requests() == []


def test_repl_line_is_logged_like_a_direct_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """A command typed in the REPL emits a usage event, exactly like a direct call."""
    from keboola_agent_cli.commands import repl as repl_module

    class _FakeClickApp:
        def __call__(self, argv: list[str], standalone_mode: bool = False) -> None:
            return None  # a successful command returns None under standalone_mode=False

    monkeypatch.setattr(repl_module.typer.main, "get_command", lambda _app: _FakeClickApp())

    class _FakeSession:
        def __init__(self, **_kwargs: object) -> None:
            self._replies = iter(["storage buckets", ""])

        def prompt(self, _text: object) -> str:
            try:
                return next(self._replies)
            except StopIteration as exc:
                raise EOFError from exc

    monkeypatch.setattr(repl_module, "PromptSession", _FakeSession)

    calls: list[tuple[list[str], int, BaseException | None]] = []
    monkeypatch.setattr(
        repl_module.telemetry,
        "emit_cli_invocation",
        lambda argv, exit_code, error, duration_s: calls.append((argv, exit_code, error)),
    )

    repl_module._run_repl(json_mode=False, verbose=False, no_color=True, config_dir=None)

    assert len(calls) == 1  # the one typed command, not the blank line
    argv, exit_code, error = calls[0]
    # The REPL forwards the typed command to emit with a program-name prefix, so
    # command_from_argv parses it the same as a direct invocation. (Asserted on
    # argv directly because this test monkeypatches typer.main.get_command.)
    assert argv[0] == "kbagent"
    assert argv[-2:] == ["storage", "buckets"]
    assert exit_code == 0
    assert error is None
