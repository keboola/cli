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
