"""``--token-stdin`` must never print the token it read.

The point of the flag is a token that never reaches the shell history. That
only holds if kbagent does not print it back either: not in human output, not
in ``--json``, not in ``--dry-run``, not in the DEBUG log.

These tests run the real CLI with the real ``ProjectService`` and the real
``KeboolaClient``. Only the network is replaced, so every string the code path
can produce is checked -- the success line, the JSON payload, the dry-run
preview, the error message, the log records.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import ProjectConfig

STACK = "https://connection.keboola.com"
VERIFY_URL = f"{STACK}/v2/storage/tokens/verify"
ALIAS = "prod"

# Fake values. The middle part is checked on its own as well: `mask_token`
# keeps the prefix and the last four characters, so a broken mask would show
# up as the middle part being visible.
NEW_TOKEN = "901-77777-fakeStdinTokenDoNotUseXXXX"
OLD_TOKEN = "901-55555-fakeStoredTokenDoNotUseYYYY"
SECRETS = (NEW_TOKEN, NEW_TOKEN[4:-4], OLD_TOKEN, OLD_TOKEN[4:-4])

VERIFY_OK = {"id": "12345", "description": "d", "owner": {"id": 1234, "name": "Test Project"}}

runner = CliRunner()


def _seed_project(config_dir: Path) -> None:
    """Register ``ALIAS`` with ``OLD_TOKEN``, as a `project edit` target."""
    ConfigStore(config_dir=config_dir).add_project(
        ALIAS,
        ProjectConfig(
            stack_url=STACK, token=OLD_TOKEN, project_name="Old Project", project_id=1234
        ),
    )


def _stored_token(config_dir: Path) -> str:
    project = ConfigStore(config_dir=config_dir).get_project(ALIAS)
    assert project is not None
    return project.token


# scenario id -> (command args, seed the project first, verify-token status or
# None when the command makes no API call)
SCENARIOS = {
    "add-ok": (["project", "add", "--project", ALIAS, "--url", STACK], False, 200),
    "add-rejected-token": (["project", "add", "--project", ALIAS, "--url", STACK], False, 401),
    "edit-ok": (["project", "edit", "--project", ALIAS], True, 200),
    "edit-rejected-token": (["project", "edit", "--project", ALIAS], True, 401),
    "edit-dry-run": (["project", "edit", "--project", ALIAS, "--dry-run"], True, None),
}


@pytest.mark.parametrize("verbose", [False, True], ids=["quiet", "verbose"])
@pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_token_read_from_stdin_is_never_printed(
    scenario: str,
    json_mode: bool,
    verbose: bool,
    tmp_path: Path,
    httpx_mock,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command, seed, verify_status = SCENARIOS[scenario]
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    if seed:
        _seed_project(config_dir)
    for name in ("KBC_TOKEN", "KBC_STORAGE_API_URL", "KBAGENT_PROJECT_FROM_ENV"):
        monkeypatch.delenv(name, raising=False)
    if verify_status is not None:
        httpx_mock.add_response(
            url=VERIFY_URL,
            status_code=verify_status,
            json=VERIFY_OK if verify_status == 200 else {"error": "Invalid access token"},
        )
    # The CLI sets the root logger to DEBUG under --verbose. pytest's own handler
    # is already on the root logger, which makes that call a no-op here, so set
    # the level the same way to see every record the run would print.
    caplog.set_level(logging.DEBUG)

    argv = [
        "--config-dir",
        str(config_dir),
        *(["--json"] if json_mode else []),
        *(["--verbose"] if verbose else []),
        *command,
        "--token-stdin",
    ]
    result = runner.invoke(app, argv, input=f"{NEW_TOKEN}\n")

    everything = "\n".join(
        [result.stdout, result.stderr, result.output, caplog.text]
        + [record.getMessage() for record in caplog.records]
    )
    for secret in SECRETS:
        assert secret not in everything

    # The run must not be vacuous: the token was read and used, and the DEBUG
    # log did capture the HTTP call it was used in.
    expected_exit = {200: 0, 401: 3, None: 0}[verify_status]
    assert result.exit_code == expected_exit, result.output
    if verify_status is None:
        assert not httpx_mock.get_requests()
    else:
        (request,) = httpx_mock.get_requests()
        assert request.headers["X-StorageApi-Token"] == NEW_TOKEN
        assert "HTTP Request" in caplog.text
    if verify_status == 200:
        assert _stored_token(config_dir) == NEW_TOKEN
    elif seed:
        assert _stored_token(config_dir) == OLD_TOKEN, (
            "a dry run or a rejected token stores nothing"
        )


class _VerifyHandler(BaseHTTPRequestHandler):
    """Answers the verify-token call and remembers the token header it got."""

    received_token: str | None = None

    def do_GET(self) -> None:
        type(self).received_token = self.headers.get("X-StorageApi-Token")
        body = b'{"id": "1", "description": "d", "owner": {"id": 1, "name": "P"}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the test output quiet."""


@contextmanager
def _verify_server() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _VerifyHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_debug_log_of_the_real_transport_omits_the_token_header(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """httpx and httpcore log at DEBUG; the header they send must not appear.

    The mocked transport above never reaches httpcore, which logs the most
    detail. This sends a real request over a loopback socket.
    """
    _VerifyHandler.received_token = None
    caplog.set_level(logging.DEBUG)

    with _verify_server() as base_url, KeboolaClient(stack_url=base_url, token=NEW_TOKEN) as client:
        client.verify_token()

    assert _VerifyHandler.received_token == NEW_TOKEN, "the token was sent on the wire"
    assert any(record.name.startswith("httpcore") for record in caplog.records)
    everything = caplog.text + "\n".join(record.getMessage() for record in caplog.records)
    for secret in (NEW_TOKEN, NEW_TOKEN[4:-4]):
        assert secret not in everything


SECOND_LINE = "second-line-of-something-piped-in-by-mistake"


def _error_text(output: str) -> str:
    """Join the lines of the Rich error box, so a wrapped message reads as one line.

    CI renders the box with colour codes, so drop them too.
    """
    plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
    return " ".join(plain.replace("\u2502", " ").split())


@pytest.mark.parametrize("separator", ["\n", "\x1b"], ids=["line-break", "control-char"])
@pytest.mark.parametrize("source", ["stdin", "file", "env", "token"])
def test_multi_line_token_is_refused_without_printing_it(
    source: str, separator: str, tmp_path: Path, httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token with a line break must not reach the HTTP layer.

    The layer fails with "Illegal header value b'<the whole value>'" and nothing
    catches that, so an unhandled traceback would print the token and whatever
    else was piped in with it (a whole `.env` file, for example).
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in ("KBC_TOKEN", "KBC_STORAGE_API_URL", "KBAGENT_PROJECT_FROM_ENV"):
        monkeypatch.delenv(name, raising=False)
    multi_line = f"{NEW_TOKEN}{separator}{SECOND_LINE}"
    token_file = tmp_path / "token.txt"
    token_file.write_text(multi_line, encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("CI_KBC_TOKEN", multi_line)
    flags, piped, flag_name = {
        "stdin": (["--token-stdin"], multi_line, "--token-stdin"),
        "file": (["--token-file", str(token_file)], None, "--token-file"),
        "env": (["--token-env", "CI_KBC_TOKEN"], None, "--token-env"),
        "token": (["--token", multi_line], None, "--token"),
    }[source]

    argv = ["--config-dir", str(config_dir), "project", "add", "--project", ALIAS, "--url", STACK]
    result = runner.invoke(app, [*argv, *flags], input=piped)

    assert result.exit_code == 2, result.output
    message = _error_text(result.output)
    assert f"Invalid value for {flag_name}: The token contains a line break" in message
    assert not httpx_mock.get_requests()
    assert token_file.exists(), "a refused token must not cost the user the file"
    everything = result.output + result.stdout + result.stderr
    for secret in (NEW_TOKEN, NEW_TOKEN[4:-4], SECOND_LINE):
        assert secret not in everything


@pytest.mark.parametrize("piped", ["", "\n", "   \n"])
def test_empty_token_stdin_is_refused_before_any_request(
    piped: str, tmp_path: Path, httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`printf '%s' "$KBC_TOKEN" | ... --token-stdin` with the variable unset pipes nothing."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in ("KBC_TOKEN", "KBC_STORAGE_API_URL", "KBAGENT_PROJECT_FROM_ENV"):
        monkeypatch.delenv(name, raising=False)

    argv = ["--config-dir", str(config_dir), "project", "add", "--project", ALIAS, "--url", STACK]
    result = runner.invoke(app, [*argv, "--token-stdin"], input=piped)

    assert result.exit_code == 2, result.output
    assert "Invalid value for --token-stdin: The token is empty." in _error_text(result.output)
    assert not httpx_mock.get_requests()


@pytest.mark.parametrize("command", ["add", "edit"])
def test_explicit_empty_token_flag_is_refused(
    command: str, tmp_path: Path, httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--token "$NEW_TOKEN"` with the variable empty must fail, not pass silently.

    For `project edit` an empty value read as "no token given" would repoint
    the alias to the new URL and keep the old stack's token, with exit 0.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in ("KBC_TOKEN", "KBC_STORAGE_API_URL", "KBAGENT_PROJECT_FROM_ENV"):
        monkeypatch.delenv(name, raising=False)
    new_url = "https://connection.north-europe.azure.keboola.com"
    if command == "edit":
        _seed_project(config_dir)

    argv = ["--config-dir", str(config_dir), "project", command, "--project", ALIAS]
    result = runner.invoke(app, [*argv, "--url", new_url, "--token", ""])

    assert result.exit_code == 2, result.output
    assert "Invalid value for --token: The token is empty." in _error_text(result.output)
    assert not httpx_mock.get_requests()
    stored = ConfigStore(config_dir=config_dir).get_project(ALIAS)
    if command == "edit":
        assert stored is not None
        assert (stored.stack_url, stored.token) == (STACK, OLD_TOKEN)
    else:
        assert stored is None
