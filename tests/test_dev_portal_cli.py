"""Tests for `kbagent dev-portal` command layer via CliRunner."""

from __future__ import annotations

import json
from unittest.mock import patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app

runner = CliRunner()


class TestIdentityCommands:
    def test_identity_add_and_list_json(self, tmp_config_dir):
        with patch(
            "keboola_agent_cli.services.dev_portal_service.DeveloperPortalService.add_identity"
        ) as add_:
            r = runner.invoke(
                app,
                [
                    "--config-dir",
                    str(tmp_config_dir),
                    "--json",
                    "dev-portal",
                    "identity",
                    "add",
                    "--alias",
                    "alpha",
                    "--username",
                    "service.keboola.x",
                    "--password",
                    "p",
                ],
            )
        assert r.exit_code == 0, r.output
        add_.assert_called_once()

    def test_identity_use_sets_default(self, tmp_config_dir, config_store):
        from keboola_agent_cli.models import DeveloperPortalIdentity

        config_store.add_dev_portal_identity(
            "alpha", DeveloperPortalIdentity(username="u", password="p")
        )
        config_store.add_dev_portal_identity(
            "beta", DeveloperPortalIdentity(username="u", password="p")
        )
        r = runner.invoke(
            app,
            [
                "--config-dir",
                str(tmp_config_dir),
                "dev-portal",
                "identity",
                "use",
                "beta",
            ],
        )
        assert r.exit_code == 0, r.output
        assert config_store.load().default_dev_portal_identity == "beta"


class TestReadCommands:
    def test_list_apps_json(self, tmp_config_dir, config_store):
        from keboola_agent_cli.models import DeveloperPortalIdentity

        config_store.add_dev_portal_identity(
            "alpha", DeveloperPortalIdentity(username="u", password="p", vendor="keboola")
        )
        with patch(
            "keboola_agent_cli.services.dev_portal_service.DeveloperPortalService.list_apps",
            return_value=[{"id": "keboola.ex-a"}],
        ):
            r = runner.invoke(
                app,
                [
                    "--config-dir",
                    str(tmp_config_dir),
                    "--json",
                    "dev-portal",
                    "list",
                    "--vendor",
                    "keboola",
                ],
            )
        assert r.exit_code == 0, r.output
        data = json.loads(r.stdout)
        assert data["data"] == [{"id": "keboola.ex-a"}]
