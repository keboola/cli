"""Tests for `kbagent dev-portal` command layer via CliRunner."""

from __future__ import annotations

import json
from unittest.mock import patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app

runner = CliRunner()


class TestReadPasswordStdin:
    """`--password-stdin` end-to-end through the CLI layer. The TTY/pipe unit
    behavior of the underlying helper is covered once, generically, in
    tests/test_helpers.py::TestReadPasswordStdin (the helper moved to
    commands/_helpers.py -- shared with `auth login-password`, PR #565
    round 2 -- rather than staying a private per-command copy)."""

    def test_identity_add_password_stdin_end_to_end(self, tmp_config_dir):
        """End-to-end CliRunner test: --password-stdin in pipe mode (the
        CliRunner's stdin is not a TTY) must thread the piped password through
        Typer's flag parsing into the helper and into the persisted identity.
        Catches a regression where the flag and the helper get rewired
        independently and the password silently lands as empty."""
        from keboola_agent_cli.config_store import ConfigStore

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
                    "piped",
                    "--username",
                    "u",
                    "--password-stdin",
                ],
                input="my-piped-secret\n",
            )
        assert r.exit_code == 0, r.output
        add_.assert_called_once()
        # The identity object passed to the service must carry the piped password,
        # stripped of trailing newline.
        call_args = add_.call_args
        identity = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs["identity"]
        assert identity.password == "my-piped-secret"
        # Sanity: nothing was persisted on disk (add_identity is mocked).
        assert ConfigStore(tmp_config_dir, source="cli-flag").load().dev_portal_identities == {}


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


class TestWriteCommands:
    """Every write must require the random-code confirm. No --yes."""

    def _seed_identity(self, config_store):
        from keboola_agent_cli.models import DeveloperPortalIdentity

        config_store.add_dev_portal_identity(
            "alpha",
            DeveloperPortalIdentity(username="u", password="p", vendor="keboola"),
        )

    def test_patch_non_tty_exits_6(self, tmp_config_dir, config_store):
        """Without a TTY there is NO bypass — exit 6, no portal call."""
        self._seed_identity(config_store)
        with patch(
            "keboola_agent_cli.services.dev_portal_service.DeveloperPortalService.prepare_patch"
        ) as prep:
            from keboola_agent_cli.services.dev_portal_service import FieldDiff, PendingPatch

            prep.return_value = PendingPatch(
                alias="alpha",
                vendor="keboola",
                app_id="keboola.ex-a",
                payload={"name": "New"},
                current={"name": "Old"},
                diff=[FieldDiff(key="name", current="Old", new="New")],
            )
            with patch(
                "keboola_agent_cli.services.dev_portal_service.DeveloperPortalService.apply"
            ) as apply_:
                # CliRunner provides a non-TTY stdin.
                r = runner.invoke(
                    app,
                    [
                        "--config-dir",
                        str(tmp_config_dir),
                        "dev-portal",
                        "patch",
                        "--app",
                        "keboola.ex-a",
                        "--data",
                        "/tmp/does-not-matter.json",
                    ],
                    input="",
                )
        assert r.exit_code == 6, r.output
        apply_.assert_not_called()

    def test_patch_dry_run_no_confirm(self, tmp_config_dir, config_store, tmp_path):
        """--dry-run prints diff and exits 0 without any confirm prompt."""
        self._seed_identity(config_store)
        data_file = tmp_path / "patch.json"
        data_file.write_text(json.dumps({"name": "New"}))
        with patch(
            "keboola_agent_cli.services.dev_portal_service.DeveloperPortalService.prepare_patch"
        ) as prep:
            from keboola_agent_cli.services.dev_portal_service import FieldDiff, PendingPatch

            prep.return_value = PendingPatch(
                alias="alpha",
                vendor="keboola",
                app_id="keboola.ex-a",
                payload={"name": "New"},
                current={"name": "Old"},
                diff=[FieldDiff(key="name", current="Old", new="New")],
            )
            with patch(
                "keboola_agent_cli.services.dev_portal_service.DeveloperPortalService.apply"
            ) as apply_:
                r = runner.invoke(
                    app,
                    [
                        "--config-dir",
                        str(tmp_config_dir),
                        "--json",
                        "dev-portal",
                        "patch",
                        "--app",
                        "keboola.ex-a",
                        "--data",
                        str(data_file),
                        "--dry-run",
                    ],
                )
        assert r.exit_code == 0, r.output
        apply_.assert_not_called()
        # JSON output should advertise the dry-run status
        assert "dry-run" in r.stdout
