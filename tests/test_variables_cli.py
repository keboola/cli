"""CLI tests for config variables-set / variables-get / variables-clear.

Mocks VariablesService to lock the CLI<->service contract: flag parsing,
--var KEY=VALUE repeatable, --replace, --dry-run, --variables-id / --values-id
overrides, --yes for clear. Verifies both --json and human output shapes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.project_service import ProjectService


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
runner = CliRunner()


def _setup_config(config_dir: Path) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name="prod",
            project_id=1234,
        ),
    )
    return store


class TestVariablesSet:
    def test_parses_multiple_vars_and_passes_dict_to_service(self, tmp_path: Path) -> None:
        """--var k=v (repeated) builds a dict handed to VariablesService.set_variables."""
        store = _setup_config(tmp_path / "config")
        mock_vars = MagicMock()
        mock_vars.set_variables.return_value = {
            "project_alias": "prod",
            "parent_component_id": "keboola.snowflake-transformation",
            "parent_config_id": "15815157",
            "variables_id": "vars-new",
            "values_id": "row-new",
            "action": "created",
            "values": {"year_start": "2016", "region": "eu"},
            "encrypted_keys": [],
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProj,
            patch("keboola_agent_cli.cli.VariablesService") as MockVars,
        ):
            MockStore.return_value = store
            MockProj.return_value = ProjectService(config_store=store)
            MockVars.return_value = mock_vars

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "variables-set",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.snowflake-transformation",
                    "--config-id",
                    "15815157",
                    "--var",
                    "year_start=2016",
                    "--var",
                    "region=eu",
                ],
            )

        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["action"] == "created"

        call_kwargs = mock_vars.set_variables.call_args.kwargs
        assert call_kwargs["variables"] == {"year_start": "2016", "region": "eu"}
        assert call_kwargs["replace"] is False

    def test_replace_flag_forwards_to_service(self, tmp_path: Path) -> None:
        """--replace -> replace=True on the service call."""
        store = _setup_config(tmp_path / "config")
        mock_vars = MagicMock()
        mock_vars.set_variables.return_value = {
            "project_alias": "prod",
            "parent_component_id": "keboola.x",
            "parent_config_id": "cfg-1",
            "variables_id": "vars-1",
            "values_id": "row-1",
            "action": "updated",
            "values": {"k": "v"},
            "encrypted_keys": [],
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProj,
            patch("keboola_agent_cli.cli.VariablesService") as MockVars,
        ):
            MockStore.return_value = store
            MockProj.return_value = ProjectService(config_store=store)
            MockVars.return_value = mock_vars

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "variables-set",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.x",
                    "--config-id",
                    "cfg-1",
                    "--var",
                    "k=v",
                    "--replace",
                ],
            )

        assert result.exit_code == 0
        assert mock_vars.set_variables.call_args.kwargs["replace"] is True

    def test_no_vars_exits_with_invalid_argument(self, tmp_path: Path) -> None:
        """Missing --var exits 2 with INVALID_ARGUMENT; service NOT called."""
        store = _setup_config(tmp_path / "config")
        mock_vars = MagicMock()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProj,
            patch("keboola_agent_cli.cli.VariablesService") as MockVars,
        ):
            MockStore.return_value = store
            MockProj.return_value = ProjectService(config_store=store)
            MockVars.return_value = mock_vars

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "variables-set",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.x",
                    "--config-id",
                    "cfg-1",
                ],
            )

        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["error"]["code"] == "INVALID_ARGUMENT"
        mock_vars.set_variables.assert_not_called()

    def test_malformed_var_exits_with_invalid_argument(self, tmp_path: Path) -> None:
        """--var missing = sign is rejected before the service is called."""
        store = _setup_config(tmp_path / "config")
        mock_vars = MagicMock()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProj,
            patch("keboola_agent_cli.cli.VariablesService") as MockVars,
        ):
            MockStore.return_value = store
            MockProj.return_value = ProjectService(config_store=store)
            MockVars.return_value = mock_vars

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "variables-set",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.x",
                    "--config-id",
                    "cfg-1",
                    "--var",
                    "no_equals_sign",
                ],
            )

        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["error"]["code"] == "INVALID_ARGUMENT"
        assert "KEY=VALUE" in output["error"]["message"]

    def test_dry_run_does_not_call_set_and_shows_preview(self, tmp_path: Path) -> None:
        """--dry-run calls get_variables + prints preview, never calls set_variables."""
        store = _setup_config(tmp_path / "config")
        mock_vars = MagicMock()
        mock_vars.get_variables.return_value = {
            "project_alias": "prod",
            "parent_component_id": "keboola.x",
            "parent_config_id": "cfg-1",
            "variables_id": "vars-1",
            "values_id": "row-1",
            "values": {"region": "eu", "year_start": "2016"},
            "linked": True,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProj,
            patch("keboola_agent_cli.cli.VariablesService") as MockVars,
        ):
            MockStore.return_value = store
            MockProj.return_value = ProjectService(config_store=store)
            MockVars.return_value = mock_vars

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "variables-set",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.x",
                    "--config-id",
                    "cfg-1",
                    "--var",
                    "region=us-west",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0
        output = json.loads(result.output)
        data = output["data"]
        assert data["dry_run"] is True
        assert data["action"] == "would_update"
        assert data["would_write"] == {"region": "us-west", "year_start": "2016"}
        mock_vars.set_variables.assert_not_called()

    def test_dry_run_replace_masks_dropped_hash_keys(self, tmp_path: Path) -> None:
        """``--replace --dry-run`` must mask ``#``-prefixed dropped rows as <encrypted>.

        Other branches (``+``/``~``/``=``) already mask; the ``-`` branch was
        missing the same mask and leaked full ``KBC::ProjectSecure::...``
        ciphertext. Flagged in PR #190 review.
        """
        store = _setup_config(tmp_path / "config")
        mock_vars = MagicMock()
        mock_vars.get_variables.return_value = {
            "project_alias": "prod",
            "parent_component_id": "keboola.x",
            "parent_config_id": "cfg-1",
            "variables_id": "vars-1",
            "values_id": "row-1",
            "values": {
                "#old_secret": "KBC::ProjectSecure::eJwOld",
                "region": "eu",
            },
            "linked": True,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProj,
            patch("keboola_agent_cli.cli.VariablesService") as MockVars,
        ):
            MockStore.return_value = store
            MockProj.return_value = ProjectService(config_store=store)
            MockVars.return_value = mock_vars

            result = runner.invoke(
                app,
                [
                    "config",
                    "variables-set",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.x",
                    "--config-id",
                    "cfg-1",
                    "--var",
                    "region=us-west",
                    "--replace",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0
        stripped = _strip_ansi(result.output)
        # Dropped #-key must be masked, never leak ciphertext.
        assert "KBC::" not in stripped
        assert "- #old_secret" in stripped
        assert "<encrypted>" in stripped

    def test_human_output_shows_action_and_values(self, tmp_path: Path) -> None:
        """Human mode prints 'created'/'updated' + final values (ANSI stripped)."""
        store = _setup_config(tmp_path / "config")
        mock_vars = MagicMock()
        mock_vars.set_variables.return_value = {
            "project_alias": "prod",
            "parent_component_id": "keboola.x",
            "parent_config_id": "cfg-1",
            "variables_id": "vars-1",
            "values_id": "row-1",
            "action": "created",
            "values": {"year_start": "2016"},
            "encrypted_keys": [],
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProj,
            patch("keboola_agent_cli.cli.VariablesService") as MockVars,
        ):
            MockStore.return_value = store
            MockProj.return_value = ProjectService(config_store=store)
            MockVars.return_value = mock_vars

            result = runner.invoke(
                app,
                [
                    "config",
                    "variables-set",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.x",
                    "--config-id",
                    "cfg-1",
                    "--var",
                    "year_start=2016",
                ],
            )

        assert result.exit_code == 0, result.output
        output = _strip_ansi(result.output)
        assert "created" in output
        assert "year_start" in output
        assert "2016" in output


class TestVariablesGet:
    def test_json_returns_linked_payload(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "config")
        mock_vars = MagicMock()
        mock_vars.get_variables.return_value = {
            "project_alias": "prod",
            "parent_component_id": "keboola.x",
            "parent_config_id": "cfg-1",
            "variables_id": "vars-1",
            "values_id": "row-1",
            "values": {"region": "eu"},
            "linked": True,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProj,
            patch("keboola_agent_cli.cli.VariablesService") as MockVars,
        ):
            MockStore.return_value = store
            MockProj.return_value = ProjectService(config_store=store)
            MockVars.return_value = mock_vars

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "variables-get",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.x",
                    "--config-id",
                    "cfg-1",
                ],
            )

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["data"]["linked"] is True
        assert output["data"]["values"] == {"region": "eu"}

    def test_human_shows_no_variables_when_unlinked(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "config")
        mock_vars = MagicMock()
        mock_vars.get_variables.return_value = {
            "project_alias": "prod",
            "parent_component_id": "keboola.x",
            "parent_config_id": "cfg-1",
            "variables_id": None,
            "values_id": None,
            "values": {},
            "linked": False,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProj,
            patch("keboola_agent_cli.cli.VariablesService") as MockVars,
        ):
            MockStore.return_value = store
            MockProj.return_value = ProjectService(config_store=store)
            MockVars.return_value = mock_vars

            result = runner.invoke(
                app,
                [
                    "config",
                    "variables-get",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.x",
                    "--config-id",
                    "cfg-1",
                ],
            )

        assert result.exit_code == 0
        assert "No variables linked" in _strip_ansi(result.output)


class TestVariablesClear:
    def test_clear_with_yes_skips_prompt(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "config")
        mock_vars = MagicMock()
        mock_vars.clear_variables.return_value = {
            "project_alias": "prod",
            "parent_component_id": "keboola.x",
            "parent_config_id": "cfg-1",
            "was_linked": True,
            "unlinked_variables_id": "vars-1",
            "unlinked_values_id": "row-1",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProj,
            patch("keboola_agent_cli.cli.VariablesService") as MockVars,
        ):
            MockStore.return_value = store
            MockProj.return_value = ProjectService(config_store=store)
            MockVars.return_value = mock_vars

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "variables-clear",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.x",
                    "--config-id",
                    "cfg-1",
                    "--yes",
                ],
            )

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["data"]["was_linked"] is True
        mock_vars.clear_variables.assert_called_once()

    def test_api_error_propagates_with_correct_exit_code(self, tmp_path: Path) -> None:
        """ENCRYPTION_FAILED on set_variables maps to non-zero exit."""
        store = _setup_config(tmp_path / "config")
        mock_vars = MagicMock()
        mock_vars.set_variables.side_effect = KeboolaApiError(
            message="Encryption failed: network",
            status_code=0,
            error_code="ENCRYPTION_FAILED",
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProj,
            patch("keboola_agent_cli.cli.VariablesService") as MockVars,
        ):
            MockStore.return_value = store
            MockProj.return_value = ProjectService(config_store=store)
            MockVars.return_value = mock_vars

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "variables-set",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.x",
                    "--config-id",
                    "cfg-1",
                    "--var",
                    "#secret=x",
                ],
            )

        assert result.exit_code != 0
        output = json.loads(result.output)
        assert output["error"]["code"] == "ENCRYPTION_FAILED"

    def test_clear_human_mode_shows_unlinked_summary(self, tmp_path: Path) -> None:
        """Human mode prints 'Unlinked' + component/config identifiers (ANSI stripped).

        Completes the §5 'both --json and human' coverage for variables-clear --
        the other clear test only exercises the JSON path.
        """
        store = _setup_config(tmp_path / "config")
        mock_vars = MagicMock()
        mock_vars.clear_variables.return_value = {
            "project_alias": "prod",
            "parent_component_id": "keboola.snowflake-transformation",
            "parent_config_id": "cfg-1",
            "was_linked": True,
            "unlinked_variables_id": "vars-1",
            "unlinked_values_id": "row-1",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProj,
            patch("keboola_agent_cli.cli.VariablesService") as MockVars,
        ):
            MockStore.return_value = store
            MockProj.return_value = ProjectService(config_store=store)
            MockVars.return_value = mock_vars

            result = runner.invoke(
                app,
                [
                    "config",
                    "variables-clear",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.snowflake-transformation",
                    "--config-id",
                    "cfg-1",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, result.output
        output = _strip_ansi(result.output)
        assert "Unlinked" in output
        assert "cfg-1" in output
        assert "keboola.snowflake-transformation" in output

    def test_config_error_exits_with_config_error_code(self, tmp_path: Path) -> None:
        """best_practices.md §5: ConfigError from the service maps to exit 5."""
        store = _setup_config(tmp_path / "config")
        mock_vars = MagicMock()
        mock_vars.clear_variables.side_effect = ConfigError("Project 'ghost' not found.")

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProj,
            patch("keboola_agent_cli.cli.VariablesService") as MockVars,
        ):
            MockStore.return_value = store
            MockProj.return_value = ProjectService(config_store=store)
            MockVars.return_value = mock_vars

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "variables-clear",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.x",
                    "--config-id",
                    "cfg-1",
                    "--yes",
                ],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["error"]["code"] == "CONFIG_ERROR"
