"""Tests for the `kbagent job` query surface added for MCP `get_jobs` parity.

Covers `job detail --log-tail-lines` (MCP `include_logs` / `log_tail_lines`)
and `job list --offset` / `--sort-by` / `--sort-order`.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from helpers import setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()

SAMPLE_EVENTS = [
    {"id": 2, "created": "2026-08-20T10:00:02Z", "message": "second"},
    {"id": 1, "created": "2026-08-20T10:00:01Z", "message": "first"},
]


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


class TestJobDetailLogTail:
    """`get_job_detail` can attach the job's log tail (MCP get_jobs include_logs)."""

    def test_log_tail_lines_attaches_events(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_job_detail.return_value = {"id": "123", "runId": "123", "status": "error"}
        mock_client.fetch_job_events.return_value = SAMPLE_EVENTS

        service = JobService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        detail = service.get_job_detail(alias="prod", job_id="123", log_tail_lines=50)

        mock_client.fetch_job_events.assert_called_once_with("123", limit=50)
        assert [e["message"] for e in detail["logTail"]] == ["second", "first"]
        mock_client.close.assert_called_once()

    def test_log_tail_off_by_default(self, tmp_config_dir: Path) -> None:
        """A plain `job detail` must not spend an extra events call."""
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_job_detail.return_value = {"id": "123", "runId": "123", "status": "error"}

        service = JobService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        detail = service.get_job_detail(alias="prod", job_id="123")

        mock_client.fetch_job_events.assert_not_called()
        assert "logTail" not in detail

    def test_events_failure_does_not_fail_the_command(self, tmp_config_dir: Path) -> None:
        """A blip on the events endpoint must not hide the job detail itself."""
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_job_detail.return_value = {"id": "123", "runId": "123", "status": "error"}
        mock_client.fetch_job_events.side_effect = RuntimeError("events down")

        service = JobService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        detail = service.get_job_detail(alias="prod", job_id="123", log_tail_lines=10)

        assert detail["id"] == "123"
        assert detail.get("logTail", []) == []

    def test_cli_flag_is_wired(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _make_store(config_dir)
        mock_client = MagicMock()
        mock_client.get_job_detail.return_value = {"id": "123", "runId": "123", "status": "error"}
        mock_client.fetch_job_events.return_value = SAMPLE_EVENTS

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockJobService.return_value = JobService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "detail",
                    "--project",
                    "prod",
                    "--job-id",
                    "123",
                    "--log-tail-lines",
                    "5",
                ],
            )

        assert result.exit_code == 0, result.output
        mock_client.fetch_job_events.assert_called_once_with("123", limit=5)
        assert len(json.loads(result.output)["data"]["logTail"]) == 2


class TestJobListPaging:
    """`job list` exposes the Queue API's offset + sort controls."""

    def test_offset_and_sort_are_forwarded(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.list_jobs.return_value = []

        service = JobService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        service.list_jobs(aliases=["prod"], offset=100, sort_by="endTime", sort_order="asc")

        kwargs = mock_client.list_jobs.call_args.kwargs
        assert kwargs["offset"] == 100
        assert kwargs["sort_by"] == "endTime"
        assert kwargs["sort_order"] == "asc"

    def test_defaults_preserve_existing_behaviour(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.list_jobs.return_value = []

        service = JobService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        service.list_jobs(aliases=["prod"])

        kwargs = mock_client.list_jobs.call_args.kwargs
        assert kwargs["offset"] == 0
        assert kwargs["sort_by"] == "startTime"
        assert kwargs["sort_order"] == "desc"

    @pytest.mark.parametrize(
        ("args", "expected_fragment"),
        [
            (["--offset", "-1"], "offset"),
            (["--sort-order", "sideways"], "sort-order"),
            (["--sort-by", "whenever"], "sort-by"),
        ],
    )
    def test_invalid_values_exit_2(
        self, tmp_path: Path, args: list[str], expected_fragment: str
    ) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _make_store(config_dir)
        mock_client = MagicMock()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockJobService.return_value = JobService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            result = runner.invoke(app, ["--json", "job", "list", *args])

        assert result.exit_code == 2, result.output
        payload = json.loads(result.output)
        assert payload["error"]["code"] == "INVALID_ARGUMENT"
        assert expected_fragment in payload["error"]["message"]
        mock_client.list_jobs.assert_not_called()
