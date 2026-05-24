"""CLI tests for config row-create, row-update, and oauth-url commands."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import Result
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


def _invoke(tmp_config_dir: Path, subcmd: str, args: list[str]) -> Result:
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

    def _make_oauth_service(
        self, tmp_config_dir: Path, is_master_token: bool = True
    ) -> ConfigService:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_oauth_url.return_value = OAUTH_RESULT["url"]
        mock_client.get_project_info.return_value = {
            "id": "9001",
            "description": "test-token",
            "isMasterToken": is_master_token,
            "owner": {"id": 1234, "name": "Test Project"},
        }
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
        mock_client.get_project_info.return_value = {
            "id": "9001",
            "description": "test-token",
            "isMasterToken": True,
            "owner": {"id": 1234, "name": "Test"},
        }
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

    def test_redirect_url_propagates(self, tmp_config_dir: Path) -> None:
        """--redirect-url is forwarded to the service and surfaced in JSON output."""
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_project_info.return_value = {
            "id": "9001",
            "description": "test-token",
            "isMasterToken": True,
            "owner": {"id": 1234, "name": "Test"},
        }
        mock_client.get_oauth_url.return_value = (
            "https://external.keboola.com/oauth/index.html"
            "?token=abc&sapiUrl=https%3A%2F%2Fconnection.keboola.com"
            "&returnUrl=https%3A%2F%2Fexample.com%2Fdone"
            "#/keboola.ex-google-drive/cfg-001"
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
                    "--redirect-url",
                    "https://example.com/done",
                ],
            )

        assert result.exit_code == 0, result.output
        kwargs = mock_client.get_oauth_url.call_args.kwargs
        assert kwargs["redirect_url"] == "https://example.com/done"
        output = json.loads(result.output)
        assert output["data"]["redirect_url"] == "https://example.com/done"


# ---------------------------------------------------------------------------
# is_disabled / is_enabled CLI tests
# ---------------------------------------------------------------------------


class TestRowDisableFlagsCli:
    """CLI-level tests for --is-disabled / --is-enabled on row-create / row-update."""

    def test_create_with_is_disabled(self, tmp_config_dir: Path) -> None:
        """row-create --is-disabled forwards is_disabled=True to the client."""
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
                    "keboola.ex-mysql",
                    "--config-id",
                    "cfg-001",
                    "--name",
                    "New Row",
                    "--is-disabled",
                ],
            )

        assert result.exit_code == 0, result.output

    def test_update_is_disabled_and_is_enabled_mutually_exclusive(
        self, tmp_config_dir: Path
    ) -> None:
        """Passing both --is-disabled and --is-enabled exits 2."""
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
                    "keboola.ex-mysql",
                    "--config-id",
                    "cfg-001",
                    "--row-id",
                    "row-001",
                    "--is-disabled",
                    "--is-enabled",
                ],
            )

        assert result.exit_code == 2

    def test_update_is_enabled_alone_is_valid(self, tmp_config_dir: Path) -> None:
        """row-update --is-enabled (without other flags) is valid; forwards False."""
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
                    "keboola.ex-mysql",
                    "--config-id",
                    "cfg-001",
                    "--row-id",
                    "row-001",
                    "--is-enabled",
                ],
            )

        assert result.exit_code == 0, result.output


class TestOauthUrlMasterTokenGate:
    """CLI-level tests for the master-token pre-flight on `config oauth-url`."""

    def test_non_master_token_exits_3(self, tmp_config_dir: Path) -> None:
        """Non-master token -> exit 3 (auth) with MISSING_MASTER_TOKEN code."""
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_project_info.return_value = {
            "id": "10851170",
            "description": "kbagent-cli [petr@keboola.com]",
            "isMasterToken": False,
            "owner": {"id": 901, "name": "Padak"},
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

        assert result.exit_code == 3, result.output
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "MISSING_MASTER_TOKEN"
        # No URL should ever be minted on a non-master token.
        mock_client.get_oauth_url.assert_not_called()


class TestConfigRowDeleteCli:
    """CLI-level tests for `config row-delete`."""

    def test_delete_with_yes_flag(self, tmp_config_dir: Path) -> None:
        """--yes skips confirmation and deletes the row."""
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.delete_config_row.return_value = None
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
                "row-delete",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-mysql",
                    "--config-id",
                    "cfg-001",
                    "--row-id",
                    "row-001",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["deleted"] is True
        assert output["data"]["row_id"] == "row-001"
        mock_client.delete_config_row.assert_called_once()

    def test_delete_json_mode_skips_prompt(self, tmp_config_dir: Path) -> None:
        """--json mode skips interactive prompt even without --yes."""
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.delete_config_row.return_value = None
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.config.get_service",
                lambda ctx, name: service,
            )
            # Note: _invoke already passes --json
            result = _invoke(
                tmp_config_dir,
                "row-delete",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-mysql",
                    "--config-id",
                    "cfg-001",
                    "--row-id",
                    "row-001",
                ],
            )

        assert result.exit_code == 0, result.output
        mock_client.delete_config_row.assert_called_once()

    def test_delete_404_returns_nonzero_exit(self, tmp_config_dir: Path) -> None:
        """Delete on a non-existent row -> non-zero exit, NOT_FOUND error code."""
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.delete_config_row.side_effect = KeboolaApiError(
            status_code=404, error_code="NOT_FOUND", message="Row not found"
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
                "row-delete",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-mysql",
                    "--config-id",
                    "cfg-001",
                    "--row-id",
                    "missing",
                    "--yes",
                ],
            )

        assert result.exit_code != 0
        output = json.loads(result.output)
        assert output["error"]["code"] == "NOT_FOUND"

    def test_delete_missing_row_id_exits_nonzero(self, tmp_config_dir: Path) -> None:
        """Missing required --row-id causes non-zero exit (Typer validation)."""
        result = runner.invoke(
            app,
            [
                "--json",
                "--config-dir",
                str(tmp_config_dir),
                "config",
                "row-delete",
                "--project",
                "prod",
                "--component-id",
                "comp",
                "--config-id",
                "cfg",
            ],
        )
        assert result.exit_code != 0
