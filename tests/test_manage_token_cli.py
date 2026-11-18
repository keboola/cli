"""Tests for the --allow-env-manage-token top-level flag (since 0.28.0).

Pins the contract: KBC_MANAGE_API_TOKEN is ignored by default; passing
--allow-env-manage-token at the top level restores the legacy env-var
resolution. The flag is session-only (not persisted), mirroring
--deny-writes / --deny-destructive.

Two CLI commands covered here consume the manage token: `org setup` and
`project refresh`. Each is tested in both modes: default-deny (asserts the
service never got a token) and allow-env (asserts the service's lazy
provider returned <sentinel>; since #955 the token is resolved only when a
token must be minted). `data-app password` needed it until CLI-23 and
now reads the password with the project token alone
(tests/test_data_app_password.py).
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.commands._helpers import manage_token_if_available, resolve_manage_token
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import AppConfig, ProjectConfig

runner = CliRunner()

TEST_STORAGE_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
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

    @staticmethod
    def _minting_org_mock(seen: list[str | None]) -> MagicMock:
        """An OrgService mock that needs to mint: it calls the lazy provider once.

        Since #955 `project refresh` resolves the Manage token lazily -- the
        service calls ``manage_token_provider`` only when a token must be
        minted, before writing anything.
        """

        def refresh_tokens(**kwargs: Any) -> dict:
            provider: Callable[[], str | None] = kwargs["manage_token_provider"]
            seen.append(provider())
            return {"projects_refreshed": [], "projects_failed": []}

        mock_org = MagicMock()
        mock_org.refresh_tokens.side_effect = refresh_tokens
        return mock_org

    def test_project_refresh_default_ignores_env_warns_and_provider_returns_none(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """R4: without --allow-env-manage-token the env var is ignored with a
        warning; with no TTY the lazy provider returns None (no exit 2), so the
        service decides what a missing Manage token means."""
        store = _make_store(tmp_path)
        monkeypatch.setenv("KBC_MANAGE_API_TOKEN", SENTINEL_MANAGE_TOKEN)

        seen: list[str | None] = []
        mock_org = self._minting_org_mock(seen)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
        ):
            MockStore.return_value = store
            MockOrgService.return_value = mock_org
            result = runner.invoke(
                app, ["--json", "project", "refresh", "--project", "prod", "--yes"]
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        # CliRunner combines stdout+stderr; the warning must appear somewhere.
        assert "found in environment but ignored" in result.output
        assert "--allow-env-manage-token" in result.output
        assert "No manage token available" not in result.output
        assert seen == [None]
        # The sentinel must not leak into output.
        assert SENTINEL_MANAGE_TOKEN not in result.output

    def test_project_refresh_allow_env_uses_env_token(self, tmp_path: Path, monkeypatch) -> None:
        """With --allow-env-manage-token, the lazy provider returns the env token."""
        store = _make_store(tmp_path)
        monkeypatch.setenv("KBC_MANAGE_API_TOKEN", SENTINEL_MANAGE_TOKEN)

        seen: list[str | None] = []
        mock_org = self._minting_org_mock(seen)
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
        assert mock_org.refresh_tokens.call_count == 1
        assert seen == [SENTINEL_MANAGE_TOKEN]
        # Even though the call succeeded, the sentinel must not appear in output.
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


class TestManageTokenResolution:
    """`resolve_manage_token` and `manage_token_if_available` share one policy (#955, R8)."""

    def test_if_available_returns_none_without_tty_and_env(self, monkeypatch, capsys) -> None:
        monkeypatch.delenv("KBC_MANAGE_API_TOKEN", raising=False)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
        assert manage_token_if_available(allow_env=True) is None
        assert capsys.readouterr().err == ""

    def test_if_available_warns_on_ignored_env(self, monkeypatch, capsys) -> None:
        """R4: env present but not allowed -- warning, still None."""
        monkeypatch.setenv("KBC_MANAGE_API_TOKEN", SENTINEL_MANAGE_TOKEN)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
        assert manage_token_if_available(allow_env=False) is None
        err = capsys.readouterr().err
        assert "found in environment but ignored" in err
        assert SENTINEL_MANAGE_TOKEN not in err

    def test_if_available_reads_allowed_env(self, monkeypatch) -> None:
        monkeypatch.setenv("KBC_MANAGE_API_TOKEN", SENTINEL_MANAGE_TOKEN)
        assert manage_token_if_available(allow_env=True) == SENTINEL_MANAGE_TOKEN

    def test_resolve_still_exits_2_with_its_old_error(self, monkeypatch, capsys) -> None:
        monkeypatch.delenv("KBC_MANAGE_API_TOKEN", raising=False)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
        with pytest.raises(typer.Exit) as exc_info:
            resolve_manage_token(allow_env=False)
        assert exc_info.value.exit_code == 2
        assert "Error: No manage token available." in capsys.readouterr().err

    def test_resolve_delegates_to_the_shared_implementation(self) -> None:
        """R8: one implementation -- resolve_manage_token goes through
        manage_token_if_available, so the two can never drift apart."""
        with patch(
            "keboola_agent_cli.commands._helpers.manage_token_if_available",
            return_value="from-shared",
        ) as shared:
            assert resolve_manage_token(allow_env=True) == "from-shared"
        shared.assert_called_once_with(allow_env=True)
