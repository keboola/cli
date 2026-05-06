"""Service-layer tests for DataAppService secrets methods.

Covers: read-modify-write sibling preservation, fail-closed encryption,
plaintext-absence on get, idempotent remove, reserved-name shadowing,
and the input-validation matrix (no '#' prefix, control chars,
already-encrypted plaintext, etc.).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.data_app_service import (
    DataAppService,
    _derive_runtime_env_var_name,
    _secret_fingerprint,
)

TEST_TOKEN = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"


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
        # Default: every input encrypts to a project-scoped ciphertext.
        encrypt_mock.encrypt.side_effect = lambda *, alias, component_id, input_data: {
            k: f"KBC::ProjectSecureGKMS::ciphertext-{k}" for k in input_data
        }
    service = DataAppService(
        config_store=store,
        client_factory=lambda url, token: storage_mock,
        ds_client_factory=lambda url, token: ds_mock,
        encrypt_service=encrypt_mock,
    )
    return service, ds_mock, storage_mock, encrypt_mock


def _baseline_config_envelope(version: str = "7") -> dict[str, Any]:
    """A representative Storage config envelope for a deployed data app.

    Has secrets, sibling git block, sibling slug + id under
    parameters.dataApp, sibling parameters.id, top-level runtime +
    authorization. The sibling-preservation test diffs against this.
    """
    return {
        "id": "01XYZCONFIGULID",
        "name": "Existing App",
        "version": version,
        "configuration": {
            "parameters": {
                "id": "12345",
                "autoSuspendAfterSeconds": 900,
                "dataApp": {
                    "slug": "existing-app",
                    "git": {
                        "repository": "https://github.com/o/r",
                        "private": True,
                        "username": "user",
                        "#password": "KBC::ProjectSecureGKMS::existing-pat",
                        "branch": "main",
                    },
                    "secrets": {
                        "#OTHER_SECRET": "KBC::ProjectSecureGKMS::other-existing",
                    },
                },
            },
            "runtime": {"backend": {"size": "small"}},
            "authorization": {"app_proxy": {"auth_providers": []}},
            "storage": {"input": {}},
        },
    }


def _ds_app_record(config_id: str = "01XYZCONFIGULID") -> dict[str, Any]:
    return {"id": "12345", "configId": config_id, "state": "running"}


# ---------------------------------------------------------------------------
# Helpers (pure, no I/O)
# ---------------------------------------------------------------------------


class TestRuntimeEnvVarTranslation:
    @pytest.mark.parametrize(
        "key,expected",
        [
            ("#KBC_TOKEN", "KBC_TOKEN"),
            ("#my-custom-var", "MY_CUSTOM_VAR"),
            ("#anthropic-api-key", "ANTHROPIC_API_KEY"),
            ("#X", "X"),
        ],
    )
    def test_canonical_examples(self, key: str, expected: str) -> None:
        assert _derive_runtime_env_var_name(key) == expected


class TestSecretFingerprint:
    def test_extracts_8_chars_after_prefix(self) -> None:
        ct = "KBC::ProjectSecureGKMS::abcdefgh12345678extra"
        assert _secret_fingerprint(ct) == "abcdefgh"

    def test_empty_for_non_ciphertext(self) -> None:
        assert _secret_fingerprint("plaintext") == ""
        assert _secret_fingerprint("") == ""


# ---------------------------------------------------------------------------
# secrets-set
# ---------------------------------------------------------------------------


class TestSetSecretsValidation:
    def test_empty_secrets_rejected(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as exc:
            service.set_data_app_secrets(alias="prod", app_id="12345", secrets={})
        assert exc.value.error_code == ErrorCode.DATA_APP_INVALID_SECRET

    @pytest.mark.parametrize(
        "bad_key",
        [
            "BAD",  # no #
            "#1bad",  # starts with digit
            "#bad key",  # space
            "#bad\x00null",  # NUL
            "#bad/slash",  # disallowed char
            "#",  # empty after #
        ],
    )
    def test_malformed_keys_rejected(self, tmp_path: Path, bad_key: str) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as exc:
            service.set_data_app_secrets(
                alias="prod",
                app_id="12345",
                secrets={bad_key: "value"},
            )
        assert exc.value.error_code == ErrorCode.DATA_APP_INVALID_SECRET

    def test_already_encrypted_value_rejected(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as exc:
            service.set_data_app_secrets(
                alias="prod",
                app_id="12345",
                secrets={"#API": "KBC::ProjectSecureGKMS::abc"},
            )
        assert exc.value.error_code == ErrorCode.DATA_APP_INVALID_SECRET


class TestSetSecretsHappyPath:
    def test_writes_ciphertext_and_returns_summary(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, encrypt_mock = _make_service(store)
        ds_mock.get_app.return_value = _ds_app_record()
        storage_mock.get_config_detail.return_value = _baseline_config_envelope("7")
        storage_mock.update_config.return_value = {"version": "8"}

        result = service.set_data_app_secrets(
            alias="prod",
            app_id="12345",
            secrets={"#API_KEY": "plaintext1", "#DB_URL": "plaintext2"},
        )

        assert result["secrets_set"] == ["API_KEY", "DB_URL"]
        assert result["config_version_before"] == "7"
        assert result["config_version_after"] == "8"
        assert result["deploy_required"] is True
        # Encryption was called per-key.
        encrypt_mock.encrypt.assert_called_once()
        # Storage write fired with the merged body.
        storage_mock.update_config.assert_called_once()
        kwargs = storage_mock.update_config.call_args.kwargs
        body = kwargs["configuration"]
        new_secrets = body["parameters"]["dataApp"]["secrets"]
        # New keys were added; existing #OTHER_SECRET is preserved.
        assert "#API_KEY" in new_secrets
        assert "#DB_URL" in new_secrets
        assert "#OTHER_SECRET" in new_secrets
        # Ciphertext shape is what encrypt_mock returned.
        assert new_secrets["#API_KEY"].startswith("KBC::ProjectSecureGKMS::")

    def test_sibling_keys_preserved_bit_identical(self, tmp_path: Path) -> None:
        """The sibling-preservation regression test from the plan (§5.5#4c).

        Hand-craft a config with extra keys at three nesting levels; assert
        that every untouched key is bit-identical after secrets-set.
        """
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _ = _make_service(store)
        ds_mock.get_app.return_value = _ds_app_record()
        envelope = _baseline_config_envelope("7")
        original_body = json.loads(json.dumps(envelope["configuration"]))
        storage_mock.get_config_detail.return_value = envelope
        storage_mock.update_config.return_value = {"version": "8"}

        service.set_data_app_secrets(
            alias="prod",
            app_id="12345",
            secrets={"#NEW_KEY": "plaintext"},
        )

        kwargs = storage_mock.update_config.call_args.kwargs
        new_body = kwargs["configuration"]

        # Every sibling key is preserved bit-identical.
        assert new_body["parameters"]["id"] == original_body["parameters"]["id"]
        assert (
            new_body["parameters"]["autoSuspendAfterSeconds"]
            == original_body["parameters"]["autoSuspendAfterSeconds"]
        )
        assert (
            new_body["parameters"]["dataApp"]["slug"]
            == original_body["parameters"]["dataApp"]["slug"]
        )
        assert (
            new_body["parameters"]["dataApp"]["git"]
            == original_body["parameters"]["dataApp"]["git"]
        )
        assert new_body["runtime"] == original_body["runtime"]
        assert new_body["authorization"] == original_body["authorization"]
        assert new_body["storage"] == original_body["storage"]
        # Existing sibling secret survives.
        assert (
            new_body["parameters"]["dataApp"]["secrets"]["#OTHER_SECRET"]
            == original_body["parameters"]["dataApp"]["secrets"]["#OTHER_SECRET"]
        )
        # New secret added.
        assert "#NEW_KEY" in new_body["parameters"]["dataApp"]["secrets"]


class TestSetSecretsFailClosed:
    def test_encryption_returning_plaintext_aborts_before_storage_write(
        self, tmp_path: Path
    ) -> None:
        store = _make_store(tmp_path)
        encrypt_mock = MagicMock()
        # Pretend encryption returned plaintext (no KBC:: prefix).
        encrypt_mock.encrypt.return_value = {"#API_KEY": "still-plaintext"}
        service, ds_mock, storage_mock, _ = _make_service(store, encrypt_mock=encrypt_mock)
        ds_mock.get_app.return_value = _ds_app_record()
        storage_mock.get_config_detail.return_value = _baseline_config_envelope("7")

        with pytest.raises(KeboolaApiError) as exc:
            service.set_data_app_secrets(
                alias="prod",
                app_id="12345",
                secrets={"#API_KEY": "plaintext"},
            )
        assert exc.value.error_code == ErrorCode.ENCRYPTION_FAILED
        # Critical: Storage write must NOT have fired.
        storage_mock.update_config.assert_not_called()

    def test_allow_plaintext_flag_writes_anyway(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        encrypt_mock = MagicMock()
        encrypt_mock.encrypt.return_value = {"#API_KEY": "still-plaintext"}
        service, ds_mock, storage_mock, _ = _make_service(store, encrypt_mock=encrypt_mock)
        ds_mock.get_app.return_value = _ds_app_record()
        storage_mock.get_config_detail.return_value = _baseline_config_envelope("7")
        storage_mock.update_config.return_value = {"version": "8"}

        result = service.set_data_app_secrets(
            alias="prod",
            app_id="12345",
            secrets={"#API_KEY": "plaintext"},
            allow_plaintext_on_encrypt_failure=True,
        )
        # Storage write fires with the (plaintext) value.
        storage_mock.update_config.assert_called_once()
        assert result["secrets_set"] == ["API_KEY"]


class TestSetSecretsReservedNames:
    def test_kbc_token_collision_emits_shadowed_field(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _ = _make_service(store)
        ds_mock.get_app.return_value = _ds_app_record()
        storage_mock.get_config_detail.return_value = _baseline_config_envelope("7")
        storage_mock.update_config.return_value = {"version": "8"}

        result = service.set_data_app_secrets(
            alias="prod",
            app_id="12345",
            secrets={"#KBC_TOKEN": "stolen-token"},
        )
        # WARN, not BLOCKING -- the secret IS still written.
        assert "KBC_TOKEN" in result["shadowed_by_runtime"]
        storage_mock.update_config.assert_called_once()


class TestSetSecretsDryRun:
    def test_dry_run_skips_api_calls(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, encrypt_mock = _make_service(store)
        ds_mock.get_app.return_value = _ds_app_record()
        storage_mock.get_config_detail.return_value = _baseline_config_envelope("7")

        result = service.set_data_app_secrets(
            alias="prod",
            app_id="12345",
            secrets={"#KEY": "value"},
            dry_run=True,
        )
        assert result["dry_run"] is True
        encrypt_mock.encrypt.assert_not_called()
        storage_mock.update_config.assert_not_called()


# ---------------------------------------------------------------------------
# secrets-list / secrets-get / secrets-remove
# ---------------------------------------------------------------------------


class TestListSecrets:
    def test_returns_metadata_no_ciphertext_in_full(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _ = _make_service(store)
        ds_mock.get_app.return_value = _ds_app_record()
        storage_mock.get_config_detail.return_value = _baseline_config_envelope()

        result = service.list_data_app_secrets(alias="prod", app_id="12345")
        assert result["count"] == 1
        assert result["secrets"][0]["key"] == "#OTHER_SECRET"
        assert result["secrets"][0]["env_var"] == "OTHER_SECRET"
        # Default omits fingerprint.
        assert "fingerprint" not in result["secrets"][0]

    def test_show_fingerprint_includes_metadata(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _ = _make_service(store)
        ds_mock.get_app.return_value = _ds_app_record()
        storage_mock.get_config_detail.return_value = _baseline_config_envelope()

        result = service.list_data_app_secrets(alias="prod", app_id="12345", show_fingerprint=True)
        assert result["secrets"][0]["fingerprint"] != ""
        assert result["secrets"][0]["encryption_prefix"].startswith("KBC::ProjectSecure")

    def test_no_secrets_returns_empty_list(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _ = _make_service(store)
        ds_mock.get_app.return_value = _ds_app_record()
        envelope = _baseline_config_envelope()
        envelope["configuration"]["parameters"]["dataApp"]["secrets"] = {}
        storage_mock.get_config_detail.return_value = envelope

        result = service.list_data_app_secrets(alias="prod", app_id="12345")
        assert result["count"] == 0
        assert result["secrets"] == []


class TestGetSecret:
    def test_returns_metadata_only(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _ = _make_service(store)
        ds_mock.get_app.return_value = _ds_app_record()
        storage_mock.get_config_detail.return_value = _baseline_config_envelope()

        result = service.get_data_app_secret(alias="prod", app_id="12345", key="#OTHER_SECRET")
        assert result["present"] is True
        assert result["env_var"] == "OTHER_SECRET"
        # Plaintext absence: the only string fields are the public metadata.
        # Verify the message explicitly says the plaintext is NOT exposed.
        assert "NOT exposed" in result["message"]
        # Verify the encrypted ciphertext does NOT appear in the response.
        ct = "KBC::ProjectSecureGKMS::other-existing"
        for value in result.values():
            if isinstance(value, str):
                assert ct not in value

    def test_absent_key_raises_not_found_without_enumerating_siblings(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _ = _make_service(store)
        ds_mock.get_app.return_value = _ds_app_record()
        storage_mock.get_config_detail.return_value = _baseline_config_envelope()

        with pytest.raises(KeboolaApiError) as exc:
            service.get_data_app_secret(alias="prod", app_id="12345", key="#MISSING")
        assert exc.value.error_code == ErrorCode.NOT_FOUND
        # Sibling key '#OTHER_SECRET' must NOT appear in the error message.
        assert "#OTHER_SECRET" not in exc.value.message
        assert "OTHER_SECRET" not in exc.value.message


class TestRemoveSecrets:
    def test_idempotent_when_keys_absent(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _ = _make_service(store)
        ds_mock.get_app.return_value = _ds_app_record()
        storage_mock.get_config_detail.return_value = _baseline_config_envelope("7")

        result = service.remove_data_app_secrets(
            alias="prod",
            app_id="12345",
            keys=["#NOT_PRESENT"],
        )
        assert result["removed"] == []
        assert "NOT_PRESENT" in result["not_found"]
        assert result["deploy_required"] is False
        # No Storage write on no-op.
        storage_mock.update_config.assert_not_called()

    def test_removes_existing_key(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, ds_mock, storage_mock, _ = _make_service(store)
        ds_mock.get_app.return_value = _ds_app_record()
        storage_mock.get_config_detail.return_value = _baseline_config_envelope("7")
        storage_mock.update_config.return_value = {"version": "8"}

        result = service.remove_data_app_secrets(
            alias="prod",
            app_id="12345",
            keys=["#OTHER_SECRET"],
        )
        assert "OTHER_SECRET" in result["removed"]
        assert result["deploy_required"] is True
        # Storage write fired without the removed key.
        kwargs = storage_mock.update_config.call_args.kwargs
        assert "#OTHER_SECRET" not in kwargs["configuration"]["parameters"]["dataApp"].get(
            "secrets", {}
        )
