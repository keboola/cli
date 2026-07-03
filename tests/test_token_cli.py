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
