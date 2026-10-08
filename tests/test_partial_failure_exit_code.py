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


ORG_SETUP_FAILED: dict[str, Any] = {
    "projects_added": [],
    "projects_skipped": [],
    "projects_failed": [{"project_id": 7, "project_name": "broken", "error": "403"}],
}
REFRESH_FAILED_ENTRY = {"alias": "prod", "project_name": "prod", "error": "Error checking token"}
SL_FAILED_ITEM = {"name": "revenue", "reason": "HTTP 409"}
BATCH_ERROR = {"type": "table", "id": "in.c-a.t1", "error": "denied"}


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

    def test_all_projects_line_marks_a_project_with_config_errors(self, tmp_path: Path) -> None:
        # The project pushed, but a config failed: its line is not a green "OK".
        mock_sync = MagicMock()
        mock_sync.push_all.return_value = {
            "summary": {"total": 1, "success": 0, "failed": 1},
            "projects": {
                "prod": {
                    "status": "pushed",
                    "created": 2,
                    "updated": 0,
                    "deleted": 0,
                    "errors": [_push_error("c1")],
                }
            },
        }
        result = _invoke_sync(
            ["sync", "push", "--all-projects", "--directory", str(tmp_path)],
            tmp_path,
            mock_sync,
        )
        assert result.exit_code == 1
        assert "OK prod" not in result.output
        assert "x prod: +2 created, ~0 updated, -0 deleted, 1 failed" in result.output

    def test_all_projects_verbose_lists_the_config_errors(self, tmp_path: Path) -> None:
        mock_sync = MagicMock()
        mock_sync.push_all.return_value = {
            "summary": {"total": 1, "success": 0, "failed": 1},
            "projects": {
                "prod": {
                    "status": "pushed",
                    "created": 0,
                    "updated": 0,
                    "deleted": 0,
                    "errors": [_push_error("c1")],
                }
            },
        }
        result = _invoke_sync(
            ["--verbose", "sync", "push", "--all-projects", "--directory", str(tmp_path)],
            tmp_path,
            mock_sync,
        )
        assert result.exit_code == 1
        assert "0 deleted, 1 failed" in result.output
        assert "Error: added keboola.ex-db-snowflake/c1: boom" in result.output

    def test_all_projects_dry_run_with_failed_project_exits_1(self, tmp_path: Path) -> None:
        mock_sync = MagicMock()
        mock_sync.push_all.return_value = {
            "summary": {"total": 2, "success": 1, "failed": 1},
            "projects": {
                "prod": {"status": "dry_run", "summary": {"added": 1}},
                "dev": {"error": "unreachable"},
            },
        }
        result = _invoke_sync(
            ["sync", "push", "--all-projects", "--dry-run", "--directory", str(tmp_path)],
            tmp_path,
            mock_sync,
        )
        assert result.exit_code == 1

    def test_all_projects_clean_dry_run_exits_0(self, tmp_path: Path) -> None:
        mock_sync = MagicMock()
        mock_sync.push_all.return_value = {
            "summary": {"total": 1, "success": 1, "failed": 0},
            "projects": {"prod": {"status": "dry_run", "summary": {"added": 1}}},
        }
        result = _invoke_sync(
            ["sync", "push", "--all-projects", "--dry-run", "--directory", str(tmp_path)],
            tmp_path,
            mock_sync,
        )
        assert result.exit_code == 0


class TestPushAllCountsConfigErrors:
    """``push_all`` counted only a raised push as failed, never a result with errors[]."""

    def _push_all(self, tmp_path: Path, push_results: dict[str, dict[str, Any]]) -> dict:
        from keboola_agent_cli.services._sync_bulk import push_all

        for alias in push_results:
            (tmp_path / alias / ".keboola").mkdir(parents=True)
            (tmp_path / alias / ".keboola" / "manifest.json").write_text("{}", encoding="utf-8")
        service = MagicMock()
        service.resolve_projects.return_value = dict.fromkeys(push_results)
        service._resolve_max_workers.return_value = 2
        service.push.side_effect = lambda alias, *_args, **_kwargs: push_results[alias]
        return push_all(service, tmp_path)

    def test_project_with_config_errors_counts_as_failed(self, tmp_path: Path) -> None:
        data = self._push_all(
            tmp_path,
            {
                "prod": {"status": "pushed", "created": 1, "errors": []},
                "dev": {"status": "pushed", "created": 3, "errors": [_push_error("c1")]},
            },
        )
        assert data["summary"]["success"] == 1
        assert data["summary"]["failed"] == 1
        # The full push result is kept, so the per-config detail is not lost.
        assert data["projects"]["dev"]["created"] == 3
        assert data["projects"]["dev"]["errors"] == [_push_error("c1")]

    def test_no_changes_and_dry_run_results_are_successes(self, tmp_path: Path) -> None:
        data = self._push_all(
            tmp_path,
            {
                "prod": {"status": "no_changes", "errors": []},
                "dev": {"status": "dry_run", "summary": {"added": 1}},
            },
        )
        assert data["summary"]["success"] == 2
        assert data["summary"]["failed"] == 0


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

    def test_pull_all_dry_run_with_failed_project_exits_1(self, tmp_path: Path) -> None:
        mock_sync = MagicMock()
        mock_sync.pull_all.return_value = {
            "summary": {"total": 2, "success": 1, "failed": 1},
            "projects": {"prod": {"details": []}, "dev": {"error": "unreachable"}},
        }
        result = _invoke_sync(
            ["sync", "pull", "--all-projects", "--dry-run", "--directory", str(tmp_path)],
            tmp_path,
            mock_sync,
        )
        assert result.exit_code == 1
        assert "1 failed" in result.output

    def test_pull_all_clean_dry_run_exits_0(self, tmp_path: Path) -> None:
        mock_sync = MagicMock()
        mock_sync.pull_all.return_value = {
            "summary": {"total": 1, "success": 1, "failed": 0},
            "projects": {"prod": {"details": []}},
        }
        result = _invoke_sync(
            ["sync", "pull", "--all-projects", "--dry-run", "--directory", str(tmp_path)],
            tmp_path,
            mock_sync,
        )
        assert result.exit_code == 0

    def test_diff_all_with_failed_project_exits_1(self, tmp_path: Path) -> None:
        # Exit 1 means something the command really did failed, reads included.
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
        assert "1 failed" in result.output

    def test_diff_all_clean_exits_0(self, tmp_path: Path) -> None:
        mock_sync = MagicMock()
        mock_sync.diff_all.return_value = {
            "summary": {"total": 1, "success": 1, "failed": 0},
            "projects": {"prod": {"summary": {}}},
        }
        result = _invoke_sync(
            ["sync", "diff", "--all-projects", "--directory", str(tmp_path)],
            tmp_path,
            mock_sync,
        )
        assert result.exit_code == 0


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

    def _invoke_clone(self, tmp_path: Path, clone_result: dict[str, Any]) -> Any:
        mock_sync = MagicMock()
        mock_sync.clone_project.return_value = clone_result
        return _invoke_sync(
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

    def test_bucket_error_exits_1_without_success_line(self, tmp_path: Path) -> None:
        clone_result = self._clone_result(4, [])
        clone_result["bucket_errors"] = [{"bucket_id": "in.c-data", "error": "denied"}]
        result = self._invoke_clone(tmp_path, clone_result)
        assert result.exit_code == 1
        assert "Success:" not in result.output
        assert "4 created" in result.output
        assert "1 failed" in result.output

    def test_rerun_with_bucket_error_exits_1(self, tmp_path: Path) -> None:
        # A re-run has no configs to push, but it tries the buckets again.
        clone_result = {
            "status": "no_changes",
            "target_alias": "prod",
            "errors": [],
            "bucket_errors": [{"bucket_id": "in.c-data", "error": "denied"}],
        }
        result = self._invoke_clone(tmp_path, clone_result)
        assert result.exit_code == 1
        assert "Already cloned into prod, 1 failed" in result.output

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

    def _invoke(
        self,
        tmp_path: Path,
        result_payload: dict[str, Any],
        extra: list[str] | None = None,
        json_mode: bool = True,
    ) -> Any:
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
                    *(["--json"] if json_mode else []),
                    "org",
                    "setup",
                    "--org-id",
                    "42",
                    "--url",
                    "https://connection.keboola.com",
                    *(["--yes"] if extra is None else extra),
                ],
            )

    def test_dry_run_with_failed_projects_exits_1(self, tmp_path: Path) -> None:
        result = self._invoke(tmp_path, {**ORG_SETUP_FAILED, "dry_run": True}, ["--dry-run"])
        assert result.exit_code == 1
        assert len(json.loads(result.output)["data"]["projects_failed"]) == 1

    def test_interactive_preview_with_only_failures_exits_1(self, tmp_path: Path) -> None:
        # No --yes in a human run: the preview has nothing to add, so it is the whole run.
        result = self._invoke(tmp_path, {**ORG_SETUP_FAILED, "dry_run": True}, [], json_mode=False)
        assert result.exit_code == 1
        assert "No new projects to add." in result.output

    def test_failed_projects_exit_1(self, tmp_path: Path) -> None:
        result = self._invoke(tmp_path, ORG_SETUP_FAILED)
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert len(payload["data"]["projects_failed"]) == 1

    def test_clean_dry_run_exits_0(self, tmp_path: Path) -> None:
        result = self._invoke(
            tmp_path,
            {
                "dry_run": True,
                "projects_added": [{"project_id": 7, "project_name": "ok", "alias": "ok"}],
                "projects_skipped": [],
                "projects_failed": [],
            },
            ["--dry-run"],
        )
        assert result.exit_code == 0

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

    def _invoke(self, tmp_path: Path, failed: int, dry_run: bool = False) -> Any:
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
                    *(["--dry-run"] if dry_run else []),
                ],
            )

    def test_failed_rows_exit_1(self, tmp_path: Path) -> None:
        assert self._invoke(tmp_path, failed=1).exit_code == 1

    def test_clean_bulk_invite_exits_0(self, tmp_path: Path) -> None:
        assert self._invoke(tmp_path, failed=0).exit_code == 0

    def test_dry_run_with_failed_rows_exits_1(self, tmp_path: Path) -> None:
        assert self._invoke(tmp_path, failed=1, dry_run=True).exit_code == 1

    def test_clean_dry_run_exits_0(self, tmp_path: Path) -> None:
        assert self._invoke(tmp_path, failed=0, dry_run=True).exit_code == 0


class TestProjectRefresh:
    """``project refresh`` collects a project whose token check or refresh failed."""

    def _invoke(
        self, tmp_path: Path, payload: dict[str, Any], args: list[str], dry_run: bool = False
    ) -> Any:
        store = _store(tmp_path / "config")
        mock_org = MagicMock()
        mock_org.refresh_tokens.return_value = {
            "dry_run": dry_run,
            "projects_checked": 1,
            "projects_refreshed": [],
            "projects_valid": [],
            "projects_skipped": [],
            "projects_failed": [],
            **payload,
        }
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrg,
            patch(
                "keboola_agent_cli.commands.project.resolve_manage_token",
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
                    *args,
                    "project",
                    "refresh",
                    "--all",
                    *(["--dry-run"] if dry_run else []),
                ],
            )

    def test_failed_project_exits_1_with_full_json(self, tmp_path: Path) -> None:
        result = self._invoke(tmp_path, {"projects_failed": [REFRESH_FAILED_ENTRY]}, ["--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["data"]["projects_failed"] == [REFRESH_FAILED_ENTRY]

    def test_clean_refresh_exits_0(self, tmp_path: Path) -> None:
        assert self._invoke(tmp_path, {}, ["--json"]).exit_code == 0

    def test_dry_run_with_failed_project_exits_1(self, tmp_path: Path) -> None:
        result = self._invoke(
            tmp_path, {"projects_failed": [REFRESH_FAILED_ENTRY]}, ["--json"], dry_run=True
        )
        assert result.exit_code == 1

    def test_clean_dry_run_exits_0(self, tmp_path: Path) -> None:
        assert self._invoke(tmp_path, {}, ["--json"], dry_run=True).exit_code == 0

    def test_interactive_preview_with_only_failures_exits_1(self, tmp_path: Path) -> None:
        # A human run without --yes stops after the preview when nothing needs a
        # refresh; the failed projects in that preview are the run's result.
        result = self._invoke(tmp_path, {"projects_failed": [REFRESH_FAILED_ENTRY]}, [])
        assert result.exit_code == 1
        assert "No token to refresh." in result.output
        assert "All tokens are valid." not in result.output


class TestItemFailureExitCode:
    """The helper maps like ``map_error_to_exit_code``: it returns, the caller raises."""

    def test_maps_the_failed_count(self) -> None:
        from keboola_agent_cli.commands._helpers import item_failure_exit_code

        assert item_failure_exit_code(0) is None
        assert item_failure_exit_code(3) == 1

    def test_records_the_failure_for_the_usage_event(self) -> None:
        from keboola_agent_cli import telemetry
        from keboola_agent_cli.commands._helpers import item_failure_exit_code

        telemetry.reset()
        item_failure_exit_code(2)
        assert telemetry._resolve_error_text(None) == "2 item(s) failed"
        telemetry.reset()


def _invoke_with(
    tmp_path: Path, service_attr: str, mock_service: MagicMock, args: list[str]
) -> Any:
    """Run the CLI with one service class replaced by ``mock_service``."""
    store = _store(tmp_path / "config")
    store.add_project(
        "dev",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name="dev",
            project_id=5678,
        ),
    )
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch(f"keboola_agent_cli.cli.{service_attr}") as MockService,
    ):
        MockStore.return_value = store
        MockService.return_value = mock_service
        return runner.invoke(app, ["--config-dir", str(tmp_path / "config"), *args])


class TestWorkspaceGc:
    """``workspace gc`` collects failed deletes (and unlisted projects) in ``errors``."""

    def _invoke(self, tmp_path: Path, result: dict[str, Any], extra: list[str]) -> Any:
        mock = MagicMock()
        mock.resolve_projects.return_value = {}
        mock.gc_workspaces.return_value = result
        return _invoke_with(
            tmp_path, "WorkspaceService", mock, ["workspace", "gc", "--project", "prod", *extra]
        )

    def test_failed_delete_exits_1(self, tmp_path: Path) -> None:
        result = self._invoke(
            tmp_path,
            {
                "dry_run": False,
                "deleted": [],
                "errors": [{"workspace_id": 7, "project_alias": "prod", "error": "denied"}],
                "message": "GC complete: 0 orphaned workspace(s) deleted, 1 error(s).",
            },
            ["--yes"],
        )
        assert result.exit_code == 1
        assert "workspace 7: denied" in result.output

    def test_clean_gc_exits_0(self, tmp_path: Path) -> None:
        result = self._invoke(
            tmp_path,
            {"dry_run": False, "deleted": [], "errors": [], "message": "GC complete."},
            ["--yes"],
        )
        assert result.exit_code == 0

    def test_dry_run_with_list_error_exits_1(self, tmp_path: Path) -> None:
        result = self._invoke(
            tmp_path,
            {
                "dry_run": True,
                "would_delete": [],
                "errors": [{"project_alias": "prod", "error_code": "X", "message": "down"}],
                "message": "DRY RUN: 0 orphaned workspace(s) would be deleted. 1 list error(s).",
            },
            ["--dry-run"],
        )
        assert result.exit_code == 1

    def test_clean_dry_run_exits_0(self, tmp_path: Path) -> None:
        result = self._invoke(
            tmp_path,
            {
                "dry_run": True,
                "would_delete": [],
                "errors": [],
                "message": "DRY RUN: 0 orphaned workspace(s) would be deleted.",
            },
            ["--dry-run"],
        )
        assert result.exit_code == 0


class TestSemanticLayerImportAndPromote:
    """``import`` / ``promote`` collect per-item failures under each type."""

    def test_import_with_failed_item_exits_1(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot.json"
        snapshot.write_text("{}", encoding="utf-8")
        mock = MagicMock()
        mock.import_snapshot.return_value = {
            "target_project": "prod",
            "imported": {
                "datasets": {"created": 2, "failed": []},
                "metrics": {"created": 0, "failed": [SL_FAILED_ITEM]},
            },
        }
        args = ["--json", "semantic-layer", "import", "--project", "prod", "--file", str(snapshot)]
        result = _invoke_with(tmp_path, "SemanticLayerService", mock, args)
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["data"]["imported"]["metrics"]["failed"] == [SL_FAILED_ITEM]

    def test_import_dry_run_with_failed_item_exits_1(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot.json"
        snapshot.write_text("{}", encoding="utf-8")
        mock = MagicMock()
        mock.import_snapshot.return_value = {
            "dry_run": True,
            "imported": {"metrics": {"created": 0, "failed": [SL_FAILED_ITEM]}},
        }
        args = ["semantic-layer", "import", "--project", "prod", "--file", str(snapshot)]
        result = _invoke_with(tmp_path, "SemanticLayerService", mock, [*args, "--dry-run"])
        assert result.exit_code == 1

    def test_clean_import_dry_run_exits_0(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot.json"
        snapshot.write_text("{}", encoding="utf-8")
        mock = MagicMock()
        mock.import_snapshot.return_value = {
            "dry_run": True,
            "imported": {"datasets": {"created": 2, "failed": []}},
        }
        args = ["semantic-layer", "import", "--project", "prod", "--file", str(snapshot)]
        result = _invoke_with(tmp_path, "SemanticLayerService", mock, [*args, "--dry-run"])
        assert result.exit_code == 0

    def test_clean_import_exits_0(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot.json"
        snapshot.write_text("{}", encoding="utf-8")
        mock = MagicMock()
        mock.import_snapshot.return_value = {
            "imported": {"datasets": {"created": 2, "failed": []}},
        }
        args = ["semantic-layer", "import", "--project", "prod", "--file", str(snapshot)]
        assert _invoke_with(tmp_path, "SemanticLayerService", mock, args).exit_code == 0

    def test_promote_with_failed_item_exits_1(self, tmp_path: Path) -> None:
        mock = MagicMock()
        mock.promote_model.return_value = {
            "from_project": "prod",
            "to_project": "dev",
            "datasets": {"new": 1, "failed": []},
            "glossary": {"new": 0, "failed": [SL_FAILED_ITEM]},
        }
        args = [
            "semantic-layer",
            "promote",
            "--from-project",
            "prod",
            "--to-project",
            "dev",
            "--yes",
        ]
        result = _invoke_with(tmp_path, "SemanticLayerService", mock, args)
        assert result.exit_code == 1
        assert "glossary.revenue: HTTP 409" in result.output

    def test_promote_dry_run_with_failed_item_exits_1(self, tmp_path: Path) -> None:
        mock = MagicMock()
        mock.promote_model.return_value = {
            "dry_run": True,
            "metrics": {"new": 0, "failed": [SL_FAILED_ITEM]},
        }
        args = [
            "semantic-layer",
            "promote",
            "--from-project",
            "prod",
            "--to-project",
            "dev",
            "--dry-run",
        ]
        assert _invoke_with(tmp_path, "SemanticLayerService", mock, args).exit_code == 1

    def test_promote_clean_dry_run_exits_0(self, tmp_path: Path) -> None:
        mock = MagicMock()
        mock.promote_model.return_value = {
            "dry_run": True,
            "metrics": {"new": 1, "failed": []},
        }
        args = [
            "semantic-layer",
            "promote",
            "--from-project",
            "prod",
            "--to-project",
            "dev",
            "--dry-run",
        ]
        assert _invoke_with(tmp_path, "SemanticLayerService", mock, args).exit_code == 0


class TestSemanticLayerBuild:
    """``build`` left a table whose schema fetch failed out of the model, silently."""

    def _result(self, fetch_errors: list[dict[str, str]], dry_run: bool) -> dict[str, Any]:
        return {
            "project": "prod",
            "dry_run": dry_run,
            "fallback_used": "heuristic",
            "fetch_errors": fetch_errors,
            "type_resolution_errors": [],
            "generated": {"datasets": [], "metrics": []},
            "validation": {"errors": [], "warnings": []},
            "validated": True,
            "created": {"model": "m1"} if not dry_run else None,
        }

    def _invoke(self, tmp_path: Path, result: dict[str, Any], extra: list[str]) -> Any:
        mock = MagicMock()
        mock.build_model.return_value = result
        args = ["semantic-layer", "build", "--project", "prod", "--tables", "in.c-a.t1,in.c-a.t2"]
        return _invoke_with(tmp_path, "SemanticLayerService", mock, [*args, *extra])

    def test_table_left_out_exits_1_and_is_listed(self, tmp_path: Path) -> None:
        missing = [{"table_id": "in.c-a.t2", "error": "not found"}]
        result = self._invoke(tmp_path, self._result(missing, dry_run=False), [])
        assert result.exit_code == 1
        assert "Failed: 1 table(s) left out of the model" in result.output
        assert "in.c-a.t2" in result.output

    def test_dry_run_with_table_left_out_exits_1(self, tmp_path: Path) -> None:
        missing = [{"table_id": "in.c-a.t2", "error": "not found"}]
        result = self._invoke(tmp_path, self._result(missing, dry_run=True), ["--dry-run"])
        assert result.exit_code == 1

    def test_clean_dry_run_exits_0(self, tmp_path: Path) -> None:
        result = self._invoke(tmp_path, self._result([], dry_run=True), ["--dry-run"])
        assert result.exit_code == 0

    def test_clean_build_exits_0(self, tmp_path: Path) -> None:
        assert self._invoke(tmp_path, self._result([], dry_run=False), []).exit_code == 0


class TestSemanticLayerEditMetricCascade:
    """A metric rename that could not repoint every constraint exited 0."""

    def _invoke(self, tmp_path: Path, cascaded: list[dict[str, Any]]) -> Any:
        mock = MagicMock()
        mock.edit_metric.return_value = {
            "updated": {"id": "m-2", "attributes": {"name": "net_revenue"}},
            "cascaded_constraints": cascaded,
            "rollback": None,
            "partial_state": any(c["status"] == "failed" for c in cascaded),
            "recovery_hint": None,
        }
        args = [
            "semantic-layer",
            "edit",
            "metric",
            "--project",
            "prod",
            "--name",
            "revenue",
            "--new-name",
            "net_revenue",
            "--yes",
        ]
        return _invoke_with(tmp_path, "SemanticLayerService", mock, args)

    def test_failed_cascade_exits_1(self, tmp_path: Path) -> None:
        cascaded = [
            {"constraint": "c1", "status": "updated", "id": "c-9"},
            {"constraint": "c2", "status": "failed", "error": "HTTP 500"},
        ]
        result = self._invoke(tmp_path, cascaded)
        assert result.exit_code == 1
        assert "PARTIAL STATE" in result.output

    def test_clean_cascade_exits_0(self, tmp_path: Path) -> None:
        cascaded = [{"constraint": "c1", "status": "updated", "id": "c-9"}]
        assert self._invoke(tmp_path, cascaded).exit_code == 0


class TestStorageDescribeBatchAndMigrate:
    """``describe-batch`` exited 1 in human mode only; both printed a green headline."""

    def _batch(self, tmp_path: Path, errors: list[dict[str, str]], json_mode: bool) -> Any:
        batch_file = tmp_path / "descriptions.yaml"
        batch_file.write_text("tables:\n  in.c-a.t1: Orders\n", encoding="utf-8")
        mock = MagicMock()
        mock.describe_batch.return_value = {
            "project_alias": "prod",
            "applied": [],
            "applied_count": 0,
            "errors": errors,
            "error_count": len(errors),
        }
        args = ["storage", "describe-batch", "--project", "prod", "--from-file", str(batch_file)]
        return _invoke_with(
            tmp_path, "StorageService", mock, [*(["--json"] if json_mode else []), *args]
        )

    def test_batch_json_with_error_exits_1_with_full_payload(self, tmp_path: Path) -> None:
        result = self._batch(tmp_path, [BATCH_ERROR], json_mode=True)
        assert result.exit_code == 1
        assert json.loads(result.output)["data"]["errors"] == [BATCH_ERROR]

    def test_batch_human_headline_states_the_failure(self, tmp_path: Path) -> None:
        result = self._batch(tmp_path, [BATCH_ERROR], json_mode=False)
        assert result.exit_code == 1
        assert "Batch complete" not in result.output
        assert "Failed: 0 applied, 1 error(s)" in result.output

    def test_clean_batch_json_exits_0(self, tmp_path: Path) -> None:
        assert self._batch(tmp_path, [], json_mode=True).exit_code == 0

    def test_migrate_headline_states_the_failure(self, tmp_path: Path) -> None:
        mock = MagicMock()
        mock.describe_migrate.return_value = {
            "dry_run": False,
            "migrated": [{"table_id": "in.c-a.t1", "columns": {"id": "Key"}}],
            "tables_scanned": 2,
            "skipped": [],
            "pruned_orphans": [],
            "errors": [{"table_id": "in.c-a.t2", "error": "denied"}],
        }
        args = ["storage", "describe-migrate", "--project", "prod", "--yes"]
        result = _invoke_with(tmp_path, "StorageService", mock, args)
        assert result.exit_code == 1
        assert "Failed: Migrated 1 table(s) of 2 scanned, 1 failed" in result.output


class TestFlowScheduleRemove:
    """A schedule whose delete failed was dropped when another one was deleted."""

    def test_partial_failure_exits_1_and_lists_the_schedule(self, tmp_path: Path) -> None:
        mock = MagicMock()
        mock.remove_flow_schedule.return_value = {
            "status": "removed",
            "deleted_schedule_ids": ["s1"],
            "deleted_count": 1,
            "errors": [{"schedule_id": "s2", "error": "HTTP 500"}],
            "warnings": [],
        }
        args = ["flow", "schedule-remove", "--project", "prod", "--flow-id", "42", "--yes"]
        result = _invoke_with(tmp_path, "FlowService", mock, args)
        assert result.exit_code == 1
        assert "Success:" not in result.output
        assert "Failed: Removed 1 schedule(s) from flow 42, 1 failed" in result.output
        assert "schedule s2: HTTP 500" in result.output

    def test_clean_remove_exits_0(self, tmp_path: Path) -> None:
        mock = MagicMock()
        mock.remove_flow_schedule.return_value = {
            "status": "removed",
            "deleted_schedule_ids": ["s1"],
            "deleted_count": 1,
            "errors": [],
            "warnings": [],
        }
        args = ["flow", "schedule-remove", "--project", "prod", "--flow-id", "42", "--yes"]
        assert _invoke_with(tmp_path, "FlowService", mock, args).exit_code == 0
