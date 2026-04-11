"""Tests for encrypt CLI commands via CliRunner.

Tests the `kbagent encrypt values` subcommand: inline JSON, @file, stdin,
output file, validation errors, and API errors. Follows the existing CLI
test pattern from test_workspace_cli.py / test_component_cli.py with
patched services in ctx.obj.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"

runner = CliRunner()


def _setup_config(config_dir: Path, projects: dict[str, dict] | None = None) -> ConfigStore:
    """Set up a ConfigStore with given projects for CLI encrypt tests."""
    store = ConfigStore(config_dir=config_dir)
    if projects:
        for alias, info in projects.items():
            store.add_project(
                alias,
                ProjectConfig(
                    stack_url=info.get("stack_url", "https://connection.keboola.com"),
                    token=info["token"],
                    project_name=info.get("project_name", alias),
                    project_id=info.get("project_id", 1234),
                ),
            )
    return store


def _make_encrypt_mock() -> MagicMock:
    """Create a fresh MagicMock for EncryptService."""
    return MagicMock()


class TestEncryptValuesJson:
    """Tests for `kbagent encrypt values` command with JSON output."""

    def test_encrypt_values_json_output(self, tmp_path: Path) -> None:
        """encrypt values --json returns structured JSON with encrypted data."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_enc = _make_encrypt_mock()
        mock_enc.encrypt.return_value = {
            "#password": "KBC::ProjectSecure::enc-abc123",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.EncryptService") as MockEncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockEncService.return_value = mock_enc

            result = runner.invoke(
                app,
                [
                    "--json",
                    "encrypt",
                    "values",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--input",
                    '{"#password": "secret123"}',
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["#password"] == "KBC::ProjectSecure::enc-abc123"
        mock_enc.encrypt.assert_called_once_with(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            input_data={"#password": "secret123"},
        )

    def test_encrypt_values_inline_json(self, tmp_path: Path) -> None:
        """Inline JSON input string is parsed and passed to service."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_enc = _make_encrypt_mock()
        mock_enc.encrypt.return_value = {
            "#key1": "KBC::ProjectSecure::enc-1",
            "#key2": "KBC::ProjectSecure::enc-2",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.EncryptService") as MockEncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockEncService.return_value = mock_enc

            result = runner.invoke(
                app,
                [
                    "--json",
                    "encrypt",
                    "values",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--input",
                    '{"#key1": "val1", "#key2": "val2"}',
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["#key1"] == "KBC::ProjectSecure::enc-1"
        assert output["data"]["#key2"] == "KBC::ProjectSecure::enc-2"


class TestEncryptValuesFileInput:
    """Tests for @file and stdin input modes."""

    def test_encrypt_values_file_input(self, tmp_path: Path) -> None:
        """@file.json input reads from file and passes parsed JSON to service."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        # Write a temp JSON file
        input_file = tmp_path / "secrets.json"
        input_file.write_text('{"#db_pass": "s3cret"}', encoding="utf-8")

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_enc = _make_encrypt_mock()
        mock_enc.encrypt.return_value = {
            "#db_pass": "KBC::ProjectSecure::enc-file",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.EncryptService") as MockEncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockEncService.return_value = mock_enc

            result = runner.invoke(
                app,
                [
                    "--json",
                    "encrypt",
                    "values",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--input",
                    f"@{input_file}",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["#db_pass"] == "KBC::ProjectSecure::enc-file"
        mock_enc.encrypt.assert_called_once_with(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            input_data={"#db_pass": "s3cret"},
        )

    def test_encrypt_values_stdin_input(self, tmp_path: Path) -> None:
        """- (stdin) input reads JSON from stdin."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_enc = _make_encrypt_mock()
        mock_enc.encrypt.return_value = {
            "#token": "KBC::ProjectSecure::enc-stdin",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.EncryptService") as MockEncService,
            patch("keboola_agent_cli.commands.encrypt.sys") as mock_sys,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockEncService.return_value = mock_enc
            mock_sys.stdin.read.return_value = '{"#token": "abc"}'

            result = runner.invoke(
                app,
                [
                    "--json",
                    "encrypt",
                    "values",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--input",
                    "-",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["#token"] == "KBC::ProjectSecure::enc-stdin"


class TestEncryptValuesOutputFile:
    """Tests for --output-file option."""

    def test_encrypt_values_output_file(self, tmp_path: Path) -> None:
        """--output-file writes encrypted JSON with 0600 permissions."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        output_file = tmp_path / "encrypted.json"

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_enc = _make_encrypt_mock()
        mock_enc.encrypt.return_value = {
            "#password": "KBC::ProjectSecure::enc-output",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.EncryptService") as MockEncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockEncService.return_value = mock_enc

            result = runner.invoke(
                app,
                [
                    "--json",
                    "encrypt",
                    "values",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--input",
                    '{"#password": "secret"}',
                    "--output-file",
                    str(output_file),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"

        # Verify file was written with correct content
        assert output_file.exists()
        file_data = json.loads(output_file.read_text(encoding="utf-8"))
        assert file_data["#password"] == "KBC::ProjectSecure::enc-output"

        # Verify file permissions are 0600
        mode = output_file.stat().st_mode & 0o777
        assert mode == 0o600


class TestEncryptValuesErrors:
    """Tests for error handling in encrypt values command."""

    def test_encrypt_values_invalid_json(self, tmp_path: Path) -> None:
        """Bad JSON input gives exit code 2 with INPUT_ERROR."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_enc = _make_encrypt_mock()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.EncryptService") as MockEncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockEncService.return_value = mock_enc

            result = runner.invoke(
                app,
                [
                    "--json",
                    "encrypt",
                    "values",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--input",
                    "not-valid-json{",
                ],
            )

        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INPUT_ERROR"
        mock_enc.encrypt.assert_not_called()

    def test_encrypt_values_missing_project(self, tmp_path: Path) -> None:
        """Missing --project gives exit code 2 (Typer required option)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.EncryptService") as MockEncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockEncService.return_value = _make_encrypt_mock()

            result = runner.invoke(
                app,
                [
                    "--json",
                    "encrypt",
                    "values",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--input",
                    '{"#x": "y"}',
                ],
            )

        assert result.exit_code == 2

    def test_encrypt_values_validation_error(self, tmp_path: Path) -> None:
        """ConfigError from service gives exit code 5 with CONFIG_ERROR."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_enc = _make_encrypt_mock()
        mock_enc.encrypt.side_effect = ConfigError("Key 'password' must start with '#'")

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.EncryptService") as MockEncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockEncService.return_value = mock_enc

            result = runner.invoke(
                app,
                [
                    "--json",
                    "encrypt",
                    "values",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--input",
                    '{"password": "secret"}',
                ],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"
        assert "must start with '#'" in output["error"]["message"]

    def test_encrypt_values_api_error(self, tmp_path: Path) -> None:
        """KeboolaApiError gives appropriate exit code based on error code."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_enc = _make_encrypt_mock()
        mock_enc.encrypt.side_effect = KeboolaApiError(
            message="Invalid token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.EncryptService") as MockEncService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockEncService.return_value = mock_enc

            result = runner.invoke(
                app,
                [
                    "--json",
                    "encrypt",
                    "values",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--input",
                    '{"#password": "secret"}',
                ],
            )

        # INVALID_TOKEN -> exit code 3
        assert result.exit_code == 3
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_TOKEN"
