"""End-to-end tests for ``data-app password`` (CLI-23).

The real CLI, service and HTTP clients run against pytest-httpx; only the
clipboard tool, the terminal and the browser are faked, so no test touches the
real clipboard, opens a browser or reaches a Keboola API. Three concerns:

1. Delivery: the terminal prompt (``c`` / Enter / timeout / no clipboard, and
   on a real pty), ``--copy`` without a terminal, ``--reveal``, the
   ``create --dry-run`` plan.
2. No leak: on every path except ``--reveal`` the password is absent from
   stdout, stderr, the DEBUG log (``--verbose``) and the telemetry event --
   for ``data-app password`` and for ``create`` / ``deploy --wait``.
3. Auth: the requests carry only the project token -- ``X-StorageApi-Token``,
   or ``Authorization: Bearer`` + ``X-KBC-ProjectId`` for a browser-login
   session -- and never ``X-KBC-ManageApiToken``.
"""

from __future__ import annotations

import json
import logging
import os
import select
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner, Result

from helpers import setup_single_project
from keboola_agent_cli import telemetry
from keboola_agent_cli.auth import environment
from keboola_agent_cli.auth.models import StackSession
from keboola_agent_cli.auth.sentinel import make_session_token
from keboola_agent_cli.auth.state_store import AuthStateStore
from keboola_agent_cli.auth.token_provider import reset_provider_registry
from keboola_agent_cli.cli import app
from keboola_agent_cli.commands import _url_copy
from keboola_agent_cli.commands._data_app_password import (
    PasswordFlags,
    password_after_deploy,
)
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.output import OutputFormatter
from keboola_agent_cli.services._data_app_bodies import (
    _build_public_auth_block,
    _build_simple_auth_block,
)
from keboola_agent_cli.services.data_app_service import DataAppService

if sys.platform != "win32":
    import pty

runner = CliRunner()

SENTINEL = "pw-sentinel-0f3c9a2b7e"
STATIC_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
DS_URL = "https://data-science.keboola.com"
CONFIG_URL = "https://connection.keboola.com/v2/storage/components/keboola.data-apps/configs/cfg-1"
APP_URL = "https://app-42.hub.keboola.com"
UI_URL = "https://connection.keboola.com/admin/projects/258/branch/default/data-apps/cfg-1"


class _Clipboard:
    """Fake copier returned by the patched ``detect_clipboard``."""

    def __init__(self, *, works: bool = True) -> None:
        self.copied: list[str] = []
        self._works = works

    def __call__(self, text: str) -> bool:
        self.copied.append(text)
        return self._works


def _mock_api(
    httpx_mock: Any,
    *,
    authorization: dict[str, Any] | None = None,
    password: str | None = SENTINEL,
    password_call: bool = True,
) -> None:
    """The three calls: app record, its Storage config, the password."""
    httpx_mock.add_response(
        method="GET",
        url=f"{DS_URL}/apps/42",
        json={"id": 42, "configId": "cfg-1", "branchId": None, "url": APP_URL},
    )
    block = _build_simple_auth_block() if authorization is None else authorization
    httpx_mock.add_response(
        method="GET",
        url=CONFIG_URL,
        json={"id": "cfg-1", "configuration": {"authorization": block}},
    )
    if password_call:
        httpx_mock.add_response(
            method="GET", url=f"{DS_URL}/apps/42/password", json={"password": password}
        )


@pytest.fixture
def config_dir(tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A static-token project, no clipboard tool, no browser, telemetry on."""
    setup_single_project(tmp_config_dir, token=STATIC_TOKEN)
    for var in ("KBAGENT_DISABLE_TELEMETRY", "DO_NOT_TRACK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(_url_copy, "detect_clipboard", lambda: None)
    monkeypatch.setattr(_url_copy, "stdio_is_interactive", lambda: False)

    def _no_browser(url: str, *, wait_seconds: float = 0.0) -> bool:
        raise AssertionError("a test without --open must not open a browser")

    monkeypatch.setattr(environment, "open_browser", _no_browser)
    return tmp_config_dir


def _clipboard(monkeypatch: pytest.MonkeyPatch, *, works: bool = True) -> _Clipboard:
    clipboard = _Clipboard(works=works)
    monkeypatch.setattr(_url_copy, "detect_clipboard", lambda: clipboard)
    return clipboard


def _terminal(monkeypatch: pytest.MonkeyPatch, keys: list[str | None]) -> None:
    """A fake terminal: interactive stdio and a key reader fed from ``keys``."""
    feed: Iterator[str | None] = iter(keys)
    monkeypatch.setattr(_url_copy, "stdio_is_interactive", lambda: True)
    monkeypatch.setattr(_url_copy.CopyableUrlWait, "_enter_cbreak", lambda self: None)
    monkeypatch.setattr(
        _url_copy.CopyableUrlWait, "_read_key", lambda self, timeout: next(feed, None)
    )


def _argv(config_dir: Path, *extra: str, json_mode: bool) -> list[str]:
    head = ["--verbose", "--config-dir", str(config_dir)]
    if json_mode:
        head.append("--json")
    return [*head, "data-app", "password", "--project", "prod", "--app-id", "42", *extra]


def _run(argv: list[str], caplog: pytest.LogCaptureFixture) -> Result:
    telemetry.reset()
    caplog.set_level(logging.DEBUG)
    return runner.invoke(app, argv)


def _telemetry_payload(argv: list[str], result: Result, monkeypatch: pytest.MonkeyPatch) -> str:
    """Build the usage event this invocation would post; return it as text."""
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        telemetry, "_send_event", lambda _config_store, **kwargs: sent.append(kwargs)
    )
    telemetry.emit_cli_invocation(["kbagent", *argv], result.exit_code, None, 0.1)
    assert len(sent) == 1, "the usage event was not built -- the check would prove nothing"
    return repr(sent)


def _assert_no_leak(
    argv: list[str],
    result: Result,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert SENTINEL not in result.stdout
    assert SENTINEL not in result.stderr
    assert SENTINEL not in caplog.text
    assert SENTINEL not in _telemetry_payload(argv, result, monkeypatch)


# ---------------------------------------------------------------------------
# Without a terminal (an AI agent, CI) and in --json mode
# ---------------------------------------------------------------------------


class TestWithoutTerminal:
    @pytest.mark.parametrize("json_mode", [True, False])
    def test_default_copies_nothing_and_points_to_the_ui(
        self, config_dir, httpx_mock, caplog, monkeypatch, json_mode: bool
    ) -> None:
        _mock_api(httpx_mock)
        clipboard = _clipboard(monkeypatch)
        argv = _argv(config_dir, json_mode=json_mode)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        assert clipboard.copied == []
        assert "--copy" in result.stdout
        assert UI_URL in result.stdout
        _assert_no_leak(argv, result, caplog, monkeypatch)

    def test_json_envelope(self, config_dir, httpx_mock, caplog, monkeypatch) -> None:
        _mock_api(httpx_mock)
        result = _run(_argv(config_dir, json_mode=True), caplog)

        data = json.loads(result.stdout)["data"]
        assert data == {
            "project_alias": "prod",
            "app_id": "42",
            "auth": "password",
            "app_url": APP_URL,
            "ui_url": UI_URL,
            "password_delivered_to": None,
            "app_opened": False,
            "message": data["message"],
        }

    @pytest.mark.parametrize("json_mode", [True, False])
    def test_copy_puts_the_password_on_the_clipboard(
        self, config_dir, httpx_mock, caplog, monkeypatch, json_mode: bool
    ) -> None:
        _mock_api(httpx_mock)
        clipboard = _clipboard(monkeypatch)
        argv = _argv(config_dir, "--copy", json_mode=json_mode)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        assert clipboard.copied == [SENTINEL]
        assert "on the clipboard" in result.stdout
        if json_mode:
            assert '"password_delivered_to": "clipboard"' in result.stdout
        _assert_no_leak(argv, result, caplog, monkeypatch)

    @pytest.mark.parametrize("json_mode", [True, False])
    @pytest.mark.parametrize("tool", ["fails", "missing"])
    def test_copy_failure_exits_0_and_points_to_the_ui(
        self, config_dir, httpx_mock, caplog, monkeypatch, json_mode: bool, tool: str
    ) -> None:
        _mock_api(httpx_mock)
        if tool == "fails":
            _clipboard(monkeypatch, works=False)
        argv = _argv(config_dir, "--copy", json_mode=json_mode)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        assert "was not copied" in result.stdout
        assert UI_URL in result.stdout
        if json_mode:
            assert '"password_delivered_to": null' in result.stdout
        _assert_no_leak(argv, result, caplog, monkeypatch)

    def test_json_in_a_terminal_never_prompts(
        self, config_dir, httpx_mock, caplog, monkeypatch
    ) -> None:
        _mock_api(httpx_mock)
        clipboard = _clipboard(monkeypatch)
        _terminal(monkeypatch, ["c"])
        argv = _argv(config_dir, json_mode=True)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        assert clipboard.copied == []
        assert "Press c" not in result.output
        assert '"password_delivered_to": null' in result.stdout
        _assert_no_leak(argv, result, caplog, monkeypatch)


# ---------------------------------------------------------------------------
# In a terminal: press c to copy
# ---------------------------------------------------------------------------


class TestTerminalPrompt:
    def test_c_copies_once_then_enter_finishes(
        self, config_dir, httpx_mock, caplog, monkeypatch
    ) -> None:
        _mock_api(httpx_mock)
        clipboard = _clipboard(monkeypatch)
        _terminal(monkeypatch, ["c", "c", "\n"])
        argv = _argv(config_dir, json_mode=False)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        assert clipboard.copied == [SENTINEL]
        assert "Press c to copy the password, Enter to finish" in result.stdout
        assert "Copied to clipboard" in result.stdout
        assert APP_URL in result.stdout
        assert UI_URL in result.stdout
        assert "not copied" not in result.stdout
        _assert_no_leak(argv, result, caplog, monkeypatch)

    @pytest.mark.parametrize("key", ["\n", "\r", "\x1b", "q"])
    def test_finish_key_ends_without_copying(
        self, config_dir, httpx_mock, caplog, monkeypatch, key: str
    ) -> None:
        _mock_api(httpx_mock)
        clipboard = _clipboard(monkeypatch)
        _terminal(monkeypatch, [key])
        argv = _argv(config_dir, json_mode=False)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        assert clipboard.copied == []
        assert "No key pressed" not in result.stdout
        _assert_no_leak(argv, result, caplog, monkeypatch)

    def test_timeout_says_the_password_was_not_copied(
        self, config_dir, httpx_mock, caplog, monkeypatch
    ) -> None:
        _mock_api(httpx_mock)
        clipboard = _clipboard(monkeypatch)
        _terminal(monkeypatch, [None])  # _read_key timed out
        argv = _argv(config_dir, json_mode=False)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        assert clipboard.copied == []
        assert "No key pressed for 120 s. The password was not copied." in result.stdout
        _assert_no_leak(argv, result, caplog, monkeypatch)

    def test_failed_copy_says_so_and_points_to_the_ui(
        self, config_dir, httpx_mock, caplog, monkeypatch
    ) -> None:
        _mock_api(httpx_mock)
        clipboard = _clipboard(monkeypatch, works=False)
        _terminal(monkeypatch, ["c", "\n"])
        argv = _argv(config_dir, json_mode=False)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        assert clipboard.copied == [SENTINEL]
        assert "Copied to clipboard" not in result.stdout
        assert "The password was not copied." in result.stdout
        assert UI_URL in result.stdout
        _assert_no_leak(argv, result, caplog, monkeypatch)

    def test_no_clipboard_skips_the_prompt_and_points_to_the_ui(
        self, config_dir, httpx_mock, caplog, monkeypatch
    ) -> None:
        _mock_api(httpx_mock)
        _terminal(monkeypatch, ["c"])
        argv = _argv(config_dir, json_mode=False)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        assert "Press c" not in result.stdout
        assert "cannot be copied" in result.stdout
        assert UI_URL in result.stdout
        _assert_no_leak(argv, result, caplog, monkeypatch)

    def test_copy_flag_in_a_terminal_copies_without_the_prompt(
        self, config_dir, httpx_mock, caplog, monkeypatch
    ) -> None:
        _mock_api(httpx_mock)
        clipboard = _clipboard(monkeypatch)
        _terminal(monkeypatch, [])
        argv = _argv(config_dir, "--copy", json_mode=False)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        assert clipboard.copied == [SENTINEL]
        assert "Press c" not in result.stdout
        _assert_no_leak(argv, result, caplog, monkeypatch)


# ---------------------------------------------------------------------------
# Errors, --reveal
# ---------------------------------------------------------------------------


class TestErrorsAndReveal:
    @pytest.mark.parametrize("json_mode", [True, False])
    def test_non_password_app_fails_before_the_password_call(
        self, config_dir, httpx_mock, caplog, monkeypatch, json_mode: bool
    ) -> None:
        _mock_api(httpx_mock, authorization=_build_public_auth_block(), password_call=False)
        argv = _argv(config_dir, json_mode=json_mode)

        result = _run(argv, caplog)

        assert result.exit_code == 1
        if json_mode:
            assert json.loads(result.stdout)["error"]["code"] == "VALIDATION_ERROR"
        assert "auth: public" in result.output
        assert all(not r.url.path.endswith("/password") for r in httpx_mock.get_requests())
        _assert_no_leak(argv, result, caplog, monkeypatch)

    def test_no_password_yet_fails(self, config_dir, httpx_mock, caplog, monkeypatch) -> None:
        _mock_api(httpx_mock, password=None)
        argv = _argv(config_dir, json_mode=True)

        result = _run(argv, caplog)

        assert result.exit_code == 1
        assert '"code": "NOT_FOUND"' in result.stdout
        assert "no password yet" in result.stdout

    @pytest.mark.parametrize("json_mode", [True, False])
    def test_reveal_prints_the_password_but_not_into_logs_or_telemetry(
        self, config_dir, httpx_mock, caplog, monkeypatch, json_mode: bool
    ) -> None:
        _mock_api(httpx_mock)
        clipboard = _clipboard(monkeypatch)
        argv = _argv(config_dir, "--reveal", json_mode=json_mode)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        assert SENTINEL in result.stdout
        assert clipboard.copied == []
        if json_mode:
            assert '"password_delivered_to": "stdout"' in result.stdout
        assert SENTINEL not in result.stderr
        assert SENTINEL not in caplog.text
        assert SENTINEL not in _telemetry_payload(argv, result, monkeypatch)


# ---------------------------------------------------------------------------
# Auth headers: project token only
# ---------------------------------------------------------------------------


def _seed_fresh_session(config_dir: Path, *, access_token: str) -> None:
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


class TestAuthHeaders:
    def test_static_token_sends_storage_token_and_no_manage_token(
        self, tmp_config_dir: Path, httpx_mock, monkeypatch
    ) -> None:
        # A Manage token in env must not be picked up any more.
        monkeypatch.setenv("KBC_MANAGE_API_TOKEN", "manage-token-must-not-be-sent")
        store = setup_single_project(tmp_config_dir, token=STATIC_TOKEN)
        _mock_api(httpx_mock)

        result = DataAppService(config_store=store).get_data_app_password("prod", "42")

        assert result.password == SENTINEL
        requests = httpx_mock.get_requests()
        assert [r.url.path for r in requests] == [
            "/apps/42",
            "/v2/storage/components/keboola.data-apps/configs/cfg-1",
            "/apps/42/password",
        ]
        for request in requests:
            assert request.headers["X-StorageApi-Token"] == STATIC_TOKEN
            assert "X-KBC-ManageApiToken" not in request.headers
            assert "Authorization" not in request.headers

    def test_session_project_sends_bearer_and_project_id(
        self, tmp_config_dir: Path, httpx_mock
    ) -> None:
        reset_provider_registry()
        try:
            setup_single_project(tmp_config_dir, token=make_session_token(258))
            _seed_fresh_session(tmp_config_dir, access_token="kbc_at_fresh")
            _mock_api(httpx_mock)

            service = DataAppService(config_store=ConfigStore(config_dir=tmp_config_dir))
            result = service.get_data_app_password("prod", "42")
        finally:
            reset_provider_registry()

        assert result.password == SENTINEL
        assert result.ui_url == UI_URL
        requests = httpx_mock.get_requests()
        assert len(requests) == 3
        for request in requests:
            assert request.headers["Authorization"] == "Bearer kbc_at_fresh"
            assert request.headers["X-KBC-ProjectId"] == "258"
            assert "X-StorageApi-Token" not in request.headers
            assert "X-KBC-ManageApiToken" not in request.headers


# ---------------------------------------------------------------------------
# After `deploy --wait` / `create --wait`
# ---------------------------------------------------------------------------

_RUNNING_APP = {
    "id": 42,
    "configId": "cfg-1",
    "branchId": None,
    "url": APP_URL,
    "state": "running",
    "desiredState": "running",
    "configVersion": "5",
}


def _mock_deployed_app(
    httpx_mock: Any,
    *,
    authorization: dict[str, Any] | None = None,
    password: str | None = SENTINEL,
    password_call: bool = True,
) -> None:
    """GET app + GET config (reused by the deploy and by the password read), then
    the password. Only the deploy PATCH / create POST + PUT are added per test."""
    httpx_mock.add_response(
        method="GET", url=f"{DS_URL}/apps/42", json=_RUNNING_APP, is_reusable=True
    )
    block = _build_simple_auth_block() if authorization is None else authorization
    configuration = {
        "authorization": block,
        "parameters": {"dataApp": {"git": {"repository": "https://github.com/o/r"}}},
    }
    httpx_mock.add_response(
        method="GET",
        url=CONFIG_URL,
        json={"id": "cfg-1", "version": "5", "configuration": configuration},
        is_reusable=True,
    )
    if password_call:
        httpx_mock.add_response(
            method="GET", url=f"{DS_URL}/apps/42/password", json={"password": password}
        )


def _mock_deploy_patch(httpx_mock: Any) -> None:
    httpx_mock.add_response(
        method="PATCH", url=f"{DS_URL}/apps/42", json={**_RUNNING_APP, "state": "starting"}
    )


def _deploy_argv(config_dir: Path, *extra: str, json_mode: bool) -> list[str]:
    head = ["--verbose", "--config-dir", str(config_dir)]
    if json_mode:
        head.append("--json")
    return [*head, "data-app", "deploy", "--project", "prod", "--app-id", "42", *extra]


def _create_argv(config_dir: Path, *extra: str, json_mode: bool) -> list[str]:
    head = ["--verbose", "--config-dir", str(config_dir)]
    if json_mode:
        head.append("--json")
    return [
        *head,
        "data-app",
        "create",
        "--project",
        "prod",
        "--name",
        "App",
        "--slug",
        "my-app",
        "--git-repo",
        "https://github.com/o/r",
        "--git-public",
        *extra,
    ]


def _mock_create(httpx_mock: Any) -> None:
    httpx_mock.add_response(
        method="POST", url=f"{DS_URL}/apps", json={"id": 42, "configId": "cfg-1", "url": APP_URL}
    )
    httpx_mock.add_response(method="PUT", url=CONFIG_URL, json={"id": "cfg-1", "version": "2"})
    _mock_deploy_patch(httpx_mock)


class TestAfterDeploy:
    def test_deploy_wait_in_a_terminal_prompts_and_copies(
        self, config_dir, httpx_mock, caplog, monkeypatch
    ) -> None:
        _mock_deployed_app(httpx_mock)
        _mock_deploy_patch(httpx_mock)
        clipboard = _clipboard(monkeypatch)
        _terminal(monkeypatch, ["c", "\n"])
        argv = _deploy_argv(config_dir, "--wait", json_mode=False)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        assert clipboard.copied == [SENTINEL]
        assert "deploy requested" in result.stdout  # the deploy result comes first
        assert "Press c to copy the password, Enter to finish" in result.stdout
        assert "Copied to clipboard" in result.stdout
        _assert_no_leak(argv, result, caplog, monkeypatch)

    def test_create_wait_in_a_terminal_prompts_and_copies(
        self, config_dir, httpx_mock, caplog, monkeypatch
    ) -> None:
        _mock_create(httpx_mock)
        _mock_deployed_app(httpx_mock)
        clipboard = _clipboard(monkeypatch)
        _terminal(monkeypatch, ["c", "\n"])
        argv = _create_argv(config_dir, "--wait", json_mode=False)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        assert clipboard.copied == [SENTINEL]
        assert "is running" in result.stdout
        assert "Press c to copy the password, Enter to finish" in result.stdout
        _assert_no_leak(argv, result, caplog, monkeypatch)

    @pytest.mark.parametrize("json_mode", [True, False])
    @pytest.mark.parametrize("command", ["deploy", "create"])
    def test_no_flag_and_no_prompt_reads_nothing(
        self, config_dir, httpx_mock, caplog, monkeypatch, command: str, json_mode: bool
    ) -> None:
        """--json (even on a terminal) or no terminal, no flag: output as before, no extra call."""
        if command == "create":
            _mock_create(httpx_mock)
        else:
            _mock_deploy_patch(httpx_mock)
            httpx_mock.add_response(
                method="GET",
                url=CONFIG_URL,
                json={"id": "cfg-1", "version": "5", "configuration": {}},
            )
        httpx_mock.add_response(
            method="GET", url=f"{DS_URL}/apps/42", json=_RUNNING_APP, is_reusable=True
        )
        clipboard = _clipboard(monkeypatch)
        if json_mode:
            _terminal(monkeypatch, ["c"])  # a terminal, but --json never prompts
        build = _create_argv if command == "create" else _deploy_argv
        argv = build(config_dir, "--wait", json_mode=json_mode)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        assert all(not r.url.path.endswith("/password") for r in httpx_mock.get_requests())
        assert clipboard.copied == []
        assert "Press c" not in result.output
        assert "--copy" not in result.stdout
        if json_mode:
            data = json.loads(result.stdout)["data"]
            assert data["state"] == "running"
            for key in ("ui_url", "password_delivered_to", "password", "warnings"):
                assert key not in data

    @pytest.mark.parametrize("command", ["deploy", "create"])
    def test_copy_after_wait(self, config_dir, httpx_mock, caplog, monkeypatch, command) -> None:
        if command == "create":
            _mock_create(httpx_mock)
        else:
            _mock_deploy_patch(httpx_mock)
        _mock_deployed_app(httpx_mock)
        clipboard = _clipboard(monkeypatch)
        build = _create_argv if command == "create" else _deploy_argv
        argv = build(config_dir, "--wait", "--copy", json_mode=True)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        assert clipboard.copied == [SENTINEL]
        assert json.loads(result.stdout)["data"]["password_delivered_to"] == "clipboard"
        _assert_no_leak(argv, result, caplog, monkeypatch)

    @pytest.mark.parametrize("command", ["deploy", "create"])
    def test_reveal_after_wait(self, config_dir, httpx_mock, caplog, monkeypatch, command) -> None:
        if command == "create":
            _mock_create(httpx_mock)
        else:
            _mock_deploy_patch(httpx_mock)
        _mock_deployed_app(httpx_mock)
        build = _create_argv if command == "create" else _deploy_argv
        argv = build(config_dir, "--wait", "--reveal", json_mode=True)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)["data"]
        assert data["password"] == SENTINEL
        assert data["password_delivered_to"] == "stdout"
        assert SENTINEL not in result.stderr
        assert SENTINEL not in caplog.text
        assert SENTINEL not in _telemetry_payload(argv, result, monkeypatch)

    @pytest.mark.parametrize(
        "argv_tail",
        [
            ("deploy", "--copy"),
            ("deploy", "--reveal"),
            ("deploy", "--wait", "--copy", "--reveal"),
            ("create", "--copy"),
            ("create", "--wait", "--no-deploy", "--copy"),
            ("create", "--dry-run", "--copy"),
            ("create", "--dry-run", "--wait", "--no-deploy", "--reveal"),
            ("create", "--dry-run", "--wait", "--copy", "--reveal"),
        ],
    )
    def test_flags_that_cannot_work_exit_2_before_any_http_call(
        self, config_dir, httpx_mock, caplog, argv_tail: tuple[str, ...]
    ) -> None:
        command, *extra = argv_tail
        build = _create_argv if command == "create" else _deploy_argv
        result = _run(build(config_dir, *extra, json_mode=True), caplog)

        assert result.exit_code == 2
        error = json.loads(result.stdout)["error"]
        assert error["code"] == "INVALID_ARGUMENT"
        if "--reveal" not in extra or "--copy" not in extra:
            assert "creates the password during the deploy" in error["message"]
        assert httpx_mock.get_requests() == []

    def test_non_password_app_with_copy_warns_and_exits_0(
        self, config_dir, httpx_mock, caplog, monkeypatch
    ) -> None:
        _mock_deployed_app(
            httpx_mock, authorization=_build_public_auth_block(), password_call=False
        )
        _mock_deploy_patch(httpx_mock)
        clipboard = _clipboard(monkeypatch)
        argv = _deploy_argv(config_dir, "--wait", "--copy", json_mode=True)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)["data"]
        assert any("auth: public" in w for w in data["warnings"])
        assert "ui_url" not in data
        assert clipboard.copied == []

    def test_non_password_app_without_flags_stays_silent(
        self, config_dir, httpx_mock, caplog, monkeypatch
    ) -> None:
        _mock_deployed_app(
            httpx_mock, authorization=_build_public_auth_block(), password_call=False
        )
        _mock_deploy_patch(httpx_mock)
        argv = _deploy_argv(config_dir, "--wait", json_mode=True)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)["data"]
        assert "warnings" not in data
        assert "ui_url" not in data

    def test_create_with_public_auth_and_copy_warns_without_extra_calls(
        self, config_dir, httpx_mock, caplog, monkeypatch
    ) -> None:
        _mock_create(httpx_mock)
        httpx_mock.add_response(
            method="GET", url=f"{DS_URL}/apps/42", json=_RUNNING_APP, is_reusable=True
        )
        argv = _create_argv(config_dir, "--auth", "public", "--wait", "--copy", json_mode=True)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)["data"]
        assert any("uses auth 'public'" in w for w in data["warnings"])
        assert all(
            r.method != "GET" or "/configs/" not in r.url.path for r in httpx_mock.get_requests()
        )

    @pytest.mark.parametrize("json_mode", [True, False])
    def test_password_read_failure_after_deploy_warns_and_exits_0(
        self, config_dir, httpx_mock, caplog, monkeypatch, json_mode: bool
    ) -> None:
        _mock_deployed_app(httpx_mock, password=None)
        _mock_deploy_patch(httpx_mock)
        _clipboard(monkeypatch)
        argv = _deploy_argv(config_dir, "--wait", "--copy", json_mode=json_mode)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        if json_mode:
            data = json.loads(result.stdout)["data"]
            assert any("is not ready yet" in w for w in data["warnings"])
            assert "password_delivered_to" not in data
        else:
            assert "is not ready yet" in result.stderr
        _assert_no_leak(argv, result, caplog, monkeypatch)

    def test_managed_repo_with_copy_exits_2_before_any_http_call(
        self, config_dir, httpx_mock, caplog
    ) -> None:
        argv = [
            "--config-dir",
            str(config_dir),
            "--json",
            "data-app",
            "create",
            "--project",
            "prod",
            "--name",
            "App",
            "--slug",
            "my-app",
            "--use-managed-git-repo",
            "--wait",
            "--copy",
        ]
        result = _run(argv, caplog)

        assert result.exit_code == 2
        assert "--use-managed-git-repo" in json.loads(result.stdout)["error"]["message"]
        assert httpx_mock.get_requests() == []


# ---------------------------------------------------------------------------
# `create --dry-run`: same flag rules, a plan, no call, no prompt, no copy
# ---------------------------------------------------------------------------


class TestDryRun:
    @pytest.mark.parametrize(("flag", "plan"), [("--copy", "clipboard"), ("--reveal", "stdout")])
    def test_valid_flags_add_the_plan(
        self, config_dir, httpx_mock, caplog, monkeypatch, flag: str, plan: str
    ) -> None:
        clipboard = _clipboard(monkeypatch)
        argv = _create_argv(config_dir, "--dry-run", "--wait", flag, json_mode=True)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)["data"]
        assert data["dry_run"] is True
        assert data["password_delivery"] == plan
        assert "warnings" not in data
        assert clipboard.copied == []
        assert httpx_mock.get_requests() == []

    def test_copy_without_a_clipboard_tool_warns(self, config_dir, httpx_mock, caplog) -> None:
        argv = _create_argv(config_dir, "--dry-run", "--wait", "--copy", json_mode=True)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)["data"]
        assert data["password_delivery"] == "clipboard"
        assert data["warnings"] == [
            "No clipboard tool was found, so the password would not be copied."
        ]

    def test_public_auth_with_copy_warns_there_is_nothing_to_copy(
        self, config_dir, httpx_mock, caplog, monkeypatch
    ) -> None:
        _clipboard(monkeypatch)
        argv = _create_argv(
            config_dir, "--dry-run", "--wait", "--auth", "public", "--copy", json_mode=True
        )

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)["data"]
        assert "password_delivery" not in data
        assert data["warnings"] == [
            "The data app uses auth 'public', so it has no password to copy."
        ]

    def test_terminal_plans_the_prompt_without_running_it(
        self, config_dir, httpx_mock, caplog, monkeypatch
    ) -> None:
        _clipboard(monkeypatch)
        _terminal(monkeypatch, [])
        read_calls: list[float] = []
        monkeypatch.setattr(
            _url_copy.CopyableUrlWait, "_read_key", lambda self, timeout: read_calls.append(timeout)
        )
        argv = _create_argv(config_dir, "--dry-run", "--wait", json_mode=False)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        assert "DRY RUN" in result.stdout
        assert "Password after the deploy: prompt" in result.stdout
        assert "Press c" not in result.stdout
        assert read_calls == []

    def test_json_without_flags_has_no_plan(self, config_dir, httpx_mock, caplog) -> None:
        argv = _create_argv(config_dir, "--dry-run", "--wait", json_mode=True)

        result = _run(argv, caplog)

        assert result.exit_code == 0, result.output
        assert "password_delivery" not in json.loads(result.stdout)["data"]


class TestPasswordAfterDeploy:
    """Branches the end-to-end tests cannot reach (a successful --wait is always running)."""

    _JSON = OutputFormatter(json_mode=True)

    def test_no_wait_means_no_call(self) -> None:
        service = MagicMock()
        result: dict[str, Any] = {"app_id": "42", "state": "starting"}
        flags = PasswordFlags(copy=True)
        assert (
            password_after_deploy(self._JSON, service, result, flags, alias="p", waited=False)
            is None
        )
        service.get_data_app_password.assert_not_called()
        assert "warnings" not in result

    def test_no_flag_and_no_prompt_means_no_call(self) -> None:
        service = MagicMock()
        result: dict[str, Any] = {"app_id": "42", "state": "running"}
        lookup = password_after_deploy(
            self._JSON, service, result, PasswordFlags(), alias="p", waited=True
        )
        assert lookup is None
        service.get_data_app_password.assert_not_called()
        assert result == {"app_id": "42", "state": "running"}

    def test_not_running_with_a_flag_warns(self) -> None:
        service = MagicMock()
        result: dict[str, Any] = {"app_id": "42", "state": "starting"}
        flags = PasswordFlags(copy=True)
        assert (
            password_after_deploy(self._JSON, service, result, flags, alias="p", waited=True)
            is None
        )
        service.get_data_app_password.assert_not_called()
        assert result["warnings"] == [
            "Data app 42 is not running (state=starting), so its password was not read."
        ]

    def test_config_error_becomes_a_warning(self) -> None:
        service = MagicMock()
        service.get_data_app_password.side_effect = ConfigError("Project 'p' not found.")
        result: dict[str, Any] = {"app_id": "42", "state": "running"}
        flags = PasswordFlags(copy=True)
        assert (
            password_after_deploy(self._JSON, service, result, flags, alias="p", waited=True)
            is None
        )
        assert result["warnings"] == [
            (
                "The deploy succeeded, but the password was not read (Project 'p' not found.); "
                "run `kbagent data-app password` to try again."
            )
        ]

    def test_unexpected_exception_is_a_warning_without_its_text(self) -> None:
        service = MagicMock()
        service.get_data_app_password.side_effect = RuntimeError("internal detail")
        result: dict[str, Any] = {"app_id": "42", "state": "running"}
        flags = PasswordFlags(reveal=True)
        assert (
            password_after_deploy(self._JSON, service, result, flags, alias="p", waited=True)
            is None
        )
        (warning,) = result["warnings"]
        assert "RuntimeError" in warning
        assert "internal detail" not in warning


# ---------------------------------------------------------------------------
# A real terminal: the prompt runs in a child process on a pty
# ---------------------------------------------------------------------------

# The child runs the real delivery (deliver_password -> terminal prompt) with
# the pty as its controlling terminal, a fake copier, and writes what happened
# to a report file. The password comes in argv and is never printed.
_PTY_CHILD = r"""
import fcntl, json, sys, termios
# A new session (start_new_session) takes the pty as its controlling terminal,
# as a login shell does, so Ctrl+C and the foreground check work as for a user.
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
from keboola_agent_cli.commands import _data_app_password as pw
from keboola_agent_cli.commands import _url_copy
from keboola_agent_cli.output import OutputFormatter
from keboola_agent_cli.services._data_app_password import DataAppPassword

report_path, password = sys.argv[1], sys.argv[2]
copied = []
def fake_copier(text):
    copied.append(text)
    return True
_url_copy.detect_clipboard = lambda: fake_copier
pw._COPY_PROMPT_TIMEOUT_SECONDS = 20.0
lookup = DataAppPassword(
    project_alias="p", app_id="42", auth="password",
    app_url="https://app-42.example", ui_url="https://ui.example", password=password,
)
outcome, delivered = "returned", None
try:
    delivered = pw.deliver_password(OutputFormatter(), lookup, pw.PasswordFlags()).delivered_to
except KeyboardInterrupt:
    outcome = "interrupted"
lflag = termios.tcgetattr(sys.stdin.fileno())[3]
with open(report_path, "w") as f:
    json.dump({
        "outcome": outcome,
        "delivered_to": delivered,
        "copied_password": copied == [password],
        "copies": len(copied),
        "icanon": bool(lflag & termios.ICANON),
        "echo": bool(lflag & termios.ECHO),
    }, f)
"""

_PTY_DEADLINE_SECONDS = 30.0


class _PtyChild:
    """The child on its pty: read its output, type keys, wait for its report."""

    def __init__(self, tmp_path: Path) -> None:
        self.report = tmp_path / "report.json"
        self.output = b""
        self.master, slave = pty.openpty()
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _PTY_CHILD, str(self.report), SENTINEL],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)

    def _read_available(self, timeout: float) -> bool:
        ready, _, _ = select.select([self.master], [], [], timeout)
        if not ready:
            return True
        try:
            chunk = os.read(self.master, 4096)
        except OSError:  # EIO: the child closed its side
            return False
        self.output += chunk
        return bool(chunk)

    def _drain(self) -> None:
        """Read what the child left in the pty after it exited."""
        while True:
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if not ready:
                return
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                return
            if not chunk:
                return
            self.output += chunk

    def wait_for_output(self, text: bytes) -> None:
        deadline = time.monotonic() + _PTY_DEADLINE_SECONDS
        while text not in self.output:
            assert time.monotonic() < deadline, f"no {text!r} in {self.output!r}"
            if not self._read_available(0.1):
                raise AssertionError(f"child ended before {text!r}: {self.output!r}")

    def type(self, keys: bytes) -> None:
        os.write(self.master, keys)

    def is_running(self) -> bool:
        return self.proc.poll() is None

    def finish(self) -> dict[str, Any]:
        deadline = time.monotonic() + _PTY_DEADLINE_SECONDS
        try:
            while self.is_running():
                self._read_available(0.05)
                assert time.monotonic() < deadline, f"child still running: {self.output!r}"
            self._drain()
        finally:
            if self.is_running():
                self.proc.kill()
            self.proc.wait()
            os.close(self.master)
        assert SENTINEL.encode() not in self.output
        return json.loads(self.report.read_text())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty")
class TestRealTerminal:
    def test_c_then_enter(self, tmp_path: Path) -> None:
        child = _PtyChild(tmp_path)
        child.wait_for_output(b"Press c")
        child.type(b"c")
        child.wait_for_output(b"Copied to clipboard")
        child.type(b"\n")
        report = child.finish()
        assert report["delivered_to"] == "clipboard"
        assert report["copied_password"] is True
        assert report["icanon"] and report["echo"]

    def test_c_and_enter_in_one_burst(self, tmp_path: Path) -> None:
        child = _PtyChild(tmp_path)
        child.wait_for_output(b"Press c")
        started = time.monotonic()
        child.type(b"c\n")
        report = child.finish()
        assert time.monotonic() - started < 10  # the Enter was seen, no timeout
        assert report["delivered_to"] == "clipboard"
        assert report["copies"] == 1

    def test_arrow_key_is_ignored(self, tmp_path: Path) -> None:
        child = _PtyChild(tmp_path)
        child.wait_for_output(b"Press c")
        child.type(b"\x1b[A")
        time.sleep(0.5)
        assert child.is_running()  # the arrow did not end the prompt
        child.type(b"q")
        report = child.finish()
        assert report["outcome"] == "returned"
        assert report["delivered_to"] is None
        assert b"No key pressed" not in child.output

    def test_lone_esc_finishes(self, tmp_path: Path) -> None:
        child = _PtyChild(tmp_path)
        child.wait_for_output(b"Press c")
        child.type(b"\x1b")
        report = child.finish()
        assert report["delivered_to"] is None
        assert report["copies"] == 0

    def test_ctrl_c_restores_the_terminal(self, tmp_path: Path) -> None:
        child = _PtyChild(tmp_path)
        child.wait_for_output(b"Press c")
        child.type(b"\x03")
        report = child.finish()
        assert report["outcome"] == "interrupted"
        assert report["icanon"] is True
        assert report["echo"] is True
