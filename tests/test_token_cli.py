"""CLI tests for the `kbagent token` command group."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.models import ProjectConfig

STACK_URL = "https://connection.keboola.com"
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
ALIAS = "padak"

runner = CliRunner()


def _seed(config_dir: Path) -> None:
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        ALIAS,
        ProjectConfig(stack_url=STACK_URL, token=TOKEN, project_name="Padak 2.0", project_id=10539),
    )


def _invoke(config_dir: Path, svc: MagicMock, args: list[str], input_text: str | None = None):
    with patch("keboola_agent_cli.cli.TokenService", return_value=svc):
        return runner.invoke(app, ["--config-dir", str(config_dir), *args], input=input_text)


class TestCreate:
    def test_create_json_parses_repeatable_flags(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        svc = MagicMock()
        svc.create_scoped_token.return_value = {
            "alias": ALIAS,
            "id": "12345",
            "token": "12345-secretValue",
            "description": "device enrollment",
            "expires": None,
        }
        result = _invoke(
            config_dir,
            svc,
            [
                "--json",
                "token",
                "create",
                "--project",
                ALIAS,
                "--description",
                "device enrollment",
                "--bucket-write",
                "out.c-b",
                "--bucket-write",
                "out.c-c",
                "--bucket-read",
                "in.c-a",
                "--component-access",
                "keboola.ex-db-mysql",
                "--can-read-all-file-uploads",
                "--expires-in",
                "3600",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["token"] == "12345-secretValue"
        svc.create_scoped_token.assert_called_once_with(
            alias=ALIAS,
            description="device enrollment",
            bucket_write=["out.c-b", "out.c-c"],
            bucket_read=["in.c-a"],
            component_access=["keboola.ex-db-mysql"],
            can_read_all_file_uploads=True,
            expires_in=3600,
        )

    def test_create_human_prints_token_once(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        svc = MagicMock()
        svc.create_scoped_token.return_value = {
            "alias": ALIAS,
            "id": "12345",
            "token": "12345-secretValue",
            "description": "d",
            "expires": None,
        }
        result = _invoke(
            config_dir,
            svc,
            ["token", "create", "--project", ALIAS, "--description", "d"],
        )
        assert result.exit_code == 0, result.output
        assert "12345-secretValue" in result.output
        svc.create_scoped_token.assert_called_once_with(
            alias=ALIAS,
            description="d",
            bucket_write=None,
            bucket_read=None,
            component_access=None,
            can_read_all_file_uploads=False,
            expires_in=None,
        )


class TestDelete:
    def test_delete_yes_skips_confirm(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        svc = MagicMock()
        svc.delete_token.return_value = {
            "status": "deleted",
            "alias": ALIAS,
            "token_id": "999",
        }
        result = _invoke(
            config_dir,
            svc,
            ["token", "delete", "--project", ALIAS, "--token-id", "999", "--yes"],
        )
        assert result.exit_code == 0, result.output
        assert "Revoked" in result.output
        svc.delete_token.assert_called_once_with(alias=ALIAS, token_id="999")

    def test_delete_confirm_abort(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        svc = MagicMock()
        result = _invoke(
            config_dir,
            svc,
            ["token", "delete", "--project", ALIAS, "--token-id", "999"],
            input_text="n\n",
        )
        assert result.exit_code == 0
        assert "Aborted" in result.output
        svc.delete_token.assert_not_called()


class TestRefresh:
    def test_refresh_json(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        svc = MagicMock()
        svc.refresh_token.return_value = {
            "alias": ALIAS,
            "id": "999",
            "token": "999-newSecret",
            "expires": None,
        }
        result = _invoke(
            config_dir,
            svc,
            ["--json", "token", "refresh", "--project", ALIAS, "--token-id", "999"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["token"] == "999-newSecret"
        svc.refresh_token.assert_called_once_with(alias=ALIAS, token_id="999")


class TestErrors:
    def test_api_error_exit_code(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        svc = MagicMock()
        svc.create_scoped_token.side_effect = KeboolaApiError(
            message="access denied", status_code=403, error_code="ACCESS_DENIED"
        )
        result = _invoke(
            config_dir,
            svc,
            ["--json", "token", "create", "--project", ALIAS, "--description", "d"],
        )
        assert result.exit_code != 0
        assert json.loads(result.output)["status"] == "error"


class TestList:
    def _svc_with(self, tokens: list[dict]) -> MagicMock:
        svc = MagicMock()
        svc.list_tokens.return_value = {
            "alias": ALIAS,
            "count": len(tokens),
            "tokens": tokens,
        }
        return svc

    def test_list_json(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        svc = self._svc_with(
            [
                {
                    "id": "12345",
                    "description": "device enrollment",
                    "created": "2026-08-01T10:00:00+0200",
                    "expires": "2026-09-01T10:00:00+0200",
                    "isExpired": False,
                    "isMasterToken": False,
                }
            ]
        )
        result = _invoke(config_dir, svc, ["--json", "token", "list", "--project", ALIAS])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["data"]["count"] == 1
        assert payload["data"]["tokens"][0]["id"] == "12345"
        svc.list_tokens.assert_called_once_with(alias=ALIAS)

    def test_list_human_renders_rows(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        svc = self._svc_with(
            [
                {"id": "1", "description": "master token", "isMasterToken": True},
                {"id": "2", "description": "device enrollment", "isMasterToken": False},
            ]
        )
        result = _invoke(config_dir, svc, ["token", "list", "--project", ALIAS])
        assert result.exit_code == 0
        assert "device enrollment" in result.stdout
        assert "12345" not in result.stdout

    def test_list_human_empty(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        result = _invoke(config_dir, self._svc_with([]), ["token", "list", "--project", ALIAS])
        assert result.exit_code == 0
        assert "No tokens" in result.stdout

    def test_list_api_error_exits_nonzero(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        svc = MagicMock()
        svc.list_tokens.side_effect = KeboolaApiError(
            message="Access denied", status_code=403, error_code="ACCESS_DENIED"
        )
        result = _invoke(config_dir, svc, ["--json", "token", "list", "--project", ALIAS])
        # ACCESS_DENIED is a general error (1) in this CLI's exit-code map;
        # only INVALID_TOKEN / session failures are the auth class (3).
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "ACCESS_DENIED"
