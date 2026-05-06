"""Tests for the --allow-env-manage-token top-level flag (since 0.28.0).

Pins the contract: KBC_MANAGE_API_TOKEN is ignored by default; passing
--allow-env-manage-token at the top level restores the legacy env-var
resolution. The flag is session-only (not persisted), mirroring
--deny-writes / --deny-destructive.

Three CLI surfaces consume the manage token: `org setup`,
`project refresh`, and `data-app password`. Each is tested in both
modes: default-deny (asserts the service was never reached) and
allow-env (asserts the service received manage_token=<sentinel>).
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import AppConfig, ProjectConfig

runner = CliRunner()

TEST_STORAGE_TOKEN = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"
SENTINEL_MANAGE_TOKEN = "manage-sentinel-7af3e9c1-test-only"


def _make_store(tmp_path: Path) -> ConfigStore:
    """Create a ConfigStore with a single registered project."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    store = ConfigStore(config_dir=config_dir)
    config = AppConfig(
        projects={
            "prod": ProjectConfig(
                stack_url="https://connection.keboola.com",
                token=TEST_STORAGE_TOKEN,
            )
        }
    )
    store.save(config)
    return store


class TestAllowEnvManageTokenFlag:
    """Default-deny env + opt-in flag for resolve_manage_token."""

    def test_project_refresh_default_ignores_env_warns_no_tty_exits_2(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Without --allow-env-manage-token, env is ignored, no TTY -> exit 2.
        The OrgService.refresh_tokens call site must never be reached."""
        store = _make_store(tmp_path)
        monkeypatch.setenv("KBC_MANAGE_API_TOKEN", SENTINEL_MANAGE_TOKEN)

        mock_org = MagicMock()
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
        ):
            MockStore.return_value = store
            MockOrgService.return_value = mock_org
            result = runner.invoke(
                app, ["--json", "project", "refresh", "--project", "prod", "--yes"]
            )

        assert result.exit_code == 2, f"Exit code {result.exit_code}: {result.output}"
        # CliRunner combines stdout+stderr; the warning must appear somewhere.
        assert "found in environment but ignored" in result.output
        assert "--allow-env-manage-token" in result.output
        # Crucial: the service was never called.
        mock_org.refresh_tokens.assert_not_called()
        # The sentinel must not leak into output.
        assert SENTINEL_MANAGE_TOKEN not in result.output

    def test_project_refresh_allow_env_uses_env_token(self, tmp_path: Path, monkeypatch) -> None:
        """With --allow-env-manage-token, env is honoured and forwarded
        to OrgService.refresh_tokens as manage_token kwarg."""
        store = _make_store(tmp_path)
        monkeypatch.setenv("KBC_MANAGE_API_TOKEN", SENTINEL_MANAGE_TOKEN)

        mock_org = MagicMock()
        mock_org.refresh_tokens.return_value = {
            "status": "ok",
            "refreshed": [],
            "errors": [],
        }
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
        ):
            MockStore.return_value = store
            MockOrgService.return_value = mock_org
            result = runner.invoke(
                app,
                [
                    "--allow-env-manage-token",
                    "--json",
                    "project",
                    "refresh",
                    "--project",
                    "prod",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        # Service called once, with the env-supplied manage_token.
        assert mock_org.refresh_tokens.call_count == 1
        kwargs = mock_org.refresh_tokens.call_args.kwargs
        assert kwargs["manage_token"] == SENTINEL_MANAGE_TOKEN
        # Even though the call succeeded, the sentinel must not appear in
        # JSON output (the resolver and service handle masking).
        assert SENTINEL_MANAGE_TOKEN not in result.output

    def test_data_app_password_default_deny_no_tty_exits_2(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """data-app password mirrors project refresh: env ignored without
        the flag, service never called."""
        store = _make_store(tmp_path)
        monkeypatch.setenv("KBC_MANAGE_API_TOKEN", SENTINEL_MANAGE_TOKEN)

        mock_data_app = MagicMock()
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.DataAppService") as MockDataAppService,
        ):
            MockStore.return_value = store
            MockDataAppService.return_value = mock_data_app
            result = runner.invoke(
                app,
                [
                    "--json",
                    "data-app",
                    "password",
                    "--project",
                    "prod",
                    "--app-id",
                    "1",
                ],
            )

        assert result.exit_code == 2
        assert "found in environment but ignored" in result.output
        assert "--allow-env-manage-token" in result.output
        mock_data_app.get_data_app_password.assert_not_called()
        assert SENTINEL_MANAGE_TOKEN not in result.output

    def test_org_setup_allow_env_passes_token_through(self, tmp_path: Path, monkeypatch) -> None:
        """org setup with --allow-env-manage-token forwards the env token
        to OrgService.setup_organization."""
        store = _make_store(tmp_path)
        monkeypatch.setenv("KBC_MANAGE_API_TOKEN", SENTINEL_MANAGE_TOKEN)

        mock_org = MagicMock()
        mock_org.setup_organization.return_value = {
            "status": "ok",
            "registered": [],
            "errors": [],
        }
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
        ):
            MockStore.return_value = store
            MockOrgService.return_value = mock_org
            result = runner.invoke(
                app,
                [
                    "--allow-env-manage-token",
                    "--json",
                    "org",
                    "setup",
                    "--org-id",
                    "1",
                    "--url",
                    "https://connection.keboola.com",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert mock_org.setup_organization.call_count == 1
        kwargs = mock_org.setup_organization.call_args.kwargs
        assert kwargs["manage_token"] == SENTINEL_MANAGE_TOKEN
        assert SENTINEL_MANAGE_TOKEN not in result.output
