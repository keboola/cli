"""Regression tests for CLI commands using unreachable service hosts."""

import json

import httpx
import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import ProjectConfig

runner = CliRunner()
STACK_URL = "https://connection.example.invalid"


@pytest.fixture
def cli_args(config_store: ConfigStore, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr("keboola_agent_cli.http_base.BACKOFF_BASE", 0)
    config_store.add_project(
        "repro",
        ProjectConfig(
            stack_url=STACK_URL, token="123-456-fake", project_id=456, project_name="Repro"
        ),
    )
    return ["--json", "--config-dir", str(config_store.config_dir)]


def test_encrypt_values_unreachable(cli_args: list[str], httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(
        httpx.ProxyError("502 Upstream unreachable"),
        method="POST",
        url="https://encryption.example.invalid/encrypt?projectId=456&componentId=keboola.ex-http",
        is_reusable=True,
    )
    result = runner.invoke(
        app,
        [
            *cli_args,
            "encrypt",
            "values",
            "--project",
            "repro",
            "--component-id",
            "keboola.ex-http",
            "--values",
            '{"#secret":"synthetic"}',
        ],
    )

    assert result.exit_code == 4, result.output
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "CONNECTION_ERROR"
    assert error["error_type"] == "network"
    assert "encryption.example.invalid" in error["message"]
    assert "Traceback" not in result.stderr


def test_schedule_remove_unreachable(cli_args: list[str], httpx_mock: HTTPXMock) -> None:
    configs_url = f"{STACK_URL}/v2/storage/components/keboola.scheduler/configs"
    httpx_mock.add_response(
        method="GET",
        url=configs_url,
        json=[
            {
                "id": "sched-1",
                "configuration": {
                    "target": {"componentId": "keboola.flow", "configurationId": "flow-1"}
                },
            }
        ],
    )
    httpx_mock.add_exception(
        httpx.ProxyError("502 Upstream unreachable"),
        method="DELETE",
        url="https://scheduler.example.invalid/configurations/sched-1",
        is_reusable=True,
    )
    result = runner.invoke(
        app,
        [
            *cli_args,
            "flow",
            "schedule-remove",
            "--project",
            "repro",
            "--flow-id",
            "flow-1",
            "--yes",
        ],
    )

    assert result.exit_code == 4, result.output
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "CONNECTION_ERROR"
    assert error["error_type"] == "network"
    assert "scheduler.example.invalid" in error["message"]
    assert "Traceback" not in result.stderr
    assert not httpx_mock.get_requests(method="DELETE", url=f"{configs_url}/sched-1")


def test_config_new_push_unreachable_schema(
    cli_args: list[str], httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
) -> None:
    httpx_mock.add_exception(
        httpx.ProxyError("502 Upstream unreachable"),
        method="GET",
        url="https://ai.example.invalid/docs/components/keboola.ex-http",
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{STACK_URL}/v2/storage/components/keboola.ex-http/configs",
        status_code=201,
        json={"id": "config-1"},
    )
    result = runner.invoke(
        app,
        [
            *cli_args,
            "config",
            "new",
            "--project",
            "repro",
            "--component-id",
            "keboola.ex-http",
            "--name",
            "repro",
            "--push",
            "--no-files",
            "--configuration",
            '{"parameters":{}}',
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["id"] == "config-1"
    assert data["validation_status"] == "skipped"
    assert data["validation_errors"] == []
    assert "Schema validation skipped for keboola.ex-http" in caplog.text
    assert "Traceback" not in result.stderr
