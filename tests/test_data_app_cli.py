"""CLI-layer tests for the ``data-app`` command group via CliRunner.

Mirrors the test_workspace_cli.py pattern: patch the cli.py service factory
so the runner sees a MagicMock; assert exit codes, JSON envelopes, and the
mutual-exclusion validation.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"

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


class TestDataAppHintMode:
    """Compile-check every rendered ``--hint`` snippet for the data-app group.

    Iterations 4 and 5 each caught a hint that rendered to invalid Python:
    iteration 4 found ``os.environ[""PAT_VAR""]`` (doubled quotes) and a
    missing ``import os`` in ``create``; iteration 5 found a sibling
    ``manage_token=<KBC_MANAGE_API_TOKEN>`` placeholder reaching the
    snippet verbatim in ``password``. The class below enumerates every
    ``data-app`` subcommand and ``ast.parse``s both the client- and
    service-mode renders so this entire bug class is caught at CI rather
    than by a fresh-context reviewer.
    """

    def _setup(self, tmp_path: Path) -> tuple[ConfigStore, MagicMock]:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
        return store, MagicMock()

    def _hint(self, store: ConfigStore, mock: MagicMock, args: list[str]) -> str:
        result = _invoke(args, store=store, data_app_mock=mock)
        assert result.exit_code == 0, result.output
        return result.output

    # One sample CLI invocation per subcommand, chosen to exercise the
    # most variable args (private repo for create, --config-version for
    # deploy, etc.). Both client and service renders are compile-checked.
    _SAMPLE_INVOCATIONS: tuple[tuple[str, list[str]], ...] = (
        ("list", ["data-app", "list", "--project", "prod"]),
        (
            "detail",
            ["data-app", "detail", "--project", "prod", "--app-id", "42"],
        ),
        (
            "create",
            [
                "data-app",
                "create",
                "--project",
                "prod",
                "--name",
                "X",
                "--slug",
                "x-x",
                "--git-repo",
                "https://github.com/o/r",
                "--git-username",
                "u",
                "--git-pat-env",
                "PAT_VAR",
                "--auth",
                "password",
            ],
        ),
        (
            "deploy",
            [
                "data-app",
                "deploy",
                "--project",
                "prod",
                "--app-id",
                "42",
                "--config-version",
                "5",
            ],
        ),
        (
            "start",
            ["data-app", "start", "--project", "prod", "--app-id", "42"],
        ),
        (
            "stop",
            ["data-app", "stop", "--project", "prod", "--app-id", "42"],
        ),
        (
            "delete",
            ["data-app", "delete", "--project", "prod", "--app-id", "42"],
        ),
        (
            "password",
            ["data-app", "password", "--project", "prod", "--app-id", "42"],
        ),
    )

    @pytest.mark.parametrize(
        "name,subcommand_args",
        _SAMPLE_INVOCATIONS,
        ids=[name for name, _ in _SAMPLE_INVOCATIONS],
    )
    @pytest.mark.parametrize("mode", ["client", "service"])
    def test_hint_snippet_compiles(
        self, tmp_path: Path, mode: str, name: str, subcommand_args: list[str]
    ) -> None:
        import ast

        store, mock = self._setup(tmp_path)
        snippet = self._hint(
            store,
            mock,
            ["--hint", mode, *subcommand_args],
        )
        ast.parse(snippet)  # raises SyntaxError if the render is broken

    def test_create_service_hint_imports_os_and_quotes_pat_env(self, tmp_path: Path) -> None:
        """Anchor for the iteration-4 fix: ``import os`` is emitted and
        the ``os.environ`` access uses single quotes (no doubling)."""
        store, mock = self._setup(tmp_path)
        snippet = self._hint(
            store,
            mock,
            [
                "--hint",
                "service",
                "data-app",
                "create",
                "--project",
                "prod",
                "--name",
                "X",
                "--slug",
                "x-x",
                "--git-repo",
                "https://github.com/o/r",
                "--git-username",
                "u",
                "--git-pat-env",
                "PAT_VAR",
                "--auth",
                "password",
            ],
        )
        assert "import os" in snippet
        assert 'os.environ["PAT_VAR"]' in snippet
        # The doubled-quote bug must never recur.
        assert 'os.environ[""' not in snippet

    def test_password_service_hint_uses_os_environ(self, tmp_path: Path) -> None:
        """Anchor for the iteration-5 fix: the ``manage_token`` kwarg
        renders as a parseable ``os.environ[...]`` lookup, not a literal
        ``<KBC_MANAGE_API_TOKEN>`` placeholder."""
        store, mock = self._setup(tmp_path)
        snippet = self._hint(
            store,
            mock,
            [
                "--hint",
                "service",
                "data-app",
                "password",
                "--project",
                "prod",
                "--app-id",
                "42",
            ],
        )
        assert "manage_token=os.environ" in snippet
        assert "<KBC_MANAGE_API_TOKEN>" not in snippet


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
