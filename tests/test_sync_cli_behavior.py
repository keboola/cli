"""Behavioral CLI tests for the sync command surface.

These tests pin the *command behaviors* that make sync safe and that distinguish
the kbagent orchestrator model from kbc's cwd-per-folder model:

  1. ``sync pull/diff/push`` REQUIRE ``--project ALIAS`` or ``--all-projects``
     (kbagent resolves projects from a central config store, it does not act on
     the current directory implicitly). Missing/contradictory selection is a
     usage error (exit 2) and must NOT reach the service.
  2. ``--project`` and ``--all-projects`` are mutually exclusive.
  3. ``--branch`` is per-project, so it cannot combine with ``--all-projects``.
  4. ``sync push --dry-run`` must call the service in dry-run mode and never
     perform a real write.

Background: a side-by-side comparison against kbc showed that pulls round-trip to
zero drift and that push is last-write-wins; these guards are the first line of
defense against an accidental wrong-target or whole-tree operation.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
runner = CliRunner()


def _store(config_dir: Path) -> ConfigStore:
    config_dir.mkdir(parents=True, exist_ok=True)
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


def _invoke(args: list[str], tmp_path: Path) -> tuple[int, MagicMock]:
    """Invoke the CLI with a mocked SyncService; return (exit_code, mock)."""
    store = _store(tmp_path / "config")
    mock_sync = MagicMock()
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ProjectService") as MockProj,
        patch("keboola_agent_cli.cli.SyncService") as MockSync,
    ):
        MockStore.return_value = store
        MockProj.return_value = ProjectService(config_store=store)
        MockSync.return_value = mock_sync
        result = runner.invoke(app, args)
    return result.exit_code, mock_sync


class TestProjectSelectionRequired:
    """pull/diff/push must be told WHICH project(s) — no implicit cwd target."""

    def test_pull_without_project_is_usage_error(self, tmp_path: Path) -> None:
        code, mock = _invoke(["sync", "pull", "--directory", str(tmp_path)], tmp_path)
        assert code == 2
        mock.pull.assert_not_called()
        mock.pull_all.assert_not_called()

    def test_diff_without_project_is_usage_error(self, tmp_path: Path) -> None:
        code, mock = _invoke(["sync", "diff", "--directory", str(tmp_path)], tmp_path)
        assert code == 2
        mock.diff.assert_not_called()

    def test_push_without_project_is_usage_error(self, tmp_path: Path) -> None:
        code, mock = _invoke(["sync", "push", "--directory", str(tmp_path)], tmp_path)
        assert code == 2
        mock.push.assert_not_called()


class TestMutuallyExclusiveSelection:
    """--project and --all-projects cannot be combined; --branch is per-project."""

    def test_pull_project_and_all_projects_conflict(self, tmp_path: Path) -> None:
        code, mock = _invoke(
            ["sync", "pull", "--project", "prod", "--all-projects", "--directory", str(tmp_path)],
            tmp_path,
        )
        assert code == 2
        mock.pull.assert_not_called()
        mock.pull_all.assert_not_called()

    def test_pull_branch_with_all_projects_conflict(self, tmp_path: Path) -> None:
        code, mock = _invoke(
            ["sync", "pull", "--all-projects", "--branch", "555", "--directory", str(tmp_path)],
            tmp_path,
        )
        assert code == 2
        mock.pull_all.assert_not_called()


class TestPushDryRunIsSafe:
    """push --dry-run must run in dry-run mode and never write."""

    def test_push_dry_run_passes_flag_and_does_not_error(self, tmp_path: Path) -> None:
        store = _store(tmp_path / "config")
        mock_sync = MagicMock()
        # JSON mode avoids the human formatter's field expectations; the point of
        # this test is that --dry-run reaches the service, not the print layout.
        mock_sync.push.return_value = {
            "status": "dry_run",
            "project_alias": "prod",
            "summary": {"to_create": 0, "to_update": 1, "to_delete": 0},
            "changes": [],
        }
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProj,
            patch("keboola_agent_cli.cli.SyncService") as MockSync,
        ):
            MockStore.return_value = store
            MockProj.return_value = ProjectService(config_store=store)
            MockSync.return_value = mock_sync
            result = runner.invoke(
                app,
                [
                    "--json",
                    "sync",
                    "push",
                    "--project",
                    "prod",
                    "--dry-run",
                    "--directory",
                    str(tmp_path),
                ],
            )
        assert result.exit_code == 0, result.output
        assert mock_sync.push.call_count == 1
        # The dry_run flag must be propagated to the service (no real write).
        _, kwargs = mock_sync.push.call_args
        assert kwargs.get("dry_run") is True
