"""Human-output snapshots: one command per shared renderer (spec rule HO7).

Each case runs a real command offline (CliRunner, pytest-httpx routes, a temp
config directory) and compares its human output with a committed snapshot in
``tests/snapshots/human_output/``. The snapshots record today's output as it
is, including known drift (a ``Warning:`` that also goes to stdout), so that a
change to a renderer shows as a snapshot diff in the pull request.

Renderers covered: a table (``config list``), a panel (``config detail``), the
``Target:`` and ``Source:`` lines, a ``Success:`` headline (``project
remove``), a ``Warning:`` notice (``branch merge``), an ``Error:`` line, the
per-project failure notice of a fan-out read (``config list``), the
multi-item headline (``storage delete-table``) and the ``auth login`` notices
on stderr.

Trailing spaces are removed from every line; Rich pads table titles with them.

To record a changed renderer on purpose:
    KBAGENT_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_cli_human_snapshots.py
and commit the snapshot diff with the change.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from helpers import mock_api_routes, setup_single_project
from keboola_agent_cli import output
from keboola_agent_cli.auth.models import DeviceAuthorization
from keboola_agent_cli.cli import app
from keboola_agent_cli.constants import ENV_AUTO_UPDATE
from keboola_agent_cli.services.auth_service import LoginResult
from keboola_agent_cli.services.storage_service import StorageService

SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "human_output"
UPDATE = os.environ.get("KBAGENT_UPDATE_SNAPSHOTS") == "1"

runner = CliRunner()

DEFAULT_BRANCH_ID = 100
CONFIG = {
    "id": "11",
    "name": "Orders",
    "description": "Nightly load",
    "created": "2026-09-01T00:00:00+0000",
    "version": 2,
    "isDeleted": False,
    "isDisabled": False,
    "configuration": {"parameters": {"db": {"host": "mysql.example.com"}}},
    "rows": [],
}
COMPONENTS = [
    {
        "id": "keboola.ex-db-mysql",
        "type": "extractor",
        "name": "MySQL",
        "configurations": [CONFIG],
    }
]
BRANCHES = [
    {"id": DEFAULT_BRANCH_ID, "name": "Main", "isDefault": True},
    {"id": 123, "name": "feature", "isDefault": False},
]
ROUTES: dict[tuple[str, ...], Any] = {
    ("GET", "/v2/storage/components"): COMPONENTS,
    ("GET", "/v2/storage/dev-branches"): BRANCHES,
    ("GET", f"/v2/storage/branch/{DEFAULT_BRANCH_ID}/search/component-configurations"): [],
    ("GET", "/v2/storage/components/keboola.ex-db-mysql/configs/11"): CONFIG,
    (
        "GET",
        f"/v2/storage/branch/{DEFAULT_BRANCH_ID}/components/keboola.ex-db-mysql/configs/11",
    ): CONFIG,
}

CASES: dict[str, list[str]] = {
    "config_list_table": ["config", "list", "--project", "prod"],
    "config_detail_panel": [
        "config",
        "detail",
        "--project",
        "prod",
        "--component-id",
        "keboola.ex-db-mysql",
        "--config-id",
        "11",
    ],
    "project_remove_success": ["project", "remove", "--project", "prod"],
    "branch_merge_warning": ["branch", "merge", "--project", "prod", "--branch", "123"],
    "config_detail_api_error": [
        "config",
        "detail",
        "--project",
        "prod",
        "--component-id",
        "keboola.ex-db-mysql",
        "--config-id",
        "99",
    ],
}


@pytest.fixture(autouse=True)
def _no_auto_update(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_AUTO_UPDATE, "false")


@pytest.fixture(autouse=True)
def _same_boxes_on_every_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render like a modern terminal on every host.

    On a Windows runner Rich detects a legacy console when output is redirected
    and replaces the heavy and rounded box characters with square ones, so the
    same snapshot would differ by platform. This changes only the test console.
    """
    real_console = output.Console

    def _console(**kwargs: Any) -> Console:
        kwargs.setdefault("legacy_windows", False)
        return real_console(**kwargs)

    monkeypatch.setattr(output, "Console", _console)


def _render(exit_code: int, stdout: str, stderr: str) -> str:
    def clean(text: str) -> str:
        return "\n".join(line.rstrip() for line in text.rstrip().splitlines())

    return f"exit: {exit_code}\n--- stdout ---\n{clean(stdout)}\n--- stderr ---\n{clean(stderr)}\n"


def _check(name: str, actual: str) -> None:
    path = SNAPSHOT_DIR / f"{name}.txt"
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8", newline="\n")
        return
    assert path.exists(), f"no snapshot {path}; record it with KBAGENT_UPDATE_SNAPSHOTS=1"
    expected = path.read_text(encoding="utf-8")
    assert actual == expected, (
        f"human output of '{name}' changed. If the change is intended, run "
        "KBAGENT_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_cli_human_snapshots.py "
        f"and commit the snapshot diff.\n--- expected ---\n{expected}\n--- actual ---\n{actual}"
    )


@pytest.mark.parametrize("name", sorted(CASES))
def test_human_output_matches_snapshot(name: str, tmp_config_dir: Path, httpx_mock: Any) -> None:
    setup_single_project(tmp_config_dir)
    mock_api_routes(httpx_mock, ROUTES)
    result = runner.invoke(app, ["--config-dir", str(tmp_config_dir), *CASES[name]])
    _check(name, _render(result.exit_code, result.stdout, result.stderr))


def test_fanout_failure_notice_matches_snapshot(tmp_config_dir: Path, httpx_mock: Any) -> None:
    """A fan-out read whose only project fails: no route answers the component listing."""
    setup_single_project(tmp_config_dir)
    mock_api_routes(httpx_mock, {("GET", "/v2/storage/dev-branches"): BRANCHES})
    result = runner.invoke(app, ["--config-dir", str(tmp_config_dir), "config", "list"])
    _check("config_list_fanout_failure", _render(result.exit_code, result.stdout, result.stderr))


DELETE_RESULTS: dict[str, dict[str, Any]] = {
    "storage_delete_table_headline_success": {
        "deleted": ["in.c-main.orders", "in.c-main.items"],
        "failed": [],
    },
    "storage_delete_table_headline_failed": {
        "deleted": ["in.c-main.orders"],
        "failed": [{"id": "in.c-main.items", "error": "Table is used by an alias"}],
    },
}


@pytest.mark.parametrize("name", sorted(DELETE_RESULTS))
def test_multi_item_headline_matches_snapshot(
    name: str, tmp_config_dir: Path, httpx_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HO2: the one closing `Success:` or `Failed: N of M` line of a multi-item command."""
    setup_single_project(tmp_config_dir)
    mock_api_routes(httpx_mock, ROUTES)
    result_data = {**DELETE_RESULTS[name], "dry_run": False, "project_alias": "prod"}
    monkeypatch.setattr(StorageService, "delete_tables", lambda _self, **_kw: result_data)
    args = ["storage", "delete-table", "--project", "prod", "--yes"]
    args += ["--table-id", "in.c-main.orders", "--table-id", "in.c-main.items"]
    result = runner.invoke(app, ["--config-dir", str(tmp_config_dir), *args])
    _check(name, _render(result.exit_code, result.stdout, result.stderr))


def test_auth_login_notices_match_snapshot(tmp_config_dir: Path) -> None:
    """HO1, row 30 of keboola/cli#791: the device panel and the notices are on stderr."""

    def _login(**kwargs: Any) -> LoginResult:
        kwargs["on_notice"]("Using device login: --no-open is set.")
        kwargs["on_device_prompt"](
            DeviceAuthorization(
                deviceCode="dc",
                userCode="ABCD-EFGH",
                verificationUri="https://connection.keboola.com/device",
            )
        )
        return LoginResult(
            status="ok",
            method="device",
            stack_url="https://connection.keboola.com",
            session_id="sess-1",
            user_email="user@example.com",
            user_name="Test User",
            access_expires_at="2026-07-27T12:00:00+00:00",
            refresh_expires_at="2026-08-26T12:00:00+00:00",
            fallback_reason="--no-open is set.",
            replaced_session_id="",
            orphaned_session_id="",
            accessible_projects=[],
            registered_projects=[],
            warnings=[],
        )

    service = MagicMock()
    service.login.side_effect = _login
    with patch("keboola_agent_cli.cli.AuthService", return_value=service):
        result = runner.invoke(
            app, ["--config-dir", str(tmp_config_dir), "auth", "login", "--no-open"]
        )
    _check("auth_login_notices", _render(result.exit_code, result.stdout, result.stderr))


def test_every_snapshot_has_a_case() -> None:
    names = {*CASES, *DELETE_RESULTS, "config_list_fanout_failure", "auth_login_notices"}
    stale = sorted(p.stem for p in SNAPSHOT_DIR.glob("*.txt") if p.stem not in names)
    assert stale == [], f"snapshots without a test case: {stale}"
