"""CLI tests for the `kbagent stream` command group."""

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
    with patch("keboola_agent_cli.cli.StreamService", return_value=svc):
        return runner.invoke(app, ["--config-dir", str(config_dir), *args], input=input_text)


class TestList:
    def test_json(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        svc = MagicMock()
        svc.list_sources.return_value = {
            "alias": ALIAS,
            "branch_id": "default",
            "sources": [
                {"source_id": "s1", "name": "s1", "type": "otlp", "base_endpoint": "https://x"}
            ],
        }
        result = _invoke(config_dir, svc, ["--json", "stream", "list", "--project", ALIAS])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["sources"][0]["source_id"] == "s1"
        svc.list_sources.assert_called_once_with(alias=ALIAS, branch_id=None)

    def test_human_empty(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        svc = MagicMock()
        svc.list_sources.return_value = {"alias": ALIAS, "branch_id": "default", "sources": []}
        result = _invoke(config_dir, svc, ["stream", "list", "--project", ALIAS])
        assert result.exit_code == 0
        assert "No Data Streams sources" in result.output


class TestCreate:
    def test_create_json(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        svc = MagicMock()
        svc.create_source.return_value = {
            "status": "created",
            "source_id": "s1",
            "name": "s1",
            "type": "otlp",
            "endpoint": "https://stream-in/.../***",
            "secret_revealed": False,
        }
        result = _invoke(
            config_dir,
            svc,
            ["--json", "stream", "create-source", "--project", ALIAS, "--name", "s1"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["status"] == "created"
        svc.create_source.assert_called_once_with(
            alias=ALIAS,
            name="s1",
            source_type="otlp",
            branch_id=None,
            if_not_exists=False,
            reveal=False,
            provision_sinks=True,
        )

    def test_no_sinks_flag(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        svc = MagicMock()
        svc.create_source.return_value = {"status": "created", "source_id": "s1"}
        result = _invoke(
            config_dir,
            svc,
            ["--json", "stream", "create-source", "--project", ALIAS, "--name", "s1", "--no-sinks"],
        )
        assert result.exit_code == 0, result.output
        assert svc.create_source.call_args.kwargs["provision_sinks"] is False

    def test_invalid_type_rejected(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        svc = MagicMock()
        result = _invoke(
            config_dir,
            svc,
            ["stream", "create-source", "--project", ALIAS, "--name", "s1", "--type", "bogus"],
        )
        assert result.exit_code == 2


class TestDetail:
    def test_detail_masked_json(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        svc = MagicMock()
        svc.get_source_detail.return_value = {
            "source_id": "s1",
            "endpoint": "https://x/***",
            "secret_revealed": False,
            "signal_endpoints": {},
            "destination": {},
        }
        result = _invoke(config_dir, svc, ["--json", "stream", "detail", "s1", "--project", ALIAS])
        assert result.exit_code == 0, result.output
        svc.get_source_detail.assert_called_once_with(
            alias=ALIAS, source_id="s1", name=None, branch_id=None, reveal=False
        )

    def test_detail_reveal_flag(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        svc = MagicMock()
        svc.get_source_detail.return_value = {"source_id": "s1", "secret_revealed": True}
        _invoke(
            config_dir,
            svc,
            ["--json", "stream", "detail", "--project", ALIAS, "--name", "s1", "--reveal"],
        )
        svc.get_source_detail.assert_called_once_with(
            alias=ALIAS, source_id=None, name="s1", branch_id=None, reveal=True
        )


class TestDelete:
    def test_dry_run(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        svc = MagicMock()
        svc.delete_source.return_value = {
            "status": "dry_run",
            "source_id": "s1",
            "branch_id": "default",
        }
        result = _invoke(
            config_dir,
            svc,
            ["--json", "stream", "delete", "s1", "--project", ALIAS, "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["status"] == "dry_run"

    def test_confirm_abort(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        svc = MagicMock()
        result = _invoke(
            config_dir, svc, ["stream", "delete", "s1", "--project", ALIAS], input_text="n\n"
        )
        assert result.exit_code == 0
        assert "Aborted" in result.output
        svc.delete_source.assert_not_called()

    def test_yes_skips_confirm(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        svc = MagicMock()
        svc.delete_source.return_value = {
            "status": "deleted",
            "source_id": "s1",
            "branch_id": "default",
        }
        result = _invoke(config_dir, svc, ["stream", "delete", "s1", "--project", ALIAS, "--yes"])
        assert result.exit_code == 0
        svc.delete_source.assert_called_once_with(
            alias=ALIAS, source_id="s1", branch_id=None, dry_run=False
        )

    def test_api_error_exit_code(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        _seed(config_dir)
        svc = MagicMock()
        svc.list_sources.side_effect = KeboolaApiError(
            message="boom", status_code=500, error_code="API_ERROR"
        )
        result = _invoke(config_dir, svc, ["--json", "stream", "list", "--project", ALIAS])
        assert result.exit_code == 1
        assert json.loads(result.output)["status"] == "error"
