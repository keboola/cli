"""CLI-layer tests for ``kbagent data-app update`` (issue #737).

Mirrors test_data_app_cli.py: patch the cli.py service factory so the runner
sees a MagicMock, then assert flag plumbing, exit codes and the JSON envelope.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()


def _setup_config(config_dir: Path) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name="prod",
            project_id=1234,
        ),
    )
    return store


def _invoke(args: list[str], *, store: ConfigStore, data_app_mock: MagicMock):
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ProjectService") as MockProj,
        patch("keboola_agent_cli.cli.ConfigService") as MockCfg,
        patch("keboola_agent_cli.cli.JobService") as MockJob,
        patch("keboola_agent_cli.cli.DataAppService") as MockDataAppService,
    ):
        MockStore.return_value = store
        MockProj.return_value = ProjectService(config_store=store)
        MockCfg.return_value = ConfigService(config_store=store)
        MockJob.return_value = JobService(config_store=store)
        MockDataAppService.return_value = data_app_mock
        return runner.invoke(app, args)


def _success_payload(**overrides):
    payload = {
        "project_alias": "prod",
        "app_id": "74021026",
        "config_id": "9001",
        "changed": ["workspace"],
        "changes": [{"field": "workspace", "before": False, "after": True}],
        "config_version_before": "7",
        "config_version_after": "8",
        "deploy_required": True,
        "next_step": "kbagent data-app deploy --project prod --app-id 74021026 --wait",
        "message": "1 field(s) updated on data app 74021026.",
    }
    payload.update(overrides)
    return payload


def test_update_workspace_flag_reaches_the_service(tmp_path: Path) -> None:
    store = _setup_config(tmp_path / "cfg")
    mock = MagicMock()
    mock.update_data_app.return_value = _success_payload()

    result = _invoke(
        [
            "--json",
            "data-app",
            "update",
            "--project",
            "prod",
            "--app-id",
            "74021026",
            "--workspace",
        ],
        store=store,
        data_app_mock=mock,
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["changed"] == ["workspace"]
    assert mock.update_data_app.call_args.kwargs["workspace"] is True


def test_update_no_workspace_sends_false(tmp_path: Path) -> None:
    store = _setup_config(tmp_path / "cfg")
    mock = MagicMock()
    mock.update_data_app.return_value = _success_payload()

    result = _invoke(
        [
            "data-app",
            "update",
            "--project",
            "prod",
            "--app-id",
            "74021026",
            "--no-workspace",
        ],
        store=store,
        data_app_mock=mock,
    )

    assert result.exit_code == 0, result.output
    assert mock.update_data_app.call_args.kwargs["workspace"] is False


def test_update_omitting_workspace_leaves_it_unset(tmp_path: Path) -> None:
    """No ``--workspace``/``--no-workspace`` must send ``None``, not ``False``.

    A ``False`` default would silently revoke Storage access on every
    ``--auto-suspend`` change.
    """
    store = _setup_config(tmp_path / "cfg")
    mock = MagicMock()
    mock.update_data_app.return_value = _success_payload(
        changed=["auto_suspend_after_seconds"],
        changes=[{"field": "auto_suspend_after_seconds", "before": 900, "after": 300}],
    )

    result = _invoke(
        [
            "data-app",
            "update",
            "--project",
            "prod",
            "--app-id",
            "74021026",
            "--auto-suspend",
            "300",
        ],
        store=store,
        data_app_mock=mock,
    )

    assert result.exit_code == 0, result.output
    kwargs = mock.update_data_app.call_args.kwargs
    assert kwargs["workspace"] is None
    assert kwargs["auto_suspend_after_seconds"] == 300


def test_update_passes_size_auth_git_branch_and_dry_run(tmp_path: Path) -> None:
    store = _setup_config(tmp_path / "cfg")
    mock = MagicMock()
    mock.update_data_app.return_value = _success_payload(dry_run=True)

    result = _invoke(
        [
            "data-app",
            "update",
            "--project",
            "prod",
            "--app-id",
            "74021026",
            "--size",
            "small",
            "--auth",
            "public",
            "--git-branch",
            "release",
            "--branch",
            "42",
            "--dry-run",
        ],
        store=store,
        data_app_mock=mock,
    )

    assert result.exit_code == 0, result.output
    kwargs = mock.update_data_app.call_args.kwargs
    assert kwargs["size"] == "small"
    assert kwargs["auth"] == "public"
    assert kwargs["git_branch"] == "release"
    assert kwargs["branch_id"] == 42
    assert kwargs["dry_run"] is True


def test_update_without_any_flag_is_a_usage_error(tmp_path: Path) -> None:
    """Caught in the command layer so it exits 2, not the generic 1."""
    store = _setup_config(tmp_path / "cfg")
    mock = MagicMock()

    result = _invoke(
        ["--json", "data-app", "update", "--project", "prod", "--app-id", "74021026"],
        store=store,
        data_app_mock=mock,
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "MISSING_PARAMETER"
    mock.update_data_app.assert_not_called()


def test_update_maps_service_error_to_exit_code(tmp_path: Path) -> None:
    store = _setup_config(tmp_path / "cfg")
    mock = MagicMock()
    mock.update_data_app.side_effect = KeboolaApiError(
        message="boom",
        status_code=500,
        error_code=ErrorCode.API_ERROR,
        retryable=False,
    )

    result = _invoke(
        [
            "--json",
            "data-app",
            "update",
            "--project",
            "prod",
            "--app-id",
            "74021026",
            "--workspace",
        ],
        store=store,
        data_app_mock=mock,
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "API_ERROR"


def test_detail_prints_storage_access(tmp_path: Path) -> None:
    store = _setup_config(tmp_path / "cfg")
    mock = MagicMock()
    mock.get_data_app.return_value = {
        "project_alias": "prod",
        "app_id": "74021026",
        "name": "People Review",
        "workspace_enabled": False,
        "git": {},
    }

    result = _invoke(
        ["data-app", "detail", "--project", "prod", "--app-id", "74021026"],
        store=store,
        data_app_mock=mock,
    )

    assert result.exit_code == 0, result.output
    assert "Storage access:" in result.output
    assert "disabled" in result.output
