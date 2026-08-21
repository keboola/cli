"""CLI-layer tests for the ``data-app`` command group via CliRunner.

Mirrors the test_workspace_cli.py pattern: patch the cli.py service factory
so the runner sees a MagicMock; assert exit codes, JSON envelopes, and the
mutual-exclusion validation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()


def _setup_config(config_dir: Path, projects: dict[str, dict] | None = None) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    if projects:
        for alias, info in projects.items():
            store.add_project(
                alias,
                ProjectConfig(
                    stack_url=info.get("stack_url", "https://connection.keboola.com"),
                    token=info["token"],
                    project_name=info.get("project_name", alias),
                    project_id=info.get("project_id", 1234),
                ),
            )
    return store


def _invoke(
    args: list[str],
    *,
    store: ConfigStore,
    data_app_mock: MagicMock,
):
    """Run the CLI with cli.py services patched to mocks."""
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


# ---------------------------------------------------------------------------
# data-app list
# ---------------------------------------------------------------------------


class TestDataAppList:
    def test_json_success(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
        mock = MagicMock()
        mock.list_data_apps.return_value = {
            "apps": [
                {
                    "project_alias": "prod",
                    "app_id": "42",
                    "config_id": "ulid",
                    "name": "App",
                    "type": "python-js",
                    "state": "running",
                    "desired_state": "running",
                    "config_version": "3",
                    "url": "https://x.hub.example.com",
                }
            ],
            "errors": [],
        }
        result = _invoke(
            ["--json", "data-app", "list", "--project", "prod"],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["status"] == "ok"
        assert body["data"]["apps"][0]["app_id"] == "42"
        assert body["data"]["apps"][0]["config_id"] == "ulid"


# ---------------------------------------------------------------------------
# data-app create -- mutual-exclusion validation (CLI layer)
# ---------------------------------------------------------------------------


class TestDataAppCreateValidation:
    def test_pat_modes_mutually_exclusive(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
        mock = MagicMock()
        result = _invoke(
            [
                "--json",
                "data-app",
                "create",
                "--project",
                "prod",
                "--name",
                "App",
                "--slug",
                "my-app",
                "--git-repo",
                "https://github.com/o/r",
                "--git-username",
                "user",
                "--git-pat-env",
                "X",
                "--git-pat-encrypted",
                "KBC::Project::abc",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 2, result.output
        body = json.loads(result.output)
        assert body["status"] == "error"
        assert body["error"]["code"] == "USAGE_ERROR"
        mock.create_data_app.assert_not_called()

    def test_managed_and_git_repo_mutually_exclusive(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
        mock = MagicMock()
        result = _invoke(
            [
                "--json",
                "data-app",
                "create",
                "--project",
                "prod",
                "--name",
                "App",
                "--slug",
                "my-app",
                "--use-managed-git-repo",
                "--git-repo",
                "https://github.com/o/r",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 2, result.output
        body = json.loads(result.output)
        assert body["error"]["code"] == "USAGE_ERROR"
        mock.create_data_app.assert_not_called()

    def test_no_git_source_rejected(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
        mock = MagicMock()
        result = _invoke(
            [
                "--json",
                "data-app",
                "create",
                "--project",
                "prod",
                "--name",
                "App",
                "--slug",
                "my-app",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 2, result.output
        body = json.loads(result.output)
        assert body["error"]["code"] == "USAGE_ERROR"
        mock.create_data_app.assert_not_called()

    def test_managed_repo_passes_flag_to_service(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
        mock = MagicMock()
        mock.create_data_app.return_value = {
            "dry_run": True,
            "project_alias": "prod",
            "use_managed_git_repo": True,
            "requests": {"post_apps": {}, "put_storage_config": {}, "patch_apps": {}},
            "message": "Dry run -- no API calls made.",
        }
        result = _invoke(
            [
                "data-app",
                "create",
                "--project",
                "prod",
                "--name",
                "App",
                "--slug",
                "my-app",
                "--use-managed-git-repo",
                "--auth",
                "public",
                "--dry-run",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output
        assert mock.create_data_app.call_args.kwargs["use_managed_git_repo"] is True
        assert mock.create_data_app.call_args.kwargs["git_repo"] == ""

    def test_missing_pat_env_var_rejected(self, tmp_path: Path, monkeypatch) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
        mock = MagicMock()
        monkeypatch.delenv("MISSING_PAT", raising=False)
        result = _invoke(
            [
                "--json",
                "data-app",
                "create",
                "--project",
                "prod",
                "--name",
                "App",
                "--slug",
                "my-app",
                "--git-repo",
                "https://github.com/o/r",
                "--git-username",
                "user",
                "--git-pat-env",
                "MISSING_PAT",
            ],
            store=store,
            data_app_mock=mock,
        )
        # typer.BadParameter -> exit code 2
        assert result.exit_code == 2

    def test_workspace_defaults_on_and_can_be_disabled(self, tmp_path: Path) -> None:
        """AI-3753: Storage access is on by default; --no-workspace opts out."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
        base_args = [
            "data-app",
            "create",
            "--project",
            "prod",
            "--name",
            "App",
            "--slug",
            "my-app",
            "--git-repo",
            "https://github.com/o/r",
            "--git-public",
            "--auth",
            "public",
            "--dry-run",
        ]
        payload = {
            "dry_run": True,
            "project_alias": "prod",
            "requests": {"post_apps": {}, "put_storage_config": {}, "patch_apps": {}},
            "message": "Dry run -- no API calls made.",
        }

        mock = MagicMock()
        mock.create_data_app.return_value = payload
        result = _invoke(base_args, store=store, data_app_mock=mock)
        assert result.exit_code == 0, result.output
        assert mock.create_data_app.call_args.kwargs["workspace"] is True

        mock = MagicMock()
        mock.create_data_app.return_value = payload
        result = _invoke([*base_args, "--no-workspace"], store=store, data_app_mock=mock)
        assert result.exit_code == 0, result.output
        assert mock.create_data_app.call_args.kwargs["workspace"] is False

    def test_disabled_storage_access_is_called_out_in_human_output(self, tmp_path: Path) -> None:
        """A dead data path must never be a silent outcome (AI-3753)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
        mock = MagicMock()
        mock.create_data_app.return_value = {
            "app_id": "42",
            "config_id": "ulid",
            "workspace": False,
            "state": "created",
            "desired_state": "stopped",
            "message": "created",
        }
        result = _invoke(
            [
                "data-app",
                "create",
                "--project",
                "prod",
                "--name",
                "App",
                "--slug",
                "my-app",
                "--git-repo",
                "https://github.com/o/r",
                "--git-public",
                "--auth",
                "public",
                "--no-workspace",
                "--no-deploy",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output
        assert "DISABLED" in result.output
        assert "WORKSPACE_ID" in result.output

    def test_enabled_storage_access_reported_in_human_output(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
        mock = MagicMock()
        mock.create_data_app.return_value = {
            "app_id": "42",
            "config_id": "ulid",
            "workspace": True,
            "state": "created",
            "desired_state": "stopped",
            "message": "created",
        }
        result = _invoke(
            [
                "data-app",
                "create",
                "--project",
                "prod",
                "--name",
                "App",
                "--slug",
                "my-app",
                "--git-repo",
                "https://github.com/o/r",
                "--git-public",
                "--auth",
                "public",
                "--no-deploy",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output
        assert "runtime.workspace.enabled=true" in result.output

    def test_dry_run_human_output(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
        mock = MagicMock()
        mock.create_data_app.return_value = {
            "dry_run": True,
            "project_alias": "prod",
            "requests": {
                "post_apps": {},
                "put_storage_config": {},
                "patch_apps": {},
            },
            "message": "Dry run -- no API calls made.",
        }
        result = _invoke(
            [
                "data-app",
                "create",
                "--project",
                "prod",
                "--name",
                "App",
                "--slug",
                "my-app",
                "--git-repo",
                "https://github.com/o/r",
                "--git-public",
                "--auth",
                "public",
                "--dry-run",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output
        assert "DRY RUN" in result.output


# ---------------------------------------------------------------------------
# data-app deploy
# ---------------------------------------------------------------------------


class TestDataAppDeploy:
    def test_deploy_success(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
        mock = MagicMock()
        mock.deploy_data_app.return_value = {
            "project_alias": "prod",
            "app_id": "42",
            "action": "deploy",
            "state": "starting",
            "desired_state": "running",
            "config_version": "5",
            "url": "https://x.hub.example.com",
            "message": "Data app 42 deploy requested.",
        }
        result = _invoke(
            [
                "--json",
                "data-app",
                "deploy",
                "--project",
                "prod",
                "--app-id",
                "42",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0
        body = json.loads(result.output)
        assert body["data"]["config_version"] == "5"
        mock.deploy_data_app.assert_called_once()

    def test_api_error_exit_code(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
        mock = MagicMock()
        mock.deploy_data_app.side_effect = KeboolaApiError(
            message="boom",
            status_code=500,
            error_code="API_ERROR",
            retryable=False,
        )
        result = _invoke(
            [
                "--json",
                "data-app",
                "deploy",
                "--project",
                "prod",
                "--app-id",
                "42",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 1
        body = json.loads(result.output)
        assert body["error"]["code"] == "API_ERROR"


# ---------------------------------------------------------------------------
# data-app delete (confirmation)
# ---------------------------------------------------------------------------


class TestDataAppDelete:
    def test_delete_with_yes(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
        mock = MagicMock()
        mock.delete_data_app.return_value = {
            "project_alias": "prod",
            "app_id": "42",
            "deleted": True,
            "message": "Data app 42 deleted.",
        }
        result = _invoke(
            [
                "--json",
                "data-app",
                "delete",
                "--project",
                "prod",
                "--app-id",
                "42",
                "--yes",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0
        mock.delete_data_app.assert_called_once_with(alias="prod", app_id="42")


# ---------------------------------------------------------------------------
# data-app password (manage token)
# ---------------------------------------------------------------------------


class TestDataAppPassword:
    def test_password_success(self, tmp_path: Path, monkeypatch) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
        mock = MagicMock()
        mock.get_data_app_password.return_value = {
            "project_alias": "prod",
            "app_id": "42",
            "password": "deadbeefcafe",
            "message": "Retrieved.",
        }
        monkeypatch.setenv("KBC_MANAGE_API_TOKEN", "manage-token")
        result = _invoke(
            [
                "--allow-env-manage-token",
                "--json",
                "data-app",
                "password",
                "--project",
                "prod",
                "--app-id",
                "42",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0
        body = json.loads(result.output)
        assert body["data"]["password"] == "deadbeefcafe"
        # The Manage token should have been forwarded but never logged.
        assert "manage-token" not in result.output
        mock.get_data_app_password.assert_called_once_with(
            alias="prod", app_id="42", manage_token="manage-token"
        )

    def test_password_missing_manage_token_no_tty(self, tmp_path: Path, monkeypatch) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
        mock = MagicMock()
        monkeypatch.delenv("KBC_MANAGE_API_TOKEN", raising=False)
        result = _invoke(
            [
                "--json",
                "data-app",
                "password",
                "--project",
                "prod",
                "--app-id",
                "42",
            ],
            store=store,
            data_app_mock=mock,
        )
        # CliRunner stdin is non-TTY, so resolve_manage_token returns exit 2.
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Generic error mapping (ConfigError -> 5)
# ---------------------------------------------------------------------------


class TestDataAppErrorMapping:
    def test_config_error_maps_to_exit_5(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
        mock = MagicMock()
        mock.list_data_apps.side_effect = ConfigError("Project 'foo' not found.")
        result = _invoke(
            ["--json", "data-app", "list", "--project", "foo"],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 5
        body = json.loads(result.output)
        assert body["error"]["code"] == "CONFIG_ERROR"


# ---------------------------------------------------------------------------
# data-app logs
# ---------------------------------------------------------------------------


class TestDataAppLogs:
    """CLI-layer tests for ``data-app logs``.

    Service is mocked; tests cover the command's own job (argument
    parsing, mutex enforcement, --since validation, default-translation,
    error -> exit-code mapping, dual JSON/human output).
    """

    SAMPLE_LOGS = (
        "[TIMING] Starting: git_clone\nCloning into '/app'...\nsupervisord started with pid 1\n"
    )

    def _setup(
        self, tmp_path: Path, *, lines_returned: int = 3, text: str | None = None
    ) -> tuple[Any, MagicMock]:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
        mock = MagicMock()
        mock.get_app_logs.return_value = {
            "project_alias": "prod",
            "app_id": "42",
            "lines_requested": 500,
            "since_requested": None,
            "lines_returned": lines_returned,
            "text": self.SAMPLE_LOGS if text is None else text,
        }
        return store, mock

    def test_logs_human_output(self, tmp_path: Path) -> None:
        store, mock = self._setup(tmp_path)
        result = _invoke(
            ["data-app", "logs", "--project", "prod", "--app-id", "42"],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output
        # Header carries the project, app id, and line count
        assert "42" in result.output
        assert "prod" in result.output
        assert "3 lines" in result.output
        # Body lines are emitted verbatim
        assert "[TIMING] Starting: git_clone" in result.output
        assert "supervisord started with pid 1" in result.output
        # Default --lines=500 is translated to the service call
        mock.get_app_logs.assert_called_once_with(alias="prod", app_id="42", lines=500, since=None)

    def test_logs_json_output(self, tmp_path: Path) -> None:
        store, mock = self._setup(tmp_path)
        result = _invoke(
            ["--json", "data-app", "logs", "--project", "prod", "--app-id", "42"],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["status"] == "ok"
        data = body["data"]
        assert data["app_id"] == "42"
        assert data["project_alias"] == "prod"
        assert data["lines_returned"] == 3
        assert data["lines_requested"] == 500
        assert data["since_requested"] is None
        assert "[TIMING] Starting: git_clone" in data["text"]

    def test_logs_mutex_lines_and_since(self, tmp_path: Path) -> None:
        store, mock = self._setup(tmp_path)
        result = _invoke(
            [
                "--json",
                "data-app",
                "logs",
                "--project",
                "prod",
                "--app-id",
                "42",
                "--lines",
                "100",
                "--since",
                "2026-05-21T13:00:00Z",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 2, result.output
        body = json.loads(result.output)
        assert body["error"]["code"] == "USAGE_ERROR"
        assert "mutually exclusive" in body["error"]["message"]
        mock.get_app_logs.assert_not_called()

    def test_logs_since_invalid_format(self, tmp_path: Path) -> None:
        store, mock = self._setup(tmp_path)
        result = _invoke(
            [
                "--json",
                "data-app",
                "logs",
                "--project",
                "prod",
                "--app-id",
                "42",
                "--since",
                "yesterday",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 2, result.output
        body = json.loads(result.output)
        assert body["error"]["code"] == "USAGE_ERROR"
        assert "ISO 8601" in body["error"]["message"]
        mock.get_app_logs.assert_not_called()

    def test_logs_since_naive_datetime(self, tmp_path: Path) -> None:
        store, mock = self._setup(tmp_path)
        result = _invoke(
            [
                "--json",
                "data-app",
                "logs",
                "--project",
                "prod",
                "--app-id",
                "42",
                "--since",
                "2026-05-21T13:00:00",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 2, result.output
        body = json.loads(result.output)
        assert body["error"]["code"] == "USAGE_ERROR"
        assert "timezone" in body["error"]["message"]
        mock.get_app_logs.assert_not_called()

    def test_logs_lines_zero_passes_none_to_service(self, tmp_path: Path) -> None:
        """--lines 0 is the explicit "no server-side cap" escape hatch."""
        store, mock = self._setup(tmp_path)
        mock.get_app_logs.return_value = {
            "project_alias": "prod",
            "app_id": "42",
            "lines_requested": None,
            "since_requested": None,
            "lines_returned": 3,
            "text": self.SAMPLE_LOGS,
        }
        result = _invoke(
            [
                "--json",
                "data-app",
                "logs",
                "--project",
                "prod",
                "--app-id",
                "42",
                "--lines",
                "0",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output
        mock.get_app_logs.assert_called_once_with(alias="prod", app_id="42", lines=None, since=None)

    def test_logs_lines_negative_rejected(self, tmp_path: Path) -> None:
        store, mock = self._setup(tmp_path)
        result = _invoke(
            [
                "--json",
                "data-app",
                "logs",
                "--project",
                "prod",
                "--app-id",
                "42",
                "--lines",
                "-5",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 2, result.output
        body = json.loads(result.output)
        assert body["error"]["code"] == "USAGE_ERROR"
        mock.get_app_logs.assert_not_called()

    def test_logs_api_error_exit_1(self, tmp_path: Path) -> None:
        """A 400 'App not running' from the server surfaces as exit 1 with the message verbatim."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
        mock = MagicMock()
        mock.get_app_logs.side_effect = KeboolaApiError(
            message='App "42" is not running',
            status_code=400,
            error_code="API_ERROR",
            retryable=False,
        )
        result = _invoke(
            ["--json", "data-app", "logs", "--project", "prod", "--app-id", "42"],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 1, result.output
        body = json.loads(result.output)
        assert body["error"]["code"] == "API_ERROR"
        assert "is not running" in body["error"]["message"]

    def test_logs_with_since_passes_through(self, tmp_path: Path) -> None:
        store, mock = self._setup(tmp_path)
        mock.get_app_logs.return_value = {
            "project_alias": "prod",
            "app_id": "42",
            "lines_requested": None,
            "since_requested": "2026-05-21T13:00:00+00:00",
            "lines_returned": 3,
            "text": self.SAMPLE_LOGS,
        }
        result = _invoke(
            [
                "--json",
                "data-app",
                "logs",
                "--project",
                "prod",
                "--app-id",
                "42",
                "--since",
                "2026-05-21T13:00:00+00:00",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output
        mock.get_app_logs.assert_called_once_with(
            alias="prod", app_id="42", lines=None, since="2026-05-21T13:00:00+00:00"
        )


class TestDataAppRuns:
    def test_runs_json(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
        mock = MagicMock()
        mock.list_app_runs.return_value = {
            "project_alias": "prod",
            "app_id": "42",
            "count": 1,
            "runs": [
                {
                    "id": "run-1",
                    "state": "failed",
                    "failure_reason": {"reason": "StartupProbeFailed", "message": "clone failed"},
                }
            ],
        }
        result = _invoke(
            ["--json", "data-app", "runs", "--project", "prod", "--app-id", "42", "--limit", "3"],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["data"]["runs"][0]["failure_reason"]["reason"] == "StartupProbeFailed"
        mock.list_app_runs.assert_called_once_with("prod", "42", limit=3)
