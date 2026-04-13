"""Tests for config update with configuration content (merge, --set, dry-run).

Covers ConfigService.update_config and the CLI command.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from helpers import setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.services.config_service import ConfigService

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_CONFIG_DETAIL = {
    "id": "cfg-001",
    "name": "My Config",
    "description": "desc",
    "configuration": {
        "parameters": {
            "user": "admin",
            "project": "proj1",
            "tables": {"old_table": {"columns": ["a", "b"]}},
            "dimensions": ["dim1", "dim2"],
        },
        "storage": {"input": {"tables": []}},
    },
}


def _make_service(tmp_config_dir: Path) -> tuple[ConfigService, MagicMock]:
    """Create a ConfigService with a mock client."""
    store = setup_single_project(tmp_config_dir)
    mock_client = MagicMock()
    mock_client.get_config_detail.return_value = SAMPLE_CONFIG_DETAIL
    mock_client.update_config.return_value = {
        "id": "cfg-001",
        "name": "My Config",
        "componentId": "keboola.ex-db-snowflake",
    }
    service = ConfigService(
        config_store=store,
        client_factory=lambda url, token: mock_client,
    )
    return service, mock_client


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


class TestConfigServiceUpdateConfiguration:
    """Tests for ConfigService.update_config with configuration content."""

    def test_full_replace(self, tmp_config_dir: Path) -> None:
        """Without --merge, configuration is sent as-is (full replace)."""
        service, client = _make_service(tmp_config_dir)
        new_cfg = {"parameters": {"tables": {"brand_new": True}}}

        service.update_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            configuration=new_cfg,
        )

        client.update_config.assert_called_once()
        call_kwargs = client.update_config.call_args.kwargs
        assert call_kwargs["configuration"] == new_cfg
        # Should NOT fetch current config for full replace
        client.get_config_detail.assert_not_called()

    def test_merge_preserves_siblings(self, tmp_config_dir: Path) -> None:
        """With merge=True, sibling keys under 'parameters' are preserved."""
        service, client = _make_service(tmp_config_dir)
        partial = {"parameters": {"tables": {"new_table": {"columns": ["x"]}}}}

        service.update_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            configuration=partial,
            merge=True,
        )

        call_kwargs = client.update_config.call_args.kwargs
        merged = call_kwargs["configuration"]

        # Sibling keys preserved
        assert merged["parameters"]["user"] == "admin"
        assert merged["parameters"]["project"] == "proj1"
        assert merged["parameters"]["dimensions"] == ["dim1", "dim2"]
        # deep_merge merges dict+dict: old table key preserved, new one added
        assert merged["parameters"]["tables"]["new_table"] == {"columns": ["x"]}
        assert merged["parameters"]["tables"]["old_table"] == {"columns": ["a", "b"]}
        # Storage section preserved
        assert "storage" in merged

    def test_set_path_preserves_siblings(self, tmp_config_dir: Path) -> None:
        """--set targets a specific key without touching siblings."""
        service, client = _make_service(tmp_config_dir)

        service.update_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            set_paths=[("parameters.tables", {"replaced": True})],
        )

        call_kwargs = client.update_config.call_args.kwargs
        cfg = call_kwargs["configuration"]
        assert cfg["parameters"]["user"] == "admin"
        assert cfg["parameters"]["project"] == "proj1"
        assert cfg["parameters"]["tables"] == {"replaced": True}

    def test_multiple_set_paths(self, tmp_config_dir: Path) -> None:
        """Multiple --set values are applied sequentially."""
        service, client = _make_service(tmp_config_dir)

        service.update_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            set_paths=[
                ("parameters.user", "new-admin"),
                ("parameters.project", "new-proj"),
            ],
        )

        cfg = client.update_config.call_args.kwargs["configuration"]
        assert cfg["parameters"]["user"] == "new-admin"
        assert cfg["parameters"]["project"] == "new-proj"
        # Untouched keys preserved
        assert "tables" in cfg["parameters"]

    def test_dry_run_returns_diff(self, tmp_config_dir: Path) -> None:
        """dry_run=True returns changes without calling update_config."""
        service, client = _make_service(tmp_config_dir)

        result = service.update_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            set_paths=[("parameters.user", "new-admin")],
            dry_run=True,
        )

        assert result["dry_run"] is True
        assert len(result["changes"]) >= 1
        assert any("parameters.user" in c for c in result["changes"])
        # Should NOT call update
        client.update_config.assert_not_called()

    def test_metadata_only_still_works(self, tmp_config_dir: Path) -> None:
        """Name/description update without configuration still works."""
        service, client = _make_service(tmp_config_dir)

        service.update_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            name="New Name",
        )

        call_kwargs = client.update_config.call_args.kwargs
        assert call_kwargs["name"] == "New Name"
        assert call_kwargs["configuration"] is None

    def test_metadata_plus_configuration(self, tmp_config_dir: Path) -> None:
        """Both metadata and configuration can be updated at once."""
        service, client = _make_service(tmp_config_dir)

        service.update_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            name="New Name",
            set_paths=[("parameters.user", "updated")],
        )

        call_kwargs = client.update_config.call_args.kwargs
        assert call_kwargs["name"] == "New Name"
        assert call_kwargs["configuration"]["parameters"]["user"] == "updated"

    def test_validation_error_when_nothing_provided(self, tmp_config_dir: Path) -> None:
        """Raise error if no metadata or configuration is given."""
        service, _ = _make_service(tmp_config_dir)

        with pytest.raises(KeboolaApiError, match="must be provided"):
            service.update_config(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                config_id="cfg-001",
            )


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestConfigUpdateCli:
    """CLI-level tests for config update command."""

    def _invoke(self, tmp_config_dir: Path, args: list[str]) -> object:
        return runner.invoke(
            app,
            ["--json", "--config-dir", str(tmp_config_dir), "config", "update", *args],
        )

    def test_set_flag(self, tmp_config_dir: Path) -> None:
        """--set flag parses PATH=VALUE and sends to service."""
        store = setup_single_project(tmp_config_dir)

        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = SAMPLE_CONFIG_DETAIL
        mock_client.update_config.return_value = {
            "id": "cfg-001",
            "name": "My Config",
            "componentId": "keboola.ex-db-snowflake",
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.config.get_service",
                lambda ctx, name: ConfigService(
                    config_store=store,
                    client_factory=lambda url, token: mock_client,
                ),
            )

            result = self._invoke(
                tmp_config_dir,
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--set",
                    "parameters.user=new-admin",
                ],
            )

        assert result.exit_code == 0, result.output
        cfg = mock_client.update_config.call_args.kwargs["configuration"]
        assert cfg["parameters"]["user"] == "new-admin"
        assert cfg["parameters"]["project"] == "proj1"

    def test_configuration_inline_json(self, tmp_config_dir: Path) -> None:
        """--configuration accepts inline JSON."""
        store = setup_single_project(tmp_config_dir)

        mock_client = MagicMock()
        mock_client.update_config.return_value = {
            "id": "cfg-001",
            "name": "My Config",
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.config.get_service",
                lambda ctx, name: ConfigService(
                    config_store=store,
                    client_factory=lambda url, token: mock_client,
                ),
            )

            new_cfg = {"parameters": {"key": "value"}}
            result = self._invoke(
                tmp_config_dir,
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "comp",
                    "--config-id",
                    "cfg-001",
                    "--configuration",
                    json.dumps(new_cfg),
                ],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = mock_client.update_config.call_args.kwargs
        assert call_kwargs["configuration"] == new_cfg

    def test_configuration_file(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """--configuration-file reads JSON from disk."""
        store = setup_single_project(tmp_config_dir)
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"parameters": {"from_file": True}}))

        mock_client = MagicMock()
        mock_client.update_config.return_value = {
            "id": "cfg-001",
            "name": "My Config",
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.config.get_service",
                lambda ctx, name: ConfigService(
                    config_store=store,
                    client_factory=lambda url, token: mock_client,
                ),
            )

            result = self._invoke(
                tmp_config_dir,
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "comp",
                    "--config-id",
                    "cfg-001",
                    "--configuration-file",
                    str(cfg_file),
                ],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = mock_client.update_config.call_args.kwargs
        assert call_kwargs["configuration"]["parameters"]["from_file"] is True

    def test_dry_run_output(self, tmp_config_dir: Path) -> None:
        """--dry-run shows changes without applying."""
        store = setup_single_project(tmp_config_dir)

        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = SAMPLE_CONFIG_DETAIL

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.config.get_service",
                lambda ctx, name: ConfigService(
                    config_store=store,
                    client_factory=lambda url, token: mock_client,
                ),
            )

            result = self._invoke(
                tmp_config_dir,
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--set",
                    "parameters.user=changed",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        data = output["data"]
        assert data["dry_run"] is True
        assert any("parameters.user" in c for c in data["changes"])
        mock_client.update_config.assert_not_called()

    def test_invalid_set_format(self, tmp_config_dir: Path) -> None:
        """--set without = sign gives validation error."""
        setup_single_project(tmp_config_dir)

        result = self._invoke(
            tmp_config_dir,
            [
                "--project",
                "prod",
                "--component-id",
                "comp",
                "--config-id",
                "cfg-001",
                "--set",
                "no-equals-sign",
            ],
        )

        assert result.exit_code == 2

    def test_both_configuration_and_file_rejected(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Cannot use --configuration and --configuration-file together."""
        setup_single_project(tmp_config_dir)
        cfg_file = tmp_path / "c.json"
        cfg_file.write_text("{}")

        result = self._invoke(
            tmp_config_dir,
            [
                "--project",
                "prod",
                "--component-id",
                "comp",
                "--config-id",
                "cfg-001",
                "--configuration",
                "{}",
                "--configuration-file",
                str(cfg_file),
            ],
        )

        assert result.exit_code == 2

    def test_set_json_value(self, tmp_config_dir: Path) -> None:
        """--set with JSON value is parsed correctly."""
        store = setup_single_project(tmp_config_dir)

        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = SAMPLE_CONFIG_DETAIL
        mock_client.update_config.return_value = {
            "id": "cfg-001",
            "name": "My Config",
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.config.get_service",
                lambda ctx, name: ConfigService(
                    config_store=store,
                    client_factory=lambda url, token: mock_client,
                ),
            )

            result = self._invoke(
                tmp_config_dir,
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--set",
                    'parameters.tables={"new": "table"}',
                ],
            )

        assert result.exit_code == 0, result.output
        cfg = mock_client.update_config.call_args.kwargs["configuration"]
        assert cfg["parameters"]["tables"] == {"new": "table"}
