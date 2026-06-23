"""Unit tests for :mod:`keboola_agent_cli.services._encryption` helpers.

Focus: the row-hoisted ``{"name": "#x", "value": "..."}`` list-element shape
used by ``keboola.variables`` / ``keboola.shared-code``. The dict-key shape is
already exercised via ``test_sync_encrypt.py`` and service-level tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.services._encryption import (
    apply_encrypted,
    apply_encrypted_to_local,
    collect_secrets,
    encrypt_secrets_in_config,
)


class TestCollectSecretsNameValueShape:
    """``collect_secrets`` must find ``#``-prefixed names in ``values: [{name,value}]``."""

    def test_single_secret_in_values_list(self) -> None:
        config = {"values": [{"name": "#api_key", "value": "plaintext-secret"}]}
        result: dict[str, str] = {}
        collect_secrets(config, "", result)

        assert result == {"#values.[0].#api_key": "plaintext-secret"}

    def test_mixed_secret_and_regular_entries(self) -> None:
        config = {
            "values": [
                {"name": "#api_key", "value": "s1"},
                {"name": "regular", "value": "public"},
                {"name": "#token", "value": "s2"},
            ]
        }
        result: dict[str, str] = {}
        collect_secrets(config, "", result)

        assert result == {
            "#values.[0].#api_key": "s1",
            "#values.[2].#token": "s2",
        }

    def test_skips_already_encrypted(self) -> None:
        config = {
            "values": [
                {"name": "#api_key", "value": "KBC::ProjectSecure::abc"},
                {"name": "#token", "value": "plaintext"},
            ]
        }
        result: dict[str, str] = {}
        collect_secrets(config, "", result)

        assert result == {"#values.[1].#token": "plaintext"}

    def test_ignores_non_name_value_dicts_in_list(self) -> None:
        """Dicts in lists that don't match the shape fall through to generic recursion."""
        config = {"rows": [{"#key": "deep-secret"}]}
        result: dict[str, str] = {}
        collect_secrets(config, "", result)

        assert result == {"#rows.[0].#key": "deep-secret"}

    def test_ignores_value_when_not_string(self) -> None:
        config = {"values": [{"name": "#api_key", "value": 42}]}
        result: dict[str, str] = {}
        collect_secrets(config, "", result)

        assert result == {}


class TestApplyEncryptedNameValueShape:
    """``apply_encrypted`` must write ciphertext back into the matching list entry."""

    def test_replaces_plaintext_with_ciphertext(self) -> None:
        config = {"values": [{"name": "#api_key", "value": "plaintext-secret"}]}
        encrypted = {"#values.[0].#api_key": "KBC::ProjectSecure::xyz"}

        apply_encrypted(config, "", encrypted)

        assert config == {"values": [{"name": "#api_key", "value": "KBC::ProjectSecure::xyz"}]}

    def test_only_replaces_targeted_entries(self) -> None:
        config = {
            "values": [
                {"name": "#api_key", "value": "p1"},
                {"name": "regular", "value": "public"},
                {"name": "#token", "value": "p2"},
            ]
        }
        encrypted = {
            "#values.[0].#api_key": "KBC::ProjectSecure::a",
            "#values.[2].#token": "KBC::ProjectSecure::b",
        }

        apply_encrypted(config, "", encrypted)

        assert config["values"][0]["value"] == "KBC::ProjectSecure::a"
        assert config["values"][1]["value"] == "public"
        assert config["values"][2]["value"] == "KBC::ProjectSecure::b"


class TestApplyEncryptedToLocalNameValueShape:
    """``apply_encrypted_to_local`` must mirror ciphertext back into local state."""

    def test_copies_ciphertext_for_matching_name(self) -> None:
        local = {"values": [{"name": "#api_key", "value": "plaintext"}]}
        pushed = {"values": [{"name": "#api_key", "value": "KBC::ProjectSecure::xyz"}]}

        apply_encrypted_to_local(local, pushed)

        assert local == {"values": [{"name": "#api_key", "value": "KBC::ProjectSecure::xyz"}]}

    def test_skips_when_names_diverge(self) -> None:
        """Defensive: if server reordered or renamed, don't blindly overwrite."""
        local = {"values": [{"name": "#api_key", "value": "plaintext"}]}
        pushed = {"values": [{"name": "#different", "value": "KBC::ProjectSecure::xyz"}]}

        apply_encrypted_to_local(local, pushed)

        assert local["values"][0]["value"] == "plaintext"

    def test_skips_when_pushed_value_is_not_encrypted(self) -> None:
        local = {"values": [{"name": "#api_key", "value": "plaintext"}]}
        pushed = {"values": [{"name": "#api_key", "value": "still-plaintext"}]}

        apply_encrypted_to_local(local, pushed)

        assert local["values"][0]["value"] == "plaintext"


class TestEncryptSecretsInConfigNameValueShape:
    """End-to-end: ``encrypt_secrets_in_config`` round-trips the hoisted shape."""

    def test_roundtrip_with_mocked_client(self) -> None:
        client = MagicMock()
        client.encrypt_values.return_value = {
            "#values.[0].#api_key": "KBC::ProjectSecure::ciphertext"
        }

        config = {"values": [{"name": "#api_key", "value": "plaintext"}]}
        result = encrypt_secrets_in_config(
            client=client,
            project_id=901,
            component_id="keboola.variables",
            configuration=config,
        )

        client.encrypt_values.assert_called_once()
        call_kwargs = client.encrypt_values.call_args.kwargs
        assert call_kwargs["project_id"] == 901
        assert call_kwargs["component_id"] == "keboola.variables"
        assert call_kwargs["data"] == {"#values.[0].#api_key": "plaintext"}

        assert result["values"][0]["value"] == "KBC::ProjectSecure::ciphertext"

    def test_fail_closed_on_encryption_failure(self) -> None:
        client = MagicMock()
        client.encrypt_values.side_effect = RuntimeError("api down")

        config = {"values": [{"name": "#api_key", "value": "plaintext"}]}

        with pytest.raises(KeboolaApiError) as exc_info:
            encrypt_secrets_in_config(
                client=client,
                project_id=901,
                component_id="keboola.variables",
                configuration=config,
            )

        assert exc_info.value.error_code == "ENCRYPTION_FAILED"
        # plaintext must not be in the config after the raise path either
        # (it's in place, but the caller must not push it — that's the contract)

    def test_plaintext_fallback_warning_names_leaked_keys(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # GHSA-7jrf: with --allow-plaintext-on-encrypt-failure AND a failing
        # Encryption API, the (otherwise silent) plaintext write must be
        # surfaced -- naming the exact leaked key-PATHS, never the values.
        client = MagicMock()
        client.encrypt_values.side_effect = RuntimeError("api down")
        config = {"parameters": {"#api_key": "s3cr3t-value"}}

        with caplog.at_level("WARNING"):
            encrypt_secrets_in_config(
                client=client,
                project_id=901,
                component_id="keboola.ex-generic",
                configuration=config,
                allow_plaintext_fallback=True,
            )

        msg = caplog.text
        assert "PLAINTEXT" in msg
        assert "#parameters.#api_key" in msg  # the key PATH is named
        assert "s3cr3t-value" not in msg  # the secret VALUE is NEVER logged

    def test_plaintext_fallback_redacts_secret_value_echoed_in_exception(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # GHSA-7jrf (Devin follow-up): even if the encryption client's exception
        # message echoes the request body, the warning must redact the secret
        # VALUE -- str(exc) is the remaining transitive leak vector.
        client = MagicMock()
        client.encrypt_values.side_effect = RuntimeError(
            "400 Bad Request while sending {'#api_key': 's3cr3t-value'}"
        )
        config = {"parameters": {"#api_key": "s3cr3t-value"}}

        with caplog.at_level("WARNING"):
            encrypt_secrets_in_config(
                client=client,
                project_id=901,
                component_id="keboola.ex-generic",
                configuration=config,
                allow_plaintext_fallback=True,
            )

        assert "s3cr3t-value" not in caplog.text  # value redacted out of exc
        assert "***" in caplog.text  # redaction marker present
        assert "#parameters.#api_key" in caplog.text  # key path still named

    def test_noop_when_no_secrets(self) -> None:
        client = MagicMock()
        config = {"values": [{"name": "regular", "value": "public"}]}

        encrypt_secrets_in_config(
            client=client,
            project_id=901,
            component_id="keboola.variables",
            configuration=config,
        )

        client.encrypt_values.assert_not_called()

    def test_noop_when_all_already_encrypted(self) -> None:
        client = MagicMock()
        config = {"values": [{"name": "#api_key", "value": "KBC::ProjectSecure::already"}]}

        encrypt_secrets_in_config(
            client=client,
            project_id=901,
            component_id="keboola.variables",
            configuration=config,
        )

        client.encrypt_values.assert_not_called()
