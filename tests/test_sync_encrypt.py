"""Tests for SyncService._encrypt_secrets_in_config security fix.

Validates that encryption failures raise KeboolaApiError by default
(preventing plaintext secrets from being pushed), and that the
allow_plaintext_fallback flag preserves the old warning behavior.
"""

import logging
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.services.sync_service import SyncService


def _make_config_with_secrets() -> dict:
    """Return a configuration dict containing unencrypted #-prefixed secrets."""
    return {
        "parameters": {
            "#apiToken": "my-secret-token",
            "baseUrl": "https://api.example.com",
            "nested": {
                "#password": "super-secret",
            },
        },
    }


def _make_config_without_secrets() -> dict:
    """Return a configuration dict with no secret values."""
    return {
        "parameters": {
            "baseUrl": "https://api.example.com",
            "limit": 100,
        },
    }


class TestEncryptSecretsRaisesOnFailure:
    """When allow_plaintext_fallback=False (default), encryption failure must raise."""

    def test_encrypt_secrets_raises_on_failure(self) -> None:
        """Encryption API error raises KeboolaApiError with ENCRYPTION_FAILED code."""
        client = MagicMock()
        client.encrypt_values.side_effect = RuntimeError("API unavailable")

        config = _make_config_with_secrets()

        with pytest.raises(KeboolaApiError) as exc_info:
            SyncService._encrypt_secrets_in_config(
                client=client,
                project_id=258,
                component_id="keboola.ex-http",
                configuration=config,
            )

        assert exc_info.value.error_code == "ENCRYPTION_FAILED"
        assert "keboola.ex-http" in str(exc_info.value)
        assert "API unavailable" in str(exc_info.value)

    def test_encrypt_secrets_raises_preserves_cause(self) -> None:
        """The raised KeboolaApiError chains the original exception via __cause__."""
        client = MagicMock()
        original = ConnectionError("timeout")
        client.encrypt_values.side_effect = original

        config = _make_config_with_secrets()

        with pytest.raises(KeboolaApiError) as exc_info:
            SyncService._encrypt_secrets_in_config(
                client=client,
                project_id=258,
                component_id="keboola.ex-http",
                configuration=config,
            )

        assert exc_info.value.__cause__ is original


class TestEncryptSecretsWarnsWithFallback:
    """When allow_plaintext_fallback=True, encryption failure logs warning only."""

    def test_encrypt_secrets_warns_on_failure_with_fallback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Fallback mode logs a warning and returns config with plaintext intact."""
        client = MagicMock()
        client.encrypt_values.side_effect = RuntimeError("API unavailable")

        config = _make_config_with_secrets()
        original_token = config["parameters"]["#apiToken"]

        with caplog.at_level(logging.WARNING):
            result = SyncService._encrypt_secrets_in_config(
                client=client,
                project_id=258,
                component_id="keboola.ex-http",
                configuration=config,
                allow_plaintext_fallback=True,
            )

        # Config returned unchanged with plaintext values
        assert result["parameters"]["#apiToken"] == original_token
        assert result["parameters"]["nested"]["#password"] == "super-secret"

        # Warning was logged
        assert any("Failed to encrypt secrets" in record.message for record in caplog.records)
        assert any("plaintext fallback allowed" in record.message for record in caplog.records)


class TestEncryptSecretsSuccess:
    """When encryption succeeds, behavior is identical regardless of fallback flag."""

    def test_encrypt_secrets_success_default(self) -> None:
        """Successful encryption applies encrypted values (default mode)."""
        client = MagicMock()
        # Keys match the format produced by _collect_secrets: #prefix.key
        client.encrypt_values.return_value = {
            "#parameters.#apiToken": "KBC::ProjectSecure::enc-aaa",
            "#parameters.nested.#password": "KBC::ProjectSecure::enc-bbb",
        }

        config = _make_config_with_secrets()

        result = SyncService._encrypt_secrets_in_config(
            client=client,
            project_id=258,
            component_id="keboola.ex-http",
            configuration=config,
        )

        assert result["parameters"]["#apiToken"] == "KBC::ProjectSecure::enc-aaa"
        client.encrypt_values.assert_called_once()

    def test_encrypt_secrets_success_with_fallback_flag(self) -> None:
        """Successful encryption applies encrypted values (fallback mode)."""
        client = MagicMock()
        client.encrypt_values.return_value = {
            "#parameters.#apiToken": "KBC::ProjectSecure::enc-aaa",
            "#parameters.nested.#password": "KBC::ProjectSecure::enc-bbb",
        }

        config = _make_config_with_secrets()

        result = SyncService._encrypt_secrets_in_config(
            client=client,
            project_id=258,
            component_id="keboola.ex-http",
            configuration=config,
            allow_plaintext_fallback=True,
        )

        assert result["parameters"]["#apiToken"] == "KBC::ProjectSecure::enc-aaa"
        client.encrypt_values.assert_called_once()

    def test_encrypt_secrets_skips_when_no_project_id(self) -> None:
        """When project_id is None, encryption is skipped entirely."""
        client = MagicMock()
        config = _make_config_with_secrets()

        result = SyncService._encrypt_secrets_in_config(
            client=client,
            project_id=None,
            component_id="keboola.ex-http",
            configuration=config,
        )

        client.encrypt_values.assert_not_called()
        assert result["parameters"]["#apiToken"] == "my-secret-token"

    def test_encrypt_secrets_skips_when_no_secrets(self) -> None:
        """When config has no #-prefixed keys, encrypt_values is never called."""
        client = MagicMock()
        config = _make_config_without_secrets()

        result = SyncService._encrypt_secrets_in_config(
            client=client,
            project_id=258,
            component_id="keboola.ex-http",
            configuration=config,
        )

        client.encrypt_values.assert_not_called()
        assert result["parameters"]["baseUrl"] == "https://api.example.com"
