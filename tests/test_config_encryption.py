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
from typer.testing import CliRunner

from helpers import setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.config_service import ConfigService

runner = CliRunner()

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


# ---------------------------------------------------------------------------
# plaintext_written: structured visibility of a plaintext-fallback leak
# ---------------------------------------------------------------------------


class TestPlaintextWrittenField:
    """The service result must carry ``plaintext_written`` so --json consumers
    see a plaintext-on-encrypt-failure fallback, not just the stderr warning.

    Empty list after a successful encryption; the leaked key-PATHS (never the
    plaintext value) after an allowed fallback.
    """

    LEAKED_PATH = "#parameters.#password"

    def test_create_config_plaintext_written_empty_on_success(self, tmp_config_dir: Path) -> None:
        service, _ = _make_service(tmp_config_dir)
        result = service.create_config(
            alias="prod",
            component_id=COMPONENT_ID,
            name="t",
            configuration=_config_with_secret(),
            validate=False,
        )
        assert result["plaintext_written"] == []

    def test_update_config_plaintext_written_empty_on_success(self, tmp_config_dir: Path) -> None:
        service, _ = _make_service(tmp_config_dir)
        result = service.update_config(
            alias="prod",
            component_id=COMPONENT_ID,
            config_id=CONFIG_ID,
            configuration=_config_with_secret(),
        )
        assert result["plaintext_written"] == []

    def test_update_config_no_secret_plaintext_written_empty(self, tmp_config_dir: Path) -> None:
        service, _ = _make_service(tmp_config_dir)
        result = service.update_config(
            alias="prod",
            component_id=COMPONENT_ID,
            config_id=CONFIG_ID,
            configuration=_config_without_secret(),
        )
        assert result["plaintext_written"] == []

    def test_update_config_lists_leaked_keys_on_fallback(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)
        client.encrypt_values.side_effect = RuntimeError("API unavailable")
        result = service.update_config(
            alias="prod",
            component_id=COMPONENT_ID,
            config_id=CONFIG_ID,
            configuration=_config_with_secret(),
            allow_plaintext_fallback=True,
        )
        # Leaked key-PATH is surfaced; the plaintext value must never appear.
        assert result["plaintext_written"] == [self.LEAKED_PATH]
        assert "plain-secret" not in result["plaintext_written"]
        # The plaintext write did happen (escape hatch), value left intact.
        assert _written_secret(client.update_config.call_args) == "plain-secret"

    def test_create_config_lists_leaked_keys_on_fallback(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)
        client.encrypt_values.side_effect = RuntimeError("API unavailable")
        result = service.create_config(
            alias="prod",
            component_id=COMPONENT_ID,
            name="t",
            configuration=_config_with_secret(),
            validate=False,
            allow_plaintext_fallback=True,
        )
        assert result["plaintext_written"] == [self.LEAKED_PATH]
        assert "plain-secret" not in result["plaintext_written"]

    def test_create_config_row_lists_leaked_keys_on_fallback(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)
        client.encrypt_values.side_effect = RuntimeError("API unavailable")
        result = service.create_config_row(
            alias="prod",
            component_id=COMPONENT_ID,
            config_id=CONFIG_ID,
            name="r",
            configuration=_config_with_secret(),
            allow_plaintext_fallback=True,
        )
        assert result["plaintext_written"] == [self.LEAKED_PATH]

    def test_update_config_row_lists_leaked_keys_on_fallback(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)
        client.encrypt_values.side_effect = RuntimeError("API unavailable")
        result = service.update_config_row(
            alias="prod",
            component_id=COMPONENT_ID,
            config_id=CONFIG_ID,
            row_id=ROW_ID,
            configuration=_config_with_secret(),
            allow_plaintext_fallback=True,
        )
        assert result["plaintext_written"] == [self.LEAKED_PATH]


# ---------------------------------------------------------------------------
# CLI wiring: --allow-plaintext-on-encrypt-failure reaches the service layer
# ---------------------------------------------------------------------------


class TestCliFlagWiring:
    """The CLI flag must arrive at the service as allow_plaintext_fallback.

    The service-layer behavior is covered above with a real ConfigService; here
    the service is a MagicMock so the assertion is purely about CLI wiring.
    """

    SECRET_CFG = '{"parameters":{"#password":"v"}}'

    @staticmethod
    def _patch(monkeypatch: pytest.MonkeyPatch, svc: MagicMock) -> None:
        monkeypatch.setattr(
            "keboola_agent_cli.commands.config.get_service",
            lambda ctx, name: svc,
        )

    def _run(self, tmp_config_dir: Path, args: list[str]):
        return runner.invoke(app, ["--json", "--config-dir", str(tmp_config_dir), "config", *args])

    def test_update_forwards_flag(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        setup_single_project(tmp_config_dir)
        svc = MagicMock()
        svc.update_config.return_value = {"id": CONFIG_ID}
        self._patch(monkeypatch, svc)
        result = self._run(
            tmp_config_dir,
            [
                "update",
                "--project",
                "prod",
                "--component-id",
                COMPONENT_ID,
                "--config-id",
                CONFIG_ID,
                "--configuration",
                self.SECRET_CFG,
                "--allow-plaintext-on-encrypt-failure",
            ],
        )
        assert result.exit_code == 0, result.output
        assert svc.update_config.call_args.kwargs["allow_plaintext_fallback"] is True

    def test_update_defaults_flag_false(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        setup_single_project(tmp_config_dir)
        svc = MagicMock()
        svc.update_config.return_value = {"id": CONFIG_ID}
        self._patch(monkeypatch, svc)
        result = self._run(
            tmp_config_dir,
            [
                "update",
                "--project",
                "prod",
                "--component-id",
                COMPONENT_ID,
                "--config-id",
                CONFIG_ID,
                "--configuration",
                self.SECRET_CFG,
            ],
        )
        assert result.exit_code == 0, result.output
        assert svc.update_config.call_args.kwargs["allow_plaintext_fallback"] is False

    def test_new_push_forwards_flag(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        setup_single_project(tmp_config_dir)
        svc = MagicMock()
        svc.create_config.return_value = {
            "id": CONFIG_ID,
            "validation_status": "skipped",
            "validation_errors": [],
        }
        self._patch(monkeypatch, svc)
        result = self._run(
            tmp_config_dir,
            [
                "new",
                "--project",
                "prod",
                "--component-id",
                COMPONENT_ID,
                "--name",
                "t",
                "--push",
                "--no-files",
                "--no-validate",
                "--configuration",
                self.SECRET_CFG,
                "--allow-plaintext-on-encrypt-failure",
            ],
        )
        assert result.exit_code == 0, result.output
        assert svc.create_config.call_args.kwargs["allow_plaintext_fallback"] is True

    def test_row_create_forwards_flag(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        setup_single_project(tmp_config_dir)
        svc = MagicMock()
        svc.create_config_row.return_value = {"id": ROW_ID}
        self._patch(monkeypatch, svc)
        result = self._run(
            tmp_config_dir,
            [
                "row-create",
                "--project",
                "prod",
                "--component-id",
                COMPONENT_ID,
                "--config-id",
                CONFIG_ID,
                "--name",
                "r",
                "--configuration",
                self.SECRET_CFG,
                "--allow-plaintext-on-encrypt-failure",
            ],
        )
        assert result.exit_code == 0, result.output
        assert svc.create_config_row.call_args.kwargs["allow_plaintext_fallback"] is True

    def test_row_update_forwards_flag(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        setup_single_project(tmp_config_dir)
        svc = MagicMock()
        svc.update_config_row.return_value = {"id": ROW_ID}
        self._patch(monkeypatch, svc)
        result = self._run(
            tmp_config_dir,
            [
                "row-update",
                "--project",
                "prod",
                "--component-id",
                COMPONENT_ID,
                "--config-id",
                CONFIG_ID,
                "--row-id",
                ROW_ID,
                "--configuration",
                self.SECRET_CFG,
                "--allow-plaintext-on-encrypt-failure",
            ],
        )
        assert result.exit_code == 0, result.output
        assert svc.update_config_row.call_args.kwargs["allow_plaintext_fallback"] is True
