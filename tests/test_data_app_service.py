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

TEST_TOKEN = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"
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
        kwargs = dict(
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

        assert result["id"] == "43661269"
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
        assert result["id"] == "42"


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

        assert result["id"] == "42"
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
