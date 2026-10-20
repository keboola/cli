"""The headline and exit code of the multi-item commands added by the 1.0 audit (HO2, E4).

`config detail` with several `--project`, `token list --with-last-used` and
`auth register-projects` end through `finish_items()` like every other
command that processes several items. The services are patched: no request
leaves the process.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from helpers import setup_single_project, setup_two_projects
from keboola_agent_cli.cli import app
from keboola_agent_cli.constants import ENV_AUTO_UPDATE
from keboola_agent_cli.services._auth_registration import (
    RegisteredProject,
    RegisterProjectsResult,
)
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.token_service import TokenService

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_auto_update(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_AUTO_UPDATE, "false")


@pytest.mark.parametrize("failed", [False, True])
def test_config_detail_bulk_ends_with_headline(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch, failed: bool
) -> None:
    setup_two_projects(tmp_config_dir)
    errors = [{"project_alias": "dev", "error_code": "INVALID_TOKEN", "message": "bad"}]
    result = {"configs": [], "errors": errors if failed else []}
    monkeypatch.setattr(ConfigService, "get_config_detail", lambda _self, **_kw: result)
    args = ["config", "detail", "--project", "prod", "--project", "dev"]
    args += ["--component-id", "keboola.ex-db-mysql"]
    out = runner.invoke(app, ["--config-dir", str(tmp_config_dir), *args])
    last = out.stdout.strip().splitlines()[-1]
    if failed:
        assert out.exit_code == 1, out.output
        assert last == "Failed: 1 of 2 projects. 1 project(s) read."
    else:
        assert out.exit_code == 0, out.output
        assert last == "Success: 2 project(s) read."


@pytest.mark.parametrize("failed", [False, True])
def test_token_list_with_last_used_exits_1_on_a_failed_lookup(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch, failed: bool
) -> None:
    setup_single_project(tmp_config_dir)
    tokens = [{"id": "1", "description": "a"}, {"id": "2", "description": "b"}]
    errors = [{"token_id": "2", "message": "events: HTTP 500"}] if failed else []
    result = {"alias": "prod", "tokens": tokens, "errors": errors}
    monkeypatch.setattr(TokenService, "list_tokens", lambda _self, **_kw: result)
    base = ["--config-dir", str(tmp_config_dir)]
    args = ["token", "list", "--project", "prod", "--with-last-used"]
    human = runner.invoke(app, [*base, *args])
    machine = runner.invoke(app, [*base, "--json", *args])
    assert human.exit_code == machine.exit_code == (1 if failed else 0)
    assert json.loads(machine.stdout)["exit_code"] == (1 if failed else 0)
    last = human.stdout.strip().splitlines()[-1]
    if failed:
        assert "Error: token 2: last-used lookup failed" in human.stdout
        assert last == "Failed: 1 of 2 tokens. 1 token(s) checked for last use."
    else:
        assert last == "Success: 2 token(s) checked for last use."


def test_register_projects_ends_with_headline(tmp_config_dir: Path) -> None:
    """`exists` and `skipped` are outcomes, not failures (like `org setup`)."""
    service = MagicMock()
    service.register_projects.return_value = RegisterProjectsResult(
        status="ok",
        stack_url="https://connection.keboola.com",
        registered_projects=[
            RegisteredProject(alias="a", project_id=1, project_name="A", status="registered"),
            RegisteredProject(alias="b", project_id=2, project_name="B", status="exists"),
            RegisteredProject(alias="c", project_id=3, project_name="C", status="skipped"),
        ],
        warnings=[],
    )
    with patch("keboola_agent_cli.cli.AuthService", return_value=service):
        out = runner.invoke(
            app, ["--config-dir", str(tmp_config_dir), "auth", "register-projects", "--all"]
        )
    assert out.exit_code == 0, out.output
    last = out.stdout.strip().splitlines()[-1]
    assert last == "Success: 1 project(s) registered, 1 already registered, 1 skipped."
