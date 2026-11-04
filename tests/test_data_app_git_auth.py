"""Tests for organization GitHub auth on data apps (``authType: organization``).

Covers ``data-app create --git-auth organization`` (validation, the Storage
body, dry-run parity), ``data-app git-auth-set`` (the switch of an existing
app that drops the stored credentials in the same write), the serve routes,
and the ``#sshKey`` redaction.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services._data_app_bodies import _redact_git_block
from keboola_agent_cli.services._data_app_git_source import (
    GitSource,
    describe_git_auth,
    plan_organization_switch,
    preview_git_block,
    validate_git_credentials,
    validate_git_flags,
    validate_organization_repository,
)
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.data_app_git_service import DataAppGitService
from keboola_agent_cli.services.data_app_service import DataAppService
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
REPO = "https://github.com/acme/dashboard"
ORG_BLOCK = {
    "repository": REPO,
    "branch": "main",
    "authType": "organization",
    "private": True,
}

runner = CliRunner()


def _make_store(tmp_path: Path) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
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


def _make_create_service(store: ConfigStore) -> tuple[DataAppService, MagicMock, MagicMock]:
    ds_mock = MagicMock()
    ds_mock.create_app.return_value = {"id": "77", "configId": "c-77", "state": "created"}
    ds_mock.create_deployment.return_value = {"id": "dep-1"}
    storage_mock = MagicMock()
    storage_mock.update_config.return_value = {"version": 2}
    encrypt_mock = MagicMock()
    service = DataAppService(
        config_store=store,
        client_factory=lambda url, token: storage_mock,
        ds_client_factory=lambda url, token: ds_mock,
        encrypt_service=encrypt_mock,
    )
    return service, storage_mock, encrypt_mock


def _create_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "alias": "prod",
        "name": "Dashboard",
        "description": "",
        "slug": "dashboard",
        "git_repo": REPO,
        "git_auth": "organization",
        "deploy": False,
    }
    kwargs.update(overrides)
    return kwargs


def _written_git_block(storage_mock: MagicMock) -> dict[str, Any]:
    configuration = storage_mock.update_config.call_args.kwargs["configuration"]
    return configuration["parameters"]["dataApp"]["git"]


# ---------------------------------------------------------------------------
# create --git-auth organization
# ---------------------------------------------------------------------------


class TestCreateWithOrganizationAuth:
    def test_writes_organization_block_without_credential(self, tmp_path: Path) -> None:
        service, storage_mock, encrypt_mock = _make_create_service(_make_store(tmp_path))

        result = service.create_data_app(**_create_kwargs())

        assert _written_git_block(storage_mock) == ORG_BLOCK
        encrypt_mock.encrypt.assert_not_called()
        assert result["git_auth"] == "organization"
        assert result["git"] == ORG_BLOCK

    def test_dry_run_body_equals_the_written_body(self, tmp_path: Path) -> None:
        service, storage_mock, _ = _make_create_service(_make_store(tmp_path))

        preview = service.create_data_app(**_create_kwargs(dry_run=True))
        service.create_data_app(**_create_kwargs())

        put_preview = preview["requests"]["put_storage_config"]
        assert put_preview["parameters"]["dataApp"]["git"] == _written_git_block(storage_mock)
        assert preview["git_auth"] == "organization"

    def test_github_enterprise_host_is_left_to_the_backend(self, tmp_path: Path) -> None:
        service, storage_mock, _ = _make_create_service(_make_store(tmp_path))

        service.create_data_app(**_create_kwargs(git_repo="https://acme.ghe.com/team/app.git"))

        assert _written_git_block(storage_mock)["repository"] == "https://acme.ghe.com/team/app.git"

    @pytest.mark.parametrize(
        ("overrides", "flag"),
        [
            ({"git_public": True}, "--git-public"),
            ({"git_username": "bob"}, "--git-username"),
            ({"git_pat_plaintext": "ghp_x"}, "--git-pat-env/--git-pat-file"),
            ({"git_pat_encrypted": "KBC::ProjectSecure::x"}, "--git-pat-encrypted"),
        ],
    )
    def test_rejects_any_credential_or_public_flag(
        self, tmp_path: Path, overrides: dict[str, Any], flag: str
    ) -> None:
        service, storage_mock, _ = _make_create_service(_make_store(tmp_path))

        with pytest.raises(KeboolaApiError) as excinfo:
            service.create_data_app(**_create_kwargs(**overrides))

        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR
        assert "--git-auth organization" in excinfo.value.message
        assert flag in excinfo.value.message
        storage_mock.update_config.assert_not_called()

    def test_rejects_managed_repo(self, tmp_path: Path) -> None:
        service, *_ = _make_create_service(_make_store(tmp_path))

        with pytest.raises(KeboolaApiError) as excinfo:
            service.create_data_app(**_create_kwargs(git_repo="", use_managed_git_repo=True))

        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR
        assert "--git-auth" in excinfo.value.message

    def test_rejects_unknown_auth_type(self, tmp_path: Path) -> None:
        service, *_ = _make_create_service(_make_store(tmp_path))

        with pytest.raises(KeboolaApiError) as excinfo:
            service.create_data_app(**_create_kwargs(git_auth="pat"))

        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_private_repo_without_pat_still_needs_username(self, tmp_path: Path) -> None:
        service, *_ = _make_create_service(_make_store(tmp_path))

        with pytest.raises(KeboolaApiError) as excinfo:
            service.create_data_app(**_create_kwargs(git_auth=None))

        assert "--git-username is required" in excinfo.value.message


class TestOrganizationRepositoryShape:
    @pytest.mark.parametrize(
        "repo",
        [
            "https://github.com/acme/dashboard",
            "https://github.com/acme/dashboard.git",
            "https://github.com/acme/dashboard/",
            "https://acme.ghe.com/team/my_app.v2",
        ],
    )
    def test_accepts(self, repo: str) -> None:
        validate_organization_repository(repo)

    @pytest.mark.parametrize(
        "repo",
        [
            "ssh://git@github.com/acme/dashboard.git",
            "http://github.com/acme/dashboard",
            "https://user:pw@github.com/acme/dashboard",
            "https://github.com:8443/acme/dashboard",
            "https://github.com/acme/dashboard?ref=x",
            "https://github.com/acme/dashboard#main",
            "https://github.com/acme",
            "https://github.com/acme/dash/tree/main",
            "https://github.com/acme/%64ash",
            "https://github.com/acme/..",
            "https://github.com/acme/.git",
        ],
    )
    def test_rejects(self, repo: str) -> None:
        with pytest.raises(KeboolaApiError) as excinfo:
            validate_organization_repository(repo)
        assert excinfo.value.error_code == ErrorCode.DATA_APP_INVALID_GIT
        assert repo not in excinfo.value.message

    def test_embedded_token_is_not_echoed(self) -> None:
        with pytest.raises(KeboolaApiError) as excinfo:
            validate_organization_repository("https://bot:ghp_SECRET@github.com/acme/dashboard")
        assert "embedded credentials" in excinfo.value.message
        assert "ghp_SECRET" not in excinfo.value.message

    def test_preview_block_matches_kbc_ui_shape(self) -> None:
        source = GitSource(repo=REPO, auth_type="organization")
        validate_git_flags(source)
        validate_git_credentials(source)
        assert preview_git_block(source) == ORG_BLOCK


# ---------------------------------------------------------------------------
# git-auth-set (switch an existing app)
# ---------------------------------------------------------------------------


def _pat_body(**git_overrides: Any) -> dict[str, Any]:
    git = {
        "repository": REPO,
        "private": True,
        "username": "bob",
        "#password": "KBC::ProjectSecureKV::abc",
        "branch": "main",
        "entrypoint": "app.py",
    }
    git.update(git_overrides)
    return {
        "parameters": {"dataApp": {"slug": "dashboard", "git": git}, "id": "77"},
        "runtime": {"backend": {"size": "tiny"}},
    }


def _make_switch_service(
    store: ConfigStore, body: dict[str, Any], *, managed: bool = False, mode: str = "prod"
) -> tuple[DataAppGitService, MagicMock]:
    ds_mock = MagicMock()
    ds_mock.get_app.return_value = {
        "id": "77",
        "configId": "c-77",
        "hasManagedGitRepo": managed,
        "mode": mode,
    }
    storage_mock = MagicMock()
    storage_mock.get_config_detail.return_value = {"version": 4, "configuration": body}
    storage_mock.update_config.return_value = {"version": 5}
    service = DataAppGitService(
        config_store=store,
        client_factory=lambda url, token: storage_mock,
        ds_client_factory=lambda url, token: ds_mock,
    )
    return service, storage_mock


class TestGitAuthSetService:
    def test_switch_removes_credentials_in_one_write(self, tmp_path: Path) -> None:
        service, storage_mock = _make_switch_service(_make_store(tmp_path), _pat_body())

        result = service.set_data_app_git_auth(alias="prod", app_id="77", git_auth="organization")

        storage_mock.update_config.assert_called_once()
        written = storage_mock.update_config.call_args.kwargs["configuration"]
        git = written["parameters"]["dataApp"]["git"]
        assert git == ORG_BLOCK | {"entrypoint": "app.py"}
        assert written["runtime"] == {"backend": {"size": "tiny"}}
        assert result["status"] == "switched"
        assert result["git_auth_before"] == "pat"
        assert result["removed_credentials"] == ["username", "#password"]
        assert result["config_version_after"] == "5"

    def test_ssh_url_needs_a_new_https_repo(self, tmp_path: Path) -> None:
        body = _pat_body(repository="ssh://git@github.com/acme/dashboard.git")
        body["parameters"]["dataApp"]["git"].pop("#password")
        body["parameters"]["dataApp"]["git"]["#sshKey"] = "KBC::ProjectSecureKV::key"
        service, storage_mock = _make_switch_service(_make_store(tmp_path), body)

        with pytest.raises(KeboolaApiError) as excinfo:
            service.set_data_app_git_auth(alias="prod", app_id="77", git_auth="organization")
        assert excinfo.value.error_code == ErrorCode.DATA_APP_INVALID_GIT
        storage_mock.update_config.assert_not_called()

        result = service.set_data_app_git_auth(
            alias="prod", app_id="77", git_auth="organization", git_repo=REPO
        )
        git = storage_mock.update_config.call_args.kwargs["configuration"]["parameters"]["dataApp"][
            "git"
        ]
        assert "#sshKey" not in git and "username" not in git
        assert git["repository"] == REPO
        assert result["git_auth_before"] == "ssh-key"

    def test_already_organization_writes_nothing(self, tmp_path: Path) -> None:
        body = {"parameters": {"dataApp": {"git": dict(ORG_BLOCK)}}}
        service, storage_mock = _make_switch_service(_make_store(tmp_path), body)

        result = service.set_data_app_git_auth(alias="prod", app_id="77", git_auth="organization")

        assert result["status"] == "unchanged"
        assert result["deploy_required"] is False
        storage_mock.update_config.assert_not_called()

    def test_dry_run_writes_nothing_and_redacts(self, tmp_path: Path) -> None:
        body = _pat_body()
        body["parameters"]["dataApp"]["secrets"] = {"#API_KEY": "KBC::ProjectSecureKV::xyz"}
        service, storage_mock = _make_switch_service(_make_store(tmp_path), body)

        result = service.set_data_app_git_auth(
            alias="prod", app_id="77", git_auth="organization", dry_run=True
        )

        storage_mock.update_config.assert_not_called()
        preview = result["put_storage_config_preview"]["configuration"]
        assert preview["parameters"]["dataApp"]["git"]["authType"] == "organization"
        assert preview["parameters"]["dataApp"]["secrets"]["#API_KEY"] == "<encrypted>"
        assert "KBC::ProjectSecureKV::xyz" not in json.dumps(preview)

    def test_dev_branch_is_read_and_written(self, tmp_path: Path) -> None:
        service, storage_mock = _make_switch_service(_make_store(tmp_path), _pat_body())

        service.set_data_app_git_auth(
            alias="prod", app_id="77", git_auth="organization", branch_id=4242
        )

        assert storage_mock.get_config_detail.call_args.kwargs["branch_id"] == 4242
        assert storage_mock.update_config.call_args.kwargs["branch_id"] == 4242

    def test_stored_token_in_url_is_not_echoed(self, tmp_path: Path) -> None:
        body = _pat_body(repository="https://bot:ghp_SECRET@github.com/acme/dashboard")
        service, storage_mock = _make_switch_service(_make_store(tmp_path), body)

        with pytest.raises(KeboolaApiError) as excinfo:
            service.set_data_app_git_auth(alias="prod", app_id="77", git_auth="organization")
        assert excinfo.value.error_code == ErrorCode.DATA_APP_INVALID_GIT
        assert "ghp_SECRET" not in excinfo.value.message
        storage_mock.update_config.assert_not_called()

    def test_managed_repo_is_refused(self, tmp_path: Path) -> None:
        service, _ = _make_switch_service(_make_store(tmp_path), _pat_body(), managed=True)

        with pytest.raises(KeboolaApiError) as excinfo:
            service.set_data_app_git_auth(alias="prod", app_id="77", git_auth="organization")
        assert "Keboola-managed" in excinfo.value.message

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_dev_mode_app_is_refused_before_any_read_or_write(
        self, tmp_path: Path, dry_run: bool
    ) -> None:
        service, storage_mock = _make_switch_service(_make_store(tmp_path), _pat_body(), mode="dev")

        with pytest.raises(KeboolaApiError) as excinfo:
            service.set_data_app_git_auth(
                alias="prod", app_id="77", git_auth="organization", dry_run=dry_run
            )
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR
        assert "dev mode" in excinfo.value.message
        storage_mock.get_config_detail.assert_not_called()
        storage_mock.update_config.assert_not_called()

    def test_config_without_git_block_is_refused(self, tmp_path: Path) -> None:
        body = {"parameters": {"dataApp": {"slug": "dashboard"}}}
        service, _ = _make_switch_service(_make_store(tmp_path), body)

        with pytest.raises(KeboolaApiError) as excinfo:
            service.set_data_app_git_auth(alias="prod", app_id="77", git_auth="organization")
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_unknown_auth_type_is_refused(self, tmp_path: Path) -> None:
        service, _ = _make_switch_service(_make_store(tmp_path), _pat_body())

        with pytest.raises(KeboolaApiError) as excinfo:
            service.set_data_app_git_auth(alias="prod", app_id="77", git_auth="pat")
        assert excinfo.value.error_code == ErrorCode.INVALID_ARGUMENT

    def test_plan_does_not_mutate_the_input(self) -> None:
        body = _pat_body()
        snapshot = json.dumps(body, sort_keys=True)
        plan_organization_switch(body)
        assert json.dumps(body, sort_keys=True) == snapshot


class TestGitBlockDescriptionAndRedaction:
    @pytest.mark.parametrize(
        ("block", "kind"),
        [
            (ORG_BLOCK, "organization"),
            ({"repository": REPO, "#sshKey": "KBC::x"}, "ssh-key"),
            ({"repository": REPO, "#password": "KBC::x"}, "pat"),
            ({"repository": REPO, "private": False}, "public"),
        ],
    )
    def test_describe_git_auth(self, block: dict[str, Any], kind: str) -> None:
        assert describe_git_auth(block) == kind

    def test_ssh_key_is_redacted(self) -> None:
        redacted = _redact_git_block({"repository": REPO, "#sshKey": "KBC::ProjectSecureKV::k"})
        assert redacted["#sshKey"] == "<encrypted>"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _invoke(args: list[str], store: ConfigStore, **services: Any) -> Any:
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ProjectService") as MockProj,
        patch("keboola_agent_cli.cli.ConfigService") as MockCfg,
        patch("keboola_agent_cli.cli.JobService") as MockJob,
        patch("keboola_agent_cli.cli.DataAppService") as MockDataApp,
        patch("keboola_agent_cli.cli.DataAppGitService") as MockDataAppGit,
    ):
        MockStore.return_value = store
        MockProj.return_value = ProjectService(config_store=store)
        MockCfg.return_value = ConfigService(config_store=store)
        MockJob.return_value = JobService(config_store=store)
        MockDataApp.return_value = services.get("data_app", MagicMock())
        MockDataAppGit.return_value = services.get("data_app_git", MagicMock())
        return runner.invoke(app, args)


CREATE_ARGS = [
    "--json",
    "data-app",
    "create",
    "--project",
    "prod",
    "--name",
    "Dashboard",
    "--slug",
    "dashboard",
    "--git-repo",
    REPO,
    "--git-auth",
    "organization",
]

SET_ARGS = ["--json", "data-app", "git-auth-set", "--project", "prod", "--app-id", "77"]


class TestCli:
    def test_create_dry_run_shows_organization_block(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_create_service(store)

        result = _invoke([*CREATE_ARGS, "--dry-run"], store, data_app=service)

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["requests"]["put_storage_config"]["parameters"]["dataApp"]["git"] == ORG_BLOCK

    def test_create_with_username_fails_with_usage_exit(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_create_service(store)

        result = _invoke([*CREATE_ARGS, "--git-username", "bob"], store, data_app=service)

        assert result.exit_code == 2, result.output
        error = json.loads(result.output)["error"]
        assert error["code"] == "VALIDATION_ERROR"
        assert "--git-username" in error["message"]

    def test_git_auth_set_forwards_to_the_service(self, tmp_path: Path) -> None:
        git_mock = MagicMock()
        git_mock.set_data_app_git_auth.return_value = {"status": "switched"}

        result = _invoke(
            [
                "--json",
                "data-app",
                "git-auth-set",
                "--project",
                "prod",
                "--app-id",
                "77",
                "--git-auth",
                "organization",
                "--git-repo",
                REPO,
                "--dry-run",
            ],
            _make_store(tmp_path),
            data_app_git=git_mock,
        )

        assert result.exit_code == 0, result.output
        git_mock.set_data_app_git_auth.assert_called_once_with(
            alias="prod",
            app_id="77",
            git_auth="organization",
            git_repo=REPO,
            git_branch=None,
            branch_id=None,
            dry_run=True,
        )

    def test_git_auth_set_rejects_unknown_value(self, tmp_path: Path) -> None:
        git_mock = MagicMock()

        result = _invoke(
            [*SET_ARGS, "--git-auth", "pat"],
            _make_store(tmp_path),
            data_app_git=git_mock,
        )

        assert result.exit_code == 2, result.output
        git_mock.set_data_app_git_auth.assert_not_called()

    def test_git_auth_set_human_output(self, tmp_path: Path) -> None:
        service, _ = _make_switch_service(_make_store(tmp_path), _pat_body())

        result = _invoke(
            [*SET_ARGS[1:], "--git-auth", "organization"],
            _make_store(tmp_path / "other"),
            data_app_git=service,
        )

        assert result.exit_code == 0, result.output
        assert "pat -> organization" in result.output
        assert "username, #password" in result.output
        assert "data-app deploy" in result.output


# ---------------------------------------------------------------------------
# kbagent serve
# ---------------------------------------------------------------------------

HAS_FASTAPI = importlib.util.find_spec("fastapi") is not None
AUTH = {"Authorization": "Bearer test-token"}


def _serve_app(tmp_path: Path, **services: Any) -> Any:
    from keboola_agent_cli.server import create_app
    from keboola_agent_cli.server.dependencies import ServiceRegistry, get_registry

    registry = ServiceRegistry.__new__(ServiceRegistry)
    registry.config_store = _make_store(tmp_path)
    for name, service in services.items():
        setattr(registry, name, service)
    serve_app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    serve_app.dependency_overrides[get_registry] = lambda: registry
    return serve_app


@pytest.mark.skipif(not HAS_FASTAPI, reason="FastAPI not installed")
class TestServe:
    def test_create_forwards_auth_type(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        data_app = MagicMock()
        data_app.create_data_app.return_value = {"app_id": "77"}
        body = {"name": "Dashboard", "slug": "dashboard", "git_repo": REPO}

        with TestClient(_serve_app(tmp_path, data_app=data_app)) as client:
            res = client.post(
                "/data-apps/prod", headers=AUTH, json=body | {"auth_type": "organization"}
            )

        assert res.status_code == 200, res.text
        assert data_app.create_data_app.call_args.kwargs["git_auth"] == "organization"

    def test_create_dry_run_body_equals_cli(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        service, *_ = _make_create_service(_make_store(tmp_path / "svc"))
        body = {"name": "Dashboard", "slug": "dashboard", "git_repo": REPO}

        with TestClient(_serve_app(tmp_path, data_app=service)) as client:
            res = client.post(
                "/data-apps/prod",
                headers=AUTH,
                json=body | {"auth_type": "organization", "dry_run": True},
            )

        assert res.status_code == 200, res.text
        put_body = res.json()["requests"]["put_storage_config"]
        assert put_body["parameters"]["dataApp"]["git"] == ORG_BLOCK

    def test_git_auth_patch_forwards_kwargs(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        data_app_git = MagicMock()
        data_app_git.set_data_app_git_auth.return_value = {"status": "dry_run"}

        with TestClient(_serve_app(tmp_path, data_app_git=data_app_git)) as client:
            res = client.patch(
                "/data-apps/prod/77/git-auth",
                headers=AUTH,
                json={"auth_type": "organization", "git_branch": "release", "dry_run": True},
            )

        assert res.status_code == 200, res.text
        data_app_git.set_data_app_git_auth.assert_called_once_with(
            alias="prod",
            app_id="77",
            git_auth="organization",
            git_repo=None,
            git_branch="release",
            branch_id=None,
            dry_run=True,
        )

    def test_git_auth_patch_refuses_undeclared_fields(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        data_app_git = MagicMock()

        with TestClient(_serve_app(tmp_path, data_app_git=data_app_git)) as client:
            res = client.patch(
                "/data-apps/prod/77/git-auth",
                headers=AUTH,
                json={"auth_type": "organization", "username": "bob"},
            )

        assert res.status_code == 422, res.text
        data_app_git.set_data_app_git_auth.assert_not_called()
