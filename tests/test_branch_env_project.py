"""`branch create/use/reset` on the environment project `__env__` (customer feedback 2.6, 3.7).

A headless caller sets KBAGENT_PROJECT_FROM_ENV=1 + KBC_TOKEN +
KBC_STORAGE_API_URL and passes `--project __env__`. That project lives only in
memory, so no active branch can be stored for it. Before the fix `branch
create` created the branch and then failed (exit 5) on the activation write,
`branch reset` was refused, and `branch use` failed only after an API call.

Real config store, real services and the real CLI / serve app; only the
Keboola Storage client is a mock.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore

runner = CliRunner()

ENV_TOKEN = "901-55555-fakeTestTokenDoNotUseEnv"
NEW_BRANCH = 4321
AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture(autouse=True)
def env_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KBAGENT_PROJECT_FROM_ENV", "1")
    monkeypatch.setenv("KBC_TOKEN", ENV_TOKEN)
    monkeypatch.setenv("KBC_STORAGE_API_URL", "https://connection.keboola.com")
    monkeypatch.delenv("KBAGENT_PROJECT", raising=False)


@pytest.fixture
def api_client() -> MagicMock:
    client = MagicMock()
    client.create_dev_branch.return_value = {
        "id": NEW_BRANCH,
        "name": "feature-x",
        "description": "",
        "created": "2026-10-29T10:00:00Z",
    }
    client.list_dev_branches.return_value = [{"id": NEW_BRANCH, "name": "feature-x"}]
    return client


def _invoke(config_dir: Path, api_client: MagicMock, *args: str) -> tuple[int, dict]:
    with patch("keboola_agent_cli.services.base.KeboolaClient", return_value=api_client):
        result = runner.invoke(
            app, ["--config-dir", str(config_dir), "--json", "branch", *args, "-p", "__env__"]
        )
    return result.exit_code, json.loads(result.stdout)


class TestCli:
    def test_create_succeeds_without_activation(
        self, tmp_path: Path, api_client: MagicMock
    ) -> None:
        code, doc = _invoke(tmp_path, api_client, "create", "--name", "feature-x")

        assert code == 0, doc
        assert doc["status"] == "ok"
        assert doc["data"]["branch_id"] == NEW_BRANCH
        assert doc["data"]["activated"] is False
        warning = next(w for w in doc["warnings"] if w["code"] == "ACTIVE_BRANCH_NOT_STORED")
        assert f"--branch {NEW_BRANCH}" in warning["hint"]
        assert "--branch" in warning["message"]
        api_client.create_dev_branch.assert_called_once()
        # Nothing was written for the environment project.
        assert ConfigStore(config_dir=tmp_path).get_project("__env__").active_branch_id is None

    def test_reset_is_a_noop(self, tmp_path: Path, api_client: MagicMock) -> None:
        code, doc = _invoke(tmp_path, api_client, "reset")

        assert code == 0, doc
        assert doc["data"]["previous_branch_id"] is None
        assert api_client.mock_calls == []

    def test_use_fails_before_any_api_call(self, tmp_path: Path, api_client: MagicMock) -> None:
        code, doc = _invoke(tmp_path, api_client, "use", "--branch", str(NEW_BRANCH))

        assert code == 5
        assert doc["error"]["code"] == "CONFIG_ERROR"
        assert "--branch" in doc["error"]["message"]
        assert api_client.mock_calls == []


@pytest.mark.skipif(importlib.util.find_spec("fastapi") is None, reason="FastAPI not installed")
class TestServe:
    @pytest.fixture
    def client(self, tmp_path: Path, api_client: MagicMock):
        from fastapi.testclient import TestClient

        from keboola_agent_cli.server import create_app

        server = create_app(config_dir=str(tmp_path), auth_token="test-token")
        server.state.registry.branch._client_factory = lambda *_a, **_k: api_client
        return TestClient(server)

    @staticmethod
    def _warning_codes(response) -> list[str]:
        header = response.headers.get("Kbagent-Warnings")
        return [w["code"] for w in json.loads(header)] if header else []

    def test_create_succeeds_without_activation(self, client) -> None:
        response = client.post("/branches/__env__", json={"name": "feature-x"}, headers=AUTH)

        assert response.status_code == 200, response.text
        assert response.json()["activated"] is False
        assert "ACTIVE_BRANCH_NOT_STORED" in self._warning_codes(response)

    def test_reset_is_a_noop(self, client, api_client: MagicMock) -> None:
        response = client.post("/branches/__env__/reset", headers=AUTH)

        assert response.status_code == 200, response.text
        assert response.json()["active_branch_cleared"] is None
        assert "ACTIVE_CHOICE_SHARED" not in self._warning_codes(response)
        assert api_client.mock_calls == []

    def test_use_fails_before_any_api_call(self, client, api_client: MagicMock) -> None:
        response = client.post(
            "/branches/__env__/use", json={"branch_id": NEW_BRANCH}, headers=AUTH
        )

        assert response.status_code == 400, response.text
        assert api_client.mock_calls == []
