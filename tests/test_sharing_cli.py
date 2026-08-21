"""Tests for `kbagent sharing link` command wiring via CliRunner.

Covers the `--stage` flag added for MCP `link_shared_bucket` parity: the MCP
tool derives the stage from the source bucket, kbagent keeps "in" as the
default and lets the caller opt into "out" explicitly.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.project_service import ProjectService
from keboola_agent_cli.services.sharing_service import SharingService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()


def _make_store(config_dir: Path) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name="Prod",
            project_id=1234,
        ),
    )
    return store


def _run_link(config_dir: Path, extra_args: list[str], mock_client: MagicMock):
    store = _make_store(config_dir)
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
        patch("keboola_agent_cli.cli.SharingService") as MockSharingService,
    ):
        MockStore.return_value = store
        MockProjService.return_value = ProjectService(config_store=store)
        MockSharingService.return_value = SharingService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )
        return runner.invoke(
            app,
            [
                "--json",
                "sharing",
                "link",
                "--project",
                "prod",
                "--source-project-id",
                "999",
                "--bucket-id",
                "out.c-data",
                *extra_args,
            ],
        )


class TestSharingLinkStage:
    """`sharing link --stage` reaches the Storage API."""

    def test_stage_out_is_forwarded(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = MagicMock()
        mock_client.link_bucket.return_value = {
            "status": "success",
            "results": {"id": "out.c-shared-data"},
        }

        result = _run_link(config_dir, ["--stage", "out"], mock_client)

        assert result.exit_code == 0, result.output
        assert mock_client.link_bucket.call_args.kwargs["stage"] == "out"
        assert json.loads(result.output)["data"]["stage"] == "out"
        mock_client.close.assert_called_once()

    def test_stage_defaults_to_in(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = MagicMock()
        mock_client.link_bucket.return_value = {
            "status": "success",
            "results": {"id": "in.c-shared-data"},
        }

        result = _run_link(config_dir, [], mock_client)

        assert result.exit_code == 0, result.output
        assert mock_client.link_bucket.call_args.kwargs["stage"] == "in"

    def test_invalid_stage_exits_2_without_api_call(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = MagicMock()

        result = _run_link(config_dir, ["--stage", "staging"], mock_client)

        assert result.exit_code == 2, result.output
        assert json.loads(result.output)["error"]["code"] == "INVALID_ARGUMENT"
        mock_client.link_bucket.assert_not_called()
