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
from typer.testing import CliRunner

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
