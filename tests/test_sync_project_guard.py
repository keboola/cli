"""Tests for the manifest project check of sync pull/diff/push (CLI-31).

A sync tree belongs to one project: pull, diff and push refuse a manifest
whose ``project.id`` or ``apiHost`` differs from the ``--project`` project.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig, TokenVerifyResponse
from keboola_agent_cli.services.project_service import ProjectService
from keboola_agent_cli.services.sync_service import SyncService

STACK_URL = "https://connection.keboola.com"
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()


def _store(tmp_path: Path, project_id: int | None = 258) -> ConfigStore:
    store = ConfigStore(config_dir=tmp_path / "config")
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url=STACK_URL, token=TOKEN, project_name="Production", project_id=project_id
        ),
    )
    return store


def _write_manifest(
    project_root: Path, project_id: int = 258, api_host: str = "connection.keboola.com"
) -> None:
    keboola_dir = project_root / ".keboola"
    keboola_dir.mkdir(parents=True)
    manifest = {
        "version": 2,
        "project": {"id": project_id, "apiHost": api_host},
        "naming": {},
        "branches": [{"id": 12345, "path": "main"}],
        "configurations": [],
    }
    (keboola_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _client(verified_project_id: int | None = 258) -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.verify_token.return_value = TokenVerifyResponse(
        token_id="tok-001",
        token_description="kbagent-cli",
        project_id=verified_project_id,
        project_name="Production",
        owner_name="My Org",
    )
    client.list_components_with_configs.return_value = []
    client.list_dev_branches.return_value = [{"id": 12345, "name": "Main", "isDefault": True}]
    return client


def _run(svc: SyncService, operation: str, project_root: Path) -> dict:
    if operation == "pull":
        return svc.pull(alias="prod", project_root=project_root, no_storage=True, no_jobs=True)
    if operation == "diff":
        return svc.diff(alias="prod", project_root=project_root)
    return svc.push(alias="prod", project_root=project_root)


OPERATIONS = ["pull", "diff", "push"]


class TestRefuseTreeOfAnotherProject:
    """pull/diff/push compare the manifest project with the --project project."""

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_refuses_other_project_id(self, tmp_path: Path, operation: str) -> None:
        """A tree of project 999 is refused for project 258 before any API call."""
        project_root = tmp_path / "tree"
        _write_manifest(project_root, project_id=999)
        client = _client()
        svc = SyncService(config_store=_store(tmp_path), client_factory=lambda u, t: client)

        with pytest.raises(ConfigError, match=r"belongs to project 999.*'prod' is project 258"):
            _run(svc, operation, project_root)

        client.verify_token.assert_not_called()
        client.list_components_with_configs.assert_not_called()

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_refuses_other_host(self, tmp_path: Path, operation: str) -> None:
        """The same project ID on another stack is another project; both hosts are named."""
        project_root = tmp_path / "tree"
        _write_manifest(project_root, api_host="connection.eu-central-1.keboola.com")
        client = _client()
        svc = SyncService(config_store=_store(tmp_path), client_factory=lambda u, t: client)

        with pytest.raises(
            ConfigError,
            match=r"project 258 on connection\.eu-central-1\.keboola\.com.*"
            r"project 258 on connection\.keboola\.com",
        ):
            _run(svc, operation, project_root)

        client.list_components_with_configs.assert_not_called()

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_matching_tree_runs_without_token_verification(
        self, tmp_path: Path, operation: str
    ) -> None:
        """With a stored project_id the check adds no API call."""
        project_root = tmp_path / "tree"
        _write_manifest(project_root, api_host="https://Connection.keboola.com/")
        client = _client()
        svc = SyncService(config_store=_store(tmp_path), client_factory=lambda u, t: client)

        _run(svc, operation, project_root)

        client.verify_token.assert_not_called()
        client.list_components_with_configs.assert_called()


class TestAllProjects:
    """--all-projects counts a refused tree as a failed project."""

    def test_diff_all_counts_a_refused_tree_as_failed(self, tmp_path: Path) -> None:
        base_dir = tmp_path / "trees"
        _write_manifest(base_dir / "prod", project_id=999)
        client = _client()
        svc = SyncService(config_store=_store(tmp_path), client_factory=lambda u, t: client)

        result = svc.diff_all(base_dir)

        assert result["summary"]["failed"] == 1
        assert "belongs to project 999" in result["projects"]["prod"]["error"]
        client.list_components_with_configs.assert_not_called()

    def test_push_all_counts_a_refused_tree_as_failed(self, tmp_path: Path) -> None:
        base_dir = tmp_path / "trees"
        _write_manifest(base_dir / "prod", project_id=999)
        client = _client()
        svc = SyncService(config_store=_store(tmp_path), client_factory=lambda u, t: client)

        result = svc.push_all(base_dir)

        assert result["summary"]["failed"] == 1
        assert "belongs to project 999" in result["projects"]["prod"]["error"]
        client.list_components_with_configs.assert_not_called()


class TestProjectWithoutStoredId:
    """A registered project without project_id reads it from token verification."""

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_refuses_when_token_names_other_project(self, tmp_path: Path, operation: str) -> None:
        project_root = tmp_path / "tree"
        _write_manifest(project_root, project_id=999)
        client = _client(verified_project_id=258)
        svc = SyncService(
            config_store=_store(tmp_path, project_id=None), client_factory=lambda u, t: client
        )

        with pytest.raises(ConfigError, match=r"belongs to project 999.*is project 258"):
            _run(svc, operation, project_root)

        client.verify_token.assert_called_once()
        client.list_components_with_configs.assert_not_called()

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_runs_when_token_names_tree_project(self, tmp_path: Path, operation: str) -> None:
        project_root = tmp_path / "tree"
        _write_manifest(project_root, project_id=258)
        client = _client(verified_project_id=258)
        svc = SyncService(
            config_store=_store(tmp_path, project_id=None), client_factory=lambda u, t: client
        )

        _run(svc, operation, project_root)

        client.list_components_with_configs.assert_called()

    def test_failed_verification_is_config_error(self, tmp_path: Path) -> None:
        project_root = tmp_path / "tree"
        _write_manifest(project_root)
        client = _client()
        client.verify_token.side_effect = KeboolaApiError(
            message="Invalid access token", status_code=401, error_code="INVALID_TOKEN"
        )
        svc = SyncService(
            config_store=_store(tmp_path, project_id=None), client_factory=lambda u, t: client
        )

        with pytest.raises(ConfigError, match=r"token verification failed.*project edit"):
            svc.diff(alias="prod", project_root=project_root)

    def test_other_host_refused_without_token_verification(self, tmp_path: Path) -> None:
        project_root = tmp_path / "tree"
        _write_manifest(project_root, api_host="connection.north-europe.azure.keboola.com")
        client = _client()
        svc = SyncService(
            config_store=_store(tmp_path, project_id=None), client_factory=lambda u, t: client
        )

        with pytest.raises(ConfigError, match=r"on connection\.north-europe"):
            svc.diff(alias="prod", project_root=project_root)

        client.verify_token.assert_not_called()


class TestTreesWithoutProjectInfo:
    """Trees the check cannot read keep the behavior they had before it."""

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_missing_manifest_still_file_not_found(self, tmp_path: Path, operation: str) -> None:
        client = _client()
        svc = SyncService(config_store=_store(tmp_path), client_factory=lambda u, t: client)

        with pytest.raises(FileNotFoundError):
            _run(svc, operation, tmp_path / "empty")

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_manifest_without_project_still_fails_validation(
        self, tmp_path: Path, operation: str
    ) -> None:
        """``project`` is a required manifest field; such a file never loaded."""
        project_root = tmp_path / "tree"
        (project_root / ".keboola").mkdir(parents=True)
        (project_root / ".keboola" / "manifest.json").write_text(
            json.dumps({"version": 2, "naming": {}, "branches": [], "configurations": []}),
            encoding="utf-8",
        )
        client = _client()
        svc = SyncService(config_store=_store(tmp_path), client_factory=lambda u, t: client)

        with pytest.raises(ValidationError):
            _run(svc, operation, project_root)

        client.verify_token.assert_not_called()


class TestCliExitCode:
    """The refusal reaches the CLI as CONFIG_ERROR, exit 5."""

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_cli_refusal_envelope(self, tmp_path: Path, operation: str) -> None:
        project_root = tmp_path / "tree"
        _write_manifest(project_root, project_id=999)
        store = _store(tmp_path)
        client = _client()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSyncService.return_value = SyncService(
                config_store=store, client_factory=lambda u, t: client
            )
            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    operation,
                    "--project",
                    "prod",
                    "--directory",
                    str(project_root),
                ],
            )

        assert result.exit_code == 5, result.output
        data = json.loads(result.output)
        assert data["error"]["code"] == "CONFIG_ERROR"
        assert "project 999" in data["error"]["message"]
        assert "project 258" in data["error"]["message"]
        client.list_components_with_configs.assert_not_called()
