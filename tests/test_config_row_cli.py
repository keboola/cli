"""CLI tests for config row-create, row-update, and oauth-url commands."""

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
# Shared sample data
# ---------------------------------------------------------------------------

SAMPLE_ROW = {
    "id": "row-001",
    "name": "My Row",
    "description": "",
    "configuration": {
        "parameters": {
            "table": "orders",
            "limit": 1000,
        }
    },
    "isDisabled": False,
    "project_alias": "prod",
    "branch_id": None,
}

OAUTH_RESULT = {
    "url": (
        "https://external.keboola.com/oauth/index.html"
        "?token=abc123&sapiUrl=https%3A%2F%2Fconnection.keboola.com"
        "#/keboola.ex-google-drive/cfg-001"
    ),
    "component_id": "keboola.ex-google-drive",
    "config_id": "cfg-001",
    "project_alias": "prod",
}


def _make_service(tmp_config_dir: Path) -> ConfigService:
    store = setup_single_project(tmp_config_dir)
    mock_client = MagicMock()
    mock_client.get_config_row.return_value = {
        "id": "row-001",
        "name": "My Row",
        "configuration": {"parameters": {"table": "orders", "limit": 1000}},
    }
    mock_client.create_config_row.return_value = {**SAMPLE_ROW, "id": "row-new"}
    mock_client.update_config_row.return_value = {**SAMPLE_ROW, "name": "Updated Row"}
    return ConfigService(
        config_store=store,
        client_factory=lambda url, token: mock_client,
    )


def _invoke(tmp_config_dir: Path, subcmd: str, args: list[str]) -> object:
    return runner.invoke(
        app,
        ["--json", "--config-dir", str(tmp_config_dir), "config", subcmd, *args],
    )


# ---------------------------------------------------------------------------
# config row-create CLI tests
# ---------------------------------------------------------------------------


class TestConfigRowCreateCli:
    """CLI-level tests for config row-create."""

    def test_minimal_create_success(self, tmp_config_dir: Path) -> None:
        """Minimal create (name only) returns exit 0 and status=ok."""
        service = _make_service(tmp_config_dir)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.config.get_service",
                lambda ctx, name: service,
            )
            result = _invoke(
                tmp_config_dir,
                "row-create",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--name",
                    "My Row",
                ],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "ok"

    def test_create_with_configuration_json(self, tmp_config_dir: Path) -> None:
        """--configuration accepts inline JSON and sends it to service."""
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.create_config_row.return_value = {**SAMPLE_ROW, "id": "row-x"}
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        cfg = {"parameters": {"table": "invoices"}}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.config.get_service",
                lambda ctx, name: service,
            )
            result = _invoke(
                tmp_config_dir,
                "row-create",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "comp",
                    "--config-id",
                    "cfg-001",
                    "--name",
                    "Row",
                    "--configuration",
                    json.dumps(cfg),
                ],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = mock_client.create_config_row.call_args.kwargs
        assert call_kwargs["configuration"] == cfg

    def test_create_invalid_json_exits_2(self, tmp_config_dir: Path) -> None:
        """Invalid JSON in --configuration gives exit code 2."""
        service = _make_service(tmp_config_dir)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.config.get_service",
                lambda ctx, name: service,
            )
            result = _invoke(
                tmp_config_dir,
                "row-create",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "comp",
                    "--config-id",
                    "cfg-001",
                    "--name",
                    "Row",
                    "--configuration",
                    "NOT_JSON",
                ],
            )

        assert result.exit_code == 2

    def test_create_missing_name_exits_nonzero(self, tmp_config_dir: Path) -> None:
        """Missing --name causes non-zero exit (Typer validation)."""
        result = runner.invoke(
            app,
            [
                "--json",
                "--config-dir",
                str(tmp_config_dir),
                "config",
                "row-create",
                "--project",
                "prod",
                "--component-id",
                "comp",
                "--config-id",
                "cfg-001",
            ],
        )
        assert result.exit_code != 0

    def test_create_with_file(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """--configuration @file reads JSON from disk."""
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.create_config_row.return_value = {**SAMPLE_ROW, "id": "row-file"}
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        cfg = {"parameters": {"source": "file"}}
        cfg_file = tmp_path / "row.json"
        cfg_file.write_text(json.dumps(cfg))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.config.get_service",
                lambda ctx, name: service,
            )
            result = _invoke(
                tmp_config_dir,
                "row-create",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "comp",
                    "--config-id",
                    "cfg-001",
                    "--name",
                    "Row",
                    "--configuration",
                    f"@{cfg_file}",
                ],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = mock_client.create_config_row.call_args.kwargs
        assert call_kwargs["configuration"] == cfg


# ---------------------------------------------------------------------------
# config row-update CLI tests
# ---------------------------------------------------------------------------


class TestConfigRowUpdateCli:
    """CLI-level tests for config row-update."""

    def test_name_update_success(self, tmp_config_dir: Path) -> None:
        """Updating only --name returns exit 0."""
        service = _make_service(tmp_config_dir)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.config.get_service",
                lambda ctx, name: service,
            )
            result = _invoke(
                tmp_config_dir,
                "row-update",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--row-id",
                    "row-001",
                    "--name",
                    "New Name",
                ],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "ok"

    def test_set_flag(self, tmp_config_dir: Path) -> None:
        """--set PATH=VALUE is parsed and forwarded to service."""
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_row.return_value = {
            "id": "row-001",
            "configuration": {"parameters": {"table": "orders", "limit": 1000}},
        }
        mock_client.update_config_row.return_value = {**SAMPLE_ROW}
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.config.get_service",
                lambda ctx, name: service,
            )
            result = _invoke(
                tmp_config_dir,
                "row-update",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--row-id",
                    "row-001",
                    "--set",
                    "parameters.table=invoices",
                ],
            )

        assert result.exit_code == 0, result.output
        cfg = mock_client.update_config_row.call_args.kwargs["configuration"]
        assert cfg["parameters"]["table"] == "invoices"
        assert cfg["parameters"]["limit"] == 1000  # sibling preserved

    def test_dry_run_output(self, tmp_config_dir: Path) -> None:
        """--dry-run shows diff in JSON mode."""
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_row.return_value = {
            "id": "row-001",
            "configuration": {"parameters": {"table": "orders"}},
        }
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.config.get_service",
                lambda ctx, name: service,
            )
            result = _invoke(
                tmp_config_dir,
                "row-update",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "comp",
                    "--config-id",
                    "cfg-001",
                    "--row-id",
                    "row-001",
                    "--set",
                    "parameters.table=changed",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        data = output["data"]
        assert data["dry_run"] is True
        assert any("parameters.table" in c for c in data["changes"])
        mock_client.update_config_row.assert_not_called()

    def test_invalid_set_format_exits_2(self, tmp_config_dir: Path) -> None:
        """--set without = sign gives exit code 2."""
        service = _make_service(tmp_config_dir)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.config.get_service",
                lambda ctx, name: service,
            )
            result = _invoke(
                tmp_config_dir,
                "row-update",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "comp",
                    "--config-id",
                    "cfg-001",
                    "--row-id",
                    "row-001",
                    "--set",
                    "no-equals-sign",
                ],
            )

        assert result.exit_code == 2

    def test_no_changes_exits_nonzero(self, tmp_config_dir: Path) -> None:
        """Providing no update options gives a non-zero exit (validation error)."""
        service = _make_service(tmp_config_dir)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.config.get_service",
                lambda ctx, name: service,
            )
            result = _invoke(
                tmp_config_dir,
                "row-update",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "comp",
                    "--config-id",
                    "cfg-001",
                    "--row-id",
                    "row-001",
                ],
            )

        assert result.exit_code != 0

    def test_configuration_inline_json(self, tmp_config_dir: Path) -> None:
        """--configuration accepts inline JSON and sends full replace."""
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.update_config_row.return_value = {**SAMPLE_ROW}
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        new_cfg = {"parameters": {"table": "users"}}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.config.get_service",
                lambda ctx, name: service,
            )
            result = _invoke(
                tmp_config_dir,
                "row-update",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "comp",
                    "--config-id",
                    "cfg-001",
                    "--row-id",
                    "row-001",
                    "--configuration",
                    json.dumps(new_cfg),
                ],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = mock_client.update_config_row.call_args.kwargs
        assert call_kwargs["configuration"] == new_cfg
        # No merge, so get_config_row should NOT have been called
        mock_client.get_config_row.assert_not_called()


# ---------------------------------------------------------------------------
# config oauth-url CLI tests
# ---------------------------------------------------------------------------


class TestConfigOauthUrlCli:
    """CLI-level tests for config oauth-url."""

    def _make_oauth_service(self, tmp_config_dir: Path) -> ConfigService:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_oauth_url.return_value = OAUTH_RESULT["url"]
        return ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

    def test_returns_url_json(self, tmp_config_dir: Path) -> None:
        """JSON mode returns url, component_id, config_id, project_alias."""
        service = self._make_oauth_service(tmp_config_dir)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.config.get_service",
                lambda ctx, name: service,
            )
            result = _invoke(
                tmp_config_dir,
                "oauth-url",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-google-drive",
                    "--config-id",
                    "cfg-001",
                ],
            )

        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        data = output["data"]
        assert "url" in data
        assert data["url"].startswith("https://external.keboola.com")
        assert data["component_id"] == "keboola.ex-google-drive"
        assert data["config_id"] == "cfg-001"
        assert data["project_alias"] == "prod"

    def test_api_error_exits_nonzero(self, tmp_config_dir: Path) -> None:
        """API error from get_oauth_url gives non-zero exit code."""
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_oauth_url.side_effect = KeboolaApiError(
            status_code=403, error_code="ACCESS_DENIED", message="Forbidden"
        )
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.config.get_service",
                lambda ctx, name: service,
            )
            result = _invoke(
                tmp_config_dir,
                "oauth-url",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-google-drive",
                    "--config-id",
                    "cfg-001",
                ],
            )

        assert result.exit_code != 0

    def test_missing_project_exits_nonzero(self, tmp_config_dir: Path) -> None:
        """Missing required --project causes non-zero exit."""
        result = runner.invoke(
            app,
            [
                "--json",
                "--config-dir",
                str(tmp_config_dir),
                "config",
                "oauth-url",
                "--component-id",
                "keboola.ex-google-drive",
                "--config-id",
                "cfg-001",
            ],
        )
        assert result.exit_code != 0
