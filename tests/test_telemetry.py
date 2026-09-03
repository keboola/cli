"""Targeted tests for per-invocation usage telemetry (``telemetry.py``).

Three properties, one test each: the event is posted with the right shape on
success; a failure is reported as ``type=error`` and a failing events endpoint
never breaks the command (best-effort); and either kill-switch env var suppresses
the event entirely.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from helpers import setup_single_project
from keboola_agent_cli import telemetry
from keboola_agent_cli.auth.models import StackSession
from keboola_agent_cli.auth.sentinel import make_session_token
from keboola_agent_cli.auth.state_store import AuthStateStore
from keboola_agent_cli.auth.token_provider import reset_provider_registry

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


def test_run_entry_point_reports_the_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    """run() re-raises the outcome and reports the exit code / error to telemetry."""
    from keboola_agent_cli import cli

    seen: list[tuple[int, str | None]] = []
    monkeypatch.setattr(
        cli.telemetry,
        "emit_cli_invocation",
        lambda argv, exit_code, error, duration_s: seen.append(
            (exit_code, type(error).__name__ if error is not None else None)
        ),
    )

    def app_raising(exc: BaseException):
        def _app() -> None:
            raise exc

        return _app

    monkeypatch.setattr(cli, "app", app_raising(SystemExit(0)))  # success
    with pytest.raises(SystemExit):
        cli.run()
    monkeypatch.setattr(cli, "app", app_raising(SystemExit(5)))  # mapped error -> typer.Exit(5)
    with pytest.raises(SystemExit):
        cli.run()
    monkeypatch.setattr(cli, "app", app_raising(RuntimeError("boom")))  # unmapped exception
    with pytest.raises(RuntimeError):
        cli.run()

    assert seen == [(0, None), (5, None), (1, "RuntimeError")]


def _seed_fresh_session(config_dir: Path, *, access_token: str) -> None:
    """Persist a session in auth.json whose access token is well inside the margin."""
    now = datetime.now(UTC)
    AuthStateStore(config_dir).put_session(
        StackSession(
            stack_url="https://connection.keboola.com",
            session_id="s1",
            access_token=access_token,
            refresh_token="kbc_rt_x",
            access_expires_at=now + timedelta(hours=1),
            refresh_expires_at=now + timedelta(days=30),
            created_at=now,
        )
    )


def test_session_project_with_a_fresh_token_posts_a_bearer_event(
    tmp_config_dir: Path, httpx_mock
) -> None:
    """A session project is covered too: it posts a Bearer event using the current token.

    The event carries the session's on-disk access token as ``Authorization: Bearer``
    plus ``X-KBC-ProjectId`` -- never an ``X-StorageApi-Token`` -- and telemetry reads
    that token WITHOUT a refresh (the fresh session is adopted, no network call).
    """
    reset_provider_registry()
    telemetry.reset()
    setup_single_project(tmp_config_dir, token=make_session_token(258))
    _seed_fresh_session(tmp_config_dir, access_token="kbc_at_fresh")
    httpx_mock.add_response(url=_EVENTS_URL, method="POST", json={"id": "evt-1"})

    telemetry.emit_cli_invocation(
        ["kbagent", "config", "list", "--config-dir", str(tmp_config_dir)],
        exit_code=0,
        error=None,
        duration_s=0.4,
    )

    request = httpx_mock.get_requests()[0]
    assert request.headers["Authorization"] == "Bearer kbc_at_fresh"
    assert request.headers["X-KBC-ProjectId"] == "258"
    assert "X-StorageApi-Token" not in request.headers
    reset_provider_registry()


def test_session_project_without_a_usable_token_posts_nothing(
    tmp_config_dir: Path, httpx_mock
) -> None:
    """A session project with no fresh on-disk token skips the event, never refreshing.

    Posting would need a token refresh (a network call plus a cross-process lease wait)
    that runs outside the event's short timeout, so telemetry skips this one event
    instead of driving the refresh. httpx_mock records no request.
    """
    reset_provider_registry()
    telemetry.reset()
    setup_single_project(tmp_config_dir, token=make_session_token(258))
    # No auth.json session written -> peek finds nothing usable -> skip, no refresh.

    telemetry.emit_cli_invocation(
        ["kbagent", "config", "list", "--config-dir", str(tmp_config_dir)],
        exit_code=0,
        error=None,
        duration_s=0.4,
    )

    assert httpx_mock.get_requests() == []
    reset_provider_registry()


def test_auth_status_exit_3_is_recorded_as_expected_not_a_failure(
    tmp_config_dir: Path, httpx_mock
) -> None:
    """``auth status`` exit 3 (signed out) posts ``type=info``, not ``error``.

    Exit 3 is a documented normal outcome an agent polls for, so a telemetry consumer
    must not count it as a failed command.
    """
    telemetry.reset()
    setup_single_project(tmp_config_dir)
    httpx_mock.add_response(url=_EVENTS_URL, method="POST", json={"id": "evt-1"})

    telemetry.emit_cli_invocation(
        ["kbagent", "auth", "status", "--config-dir", str(tmp_config_dir)],
        exit_code=3,
        error=None,
        duration_s=0.2,
    )

    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["params"]["command"] == "auth status"
    assert body["type"] == "info"
    assert "error" not in body.get("results", {})
