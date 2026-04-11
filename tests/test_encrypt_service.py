"""Tests for EncryptService - encrypt secret values via Keboola Encryption API."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from helpers import setup_single_project
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import TokenVerifyResponse
from keboola_agent_cli.services.encrypt_service import EncryptService


def _make_encrypt_client(
    encrypted_response: dict[str, str] | None = None,
    project_id: int = 258,
) -> MagicMock:
    """Create a mock KeboolaClient with encrypt_values and verify_token."""
    mock_client = MagicMock()
    mock_client.verify_token.return_value = TokenVerifyResponse(
        token_id="12345",
        token_description="Test Token",
        project_id=project_id,
        project_name="Production",
        owner_name="Production",
    )
    if encrypted_response is not None:
        mock_client.encrypt_values.return_value = encrypted_response
    return mock_client


class TestEncryptSuccess:
    """Tests for successful encryption scenarios."""

    def test_encrypt_success(self, tmp_config_dir: Path) -> None:
        """Valid input with #-prefixed keys returns encrypted values from API."""
        encrypted = {
            "#password": "KBC::ProjectSecure::enc-abc123",
            "#token": "KBC::ProjectSecure::enc-def456",
        }
        mock_client = _make_encrypt_client(encrypted_response=encrypted)

        store = setup_single_project(tmp_config_dir)
        svc = EncryptService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.encrypt(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            input_data={"#password": "secret123", "#token": "my-api-key"},
        )

        assert result == encrypted
        mock_client.encrypt_values.assert_called_once_with(
            project_id=258,
            component_id="keboola.ex-db-snowflake",
            data={"#password": "secret123", "#token": "my-api-key"},
        )

    def test_encrypt_passthrough_already_encrypted(self, tmp_config_dir: Path) -> None:
        """Input with KBC:: values passes through unchanged, no API call made."""
        mock_client = _make_encrypt_client()

        store = setup_single_project(tmp_config_dir)
        svc = EncryptService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.encrypt(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            input_data={
                "#password": "KBC::ProjectSecure::already-encrypted",
                "#token": "KBC::ProjectSecure::also-encrypted",
            },
        )

        assert result == {
            "#password": "KBC::ProjectSecure::already-encrypted",
            "#token": "KBC::ProjectSecure::also-encrypted",
        }
        mock_client.encrypt_values.assert_not_called()

    def test_encrypt_mixed_new_and_encrypted(self, tmp_config_dir: Path) -> None:
        """Mix of plaintext and already-encrypted values: only plaintext sent to API."""
        api_response = {"#password": "KBC::ProjectSecure::enc-new"}
        mock_client = _make_encrypt_client(encrypted_response=api_response)

        store = setup_single_project(tmp_config_dir)
        svc = EncryptService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.encrypt(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            input_data={
                "#password": "secret123",
                "#token": "KBC::ProjectSecure::already-encrypted",
            },
        )

        # Result merges API response with already-encrypted values
        assert result == {
            "#password": "KBC::ProjectSecure::enc-new",
            "#token": "KBC::ProjectSecure::already-encrypted",
        }
        # Only the plaintext value was sent to the API
        mock_client.encrypt_values.assert_called_once_with(
            project_id=258,
            component_id="keboola.ex-db-snowflake",
            data={"#password": "secret123"},
        )


class TestEncryptValidation:
    """Tests for input validation in EncryptService.encrypt()."""

    def test_encrypt_validation_missing_hash(self, tmp_config_dir: Path) -> None:
        """Key without # prefix raises ConfigError."""
        store = setup_single_project(tmp_config_dir)
        svc = EncryptService(config_store=store)

        with pytest.raises(ConfigError, match="Key 'password' must start with '#'"):
            svc.encrypt(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                input_data={"password": "secret123"},
            )

    def test_encrypt_validation_non_string_value(self, tmp_config_dir: Path) -> None:
        """Non-string value raises ConfigError."""
        store = setup_single_project(tmp_config_dir)
        svc = EncryptService(config_store=store)

        with pytest.raises(ConfigError, match="Value for '#count' must be a string, got int"):
            svc.encrypt(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                input_data={"#count": 42},
            )


class TestEncryptEdgeCases:
    """Tests for edge cases and error propagation."""

    def test_encrypt_empty_after_filter(self, tmp_config_dir: Path) -> None:
        """All values already encrypted means no API call, returns passthrough."""
        mock_client = _make_encrypt_client()

        store = setup_single_project(tmp_config_dir)
        svc = EncryptService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.encrypt(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            input_data={"#secret": "KBC::ProjectSecure::already"},
        )

        assert result == {"#secret": "KBC::ProjectSecure::already"}
        mock_client.encrypt_values.assert_not_called()
        # verify_token should also NOT be called since we never reach API
        mock_client.verify_token.assert_not_called()

    def test_encrypt_api_error_propagates(self, tmp_config_dir: Path) -> None:
        """KeboolaApiError from client.encrypt_values propagates to caller."""
        mock_client = _make_encrypt_client()
        mock_client.encrypt_values.side_effect = KeboolaApiError(
            message="Encryption service unavailable",
            error_code="SERVICE_UNAVAILABLE",
            status_code=503,
            retryable=True,
        )

        store = setup_single_project(tmp_config_dir)
        svc = EncryptService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.raises(KeboolaApiError, match="Encryption service unavailable"):
            svc.encrypt(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                input_data={"#password": "secret"},
            )
