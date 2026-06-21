"""Tests for the ``data-app git-repo`` family (sandboxes-service
``/apps/{id}/git-repo/*``): service-layer orchestration + CLI dual output.

Service tests mock the Data Science client factory and assert the
camelCase->snake_case translation, the raw-array shapes, the managed-repo
credential mutex validation, and that the one-time secret surfaces only for
``http_token`` (and never leaks into the credential *list*). CLI tests patch
the cli.py service factory and assert exit codes, JSON envelopes, the
``--type`` / ``--public-key`` usage validation, and the one-time-secret print.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.data_science_client import DataScienceClient
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.data_app_git_service import DataAppGitService
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name="prod",
            project_id=5725,
        ),
    )
    return store


def _make_service(store: ConfigStore, ds_mock: MagicMock) -> DataAppGitService:
    return DataAppGitService(
        config_store=store,
        client_factory=lambda url, token: MagicMock(),
        ds_client_factory=lambda url, token: ds_mock,
    )


def _invoke(args: list[str], *, store: ConfigStore, data_app_mock: MagicMock):
    """Run the CLI with cli.py services patched to mocks."""
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ProjectService") as MockProj,
        patch("keboola_agent_cli.cli.ConfigService") as MockCfg,
        patch("keboola_agent_cli.cli.JobService") as MockJob,
        patch("keboola_agent_cli.cli.DataAppGitService") as MockDataAppGitService,
    ):
        MockStore.return_value = store
        MockProj.return_value = ProjectService(config_store=store)
        MockCfg.return_value = ConfigService(config_store=store)
        MockJob.return_value = JobService(config_store=store)
        MockDataAppGitService.return_value = data_app_mock
        return runner.invoke(app, args)


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


class TestGitRepoService:
    def test_get_git_repo_translates_fields(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        ds = MagicMock()
        ds.get_git_repo.return_value = {
            "sshUrl": None,
            "httpsUrl": "https://github.com/o/r",
            "isManagedGitRepo": False,
        }
        result = _make_service(store, ds).get_data_app_git_repo("prod", "42")
        assert result["ssh_url"] is None
        assert result["https_url"] == "https://github.com/o/r"
        assert result["is_managed_git_repo"] is False
        assert result["app_id"] == "42"
        ds.close.assert_called_once()

    def test_list_branches_normalizes_items(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        ds = MagicMock()
        ds.list_git_branches.return_value = [
            {
                "branch": "master",
                "sha": "8bd2197",
                "comment": "Make spirals more awesome",
                "author": {"name": "Thiago", "email": "t@example.com"},
                "date": "2023-11-02T12:32:17-07:00",
            }
        ]
        result = _make_service(store, ds).list_data_app_git_branches("prod", "42")
        assert result["count"] == 1
        branch = result["branches"][0]
        assert branch["branch"] == "master"
        assert branch["author"]["name"] == "Thiago"
        assert branch["author"]["email"] == "t@example.com"

    def test_list_entrypoints_passes_through(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        ds = MagicMock()
        ds.list_git_entrypoints.return_value = ["streamlit_app.py", "app.py"]
        result = _make_service(store, ds).list_data_app_git_entrypoints("prod", "42")
        assert result["entrypoints"] == ["streamlit_app.py", "app.py"]
        assert result["count"] == 2

    def test_list_credentials_never_leaks_secret(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        ds = MagicMock()
        ds.list_git_credentials.return_value = {
            "credentials": [
                {
                    "id": "uuid-1",
                    "type": "http_token",
                    "name": "ci",
                    "permissions": "readOnly",
                    "ownerAdminId": "7",
                    "createdAt": "2026-06-13T00:00:00+00:00",
                    # A hostile/forward-compatible server adding 'secret' here
                    # must NOT propagate through the normalizer.
                    "secret": "ghs_should_never_appear",
                }
            ]
        }
        result = _make_service(store, ds).list_data_app_git_credentials("prod", "42")
        assert result["count"] == 1
        cred = result["credentials"][0]
        assert cred["owner_admin_id"] == "7"
        assert cred["created_at"] == "2026-06-13T00:00:00+00:00"
        assert "secret" not in cred

    def test_create_http_token_returns_one_time_secret(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        ds = MagicMock()
        ds.create_git_credential.return_value = {
            "id": "uuid-2",
            "type": "http_token",
            "name": "ci",
            "permissions": "readOnly",
            "ownerAdminId": "7",
            "createdAt": "2026-06-13T00:00:00+00:00",
            "secret": "ghs_one_time",
        }
        result = _make_service(store, ds).create_data_app_git_credential(
            alias="prod",
            app_id="42",
            type_="http_token",
            permissions="readOnly",
            name="ci",
        )
        assert result["credential"]["secret"] == "ghs_one_time"
        # The client must be called without a publicKey for http_token.
        _, kwargs = ds.create_git_credential.call_args
        assert kwargs["public_key"] is None

    def test_create_ssh_key_has_no_secret_and_forwards_key(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        ds = MagicMock()
        ds.create_git_credential.return_value = {
            "id": "uuid-3",
            "type": "ssh_key",
            "name": "deploy",
            "permissions": "readWrite",
            "ownerAdminId": "7",
            "createdAt": "2026-06-13T00:00:00+00:00",
        }
        result = _make_service(store, ds).create_data_app_git_credential(
            alias="prod",
            app_id="42",
            type_="ssh_key",
            permissions="readWrite",
            public_key="ssh-ed25519 AAAA...",
        )
        assert "secret" not in result["credential"]
        _, kwargs = ds.create_git_credential.call_args
        assert kwargs["public_key"] == "ssh-ed25519 AAAA..."

    def test_create_ssh_key_without_public_key_rejected(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        ds = MagicMock()
        service = _make_service(store, ds)
        try:
            service.create_data_app_git_credential(
                alias="prod", app_id="42", type_="ssh_key", permissions="readOnly"
            )
            raise AssertionError("expected KeboolaApiError")
        except KeboolaApiError as exc:
            assert exc.error_code == ErrorCode.INVALID_ARGUMENT
        ds.create_git_credential.assert_not_called()

    def test_create_http_token_with_public_key_rejected(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        ds = MagicMock()
        service = _make_service(store, ds)
        try:
            service.create_data_app_git_credential(
                alias="prod",
                app_id="42",
                type_="http_token",
                permissions="readOnly",
                public_key="ssh-ed25519 AAAA...",
            )
            raise AssertionError("expected KeboolaApiError")
        except KeboolaApiError as exc:
            assert exc.error_code == ErrorCode.INVALID_ARGUMENT
        ds.create_git_credential.assert_not_called()

    def test_create_invalid_type_rejected(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        ds = MagicMock()
        service = _make_service(store, ds)
        try:
            service.create_data_app_git_credential(
                alias="prod", app_id="42", type_="pgp", permissions="readOnly"
            )
            raise AssertionError("expected KeboolaApiError")
        except KeboolaApiError as exc:
            assert exc.error_code == ErrorCode.INVALID_ARGUMENT
        ds.create_git_credential.assert_not_called()


# ---------------------------------------------------------------------------
# CLI layer
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
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


class TestGitRepoCli:
    def test_git_repo_json(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        mock = MagicMock()
        mock.get_data_app_git_repo.return_value = {
            "project_alias": "prod",
            "app_id": "42",
            "ssh_url": None,
            "https_url": "https://github.com/o/r",
            "is_managed_git_repo": False,
        }
        result = _invoke(
            ["--json", "data-app", "git-repo", "--project", "prod", "--app-id", "42"],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["data"]["https_url"] == "https://github.com/o/r"
        assert body["data"]["is_managed_git_repo"] is False

    def test_git_repo_api_error_exit_1(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        mock = MagicMock()
        mock.get_data_app_git_repo.side_effect = KeboolaApiError(
            message='App "42" has no Git repository configured',
            status_code=409,
            error_code=ErrorCode.API_ERROR,
            retryable=False,
        )
        result = _invoke(
            ["--json", "data-app", "git-repo", "--project", "prod", "--app-id", "42"],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 1, result.output
        body = json.loads(result.output)
        assert body["status"] == "error"

    def test_git_branches_human(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        mock = MagicMock()
        mock.list_data_app_git_branches.return_value = {
            "project_alias": "prod",
            "app_id": "42",
            "branches": [
                {
                    "branch": "master",
                    "sha": "8bd2197",
                    "comment": "Make spirals more awesome",
                    "author": {"name": "Thiago", "email": "t@example.com"},
                    "date": "2023-11-02T12:32:17-07:00",
                }
            ],
            "count": 1,
        }
        result = _invoke(
            ["data-app", "git-branches", "--project", "prod", "--app-id", "42"],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output
        assert "master" in result.output
        assert "8bd2197" in result.output

    def test_git_entrypoints_json(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        mock = MagicMock()
        mock.list_data_app_git_entrypoints.return_value = {
            "project_alias": "prod",
            "app_id": "42",
            "entrypoints": ["streamlit_app.py"],
            "count": 1,
        }
        result = _invoke(
            ["--json", "data-app", "git-entrypoints", "--project", "prod", "--app-id", "42"],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["data"]["entrypoints"] == ["streamlit_app.py"]

    def test_git_credentials_empty(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        mock = MagicMock()
        mock.list_data_app_git_credentials.return_value = {
            "project_alias": "prod",
            "app_id": "42",
            "credentials": [],
            "count": 0,
        }
        result = _invoke(
            ["data-app", "git-credentials", "--project", "prod", "--app-id", "42"],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output
        assert "No git credentials" in result.output

    def test_credentials_create_ssh_key_without_public_key_exit_2(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        mock = MagicMock()
        result = _invoke(
            [
                "--json",
                "data-app",
                "git-credentials-create",
                "--project",
                "prod",
                "--app-id",
                "42",
                "--type",
                "ssh_key",
                "--permissions",
                "readOnly",
                "--yes",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 2, result.output
        mock.create_data_app_git_credential.assert_not_called()

    def test_credentials_create_http_token_with_public_key_exit_2(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        mock = MagicMock()
        result = _invoke(
            [
                "--json",
                "data-app",
                "git-credentials-create",
                "--project",
                "prod",
                "--app-id",
                "42",
                "--type",
                "http_token",
                "--permissions",
                "readOnly",
                "--public-key",
                "ssh-ed25519 AAAA...",
                "--yes",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 2, result.output
        mock.create_data_app_git_credential.assert_not_called()

    def test_credentials_create_http_token_prints_one_time_secret(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        mock = MagicMock()
        mock.create_data_app_git_credential.return_value = {
            "project_alias": "prod",
            "app_id": "42",
            "credential": {
                "id": "uuid",
                "type": "http_token",
                "name": "ci",
                "permissions": "readOnly",
                "owner_admin_id": "7",
                "created_at": "2026-06-13T00:00:00+00:00",
                "secret": "ghs_one_time",
            },
            "message": "Created http_token credential.",
        }
        result = _invoke(
            [
                "data-app",
                "git-credentials-create",
                "--project",
                "prod",
                "--app-id",
                "42",
                "--type",
                "http_token",
                "--permissions",
                "readOnly",
                "--yes",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output
        assert "One-time secret" in result.output
        assert "ghs_one_time" in result.output
        _, kwargs = mock.create_data_app_git_credential.call_args
        assert kwargs["type_"] == "http_token"
        assert kwargs["public_key"] is None

    def test_credentials_create_ssh_key_from_file(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        key_file = tmp_path / "deploy.pub"
        key_file.write_text("ssh-ed25519 AAAAfromfile\n", encoding="utf-8")
        mock = MagicMock()
        mock.create_data_app_git_credential.return_value = {
            "project_alias": "prod",
            "app_id": "42",
            "credential": {
                "id": "uuid",
                "type": "ssh_key",
                "name": "",
                "permissions": "readWrite",
                "owner_admin_id": "7",
                "created_at": "2026-06-13T00:00:00+00:00",
            },
            "message": "Created ssh_key credential.",
        }
        result = _invoke(
            [
                "--json",
                "data-app",
                "git-credentials-create",
                "--project",
                "prod",
                "--app-id",
                "42",
                "--type",
                "ssh_key",
                "--permissions",
                "readWrite",
                "--public-key-file",
                str(key_file),
                "--yes",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output
        _, kwargs = mock.create_data_app_git_credential.call_args
        assert kwargs["public_key"] == "ssh-ed25519 AAAAfromfile"

    def test_credentials_create_invalid_type_exit_2(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        mock = MagicMock()
        result = _invoke(
            [
                "--json",
                "data-app",
                "git-credentials-create",
                "--project",
                "prod",
                "--app-id",
                "42",
                "--type",
                "pgp",
                "--permissions",
                "readOnly",
                "--yes",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 2, result.output
        mock.create_data_app_git_credential.assert_not_called()


# ---------------------------------------------------------------------------
# Client HTTP layer (via httpx_mock): URL composition + raw-shape passthrough
# ---------------------------------------------------------------------------


class TestGitRepoClient:
    DATA_SCIENCE_BASE = "https://data-science.keboola.com"

    def _client(self) -> DataScienceClient:
        return DataScienceClient(
            stack_url="https://connection.keboola.com",
            token="901-test-token",
        )

    def test_get_git_repo_url_and_shape(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{self.DATA_SCIENCE_BASE}/apps/42/git-repo",
            json={"sshUrl": None, "httpsUrl": "https://github.com/o/r", "isManagedGitRepo": True},
            status_code=200,
        )
        with self._client() as client:
            repo = client.get_git_repo("42")
        assert repo["isManagedGitRepo"] is True

    def test_list_branches_returns_raw_array(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{self.DATA_SCIENCE_BASE}/apps/42/git-repo/branches",
            json=[{"branch": "master", "sha": "abc", "author": {"name": "x", "email": "y"}}],
            status_code=200,
        )
        with self._client() as client:
            branches = client.list_git_branches("42")
        assert isinstance(branches, list)
        assert branches[0]["branch"] == "master"

    def test_list_entrypoints_returns_raw_string_array(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{self.DATA_SCIENCE_BASE}/apps/42/git-repo/entrypoints",
            json=["streamlit_app.py", "app.py"],
            status_code=200,
        )
        with self._client() as client:
            entrypoints = client.list_git_entrypoints("42")
        assert entrypoints == ["streamlit_app.py", "app.py"]

    def test_list_credentials_wrapped(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{self.DATA_SCIENCE_BASE}/apps/42/git-repo/credentials",
            json={"credentials": [{"id": "u1", "type": "ssh_key"}]},
            status_code=200,
        )
        with self._client() as client:
            payload = client.list_git_credentials("42")
        assert payload["credentials"][0]["id"] == "u1"

    def test_create_app_managed_sends_flag(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{self.DATA_SCIENCE_BASE}/apps",
            method="POST",
            json={"id": "99", "configId": "ulid", "hasManagedGitRepo": True},
            status_code=200,
        )
        with self._client() as client:
            client.create_app(
                type_="python-js",
                name="App",
                description="",
                config={"parameters": {}},
                use_managed_git_repo=True,
            )
        sent = json.loads(httpx_mock.get_requests()[0].content)
        assert sent["useManagedGitRepo"] is True

    def test_create_app_external_omits_flag(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{self.DATA_SCIENCE_BASE}/apps",
            method="POST",
            json={"id": "99", "configId": "ulid"},
            status_code=200,
        )
        with self._client() as client:
            client.create_app(
                type_="python-js",
                name="App",
                description="",
                config={"parameters": {}},
            )
        sent = json.loads(httpx_mock.get_requests()[0].content)
        assert "useManagedGitRepo" not in sent

    def test_list_app_runs_returns_array_with_failure_reason(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{self.DATA_SCIENCE_BASE}/apps/42/runs?limit=5&offset=0",
            json=[
                {
                    "id": "run-1",
                    "state": "failed",
                    "failureReason": {"reason": "StartupProbeFailed", "message": "clone failed"},
                }
            ],
            status_code=200,
        )
        with self._client() as client:
            runs = client.list_app_runs("42")
        assert runs[0]["state"] == "failed"
        assert runs[0]["failureReason"]["reason"] == "StartupProbeFailed"

    def test_create_ssh_key_sends_public_key(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{self.DATA_SCIENCE_BASE}/apps/42/git-repo/credentials",
            method="POST",
            json={"id": "u2", "type": "ssh_key", "permissions": "readOnly"},
            status_code=201,
        )
        with self._client() as client:
            client.create_git_credential(
                "42", type_="ssh_key", permissions="readOnly", public_key="ssh-ed25519 AAAA"
            )
        sent = json.loads(httpx_mock.get_requests()[0].content)
        assert sent == {
            "type": "ssh_key",
            "permissions": "readOnly",
            "publicKey": "ssh-ed25519 AAAA",
        }

    def test_create_http_token_omits_public_key(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{self.DATA_SCIENCE_BASE}/apps/42/git-repo/credentials",
            method="POST",
            json={"id": "u3", "type": "http_token", "secret": "ghs_x"},
            status_code=201,
        )
        with self._client() as client:
            created = client.create_git_credential(
                "42", type_="http_token", permissions="readWrite", name="ci"
            )
        sent = json.loads(httpx_mock.get_requests()[0].content)
        assert "publicKey" not in sent
        assert sent == {"type": "http_token", "permissions": "readWrite", "name": "ci"}
        assert created["secret"] == "ghs_x"
