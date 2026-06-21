"""Service-layer tests for DataAppService.

Covers: input validation, the §9 redeploy contract, cleanup-in-finally,
the §8 pitfall #1 (transient stopped during initial deploy), encryption
round-trip, and password retrieval.

The tests speak to a fully-mocked Data Science + Storage + Encryption
stack -- they verify orchestration, not HTTP shapes (those live in
test_data_science_client.py / test_e2e.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.data_app_service import (
    DataAppService,
    _redact_git_block,
    _redact_storage_config,
)

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
TEST_MANAGE_TOKEN = "manage-test-token"


# ---------------------------------------------------------------------------
# Helpers
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


def _make_service(
    store: ConfigStore,
    *,
    ds_mock: MagicMock | None = None,
    storage_mock: MagicMock | None = None,
    encrypt_mock: MagicMock | None = None,
) -> tuple[DataAppService, MagicMock, MagicMock, MagicMock]:
    ds_mock = ds_mock or MagicMock()
    storage_mock = storage_mock or MagicMock()
    if encrypt_mock is None:
        encrypt_mock = MagicMock()
        encrypt_mock.encrypt.return_value = {"#password": "KBC::ProjectSecureGKMS::ciphertext-prod"}

    service = DataAppService(
        config_store=store,
        client_factory=lambda url, token: storage_mock,
        ds_client_factory=lambda url, token: ds_mock,
        encrypt_service=encrypt_mock,
    )
    return service, ds_mock, storage_mock, encrypt_mock


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestDataAppCreateValidation:
    """The service is the single source of truth for input shape."""

    def _create(self, service: DataAppService, **overrides: Any) -> Any:
        kwargs: dict[str, Any] = dict(
            alias="prod",
            name="My App",
            description="",
            slug="my-app",
            git_repo="https://github.com/o/r",
            git_public=False,
            git_username="user",
            git_pat_plaintext="ghp_xxxxxxxxxxxxxxxxxxxx",
            auth="password",
            size="tiny",
            auto_suspend_after_seconds=900,
            type_="python-js",
            deploy=False,
            wait=False,
            dry_run=True,
        )
        kwargs.update(overrides)
        return service.create_data_app(**kwargs)

    def test_invalid_size(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            self._create(service, size="huge")
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_invalid_type(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            self._create(service, type_="rust")
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_invalid_auth(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            self._create(service, auth="oauth")
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_invalid_slug(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            self._create(service, slug="UPPER")
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_public_repo_rejects_credentials(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            self._create(
                service,
                git_public=True,
                git_username="user",
                git_pat_plaintext="x",
            )
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_private_repo_requires_username(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            self._create(service, git_username=None)
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_private_repo_requires_pat(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            self._create(service, git_pat_plaintext=None)
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_pat_modes_mutually_exclusive(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            self._create(
                service,
                git_pat_plaintext="ghp_x",
                git_pat_encrypted="KBC::ProjectSecureGKMS::abc",
            )
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_pre_encrypted_must_be_project_scoped(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            self._create(
                service,
                git_pat_plaintext=None,
                git_pat_encrypted="KBC::Encrypted::not-project-scoped",
            )
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_pre_encrypted_loose_project_prefix_rejected(self, tmp_path: Path) -> None:
        """A bare 'KBC::Project' prefix is no longer enough — validator now
        requires a known full prefix (Secure / SecureGKMS / SecureKMS)."""
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            self._create(
                service,
                git_pat_plaintext=None,
                git_pat_encrypted="KBC::ProjectAttacker::xyz",
            )
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_oversize_name_rejected(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            self._create(service, name="A" * 1000)
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_control_char_in_name_rejected(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            self._create(service, name="bad\x00name")
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_control_char_in_git_username_rejected(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            self._create(service, git_username="user\nname")
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_description_allows_markdown_newlines(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        result = self._create(service, description="line one\nline two\n\ttabbed")
        assert result["dry_run"] is True

    def test_managed_repo_rejects_external_url(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            self._create(
                service,
                use_managed_git_repo=True,
                git_repo="https://github.com/o/r",
            )
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_managed_repo_rejects_git_public(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            self._create(
                service,
                use_managed_git_repo=True,
                git_repo="",
                git_public=True,
                git_username=None,
                git_pat_plaintext=None,
            )
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_managed_repo_rejects_credentials(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            self._create(
                service,
                use_managed_git_repo=True,
                git_repo="",
                git_username="user",
                git_pat_plaintext="ghp_xxxxxxxxxxxxxxxxxxxx",
            )
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_description_rejects_nul_byte(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            self._create(service, description="oops\x00")
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_git_repo_rejects_file_scheme(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            self._create(service, git_repo="file:///etc/passwd")
        assert excinfo.value.error_code == ErrorCode.DATA_APP_INVALID_GIT

    def test_git_repo_rejects_bare_ssh_syntax(self, tmp_path: Path) -> None:
        """``git@github.com:org/repo`` style. Some clients accept this but
        the data-app runner does not -- reject upfront."""
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            self._create(service, git_repo="git@github.com:org/repo")
        assert excinfo.value.error_code == ErrorCode.DATA_APP_INVALID_GIT

    def test_git_pat_plaintext_starting_with_kbc_rejected(self, tmp_path: Path) -> None:
        """A plaintext PAT that already looks like a ciphertext is almost
        certainly someone pasting an encrypted value into the wrong flag.
        Reject upfront so EncryptService's KBC:: short-circuit cannot
        ferry a stale ciphertext into Storage."""
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            self._create(
                service,
                git_pat_plaintext="KBC::ProjectSecureGKMS::accidentally-pasted",
            )
        assert excinfo.value.error_code == ErrorCode.DATA_APP_INVALID_GIT


# ---------------------------------------------------------------------------
# Create flow
# ---------------------------------------------------------------------------


class TestDataAppCreateAuthBlock:
    """The authorization block written into the Storage config must match the
    canonical shapes the platform's app-proxy expects.

    Source of truth: the public backend validator at
    keboola/job-queue-job-configuration
    ``src/JobDefinition/Configuration/Authorization/AppProxyDefinition.php``
    (when ``auth_required=false``, ``auth`` MUST NOT be set). The private
    keboola/ui repo's
    ``apps/kbc-ui/src/scripts/modules/data-apps/constants.ts``
    corroborates: it exports this exact shape as
    ``noneProxyAuthorization``.
    """

    PASSWORD_BLOCK: ClassVar[dict[str, Any]] = {
        "app_proxy": {
            "auth_providers": [{"id": "simpleAuth", "type": "password"}],
            "auth_rules": [
                {
                    "type": "pathPrefix",
                    "value": "/",
                    "auth_required": True,
                    "auth": ["simpleAuth"],
                }
            ],
        },
    }
    PUBLIC_BLOCK: ClassVar[dict[str, Any]] = {
        "app_proxy": {
            "auth_providers": [],
            "auth_rules": [{"type": "pathPrefix", "value": "/", "auth_required": False}],
        },
    }

    def _create_kwargs(self, **overrides: Any) -> dict[str, Any]:
        return {
            "alias": "prod",
            "name": "Public App",
            "description": "",
            "slug": "public-app",
            "git_repo": "https://github.com/o/r",
            "git_public": True,
            "auth": "public",
            "size": "tiny",
            "auto_suspend_after_seconds": 900,
            "type_": "python-js",
            "deploy": False,
            "wait": False,
            **overrides,
        }

    def test_auth_public_writes_canonical_none_block(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _ = _make_service(store)
        ds_mock.create_app.return_value = {"id": "10", "configId": "01CFG"}
        storage_mock.update_config.return_value = {"version": "2"}

        service.create_data_app(**self._create_kwargs(auth="public"))

        # Step 1 -- POST /apps shell config: authorization should be the
        # public block (no longer absent as in v0.27.0).
        post_call = ds_mock.create_app.call_args
        post_config = post_call.kwargs["config"]
        assert post_config["authorization"] == self.PUBLIC_BLOCK

        # Step 4 -- PUT Storage config: same public block.
        put_call = storage_mock.update_config.call_args
        put_body = put_call.kwargs["configuration"]
        assert put_body["authorization"] == self.PUBLIC_BLOCK

    def test_auth_password_unchanged(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _ = _make_service(store)
        ds_mock.create_app.return_value = {"id": "10", "configId": "01CFG"}
        storage_mock.update_config.return_value = {"version": "2"}

        service.create_data_app(
            **self._create_kwargs(
                auth="password",
                git_public=False,
                git_username="user",
                git_pat_plaintext="ghp_xxxxxxxxxxxxxxxxxxxx",
            )
        )

        post_call = ds_mock.create_app.call_args
        assert post_call.kwargs["config"]["authorization"] == self.PASSWORD_BLOCK
        put_call = storage_mock.update_config.call_args
        assert put_call.kwargs["configuration"]["authorization"] == self.PASSWORD_BLOCK

    def test_dry_run_renders_public_block(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        result = service.create_data_app(**self._create_kwargs(auth="public", dry_run=True))
        post = result["requests"]["post_apps"]
        put = result["requests"]["put_storage_config"]
        assert post["config"]["authorization"] == self.PUBLIC_BLOCK
        assert put["authorization"] == self.PUBLIC_BLOCK

    def test_invalid_auth_value_rejected(self, tmp_path: Path) -> None:
        # Use a clearly-invalid sentinel (NOT a future-supported provider
        # like 'oidc' / 'github' / 'gitlab' / 'jumpcloud') so this test
        # stays valid when those modes are added in a follow-up PR.
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as exc:
            service.create_data_app(**self._create_kwargs(auth="banana", dry_run=True))
        assert exc.value.error_code == ErrorCode.VALIDATION_ERROR


class TestDataAppCreate:
    def test_dry_run_makes_no_calls(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, encrypt_mock = _make_service(store)
        result = service.create_data_app(
            alias="prod",
            name="App",
            description="",
            slug="my-app",
            git_repo="https://github.com/o/r",
            git_public=True,
            auth="public",
            size="tiny",
            auto_suspend_after_seconds=900,
            type_="python-js",
            deploy=True,
            wait=False,
            dry_run=True,
        )
        assert result["dry_run"] is True
        assert "post_apps" in result["requests"]
        assert "put_storage_config" in result["requests"]
        assert "patch_apps" in result["requests"]
        ds_mock.assert_not_called()
        storage_mock.assert_not_called()
        encrypt_mock.encrypt.assert_not_called()

    def test_happy_path_private_repo(self, tmp_path: Path) -> None:
        """Verifies POST -> encrypt -> PUT -> PATCH order and arguments."""
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, encrypt_mock = _make_service(store)

        ds_mock.create_app.return_value = {
            "id": "43661269",
            "configId": "01kqj88t0vktxe0vfhk6ps5kzs",
        }
        storage_mock.update_config.return_value = {"version": "3"}
        ds_mock.patch_app.return_value = {
            "id": "43661269",
            "state": "starting",
            "desiredState": "running",
            "url": "https://my-app-43661269.hub.us-east4.gcp.keboola.com",
            "configVersion": "3",
        }

        result = service.create_data_app(
            alias="prod",
            name="My App",
            description="long form",
            slug="my-app",
            git_repo="https://github.com/o/r",
            git_branch="main",
            git_public=False,
            git_username="user",
            git_pat_plaintext="ghp_xxxxxxxxxxxxxxxxxxxx",
            auth="password",
            size="tiny",
            auto_suspend_after_seconds=900,
            type_="python-js",
            deploy=True,
            wait=False,
            dry_run=False,
        )

        # 1. Shell created
        ds_mock.create_app.assert_called_once()
        create_kwargs = ds_mock.create_app.call_args.kwargs
        assert create_kwargs["type_"] == "python-js"
        assert create_kwargs["name"] == "My App"
        assert create_kwargs["description"] == ""  # description goes to Storage

        # 2. PAT encrypted under target project's KMS
        encrypt_mock.encrypt.assert_called_once_with(
            alias="prod",
            component_id="keboola.data-apps",
            input_data={"#password": "ghp_xxxxxxxxxxxxxxxxxxxx"},
        )

        # 3. Storage config written with parameters.id back-pointer
        storage_mock.update_config.assert_called_once()
        put_kwargs = storage_mock.update_config.call_args.kwargs
        body = put_kwargs["configuration"]
        assert body["parameters"]["id"] == "43661269"
        assert body["parameters"]["dataApp"]["slug"] == "my-app"
        assert body["parameters"]["dataApp"]["git"]["#password"].startswith("KBC::Project")
        assert body["parameters"]["dataApp"]["git"]["private"] is True
        assert body["runtime"]["backend"]["size"] == "tiny"
        assert "authorization" in body  # simpleAuth on by default

        # 4. PATCH /apps deploys the trio
        ds_mock.patch_app.assert_called_once_with(
            "43661269",
            desired_state="running",
            config_version="3",
            restart_if_running=True,
        )

        assert result["app_id"] == "43661269"
        assert result["config_id"] == "01kqj88t0vktxe0vfhk6ps5kzs"
        assert result["url"].endswith("hub.us-east4.gcp.keboola.com")
        # Encrypted PAT is redacted in the returned dict for human display.
        assert result["git"]["#password"] == "<encrypted>"

    def test_no_deploy_skips_patch(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _enc = _make_service(store)
        ds_mock.create_app.return_value = {"id": "1", "configId": "ulid"}
        storage_mock.update_config.return_value = {"version": "2"}

        service.create_data_app(
            alias="prod",
            name="App",
            description="",
            slug="my-app",
            git_repo="https://github.com/o/r",
            git_public=True,
            auth="public",
            size="tiny",
            auto_suspend_after_seconds=900,
            type_="python-js",
            deploy=False,
            wait=False,
            dry_run=False,
        )
        ds_mock.patch_app.assert_not_called()

    def test_managed_repo_happy_path(self, tmp_path: Path) -> None:
        """Managed repo: useManagedGitRepo on POST, NO git block in PUT,
        no encryption, deploy forced off even when caller asks for it."""
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, encrypt_mock = _make_service(store)
        ds_mock.create_app.return_value = {
            "id": "43676897",
            "configId": "01kvj0rwjr3g6tf10nc0xcvyyg",
            "managedGitRepoId": "repo-77",
        }
        storage_mock.update_config.return_value = {"version": "4"}

        result = service.create_data_app(
            alias="prod",
            name="Managed App",
            description="long form",
            slug="managed-app",
            git_repo="",
            git_public=False,
            git_username=None,
            git_pat_plaintext=None,
            use_managed_git_repo=True,
            auth="public",
            size="tiny",
            auto_suspend_after_seconds=900,
            type_="python-js",
            deploy=True,  # caller asks to deploy; managed must force it off
            wait=True,
            dry_run=False,
        )

        # 1. POST /apps carries the managed flag.
        create_kwargs = ds_mock.create_app.call_args.kwargs
        assert create_kwargs["use_managed_git_repo"] is True

        # 2. No PAT encryption for a managed repo.
        encrypt_mock.encrypt.assert_not_called()

        # 3. Storage config omits parameters.dataApp.git entirely.
        body = storage_mock.update_config.call_args.kwargs["configuration"]
        assert body["parameters"]["dataApp"]["slug"] == "managed-app"
        assert "git" not in body["parameters"]["dataApp"]

        # 4. Deploy is forced off -- empty repo has nothing to run yet.
        ds_mock.patch_app.assert_not_called()

        assert result["use_managed_git_repo"] is True
        assert result["managed_git_repo_id"] == "repo-77"
        assert result["deployed"] is False
        assert result["git"] == {}

    def test_managed_repo_dry_run(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, encrypt_mock = _make_service(store)
        result = service.create_data_app(
            alias="prod",
            name="Managed App",
            description="",
            slug="managed-app",
            git_repo="",
            git_public=False,
            git_username=None,
            git_pat_plaintext=None,
            use_managed_git_repo=True,
            auth="public",
            size="tiny",
            auto_suspend_after_seconds=900,
            type_="python-js",
            deploy=True,
            wait=False,
            dry_run=True,
        )
        assert result["dry_run"] is True
        assert result["use_managed_git_repo"] is True
        post = result["requests"]["post_apps"]
        assert post["useManagedGitRepo"] is True
        # PUT preview omits the git block; PATCH is empty (no deploy).
        assert "git" not in result["requests"]["put_storage_config"]["parameters"]["dataApp"]
        assert result["requests"]["patch_apps"] == {}
        ds_mock.create_app.assert_not_called()
        storage_mock.update_config.assert_not_called()
        encrypt_mock.encrypt.assert_not_called()

    def test_public_repo_skips_encryption(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, encrypt_mock = _make_service(store)
        ds_mock.create_app.return_value = {"id": "1", "configId": "ulid"}
        storage_mock.update_config.return_value = {"version": "2"}

        service.create_data_app(
            alias="prod",
            name="App",
            description="",
            slug="my-app",
            git_repo="https://github.com/o/r",
            git_public=True,
            auth="public",
            size="tiny",
            auto_suspend_after_seconds=900,
            type_="python-js",
            deploy=False,
            wait=False,
            dry_run=False,
        )
        encrypt_mock.encrypt.assert_not_called()

    def test_cleanup_on_storage_put_failure(self, tmp_path: Path) -> None:
        """If PUT fails after POST, the orphan shell is deleted by default."""
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _enc = _make_service(store)
        ds_mock.create_app.return_value = {"id": "999", "configId": "ulid"}
        storage_mock.update_config.side_effect = KeboolaApiError(
            message="boom",
            status_code=500,
            error_code=ErrorCode.API_ERROR,
            retryable=False,
        )

        with pytest.raises(KeboolaApiError):
            service.create_data_app(
                alias="prod",
                name="App",
                description="",
                slug="my-app",
                git_repo="https://github.com/o/r",
                git_public=True,
                auth="public",
                size="tiny",
                auto_suspend_after_seconds=900,
                type_="python-js",
                deploy=False,
                wait=False,
                dry_run=False,
            )
        ds_mock.delete_app.assert_called_once_with("999")

    def test_keep_on_failure_skips_cleanup(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _enc = _make_service(store)
        ds_mock.create_app.return_value = {"id": "999", "configId": "ulid"}
        storage_mock.update_config.side_effect = KeboolaApiError(
            message="boom",
            status_code=500,
            error_code=ErrorCode.API_ERROR,
            retryable=False,
        )

        with pytest.raises(KeboolaApiError):
            service.create_data_app(
                alias="prod",
                name="App",
                description="",
                slug="my-app",
                git_repo="https://github.com/o/r",
                git_public=True,
                auth="public",
                size="tiny",
                auto_suspend_after_seconds=900,
                type_="python-js",
                deploy=False,
                wait=False,
                keep_on_failure=True,
                dry_run=False,
            )
        ds_mock.delete_app.assert_not_called()

    def test_encryption_failure_aborts_loud(self, tmp_path: Path) -> None:
        """The service refuses to write plaintext if the Encryption API
        does not return a project-scoped ciphertext."""
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, encrypt_mock = _make_service(store)
        ds_mock.create_app.return_value = {"id": "999", "configId": "ulid"}
        encrypt_mock.encrypt.return_value = {"#password": "not-a-ciphertext"}

        with pytest.raises(KeboolaApiError) as excinfo:
            service.create_data_app(
                alias="prod",
                name="App",
                description="",
                slug="my-app",
                git_repo="https://github.com/o/r",
                git_public=False,
                git_username="user",
                git_pat_plaintext="ghp_xxxxxxxxxxxxxxxxxxxx",
                auth="password",
                size="tiny",
                auto_suspend_after_seconds=900,
                type_="python-js",
                deploy=False,
                wait=False,
                dry_run=False,
            )
        assert excinfo.value.error_code == ErrorCode.ENCRYPTION_FAILED
        # Plaintext never reached Storage.
        storage_mock.update_config.assert_not_called()
        # Shell was cleaned up.
        ds_mock.delete_app.assert_called_once_with("999")


# ---------------------------------------------------------------------------
# Deploy / start / stop
# ---------------------------------------------------------------------------


class TestDataAppDeploy:
    def test_deploy_reads_latest_storage_version(self, tmp_path: Path) -> None:
        """The §9 redeploy contract."""
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _enc = _make_service(store)
        ds_mock.get_app.return_value = {"configId": "ulid"}
        storage_mock.get_config_detail.return_value = {"version": 7}
        ds_mock.patch_app.return_value = {"state": "starting"}

        service.deploy_data_app(alias="prod", app_id="42")

        ds_mock.patch_app.assert_called_once_with(
            "42",
            desired_state="running",
            config_version="7",
            restart_if_running=True,
        )

    def test_deploy_pins_explicit_version(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _enc = _make_service(store)
        ds_mock.get_app.return_value = {"configId": "ulid"}
        ds_mock.patch_app.return_value = {"state": "starting"}

        service.deploy_data_app(alias="prod", app_id="42", config_version="3")

        # Service did NOT need to read Storage to derive a version
        storage_mock.get_config_detail.assert_not_called()
        ds_mock.patch_app.assert_called_once_with(
            "42",
            desired_state="running",
            config_version="3",
            restart_if_running=True,
        )

    def test_deploy_never_sends_config_block(self, tmp_path: Path) -> None:
        """`PATCH /apps {config: ...}` is silently dropped (writeup §8 row 3).
        We never construct that payload in the first place."""
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _enc = _make_service(store)
        ds_mock.get_app.return_value = {"configId": "ulid"}
        storage_mock.get_config_detail.return_value = {"version": "5"}
        ds_mock.patch_app.return_value = {"state": "running", "desiredState": "running"}

        service.deploy_data_app(alias="prod", app_id="42")
        kwargs = ds_mock.patch_app.call_args.kwargs
        assert "config" not in kwargs

    def test_deploy_pure_managed_omits_config_version(self, tmp_path: Path) -> None:
        """A managed-repo app with NO git block deploys from managedGitRepoId,
        so configVersion is omitted (matches keboola-mcp-server)."""
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _enc = _make_service(store)
        ds_mock.get_app.return_value = {"configId": "ulid", "hasManagedGitRepo": True}
        storage_mock.get_config_detail.return_value = {
            "version": 4,
            "configuration": {"parameters": {"dataApp": {"slug": "x"}}},  # no git block
        }
        ds_mock.patch_app.return_value = {"state": "starting"}

        service.deploy_data_app(alias="prod", app_id="42")

        kwargs = ds_mock.patch_app.call_args.kwargs
        assert kwargs["config_version"] is None  # omitted

    def test_deploy_managed_with_git_block_pins_latest(self, tmp_path: Path) -> None:
        """Once a credential is wired (parameters.dataApp.git present), the source
        pointer lives in Storage, so the latest configVersion is pinned."""
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _enc = _make_service(store)
        ds_mock.get_app.return_value = {"configId": "ulid", "hasManagedGitRepo": True}
        storage_mock.get_config_detail.return_value = {
            "version": 9,
            "configuration": {
                "parameters": {"dataApp": {"slug": "x", "git": {"repository": "https://g/r"}}}
            },
        }
        ds_mock.patch_app.return_value = {"state": "starting"}

        service.deploy_data_app(alias="prod", app_id="42")

        kwargs = ds_mock.patch_app.call_args.kwargs
        assert kwargs["config_version"] == "9"  # pinned


class TestDataAppBindManagedCredential:
    def test_wires_encrypted_git_block(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, encrypt_mock = _make_service(store)
        ds_mock.get_app.return_value = {"configId": "ulid", "hasManagedGitRepo": True}
        ds_mock.get_git_repo.return_value = {
            "httpsUrl": "https://git.example.com/keboola/app-42.git",
            "isManagedGitRepo": True,
        }
        ds_mock.create_git_credential.return_value = {"id": "cred-1", "secret": "tok-xyz"}
        storage_mock.get_config_detail.return_value = {
            "configuration": {"parameters": {"dataApp": {"slug": "x"}}}
        }
        storage_mock.update_config.return_value = {"version": "9"}

        result = service.bind_managed_credential("prod", "42", branch="main")

        # Minted an http_token on the app
        cred_kwargs = ds_mock.create_git_credential.call_args.kwargs
        assert cred_kwargs["type_"] == "http_token"
        # Encrypted the minted secret (never written plaintext)
        enc_kwargs = encrypt_mock.encrypt.call_args.kwargs
        assert enc_kwargs["input_data"] == {"#password": "tok-xyz"}
        # Wrote an encrypted git block into the config
        cfg = storage_mock.update_config.call_args.kwargs["configuration"]
        git = cfg["parameters"]["dataApp"]["git"]
        assert git["repository"] == "https://git.example.com/keboola/app-42.git"
        assert git["#password"].startswith("KBC::Project")
        assert git["private"] is True
        # Result redacts the ciphertext
        assert result["git"]["#password"] == "<encrypted>"
        assert result["repository"].endswith("app-42.git")

    def test_rejects_non_managed_app(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)
        ds_mock.get_app.return_value = {"configId": "ulid", "hasManagedGitRepo": False}
        with pytest.raises(KeboolaApiError) as excinfo:
            service.bind_managed_credential("prod", "42")
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR
        ds_mock.create_git_credential.assert_not_called()

    def test_dry_run_mints_nothing_and_leaves_config_untouched(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, encrypt_mock = _make_service(store)
        ds_mock.get_app.return_value = {"configId": "ulid", "hasManagedGitRepo": True}
        ds_mock.get_git_repo.return_value = {
            "httpsUrl": "https://git.example.com/keboola/app-42.git",
            "isManagedGitRepo": True,
        }

        result = service.bind_managed_credential("prod", "42", branch="main", dry_run=True)

        assert result["dry_run"] is True
        assert result["repository"] == "https://git.example.com/keboola/app-42.git"
        # No credential minted, nothing encrypted, config left untouched.
        ds_mock.create_git_credential.assert_not_called()
        encrypt_mock.encrypt.assert_not_called()
        storage_mock.update_config.assert_not_called()


class TestDataAppRuns:
    def test_normalizes_runs_and_failure_reason(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)
        ds_mock.list_app_runs.return_value = [
            {
                "id": "run-1",
                "state": "failed",
                "createdAt": "2026-06-20T10:00:00Z",
                "failureReason": {"reason": "StartupProbeFailed", "message": "clone failed"},
                "startupLogs": "Cloning into '/app'...",
            }
        ]
        result = service.list_app_runs("prod", "42", limit=3)
        assert result["count"] == 1
        run = result["runs"][0]
        assert run["state"] == "failed"
        assert run["failure_reason"]["reason"] == "StartupProbeFailed"
        ds_mock.list_app_runs.assert_called_once_with("42", limit=3)


class TestDataAppStartStop:
    def test_start_does_not_send_config_version(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)
        ds_mock.patch_app.return_value = {"state": "starting"}

        service.start_data_app(alias="prod", app_id="42")

        kwargs = ds_mock.patch_app.call_args.kwargs
        assert kwargs["desired_state"] == "running"
        assert kwargs["restart_if_running"] is True
        assert "config_version" not in kwargs

    def test_stop_sends_only_desired_state(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)
        ds_mock.patch_app.return_value = {"state": "stopping"}

        service.stop_data_app(alias="prod", app_id="42")
        kwargs = ds_mock.patch_app.call_args.kwargs
        assert kwargs["desired_state"] == "stopped"
        assert "config_version" not in kwargs
        assert "restart_if_running" not in kwargs


# ---------------------------------------------------------------------------
# Poll loop -- writeup §8 pitfall #1
# ---------------------------------------------------------------------------


class TestDataAppPoll:
    def test_stopped_is_not_terminal_during_initial_deploy(self, tmp_path: Path) -> None:
        """While desiredState=running, observing state=stopped MUST NOT exit
        the poll. The platform transitions created -> stopped -> starting ->
        running on initial deploy."""
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)

        # Sequence: stopped (transient) -> starting -> running
        ds_mock.get_app.side_effect = [
            {"state": "stopped", "desiredState": "running"},
            {"state": "starting", "desiredState": "running"},
            {
                "state": "running",
                "desiredState": "running",
                "url": "https://x.hub.example.com",
            },
        ]

        with patch("keboola_agent_cli.services.data_app_service.time.sleep", lambda _: None):
            result = service._poll_until_terminal(
                ds_mock,
                "42",
                target_desired_state="running",
                timeout_seconds=60.0,
            )
        assert result["state"] == "running"
        assert ds_mock.get_app.call_count == 3

    def test_error_state_raises(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)
        ds_mock.get_app.return_value = {"state": "error"}

        with (
            patch("keboola_agent_cli.services.data_app_service.time.sleep", lambda _: None),
            pytest.raises(KeboolaApiError) as excinfo,
        ):
            service._poll_until_terminal(
                ds_mock,
                "42",
                target_desired_state="running",
                timeout_seconds=60.0,
            )
        assert excinfo.value.error_code == ErrorCode.DATA_APP_BUILD_FAILED

    def test_timeout_raises(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)
        ds_mock.get_app.return_value = {"state": "starting"}

        # time.monotonic returns 0 then 100 -> exceeds the 1-second deadline
        # immediately on the second iteration.
        with (
            patch("keboola_agent_cli.services.data_app_service.time.sleep", lambda _: None),
            patch(
                "keboola_agent_cli.services.data_app_service.time.monotonic",
                side_effect=[0.0, 100.0, 200.0],
            ),
            pytest.raises(KeboolaApiError) as excinfo,
        ):
            service._poll_until_terminal(
                ds_mock,
                "42",
                target_desired_state="running",
                timeout_seconds=1.0,
            )
        assert excinfo.value.error_code == ErrorCode.DATA_APP_DEPLOY_TIMEOUT

    def test_timeout_surfaces_managed_clone_failure_and_hint(self, tmp_path: Path) -> None:
        """On timeout, the latest run's failure_reason is fetched and, for a
        managed-repo clone-auth failure, an actionable git-bind-credential hint
        is appended."""
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)
        ds_mock.get_app.return_value = {
            "state": "stopped",
            "desiredState": "stopped",
            "hasManagedGitRepo": True,
        }
        ds_mock.list_app_runs.return_value = [
            {
                "id": "run-1",
                "state": "failed",
                "failureReason": {
                    "reason": "StartupProbeFailed",
                    "message": (
                        "app_setup\nCloning into '/app'...\n"
                        "fatal: could not read Username for 'https://git.example.com'"
                    ),
                },
            }
        ]
        with (
            patch("keboola_agent_cli.services.data_app_service.time.sleep", lambda _: None),
            patch(
                "keboola_agent_cli.services.data_app_service.time.monotonic",
                side_effect=[0.0, 100.0, 200.0],
            ),
            pytest.raises(KeboolaApiError) as excinfo,
        ):
            service._poll_until_terminal(
                ds_mock, "42", target_desired_state="running", timeout_seconds=1.0
            )
        exc = excinfo.value
        assert exc.error_code == ErrorCode.DATA_APP_DEPLOY_TIMEOUT
        assert "StartupProbeFailed" in exc.message
        assert "could not read Username" in exc.message
        assert "git-bind-credential" in exc.message
        assert exc.details["failure_reason"]["reason"] == "StartupProbeFailed"

    def test_diagnostic_is_best_effort(self, tmp_path: Path) -> None:
        """A failing runs fetch must NOT mask the original timeout error."""
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)
        ds_mock.get_app.return_value = {"state": "starting", "desiredState": "running"}
        ds_mock.list_app_runs.side_effect = RuntimeError("network down")
        with (
            patch("keboola_agent_cli.services.data_app_service.time.sleep", lambda _: None),
            patch(
                "keboola_agent_cli.services.data_app_service.time.monotonic",
                side_effect=[0.0, 100.0, 200.0],
            ),
            pytest.raises(KeboolaApiError) as excinfo,
        ):
            service._poll_until_terminal(
                ds_mock, "42", target_desired_state="running", timeout_seconds=1.0
            )
        assert excinfo.value.error_code == ErrorCode.DATA_APP_DEPLOY_TIMEOUT


# ---------------------------------------------------------------------------
# Password retrieval
# ---------------------------------------------------------------------------


class TestDataAppPassword:
    def test_returns_password(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)
        ds_mock.get_app_password.return_value = {"password": "deadbeefcafe"}

        result = service.get_data_app_password(
            alias="prod", app_id="42", manage_token=TEST_MANAGE_TOKEN
        )
        assert result["password"] == "deadbeefcafe"
        ds_mock.get_app_password.assert_called_once_with("42", manage_token=TEST_MANAGE_TOKEN)

    def test_missing_manage_token(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, _ds, _storage, _enc = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            service.get_data_app_password(alias="prod", app_id="42", manage_token="")
        assert excinfo.value.error_code == ErrorCode.INVALID_TOKEN


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDataAppDelete:
    def test_delete_calls_data_science_delete(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)
        result = service.delete_data_app(alias="prod", app_id="42")
        ds_mock.delete_app.assert_called_once_with("42")
        assert result["deleted"] is True
        assert result["app_id"] == "42"


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


class TestDataAppDetail:
    def test_detail_merges_data_science_and_storage(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _enc = _make_service(store)
        ds_mock.get_app.return_value = {
            "id": "42",
            "configId": "ulid",
            "state": "running",
            "desiredState": "running",
            "url": "https://x.hub.example.com",
            "configVersion": "3",
            "type": "python-js",
            "size": "tiny",
            "autoSuspendAfterSeconds": 900,
            "lastStartTimestamp": "2026-05-01T00:00:00Z",
        }
        storage_mock.get_config_detail.return_value = {
            "version": 5,
            "name": "App",
            "description": "long",
            "configuration": {
                "parameters": {
                    "dataApp": {
                        "slug": "my-app",
                        "git": {
                            "repository": "https://github.com/o/r",
                            "private": True,
                            "username": "user",
                            "#password": "KBC::ProjectSecure::xyz",
                            "branch": "main",
                        },
                    },
                    "id": "42",
                },
                "runtime": {"backend": {"size": "tiny"}},
            },
        }
        result = service.get_data_app(alias="prod", app_id="42")

        assert result["app_id"] == "42"
        assert result["state"] == "running"
        assert result["config_version_storage"] == "5"
        assert result["config_version_deployed"] == "3"
        assert result["slug"] == "my-app"
        # PAT redaction
        assert result["git"]["#password"] == "<encrypted>"
        # Plaintext repository / branch preserved.
        assert result["git"]["repository"] == "https://github.com/o/r"


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


class TestRedactGitBlock:
    def test_redacts_encrypted_password(self) -> None:
        block = {"#password": "KBC::Project::xyz", "username": "u"}
        out = _redact_git_block(block)
        assert out["#password"] == "<encrypted>"
        assert out["username"] == "u"

    def test_no_password_key_is_noop(self) -> None:
        block = {"username": "u", "repository": "https://x"}
        out = _redact_git_block(block)
        assert out == block


class TestRedactStorageConfig:
    def test_redacts_nested_password(self) -> None:
        cfg = {
            "id": "ulid",
            "version": 3,
            "configuration": {
                "parameters": {
                    "dataApp": {
                        "slug": "x",
                        "git": {
                            "repository": "https://github.com/o/r",
                            "#password": "KBC::ProjectSecureGKMS::deadbeef",
                            "username": "u",
                        },
                    }
                }
            },
        }
        out = _redact_storage_config(cfg)
        assert out["configuration"]["parameters"]["dataApp"]["git"]["#password"] == "<encrypted>"
        # Original input not mutated (function returns a deep-copy of the
        # affected branch).
        assert (
            cfg["configuration"]["parameters"]["dataApp"]["git"]["#password"]
            == "KBC::ProjectSecureGKMS::deadbeef"
        )

    def test_no_git_block_is_noop(self) -> None:
        cfg = {"id": "ulid", "configuration": {"parameters": {}}}
        out = _redact_storage_config(cfg)
        assert out == cfg

    def test_empty_dict_passes_through(self) -> None:
        assert _redact_storage_config({}) == {}


# ---------------------------------------------------------------------------
# List service — output key rename to `app_id` (v0.33.0)
# ---------------------------------------------------------------------------


class TestDataAppListOutputKeys:
    """Lock the v0.33.0 JSON output rename ``id`` -> ``app_id``.

    Prior to v0.33.0 ``list_data_apps`` emitted the data-app's own
    identifier as bare ``id``. The renamed key matches the ``--app-id``
    input flag and the rest of kbagent's CLI convention (e.g.
    ``config_id``, ``bucket_id``, ``table_id``).
    """

    def test_list_emits_app_id_key(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _enc = _make_service(store)
        ds_mock.list_apps.return_value = [
            {
                "id": "43661269",
                "configId": "01kqj88t0vktxe0vfhk6ps5kzs",
                "type": "python-js",
                "state": "running",
                "desiredState": "running",
                "configVersion": "3",
                "url": "https://x.hub.example.com",
                "size": "tiny",
                "autoSuspendAfterSeconds": 900,
                "lastStartTimestamp": "2026-05-01T00:00:00Z",
            }
        ]
        storage_mock.list_component_configs.return_value = [
            {"id": "01kqj88t0vktxe0vfhk6ps5kzs", "name": "App"}
        ]

        result = service.list_data_apps(aliases=["prod"])

        assert result["errors"] == []
        assert len(result["apps"]) == 1
        app = result["apps"][0]
        assert app["app_id"] == "43661269"
        # Regression guard: pre-0.33.0 callers would have read ``app["id"]``.
        assert "id" not in app
        assert app["config_id"] == "01kqj88t0vktxe0vfhk6ps5kzs"
        assert app["name"] == "App"


class TestDataAppEnvelopesNoBareIdKey:
    """Regression guard: NO ``DataAppService`` envelope emits the legacy bare
    ``id`` key. The id key was renamed to ``app_id`` in v0.33.0; future
    edits that accidentally re-add ``"id":`` to any envelope must fail here.

    Covers every method whose return dict carries the data-app identifier:
    detail, create, deploy, start, stop, delete, password, secrets-* (set,
    list, get, remove). ``list_data_apps`` is covered by
    ``TestDataAppListOutputKeys``.
    """

    def test_detail_envelope(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _enc = _make_service(store)
        ds_mock.get_app.return_value = {"id": "42", "configId": "ulid", "state": "running"}
        storage_mock.get_config_detail.return_value = {"version": "3", "name": "App"}
        result = service.get_data_app(alias="prod", app_id="42")
        assert result["app_id"] == "42"
        assert "id" not in result

    def test_create_envelope(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _enc = _make_service(store)
        ds_mock.create_app.return_value = {"id": "42", "configId": "ulid"}
        storage_mock.update_config.return_value = {"version": "2"}
        result = service.create_data_app(
            alias="prod",
            name="App",
            description="",
            slug="my-app",
            git_repo="https://github.com/o/r",
            git_public=True,
            auth="password",
            size="tiny",
            auto_suspend_after_seconds=900,
            type_="python-js",
            deploy=False,
            wait=False,
            dry_run=False,
        )
        assert result["app_id"] == "42"
        assert "id" not in result

    def test_deploy_envelope(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _enc = _make_service(store)
        ds_mock.get_app.return_value = {"configId": "ulid"}
        storage_mock.get_config_detail.return_value = {"version": "3"}
        ds_mock.patch_app.return_value = {"state": "starting", "desiredState": "running"}
        result = service.deploy_data_app(alias="prod", app_id="42")
        assert result["app_id"] == "42"
        assert "id" not in result

    def test_start_envelope(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)
        ds_mock.patch_app.return_value = {"state": "starting", "desiredState": "running"}
        result = service.start_data_app(alias="prod", app_id="42")
        assert result["app_id"] == "42"
        assert "id" not in result

    def test_stop_envelope(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)
        ds_mock.patch_app.return_value = {"state": "stopping", "desiredState": "stopped"}
        result = service.stop_data_app(alias="prod", app_id="42")
        assert result["app_id"] == "42"
        assert "id" not in result

    def test_delete_envelope(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, _ds, _storage, _enc = _make_service(store)
        result = service.delete_data_app(alias="prod", app_id="42")
        assert result["app_id"] == "42"
        assert "id" not in result

    def test_password_envelope(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)
        ds_mock.get_app_password.return_value = {"password": "deadbeefcafe"}
        result = service.get_data_app_password(
            alias="prod", app_id="42", manage_token=TEST_MANAGE_TOKEN
        )
        assert result["app_id"] == "42"
        assert "id" not in result

    def test_secrets_set_envelope(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, encrypt_mock = _make_service(store)
        encrypt_mock.encrypt.return_value = {"#API_KEY": "KBC::ProjectSecureGKMS::xyz"}
        ds_mock.get_app.return_value = {"configId": "ulid"}
        storage_mock.get_config_detail.return_value = {
            "version": "5",
            "configuration": {"parameters": {"dataApp": {"slug": "x"}}},
        }
        storage_mock.update_config.return_value = {"version": "6"}
        result = service.set_data_app_secrets(
            alias="prod", app_id="42", secrets={"#API_KEY": "plaintext"}
        )
        assert result["app_id"] == "42"
        assert "id" not in result

    def test_secrets_list_envelope(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _enc = _make_service(store)
        ds_mock.get_app.return_value = {"configId": "ulid"}
        storage_mock.get_config_detail.return_value = {
            "version": "5",
            "configuration": {
                "parameters": {
                    "dataApp": {
                        "slug": "x",
                        "secrets": {"#FOO": "KBC::ProjectSecureGKMS::abc"},
                    }
                }
            },
        }
        result = service.list_data_app_secrets(alias="prod", app_id="42")
        assert result["app_id"] == "42"
        assert "id" not in result

    def test_secrets_get_envelope(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _enc = _make_service(store)
        ds_mock.get_app.return_value = {"configId": "ulid"}
        storage_mock.get_config_detail.return_value = {
            "version": "5",
            "configuration": {
                "parameters": {
                    "dataApp": {
                        "slug": "x",
                        "secrets": {"#FOO": "KBC::ProjectSecureGKMS::abc"},
                    }
                }
            },
        }
        result = service.get_data_app_secret(alias="prod", app_id="42", key="#FOO")
        assert result["app_id"] == "42"
        assert "id" not in result

    def test_secrets_remove_envelope(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _enc = _make_service(store)
        ds_mock.get_app.return_value = {"configId": "ulid"}
        storage_mock.get_config_detail.return_value = {
            "version": "5",
            "configuration": {
                "parameters": {
                    "dataApp": {
                        "slug": "x",
                        "secrets": {"#FOO": "KBC::ProjectSecureGKMS::abc"},
                    }
                }
            },
        }
        storage_mock.update_config.return_value = {"version": "6"}
        result = service.remove_data_app_secrets(alias="prod", app_id="42", keys=["#FOO"])
        assert result["app_id"] == "42"
        assert "id" not in result


# ---------------------------------------------------------------------------
# data-app logs (service-layer)
# ---------------------------------------------------------------------------


class TestDataAppLogs:
    """Service-layer tests for DataAppService.get_app_logs.

    Verifies orchestration (kwarg passthrough, mutex guard, client cleanup
    in finally, envelope shape, project resolution) -- NOT HTTP shapes
    (those live in TestTailAppLogsClient below).
    """

    SAMPLE_LOGS = (
        "[TIMING] Starting: input_mapping_init\n"
        "[TIMING] Completed: input_mapping_init (took 0.031s)\n"
        "[TIMING] Starting: git_clone\n"
        "Cloning into '/app'...\n"
        "supervisord started with pid 1\n"
    )

    def test_logs_with_lines_passes_kwarg_to_client(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)
        ds_mock.tail_app_logs.return_value = self.SAMPLE_LOGS

        result = service.get_app_logs(alias="prod", app_id="42", lines=500)

        ds_mock.tail_app_logs.assert_called_once_with("42", lines=500, since=None)
        assert result["lines_requested"] == 500
        assert result["since_requested"] is None
        assert result["lines_returned"] == 5
        assert result["text"] == self.SAMPLE_LOGS
        assert result["app_id"] == "42"
        assert result["project_alias"] == "prod"
        ds_mock.close.assert_called_once()

    def test_logs_with_since_passes_kwarg_to_client(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)
        ds_mock.tail_app_logs.return_value = self.SAMPLE_LOGS

        result = service.get_app_logs(alias="prod", app_id="42", since="2026-05-21T13:00:00Z")

        ds_mock.tail_app_logs.assert_called_once_with(
            "42", lines=None, since="2026-05-21T13:00:00Z"
        )
        assert result["lines_requested"] is None
        assert result["since_requested"] == "2026-05-21T13:00:00Z"

    def test_logs_buffer_all_sends_no_params(self, tmp_path: Path) -> None:
        """``lines=None, since=None`` is the CLI's ``--lines 0`` semantics."""
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)
        ds_mock.tail_app_logs.return_value = self.SAMPLE_LOGS

        result = service.get_app_logs(alias="prod", app_id="42")

        ds_mock.tail_app_logs.assert_called_once_with("42", lines=None, since=None)
        assert result["lines_requested"] is None
        assert result["since_requested"] is None

    def test_logs_mutex_raises_invalid_argument(self, tmp_path: Path) -> None:
        """Both lines+since -> KeboolaApiError(INVALID_ARGUMENT) BEFORE client call."""
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)

        with pytest.raises(KeboolaApiError) as excinfo:
            service.get_app_logs(alias="prod", app_id="42", lines=100, since="2026-05-21T13:00:00Z")

        assert excinfo.value.error_code == ErrorCode.INVALID_ARGUMENT
        assert "mutually exclusive" in excinfo.value.message
        ds_mock.tail_app_logs.assert_not_called()
        # No client created when the mutex guard fires; close is not called.
        ds_mock.close.assert_not_called()

    def test_logs_negative_lines_raises_invalid_argument(self, tmp_path: Path) -> None:
        """Service-layer guard for `kbagent serve` REST callers."""
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)

        with pytest.raises(KeboolaApiError) as excinfo:
            service.get_app_logs(alias="prod", app_id="42", lines=-5)

        assert excinfo.value.error_code == ErrorCode.INVALID_ARGUMENT
        assert "positive integer" in excinfo.value.message
        ds_mock.tail_app_logs.assert_not_called()

    def test_logs_invalid_since_format_raises_invalid_argument(self, tmp_path: Path) -> None:
        """Service-layer guard rejects garbage --since before the round-trip."""
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)

        with pytest.raises(KeboolaApiError) as excinfo:
            service.get_app_logs(alias="prod", app_id="42", since="yesterday")

        assert excinfo.value.error_code == ErrorCode.INVALID_ARGUMENT
        assert "ISO 8601" in excinfo.value.message
        ds_mock.tail_app_logs.assert_not_called()

    def test_logs_naive_since_raises_invalid_argument(self, tmp_path: Path) -> None:
        """Service-layer guard rejects naive (no-tz) --since before the round-trip."""
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)

        with pytest.raises(KeboolaApiError) as excinfo:
            service.get_app_logs(alias="prod", app_id="42", since="2026-05-21T13:00:00")

        assert excinfo.value.error_code == ErrorCode.INVALID_ARGUMENT
        assert "timezone" in excinfo.value.message
        ds_mock.tail_app_logs.assert_not_called()

    def test_logs_empty_response_returns_zero_lines(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)
        ds_mock.tail_app_logs.return_value = ""

        result = service.get_app_logs(alias="prod", app_id="42", lines=100)

        assert result["lines_returned"] == 0
        assert result["text"] == ""

    def test_logs_http_error_propagates(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)
        ds_mock.tail_app_logs.side_effect = KeboolaApiError(
            message='App "42" is not running',
            status_code=400,
            error_code=ErrorCode.API_ERROR,
            retryable=False,
        )

        with pytest.raises(KeboolaApiError) as excinfo:
            service.get_app_logs(alias="prod", app_id="42", lines=100)

        assert excinfo.value.status_code == 400
        ds_mock.close.assert_called_once()  # finally block ran

    def test_logs_client_closed_on_exception(self, tmp_path: Path) -> None:
        """Client.close() must run even when tail_app_logs raises unexpectedly."""
        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)
        ds_mock.tail_app_logs.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError):
            service.get_app_logs(alias="prod", app_id="42", lines=100)

        ds_mock.close.assert_called_once()

    def test_logs_unknown_alias_raises_config_error(self, tmp_path: Path) -> None:
        """resolve_projects raises before any client is created."""
        from keboola_agent_cli.errors import ConfigError

        store = _make_store(tmp_path)
        service, ds_mock, _storage, _enc = _make_service(store)

        with pytest.raises(ConfigError):
            service.get_app_logs(alias="nonexistent", app_id="42", lines=100)

        ds_mock.tail_app_logs.assert_not_called()
        ds_mock.close.assert_not_called()


# ---------------------------------------------------------------------------
# data-app logs (client HTTP layer via httpx_mock)
# ---------------------------------------------------------------------------


class TestTailAppLogsClient:
    """Client-layer round-trip tests for DataScienceClient.tail_app_logs.

    Asserts URL composition, query-param encoding (notably ``+`` in
    timezone offsets), and plain-text body passthrough.
    """

    DATA_SCIENCE_BASE = "https://data-science.keboola.com"

    def test_tail_app_logs_with_lines_url_and_params(self, httpx_mock) -> None:
        from keboola_agent_cli.data_science_client import DataScienceClient

        httpx_mock.add_response(
            url=f"{self.DATA_SCIENCE_BASE}/apps/42/logs/tail?lines=500",
            text="line1\nline2\nline3\n",
            status_code=200,
            headers={"content-type": "text/plain"},
        )

        with DataScienceClient(
            stack_url="https://connection.keboola.com",
            token="901-test-token",
        ) as client:
            text = client.tail_app_logs("42", lines=500)

        assert text == "line1\nline2\nline3\n"

    def test_tail_app_logs_with_since_url_encodes_plus(self, httpx_mock) -> None:
        """Timezone offset ``+00:00`` must be URL-encoded as ``%2B00%3A00``."""
        from keboola_agent_cli.data_science_client import DataScienceClient

        # httpx auto-encodes the params dict; assert against the encoded URL.
        httpx_mock.add_response(
            url=(
                f"{self.DATA_SCIENCE_BASE}/apps/42/logs/tail"
                "?since=2026-05-21T13%3A00%3A00%2B00%3A00"
            ),
            text="",
            status_code=200,
        )

        with DataScienceClient(
            stack_url="https://connection.keboola.com",
            token="901-test-token",
        ) as client:
            text = client.tail_app_logs("42", since="2026-05-21T13:00:00+00:00")

        assert text == ""

    def test_tail_app_logs_no_params_sends_clean_url(self, httpx_mock) -> None:
        """Neither lines nor since -> URL with no query string."""
        from keboola_agent_cli.data_science_client import DataScienceClient

        httpx_mock.add_response(
            url=f"{self.DATA_SCIENCE_BASE}/apps/42/logs/tail",
            text="full buffer\n",
            status_code=200,
        )

        with DataScienceClient(
            stack_url="https://connection.keboola.com",
            token="901-test-token",
        ) as client:
            text = client.tail_app_logs("42")

        assert text == "full buffer\n"
