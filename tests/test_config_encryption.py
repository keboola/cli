"""Tests for #-prefixed secret encryption in ConfigService write paths (issue #378).

The Storage API stores configuration JSON verbatim; #-prefixed secrets must be
pre-encrypted client-side via the Encryption API or they land in plaintext. These
tests pin down that create_config / update_config / create_config_row /
update_config_row all encrypt before writing, fail closed on encryption errors,
honor the allow_plaintext_fallback escape hatch, skip the round-trip when there
are no secrets, resolve project_id via verify_token when it is not stored, and do
NOT encrypt on dry-run (so the diff stays readable and deterministic).
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.config_service import ConfigService

COMPONENT_ID = "keboola.ex-db-snowflake"
CONFIG_ID = "cfg-001"
ROW_ID = "row-001"


def _config_with_secret() -> dict:
    return {"parameters": {"#password": "plain-secret", "user": "admin"}}


def _config_without_secret() -> dict:
    return {"parameters": {"user": "admin", "limit": 100}}


def _fake_encrypt(project_id: int, component_id: str, data: dict) -> dict:
    """Mimic the Encryption API: return a KBC:: ciphertext for each flat key."""
    return {key: f"KBC::ProjectSecure::{value}" for key, value in data.items()}


def _make_service(
    tmp_config_dir: Path, *, project_id: int | None = 258
) -> tuple[ConfigService, MagicMock]:
    """ConfigService wired to a mock client. project_id=None simulates a config
    added before project_id was persisted (forces the verify_token fallback)."""
    store = ConfigStore(config_dir=tmp_config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-xxx",
            project_name="Production",
            project_id=project_id,
        ),
    )
    client = MagicMock()
    client.encrypt_values.side_effect = _fake_encrypt
    client.create_config.return_value = {"id": CONFIG_ID, "name": "t"}
    client.update_config.return_value = {"id": CONFIG_ID, "name": "t"}
    client.create_config_row.return_value = {"id": ROW_ID, "name": "r"}
    client.update_config_row.return_value = {"id": ROW_ID, "name": "r"}
    client.get_config_detail.return_value = {"configuration": {}}
    client.get_config_row.return_value = {"configuration": {}}
    service = ConfigService(
        config_store=store,
        client_factory=lambda url, token: client,
    )
    return service, client


def _written_secret(call_args) -> str:
    """Pull configuration.parameters.#password out of a mock write call."""
    return call_args.kwargs["configuration"]["parameters"]["#password"]


# ---------------------------------------------------------------------------
# Happy path: secrets get encrypted before the write
# ---------------------------------------------------------------------------


class TestSecretsEncryptedBeforeWrite:
    def test_create_config_encrypts_secret(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)
        service.create_config(
            alias="prod",
            component_id=COMPONENT_ID,
            name="t",
            configuration=_config_with_secret(),
            validate=False,
        )
        client.encrypt_values.assert_called_once()
        assert _written_secret(client.create_config.call_args).startswith("KBC::")

    def test_update_config_encrypts_secret(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)
        service.update_config(
            alias="prod",
            component_id=COMPONENT_ID,
            config_id=CONFIG_ID,
            configuration=_config_with_secret(),
        )
        client.encrypt_values.assert_called_once()
        assert _written_secret(client.update_config.call_args).startswith("KBC::")

    def test_create_config_row_encrypts_secret(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)
        service.create_config_row(
            alias="prod",
            component_id=COMPONENT_ID,
            config_id=CONFIG_ID,
            name="r",
            configuration=_config_with_secret(),
        )
        client.encrypt_values.assert_called_once()
        assert _written_secret(client.create_config_row.call_args).startswith("KBC::")

    def test_update_config_row_encrypts_secret(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)
        service.update_config_row(
            alias="prod",
            component_id=COMPONENT_ID,
            config_id=CONFIG_ID,
            row_id=ROW_ID,
            configuration=_config_with_secret(),
        )
        client.encrypt_values.assert_called_once()
        assert _written_secret(client.update_config_row.call_args).startswith("KBC::")


# ---------------------------------------------------------------------------
# Fail-closed: encryption error must abort the write, not leak plaintext
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_create_config_raises_and_does_not_write(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)
        client.encrypt_values.side_effect = RuntimeError("API unavailable")
        with pytest.raises(KeboolaApiError) as exc_info:
            service.create_config(
                alias="prod",
                component_id=COMPONENT_ID,
                name="t",
                configuration=_config_with_secret(),
                validate=False,
            )
        assert exc_info.value.error_code == "ENCRYPTION_FAILED"
        client.create_config.assert_not_called()

    def test_update_config_raises_and_does_not_write(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)
        client.encrypt_values.side_effect = RuntimeError("API unavailable")
        with pytest.raises(KeboolaApiError) as exc_info:
            service.update_config(
                alias="prod",
                component_id=COMPONENT_ID,
                config_id=CONFIG_ID,
                configuration=_config_with_secret(),
            )
        assert exc_info.value.error_code == "ENCRYPTION_FAILED"
        client.update_config.assert_not_called()


# ---------------------------------------------------------------------------
# Escape hatch: allow_plaintext_fallback downgrades the failure to a warning
# ---------------------------------------------------------------------------


class TestEscapeHatch:
    def test_fallback_writes_plaintext_on_failure(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)
        client.encrypt_values.side_effect = RuntimeError("API unavailable")
        service.create_config(
            alias="prod",
            component_id=COMPONENT_ID,
            name="t",
            configuration=_config_with_secret(),
            validate=False,
            allow_plaintext_fallback=True,
        )
        # No raise; the write happened with the plaintext intact.
        assert _written_secret(client.create_config.call_args) == "plain-secret"


# ---------------------------------------------------------------------------
# No secrets: skip the Encryption API (and the verify_token round-trip) entirely
# ---------------------------------------------------------------------------


class TestNoSecretsSkipsEncryption:
    def test_secret_free_config_skips_encrypt_and_verify(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir, project_id=None)
        service.create_config(
            alias="prod",
            component_id=COMPONENT_ID,
            name="t",
            configuration=_config_without_secret(),
            validate=False,
        )
        client.encrypt_values.assert_not_called()
        client.verify_token.assert_not_called()
        client.create_config.assert_called_once()


# ---------------------------------------------------------------------------
# project_id resolution: fall back to verify_token, fail closed if unresolvable
# ---------------------------------------------------------------------------


class TestProjectIdResolution:
    def test_project_id_resolved_via_verify_token(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir, project_id=None)
        client.verify_token.return_value.project_id = 999
        service.create_config(
            alias="prod",
            component_id=COMPONENT_ID,
            name="t",
            configuration=_config_with_secret(),
            validate=False,
        )
        client.verify_token.assert_called_once()
        assert client.encrypt_values.call_args.kwargs["project_id"] == 999
        assert _written_secret(client.create_config.call_args).startswith("KBC::")

    def test_fail_closed_when_project_id_unresolvable(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir, project_id=None)
        client.verify_token.return_value.project_id = None
        with pytest.raises(KeboolaApiError) as exc_info:
            service.create_config(
                alias="prod",
                component_id=COMPONENT_ID,
                name="t",
                configuration=_config_with_secret(),
                validate=False,
            )
        assert exc_info.value.error_code == "ENCRYPTION_FAILED"
        client.create_config.assert_not_called()
        client.encrypt_values.assert_not_called()


# ---------------------------------------------------------------------------
# Dry-run must NOT encrypt (diff stays readable and deterministic)
# ---------------------------------------------------------------------------


class TestDryRunNotEncrypted:
    def test_update_config_dry_run_keeps_plaintext(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)
        result = service.update_config(
            alias="prod",
            component_id=COMPONENT_ID,
            config_id=CONFIG_ID,
            configuration=_config_with_secret(),
            dry_run=True,
        )
        client.encrypt_values.assert_not_called()
        client.update_config.assert_not_called()
        assert result["new_configuration"]["parameters"]["#password"] == "plain-secret"
