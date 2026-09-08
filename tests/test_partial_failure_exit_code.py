"""Per-item failures must show in the headline AND in the exit code (issue #745).

Several commands keep going when one item fails: ``sync push`` collects a
failed config into ``result["errors"]``, ``org setup`` into
``projects_failed``, ``project invite --from-csv`` into ``failed``. That
resilience is deliberate -- one bad item must not abort the batch -- but the
command layer then printed a fixed green "Success" line and exited 0 anyway.
A run where EVERY item failed was indistinguishable, by exit code, from a
clean one, so no script could branch on it.

These tests pin both halves of the fix:

  * exit code 1 whenever at least one item failed (the convention the bulk
    storage commands already used), 0 when none did;
  * the human headline states the failure instead of claiming success;
  * JSON callers still receive the complete payload -- the non-zero exit is
    raised AFTER the result is emitted, never instead of it.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import ProjectConfig

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


def _push_error(config_id: str) -> dict[str, str]:
    return {
        "change_type": "added",
        "component_id": "keboola.ex-db-snowflake",
        "config_id": config_id,
        "message": "boom",
    }


def _invoke_sync(args: list[str], tmp_path: Path, mock_sync: MagicMock) -> Any:
    store = _store(tmp_path / "config")
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.SyncService") as MockSync,
    ):
        MockStore.return_value = store
        MockSync.return_value = mock_sync
        return runner.invoke(app, ["--config-dir", str(tmp_path / "config"), *args])


class TestSyncPush:
    """``sync push`` collects per-config failures into ``result['errors']``."""

    def test_all_configs_failed_exits_1_without_success_line(self, tmp_path: Path) -> None:
        mock_sync = MagicMock()
        mock_sync.push.return_value = {
            "status": "pushed",
            "created": 0,
            "updated": 0,
            "deleted": 0,
            "errors": [_push_error("c1"), _push_error("c2")],
            "pushed_details": [],
        }
        result = _invoke_sync(
            ["sync", "push", "--project", "prod", "--directory", str(tmp_path)],
            tmp_path,
            mock_sync,
        )
        assert result.exit_code == 1
        assert "Success:" not in result.output
        assert "2 failed" in result.output

    def test_partial_failure_exits_1_and_states_the_failed_count(self, tmp_path: Path) -> None:
        mock_sync = MagicMock()
        mock_sync.push.return_value = {
            "status": "pushed",
            "created": 3,
            "updated": 0,
            "deleted": 0,
            "errors": [_push_error("c1")],
            "pushed_details": [],
        }
        result = _invoke_sync(
            ["sync", "push", "--project", "prod", "--directory", str(tmp_path)],
            tmp_path,
            mock_sync,
        )
        assert result.exit_code == 1
        assert "3 created" in result.output
        assert "1 failed" in result.output

    def test_clean_push_still_succeeds(self, tmp_path: Path) -> None:
        mock_sync = MagicMock()
        mock_sync.push.return_value = {
            "status": "pushed",
            "created": 2,
            "updated": 1,
            "deleted": 0,
            "errors": [],
            "pushed_details": [],
        }
        result = _invoke_sync(
            ["sync", "push", "--project", "prod", "--directory", str(tmp_path)],
            tmp_path,
            mock_sync,
        )
        assert result.exit_code == 0
        assert "Success:" in result.output

    def test_no_changes_is_not_a_failure(self, tmp_path: Path) -> None:
        mock_sync = MagicMock()
        mock_sync.push.return_value = {
            "status": "no_changes",
            "created": 0,
            "updated": 0,
            "deleted": 0,
            "errors": [],
        }
        result = _invoke_sync(
            ["sync", "push", "--project", "prod", "--directory", str(tmp_path)],
            tmp_path,
            mock_sync,
        )
        assert result.exit_code == 0

    def test_json_mode_emits_full_payload_and_exits_1(self, tmp_path: Path) -> None:
        mock_sync = MagicMock()
        mock_sync.push.return_value = {
            "status": "pushed",
            "created": 0,
            "updated": 0,
            "deleted": 0,
            "errors": [_push_error("c1")],
            "pushed_details": [],
        }
        result = _invoke_sync(
            ["--json", "sync", "push", "--project", "prod", "--directory", str(tmp_path)],
            tmp_path,
            mock_sync,
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert len(payload["data"]["errors"]) == 1

    def test_all_projects_fan_out_reports_failed_projects(self, tmp_path: Path) -> None:
        mock_sync = MagicMock()
        mock_sync.push_all.return_value = {
            "summary": {"total": 2, "success": 1, "failed": 1},
            "projects": {
                "prod": {"status": "pushed", "created": 1, "updated": 0, "deleted": 0},
                "dev": {"error": "unreachable"},
            },
        }
        result = _invoke_sync(
            ["sync", "push", "--all-projects", "--directory", str(tmp_path)],
            tmp_path,
            mock_sync,
        )
        assert result.exit_code == 1

    def test_all_projects_fan_out_clean_run_exits_0(self, tmp_path: Path) -> None:
        mock_sync = MagicMock()
        mock_sync.push_all.return_value = {
            "summary": {"total": 1, "success": 1, "failed": 0},
            "projects": {"prod": {"status": "pushed", "created": 1, "updated": 0, "deleted": 0}},
        }
        result = _invoke_sync(
            ["sync", "push", "--all-projects", "--directory", str(tmp_path)],
            tmp_path,
            mock_sync,
        )
        assert result.exit_code == 0


class TestSyncPullAndDiffFanOut:
    """``--all-projects`` reported ``N failed`` in the summary and exited 0."""

    def test_pull_all_with_failed_project_exits_1(self, tmp_path: Path) -> None:
        mock_sync = MagicMock()
        mock_sync.pull_all.return_value = {
            "summary": {"total": 2, "success": 1, "failed": 1},
            "projects": {"prod": {"details": []}, "dev": {"error": "unreachable"}},
        }
        result = _invoke_sync(
            ["sync", "pull", "--all-projects", "--directory", str(tmp_path)],
            tmp_path,
            mock_sync,
        )
        assert result.exit_code == 1

    def test_diff_all_with_failed_project_exits_1(self, tmp_path: Path) -> None:
        mock_sync = MagicMock()
        mock_sync.diff_all.return_value = {
            "summary": {"total": 2, "success": 1, "failed": 1},
            "projects": {"prod": {"summary": {}}, "dev": {"error": "unreachable"}},
        }
        result = _invoke_sync(
            ["sync", "diff", "--all-projects", "--directory", str(tmp_path)],
            tmp_path,
            mock_sync,
        )
        assert result.exit_code == 1


class TestSyncClone:
    """``sync clone`` printed ``Success ... 0 created`` for a total failure."""

    def _clone_result(self, created: int, errors: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "status": "cloned",
            "target_alias": "prod",
            "created": created,
            "bucket_rewrites": 0,
            "variable_overrides": 0,
            "renamed_instances": 0,
            "flow_task_remaps": 0,
            "errors": errors,
        }

    def test_every_config_failed_exits_1_without_success_line(self, tmp_path: Path) -> None:
        mock_sync = MagicMock()
        mock_sync.clone_project.return_value = self._clone_result(
            0, [_push_error("c1"), _push_error("c2"), _push_error("c3")]
        )
        result = _invoke_sync(
            [
                "sync",
                "clone",
                "--source",
                str(tmp_path),
                "--target",
                "prod",
                "--target-dir",
                str(tmp_path / "out"),
            ],
            tmp_path,
            mock_sync,
        )
        assert result.exit_code == 1
        assert "Success:" not in result.output
        assert "3 failed" in result.output

    def test_clean_clone_succeeds(self, tmp_path: Path) -> None:
        mock_sync = MagicMock()
        mock_sync.clone_project.return_value = self._clone_result(4, [])
        result = _invoke_sync(
            [
                "sync",
                "clone",
                "--source",
                str(tmp_path),
                "--target",
                "prod",
                "--target-dir",
                str(tmp_path / "out"),
            ],
            tmp_path,
            mock_sync,
        )
        assert result.exit_code == 0
        assert "Success:" in result.output

    def test_dry_run_without_errors_exits_0(self, tmp_path: Path) -> None:
        mock_sync = MagicMock()
        mock_sync.clone_project.return_value = {
            "status": "dry_run",
            "target_alias": "prod",
            "summary": {"added": 5},
            "bucket_rewrites": 0,
            "variable_overrides": 0,
            "renamed_instances": 0,
            "errors": [],
        }
        result = _invoke_sync(
            [
                "sync",
                "clone",
                "--source",
                str(tmp_path),
                "--target",
                "prod",
                "--target-dir",
                str(tmp_path / "out"),
                "--dry-run",
            ],
            tmp_path,
            mock_sync,
        )
        assert result.exit_code == 0


class TestOrgSetup:
    """``org setup`` accumulates unreachable projects into ``projects_failed``."""

    def _invoke(self, tmp_path: Path, result_payload: dict[str, Any]) -> Any:
        store = _store(tmp_path / "config")
        mock_org = MagicMock()
        mock_org.setup_organization.return_value = result_payload
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrg,
            patch(
                "keboola_agent_cli.commands.org.resolve_manage_token",
                return_value="manage-token",
            ),
        ):
            MockStore.return_value = store
            MockOrg.return_value = mock_org
            return runner.invoke(
                app,
                [
                    "--config-dir",
                    str(tmp_path / "config"),
                    "--json",
                    "org",
                    "setup",
                    "--org-id",
                    "42",
                    "--url",
                    "https://connection.keboola.com",
                    "--yes",
                ],
            )

    def test_failed_projects_exit_1(self, tmp_path: Path) -> None:
        result = self._invoke(
            tmp_path,
            {
                "projects_added": [],
                "projects_skipped": [],
                "projects_failed": [
                    {"project_id": 7, "project_name": "broken", "error": "403"},
                ],
            },
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert len(payload["data"]["projects_failed"]) == 1

    def test_clean_setup_exits_0(self, tmp_path: Path) -> None:
        result = self._invoke(
            tmp_path,
            {
                "projects_added": [{"project_id": 7, "project_name": "ok", "alias": "ok"}],
                "projects_skipped": [],
                "projects_failed": [],
            },
        )
        assert result.exit_code == 0


class TestProjectInviteBulk:
    """``project invite --from-csv`` reported ``failed=N`` and exited 0."""

    def _invoke(self, tmp_path: Path, failed: int) -> Any:
        store = _store(tmp_path / "config")
        csv_path = tmp_path / "invites.csv"
        csv_path.write_text("project,email,role\nprod,a@example.com,guest\n", encoding="utf-8")

        bulk_result = MagicMock()
        bulk_result.model_dump.return_value = {
            "total": 1,
            "succeeded": 1 - failed,
            "noop": 0,
            "failed": failed,
            "rows": [],
        }
        mock_member = MagicMock()
        mock_member.invite_bulk.return_value = bulk_result

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.MemberService") as MockMember,
            patch(
                "keboola_agent_cli.commands.project.resolve_manage_token",
                return_value="manage-token",
            ),
        ):
            MockStore.return_value = store
            MockMember.return_value = mock_member
            return runner.invoke(
                app,
                [
                    "--config-dir",
                    str(tmp_path / "config"),
                    "project",
                    "invite",
                    "--from-csv",
                    str(csv_path),
                ],
            )

    def test_failed_rows_exit_1(self, tmp_path: Path) -> None:
        assert self._invoke(tmp_path, failed=1).exit_code == 1

    def test_clean_bulk_invite_exits_0(self, tmp_path: Path) -> None:
        assert self._invoke(tmp_path, failed=0).exit_code == 0
