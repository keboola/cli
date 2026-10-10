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
from keboola_agent_cli.errors import KeboolaApiError
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

    def _invoke(self, tmp_path: Path, argv: list[str], *, stdin: str | None = None):
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
            result = runner.invoke(app, argv, input=stdin)
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

    def test_token_stdin_still_warns_about_the_conversion(self, tmp_path: Path) -> None:
        """The new sources change where the token comes from, nothing else."""
        store, result = self._invoke(
            tmp_path,
            ["project", "edit", "--project", self.SESSION_ALIAS, "--token-stdin"],
            stdin=f"{self.NEW_TOKEN}\n",
        )

        assert result.exit_code == 0, result.output
        assert "auth logout --remove-projects" in " ".join(result.stderr.split())
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


class TestProjectEditTokenSources:
    """A new token can reach ``project edit`` without the command line.

    ``--token`` puts the value in the shell history, in the kbagent REPL
    history file, and in a process listing. ``--token-stdin``,
    ``--token-file`` and ``--token-env`` all keep it out of those places.
    ``--token-file`` also deletes the file it read, but only once the edit
    has actually succeeded.
    """

    ALIAS = "tokentest"
    NEW_TOKEN = "901-22222-replacementTokenValue123"

    def _seeded_store(self, config_dir: Path) -> ConfigStore:
        store = ConfigStore(config_dir=config_dir)
        store.add_project(
            self.ALIAS,
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token=TEST_TOKEN,
                project_name="Test Project",
                project_id=1234,
            ),
        )
        return store

    def _invoke(self, tmp_path: Path, argv: list[str], *, client=None, stdin: str | None = None):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = self._seeded_store(config_dir)
        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: client if client is not None else make_mock_client(),
        )
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
        ):
            MockStore.return_value = store
            MockService.return_value = service
            result = runner.invoke(app, argv, input=stdin)
        return store, result

    def _token_file(self, tmp_path: Path) -> Path:
        path = tmp_path / "token.txt"
        path.write_text(f"{self.NEW_TOKEN}\n", encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_token_file_is_read_and_then_deleted(self, tmp_path: Path) -> None:
        path = self._token_file(tmp_path)
        store, result = self._invoke(
            tmp_path,
            ["project", "edit", "--project", self.ALIAS, "--token-file", str(path)],
        )

        assert result.exit_code == 0, result.output
        project = store.get_project(self.ALIAS)
        assert project is not None
        assert project.token == self.NEW_TOKEN
        assert not path.exists()
        assert self.NEW_TOKEN not in result.output

    def test_dry_run_keeps_the_token_file(self, tmp_path: Path) -> None:
        """A preview must not destroy the caller's input."""
        path = self._token_file(tmp_path)
        store, result = self._invoke(
            tmp_path,
            [
                "project",
                "edit",
                "--project",
                self.ALIAS,
                "--token-file",
                str(path),
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output
        assert path.exists()
        assert "--dry-run" in result.stderr
        project = store.get_project(self.ALIAS)
        assert project is not None
        assert project.token == TEST_TOKEN, "dry run must not change the stored token"

    def test_keep_token_file_keeps_the_file_and_warns(self, tmp_path: Path) -> None:
        path = self._token_file(tmp_path)
        _store, result = self._invoke(
            tmp_path,
            [
                "project",
                "edit",
                "--project",
                self.ALIAS,
                "--token-file",
                str(path),
                "--keep-token-file",
            ],
        )

        assert result.exit_code == 0, result.output
        assert path.exists()
        assert "still holds the token" in result.stderr

    def test_a_failed_edit_keeps_the_token_file(self, tmp_path: Path) -> None:
        """The token was never stored, so the file is still the only copy."""
        path = self._token_file(tmp_path)
        failing = make_mock_client()
        failing.verify_token.side_effect = KeboolaApiError(
            "Invalid access token", status_code=401, error_code="INVALID_TOKEN"
        )

        _store, result = self._invoke(
            tmp_path,
            ["project", "edit", "--project", self.ALIAS, "--token-file", str(path)],
            client=failing,
        )

        assert result.exit_code != 0
        assert path.exists()

    def test_token_env_names_the_variable(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"CI_KBC_TOKEN": self.NEW_TOKEN}):
            store, result = self._invoke(
                tmp_path,
                [
                    "project",
                    "edit",
                    "--project",
                    self.ALIAS,
                    "--token-env",
                    "CI_KBC_TOKEN",
                ],
            )

        assert result.exit_code == 0, result.output
        project = store.get_project(self.ALIAS)
        assert project is not None
        assert project.token == self.NEW_TOKEN

    def test_kbc_token_in_the_environment_is_ignored(self, tmp_path: Path) -> None:
        """Unlike `project add`, an edit never picks the token up implicitly.

        A new token rewrites the alias's project_id and project_name from
        whatever it verifies as, so an exported variable could silently
        repoint the alias at a different project during an unrelated edit.
        """
        with patch.dict(os.environ, {"KBC_TOKEN": self.NEW_TOKEN}):
            store, result = self._invoke(
                tmp_path,
                ["project", "edit", "--project", self.ALIAS, "--new-alias", "renamed"],
            )

        assert result.exit_code == 0, result.output
        project = store.get_project("renamed")
        assert project is not None
        assert project.token == TEST_TOKEN, "the rename must not touch the credential"

    def test_token_stdin_is_read_from_a_pipe(self, tmp_path: Path) -> None:
        store, result = self._invoke(
            tmp_path,
            ["project", "edit", "--project", self.ALIAS, "--token-stdin"],
            stdin=f"{self.NEW_TOKEN}\n",
        )

        assert result.exit_code == 0, result.output
        project = store.get_project(self.ALIAS)
        assert project is not None
        assert project.token == self.NEW_TOKEN
        assert self.NEW_TOKEN not in result.output

    def test_two_token_sources_are_rejected(self, tmp_path: Path) -> None:
        path = self._token_file(tmp_path)
        _store, result = self._invoke(
            tmp_path,
            [
                "project",
                "edit",
                "--project",
                self.ALIAS,
                "--token",
                self.NEW_TOKEN,
                "--token-file",
                str(path),
            ],
        )

        assert result.exit_code == 2
        assert path.exists(), "a rejected command must not delete the file"
