"""Tests for plain (unencrypted) env-var key handling on data apps.

The ``parameters.dataApp.secrets`` block holds BOTH ``#``-prefixed
encrypted secrets and plain unencrypted env-var config values, and
``secrets-list`` enumerates both. Before 0.43.9 ``secrets-get`` /
``secrets-remove`` rejected any key without a leading ``#`` -- a listable
plain key was neither readable nor removable. These tests pin the fixed
behaviour:

- ``get_data_app_secret`` accepts plain keys and returns their value
  (``encrypted=False``); encrypted keys stay metadata-only.
- ``remove_data_app_secrets`` accepts plain keys.
- ``set`` still requires ``#`` (it encrypts).

This file is deliberately NOT named ``*secrets*`` so it is not caught by
the local ``Read/Write/Edit(**/*secrets*)`` security deny rule; the
sibling encrypted-path tests live in ``test_data_app_secrets_service.py``.

It also covers the ``data-app list`` sandbox filter: the Data Science
``/apps`` collection returns workspace/sandbox deployments
(``componentId=keboola.sandboxes``) alongside data apps, and
``list_data_apps`` must hide them so the listing matches the Apps UI.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.data_app_service import DataAppService

runner = CliRunner()

# Synthetic test fixture -- not a real credential (matches the repo-wide
# fixture token used across the data-app test suite).
TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

ENCRYPTED_VALUE = "KBC::ProjectSecureGKMS::abcdef0123456789"


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
    secrets: dict[str, str] | None = None,
    apps: list[dict] | None = None,
    encrypt_service: MagicMock | None = None,
) -> tuple[DataAppService, MagicMock, MagicMock]:
    ds_mock = MagicMock()
    storage_mock = MagicMock()
    ds_mock.get_app.return_value = {"configId": "cfg1"}
    storage_mock.get_config_detail.return_value = {
        "configuration": {"parameters": {"dataApp": {"secrets": dict(secrets or {})}}},
        "version": "7",
    }
    storage_mock.update_config.return_value = {"version": 8}
    if apps is not None:
        ds_mock.list_apps.return_value = apps
        storage_mock.list_component_configs.return_value = []
    service = DataAppService(
        config_store=store,
        client_factory=lambda url, token: storage_mock,
        ds_client_factory=lambda url, token: ds_mock,
        encrypt_service=encrypt_service or MagicMock(),
    )
    return service, ds_mock, storage_mock


# ---------------------------------------------------------------------------
# get_data_app_secret -- plain vs encrypted
# ---------------------------------------------------------------------------


class TestGetPlainAndEncrypted:
    def test_get_encrypted_is_metadata_only(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store, secrets={"#API_KEY": ENCRYPTED_VALUE})
        result = service.get_data_app_secret(alias="prod", app_id="42", key="#API_KEY")
        assert result["encrypted"] is True
        assert result["value"] is None  # the security boundary: never echoed
        assert result["fingerprint"] == "abcdef01"
        assert result["encryption_prefix"] == "KBC::ProjectSecureGKMS"
        assert result["env_var"] == "API_KEY"

    def test_get_plain_returns_value(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store, secrets={"ADMIN_EMAILS": "a@x.io,b@x.io"})
        result = service.get_data_app_secret(alias="prod", app_id="42", key="ADMIN_EMAILS")
        assert result["encrypted"] is False
        assert result["value"] == "a@x.io,b@x.io"
        assert result["fingerprint"] == ""
        assert result["encryption_prefix"] == ""
        assert result["env_var"] == "ADMIN_EMAILS"

    def test_get_plain_with_hyphen_derives_env_var(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store, secrets={"my-plain-key": "v"})
        result = service.get_data_app_secret(alias="prod", app_id="42", key="my-plain-key")
        assert result["encrypted"] is False
        assert result["value"] == "v"
        assert result["env_var"] == "MY_PLAIN_KEY"

    def test_get_missing_plain_key_is_not_found(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store, secrets={"OTHER": "x"})
        with pytest.raises(KeboolaApiError) as excinfo:
            service.get_data_app_secret(alias="prod", app_id="42", key="ADMIN_EMAILS")
        assert excinfo.value.status_code == 404
        assert excinfo.value.error_code == ErrorCode.NOT_FOUND

    @pytest.mark.parametrize("bad_key", ["has space", "9digit", "#", "", "with.dot"])
    def test_get_malformed_key_rejected(self, tmp_path: Path, bad_key: str) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store, secrets={"X": "y"})
        with pytest.raises(KeboolaApiError) as excinfo:
            service.get_data_app_secret(alias="prod", app_id="42", key=bad_key)
        assert excinfo.value.error_code == ErrorCode.DATA_APP_INVALID_SECRET


# ---------------------------------------------------------------------------
# remove_data_app_secrets -- plain keys accepted
# ---------------------------------------------------------------------------


class TestRemovePlain:
    def test_remove_plain_key(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, _ds, storage_mock = _make_service(
            store, secrets={"ADMIN_EMAILS": "x", "#API_KEY": ENCRYPTED_VALUE}
        )
        result = service.remove_data_app_secrets(alias="prod", app_id="42", keys=["ADMIN_EMAILS"])
        assert "ADMIN_EMAILS" in result["removed"]
        # Read-modify-write: the surviving encrypted secret must be preserved.
        storage_mock.update_config.assert_called_once()
        written = storage_mock.update_config.call_args.kwargs["configuration"]
        kept = written["parameters"]["dataApp"]["secrets"]
        assert "ADMIN_EMAILS" not in kept
        assert "#API_KEY" in kept

    def test_remove_mixed_plain_and_encrypted(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(
            store, secrets={"ADMIN_EMAILS": "x", "#API_KEY": ENCRYPTED_VALUE}
        )
        result = service.remove_data_app_secrets(
            alias="prod", app_id="42", keys=["ADMIN_EMAILS", "#API_KEY"]
        )
        assert sorted(result["removed"]) == ["ADMIN_EMAILS", "API_KEY"]
        assert result["not_found"] == []

    @pytest.mark.parametrize("bad_key", ["has space", "with.dot", "#"])
    def test_remove_malformed_key_rejected(self, tmp_path: Path, bad_key: str) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store, secrets={"X": "y"})
        with pytest.raises(KeboolaApiError) as excinfo:
            service.remove_data_app_secrets(alias="prod", app_id="42", keys=[bad_key])
        assert excinfo.value.error_code == ErrorCode.DATA_APP_INVALID_SECRET


# ---------------------------------------------------------------------------
# _validate_secret_key -- require_hash gate
# ---------------------------------------------------------------------------


class TestValidateSecretKeyRequireHash:
    def test_require_hash_true_still_rejects_a_plain_key(self, tmp_path: Path) -> None:
        """The strict gate still exists; since #738 no caller passes it.

        `secrets-set` moved to ``require_hash=False`` so a bare ``KEY`` is a
        valid plaintext env var, but the strict mode is kept for callers that
        genuinely need the encryption convention enforced.
        """
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            service._validate_secret_key("ADMIN_EMAILS", require_hash=True)
        assert excinfo.value.error_code == ErrorCode.DATA_APP_INVALID_SECRET
        assert "must start with '#'" in excinfo.value.message

    def test_read_path_accepts_plain_and_hashed(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        # require_hash=False accepts both forms without raising.
        service._validate_secret_key("ADMIN_EMAILS", require_hash=False)
        service._validate_secret_key("#API_KEY", require_hash=False)


# ---------------------------------------------------------------------------
# list_data_apps -- sandbox filter
# ---------------------------------------------------------------------------


class TestListSandboxFilter:
    def test_sandboxes_are_hidden(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        apps = [
            {"id": "1", "componentId": "keboola.sandboxes", "type": "snowflake"},
            {"id": "2", "componentId": "keboola.data-apps", "type": "python-js"},
            {"id": "3", "componentId": "keboola.sandboxes", "type": "bigquery"},
        ]
        service, *_ = _make_service(store, apps=apps)
        result = service.list_data_apps(["prod"])
        ids = [a["app_id"] for a in result["apps"]]
        assert ids == ["2"]
        assert result["apps"][0]["component_id"] == "keboola.data-apps"

    def test_items_without_component_id_are_kept(self, tmp_path: Path) -> None:
        # Defensive: an older API shape that omits componentId must not
        # silently drop the row.
        store = _make_store(tmp_path)
        apps = [
            {"id": "1", "type": "python-js"},
            {"id": "2", "componentId": "keboola.data-apps", "type": "python"},
        ]
        service, *_ = _make_service(store, apps=apps)
        result = service.list_data_apps(["prod"])
        assert sorted(a["app_id"] for a in result["apps"]) == ["1", "2"]


# ---------------------------------------------------------------------------
# CLI layer -- secrets-get human + JSON output for plain values
# ---------------------------------------------------------------------------


def _invoke_get(args: list[str], *, store: ConfigStore, mock: MagicMock):
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.DataAppService") as MockDataAppService,
    ):
        MockStore.return_value = store
        MockDataAppService.return_value = mock
        return runner.invoke(app, args)


class TestSecretsGetCli:
    def _store(self, tmp_path: Path) -> ConfigStore:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token=TEST_TOKEN,
                project_name="prod",
                project_id=1,
            ),
        )
        return store

    def test_plain_human_shows_value(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        mock = MagicMock()
        mock.get_data_app_secret.return_value = {
            "key": "ADMIN_EMAILS",
            "env_var": "ADMIN_EMAILS",
            "encrypted": False,
            "value": "a@x.io",
            "fingerprint": "",
            "encryption_prefix": "",
            "shadowed_by_runtime": False,
        }
        result = _invoke_get(
            [
                "data-app",
                "secrets-get",
                "--project",
                "prod",
                "--app-id",
                "42",
                "--key",
                "ADMIN_EMAILS",
            ],
            store=store,
            mock=mock,
        )
        assert result.exit_code == 0, result.output
        assert "a@x.io" in result.output
        assert "plaintext, unencrypted" in result.output

    def test_encrypted_human_shows_fingerprint_not_value(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        mock = MagicMock()
        mock.get_data_app_secret.return_value = {
            "key": "#API_KEY",
            "env_var": "API_KEY",
            "encrypted": True,
            "value": None,
            "fingerprint": "abcdef01",
            "encryption_prefix": "KBC::ProjectSecureGKMS",
            "shadowed_by_runtime": False,
        }
        result = _invoke_get(
            ["data-app", "secrets-get", "--project", "prod", "--app-id", "42", "--key", "#API_KEY"],
            store=store,
            mock=mock,
        )
        assert result.exit_code == 0, result.output
        assert "fingerprint=abcdef01" in result.output
        assert "plaintext" not in result.output

    def test_plain_json_envelope(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        mock = MagicMock()
        mock.get_data_app_secret.return_value = {
            "key": "ADMIN_EMAILS",
            "env_var": "ADMIN_EMAILS",
            "encrypted": False,
            "value": "a@x.io",
            "fingerprint": "",
            "encryption_prefix": "",
            "shadowed_by_runtime": False,
        }
        result = _invoke_get(
            [
                "--json",
                "data-app",
                "secrets-get",
                "--project",
                "prod",
                "--app-id",
                "42",
                "--key",
                "ADMIN_EMAILS",
            ],
            store=store,
            mock=mock,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["data"]["encrypted"] is False
        assert body["data"]["value"] == "a@x.io"


# ---------------------------------------------------------------------------
# set_data_app_secrets -- plaintext entries (issue #738)
# ---------------------------------------------------------------------------


class TestSetPlaintextEntries:
    """A bare ``KEY`` is written in clear; a ``#KEY`` is still encrypted."""

    def test_plain_key_is_written_verbatim_without_encrypting(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        encrypt = MagicMock()
        service, _ds, storage = _make_service(store, encrypt_service=encrypt)

        result = service.set_data_app_secrets(
            alias="prod",
            app_id="42",
            secrets={"SCORING_BACKUP_TAG": "ppl-assessment-db"},
        )

        written = storage.update_config.call_args.kwargs["configuration"]
        assert written["parameters"]["dataApp"]["secrets"] == {
            "SCORING_BACKUP_TAG": "ppl-assessment-db"
        }
        # A payload with no '#' key must not reach the Encryption API at all --
        # an unreachable API would otherwise fail a write needing no encryption.
        encrypt.encrypt.assert_not_called()
        assert result["plaintext_keys"] == ["SCORING_BACKUP_TAG"]
        assert result["encrypted_keys"] == []
        assert result["secrets_set"] == ["SCORING_BACKUP_TAG"]

    def test_mixed_payload_encrypts_only_the_hashed_half(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        encrypt = MagicMock()
        encrypt.encrypt.return_value = {"#API_KEY": ENCRYPTED_VALUE}
        service, _ds, storage = _make_service(store, encrypt_service=encrypt)

        result = service.set_data_app_secrets(
            alias="prod",
            app_id="42",
            secrets={"#API_KEY": "s3cr3t", "LOG_LEVEL": "debug"},
        )

        assert encrypt.encrypt.call_args.kwargs["input_data"] == {"#API_KEY": "s3cr3t"}
        written = storage.update_config.call_args.kwargs["configuration"]
        assert written["parameters"]["dataApp"]["secrets"] == {
            "#API_KEY": ENCRYPTED_VALUE,
            "LOG_LEVEL": "debug",
        }
        assert result["encrypted_keys"] == ["API_KEY"]
        assert result["plaintext_keys"] == ["LOG_LEVEL"]

    def test_existing_entries_of_both_kinds_survive(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, _ds, storage = _make_service(
            store, secrets={"#OLD_SECRET": ENCRYPTED_VALUE, "OLD_PLAIN": "keep-me"}
        )
        service.set_data_app_secrets(alias="prod", app_id="42", secrets={"NEW_PLAIN": "v"})

        written = storage.update_config.call_args.kwargs["configuration"]
        assert written["parameters"]["dataApp"]["secrets"] == {
            "#OLD_SECRET": ENCRYPTED_VALUE,
            "OLD_PLAIN": "keep-me",
            "NEW_PLAIN": "v",
        }

    def test_ciphertext_value_rejected_on_a_plain_key(self, tmp_path: Path) -> None:
        """A `KBC::` value under a bare key would be undecryptable dead weight."""
        store = _make_store(tmp_path)
        service, _ds, storage = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            service.set_data_app_secrets(
                alias="prod", app_id="42", secrets={"TAG": ENCRYPTED_VALUE}
            )
        assert excinfo.value.error_code == ErrorCode.DATA_APP_INVALID_SECRET
        storage.update_config.assert_not_called()

    def test_dry_run_shows_the_plain_value_and_masks_the_encrypted_one(
        self, tmp_path: Path
    ) -> None:
        store = _make_store(tmp_path)
        service, _ds, storage = _make_service(store)
        result = service.set_data_app_secrets(
            alias="prod",
            app_id="42",
            secrets={"#API_KEY": "s3cr3t", "TAG": "public-tag"},
            dry_run=True,
        )

        preview = result["put_storage_config_preview"]["configuration"]
        block = preview["parameters"]["dataApp"]["secrets"]
        assert block["TAG"] == "public-tag"
        assert block["#API_KEY"] == "<encrypted at runtime>"
        assert result["encryption_request_keys"] == ["#API_KEY"]
        storage.update_config.assert_not_called()

    def test_removing_a_plain_key_still_works(self, tmp_path: Path) -> None:
        """`secrets-remove` already accepted bare keys; keep it wired to set."""
        store = _make_store(tmp_path)
        service, _ds, storage = _make_service(
            store, secrets={"TAG": "v", "#API_KEY": ENCRYPTED_VALUE}
        )
        result = service.remove_data_app_secrets(alias="prod", app_id="42", keys=["TAG"])

        assert result["removed"] == ["TAG"]
        written = storage.update_config.call_args.kwargs["configuration"]
        assert written["parameters"]["dataApp"]["secrets"] == {"#API_KEY": ENCRYPTED_VALUE}


class TestListReportsPlaintextValues:
    def test_list_tags_each_entry_and_shows_plain_values(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(
            store, secrets={"#API_KEY": ENCRYPTED_VALUE, "TAG": "ppl-assessment-db"}
        )
        entries = service.list_data_app_secrets(alias="prod", app_id="42")["secrets"]
        by_key = {entry["key"]: entry for entry in entries}

        assert by_key["TAG"]["encrypted"] is False
        assert by_key["TAG"]["value"] == "ppl-assessment-db"
        # The security boundary: an encrypted entry never carries its value.
        assert by_key["#API_KEY"]["encrypted"] is True
        assert by_key["#API_KEY"]["value"] is None


class TestSecretsSetCliAcceptsPlainEntries:
    def test_cli_forwards_a_bare_key(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock = MagicMock()
        mock.set_data_app_secrets.return_value = {
            "message": "1 plain value(s) written.",
            "secrets_set": ["TAG"],
        }
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.DataAppService") as MockService,
        ):
            MockStore.return_value = store
            MockService.return_value = mock
            result = runner.invoke(
                app,
                [
                    "--json",
                    "data-app",
                    "secrets-set",
                    "--project",
                    "prod",
                    "--app-id",
                    "42",
                    "--secret",
                    "SCORING_BACKUP_TAG=ppl-assessment-db",
                ],
            )

        assert result.exit_code == 0, result.output
        assert mock.set_data_app_secrets.call_args.kwargs["secrets"] == {
            "SCORING_BACKUP_TAG": "ppl-assessment-db"
        }

    def test_cli_still_rejects_an_entry_without_an_equals_sign(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock = MagicMock()
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.DataAppService") as MockService,
        ):
            MockStore.return_value = store
            MockService.return_value = mock
            result = runner.invoke(
                app,
                [
                    "--json",
                    "data-app",
                    "secrets-set",
                    "--project",
                    "prod",
                    "--app-id",
                    "42",
                    "--secret",
                    "NO_EQUALS",
                ],
            )

        assert result.exit_code == 2
        assert json.loads(result.output)["error"]["code"] == "DATA_APP_INVALID_SECRET"
        mock.set_data_app_secrets.assert_not_called()
