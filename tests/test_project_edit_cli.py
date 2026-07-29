"""CLI tests for ``kbagent project edit --new-alias`` (v0.30.3).

Mirrors the patching pattern of ``tests/test_cli.py::TestProjectEdit`` --
patches ``cli.ConfigStore`` and ``cli.ProjectService`` so the Typer
callback wires the same temp-dir-backed ``ConfigStore`` into both the
``project add`` setup step and the ``project edit`` invocation under
test.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from helpers import make_mock_client
from keboola_agent_cli.auth.sentinel import make_session_token
from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()


def _setup_runner(config_dir: Path):
    """Yield a wired (store, service) pair under the cli.* patch context."""
    mock_client = make_mock_client()
    store = ConfigStore(config_dir=config_dir)
    service = ProjectService(
        config_store=store,
        client_factory=lambda url, token: mock_client,
    )
    return store, service


class TestProjectEditNewAlias:
    """``--new-alias`` happy paths via CliRunner."""

    def test_rename_json_output_includes_old_alias_and_new_alias(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store, service = _setup_runner(config_dir)
            MockStore.return_value = store
            MockService.return_value = service

            runner.invoke(
                app,
                ["project", "add", "--project", "old", "--url", "https://x.example.com"],
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "edit",
                    "--project",
                    "old",
                    "--new-alias",
                    "new",
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["data"]["alias"] == "new"
        assert payload["data"]["old_alias"] == "old"
        assert payload["data"]["rename"]["new_alias"] == "new"

    def test_rename_human_output_uses_renamed_phrasing(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store, service = _setup_runner(config_dir)
            MockStore.return_value = store
            MockService.return_value = service

            runner.invoke(
                app, ["project", "add", "--project", "old", "--url", "https://x.example.com"]
            )

            result = runner.invoke(
                app,
                ["project", "edit", "--project", "old", "--new-alias", "new"],
            )

        assert result.exit_code == 0, result.output
        # Human formatter prints `Project old renamed to new.`
        assert "renamed to" in result.output
        # Strip Rich style markers before substring checks (no-color CliRunner default).
        normalized = result.output.replace("\n", " ")
        assert "old" in normalized
        assert "new" in normalized


class TestProjectEditNewAliasErrorPaths:
    """Validation errors and exit codes for ``--new-alias``."""

    def test_collision_exits_5_and_keeps_both_projects(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store, service = _setup_runner(config_dir)
            MockStore.return_value = store
            MockService.return_value = service

            runner.invoke(
                app, ["project", "add", "--project", "a", "--url", "https://a.example.com"]
            )
            runner.invoke(
                app, ["project", "add", "--project", "b", "--url", "https://b.example.com"]
            )

            result = runner.invoke(
                app,
                ["--json", "project", "edit", "--project", "a", "--new-alias", "b"],
            )

        assert result.exit_code == 5
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert "already in use" in payload["error"]["message"]

        # Both projects intact after the failed rename.
        config = store.load()
        assert set(config.projects.keys()) == {"a", "b"}

    def test_dry_run_human_output_has_dry_run_label(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store, service = _setup_runner(config_dir)
            MockStore.return_value = store
            MockService.return_value = service

            runner.invoke(
                app, ["project", "add", "--project", "old", "--url", "https://x.example.com"]
            )

            result = runner.invoke(
                app,
                ["project", "edit", "--project", "old", "--new-alias", "new", "--dry-run"],
            )

        assert result.exit_code == 0, result.output
        assert "DRY RUN" in result.output
        # No mutation: store still has the original alias.
        assert "old" in store.load().projects
        assert "new" not in store.load().projects

    def test_dry_run_json_output_has_planned_block(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store, service = _setup_runner(config_dir)
            MockStore.return_value = store
            MockService.return_value = service

            runner.invoke(
                app, ["project", "add", "--project", "old", "--url", "https://x.example.com"]
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "edit",
                    "--project",
                    "old",
                    "--new-alias",
                    "new",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["data"]["dry_run"] is True
        assert payload["data"]["alias"] == "old"  # unchanged
        assert payload["data"]["planned"]["new_alias"] == "new"
        assert payload["data"]["planned"]["rename"]["new_alias"] == "new"

    def test_no_changes_specified_exits_5(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store, service = _setup_runner(config_dir)
            MockStore.return_value = store
            MockService.return_value = service

            runner.invoke(
                app, ["project", "add", "--project", "test", "--url", "https://x.example.com"]
            )

            result = runner.invoke(app, ["--json", "project", "edit", "--project", "test"])

        assert result.exit_code == 5
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        # Updated message mentions all three flags.
        assert "--new-alias" in payload["error"]["message"]
        assert "--url" in payload["error"]["message"]
        assert "--token" in payload["error"]["message"]


class TestProjectEditTokenOnSessionProject:
    """``--token`` on a browser-login project converts it and says so.

    Human mode puts the warning on stderr so stdout stays parseable; ``--json``
    carries the same text in an additive ``warnings`` key.
    """

    SESSION_ALIAS = "session-proj"
    SESSION_PROJECT_ID = 9840
    NEW_TOKEN = "901-22222-replacementTokenValue123"

    def _seed_session_project(self, config_dir: Path) -> ConfigStore:
        store = ConfigStore(config_dir=config_dir)
        store.add_project(
            self.SESSION_ALIAS,
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token=make_session_token(self.SESSION_PROJECT_ID),
                project_name="Session Project",
                project_id=self.SESSION_PROJECT_ID,
            ),
        )
        return store

    def _invoke(self, tmp_path: Path, argv: list[str]):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = self._seed_session_project(config_dir)
        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: make_mock_client(
                project_name="Session Project", project_id=self.SESSION_PROJECT_ID
            ),
        )
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
        ):
            MockStore.return_value = store
            MockService.return_value = service
            result = runner.invoke(app, argv)
        return store, result

    def test_human_mode_warns_on_stderr_and_converts(self, tmp_path: Path) -> None:
        store, result = self._invoke(
            tmp_path,
            ["project", "edit", "--project", self.SESSION_ALIAS, "--token", self.NEW_TOKEN],
        )

        assert result.exit_code == 0, result.output
        stderr = " ".join(result.stderr.split())
        assert "Warning:" in stderr
        assert "auth logout --remove-projects" in stderr
        # stdout stays the success line only.
        assert "Warning" not in result.stdout

        project = store.get_project(self.SESSION_ALIAS)
        assert project is not None
        assert project.token == self.NEW_TOKEN

    def test_json_mode_carries_the_warning_in_the_payload(self, tmp_path: Path) -> None:
        _store, result = self._invoke(
            tmp_path,
            [
                "--json",
                "project",
                "edit",
                "--project",
                self.SESSION_ALIAS,
                "--token",
                self.NEW_TOKEN,
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "ok"
        assert len(payload["data"]["warnings"]) == 1
        assert "auth logout --remove-projects" in payload["data"]["warnings"][0]
