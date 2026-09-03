"""Tests for the `kbagent merge-request` command group via CliRunner (DMD-1900).

The CLI-layer third of the group's coverage (service: test_merge_request_service.py,
client: test_merge_request_client.py). While the E2E path is unresolved -- no
project carries the feature -- this file is the only automated coverage the
commands have, so it pins every Layer 1 decision in docs/merge-requests-layer1.md
that the service cannot: target resolution, the one error handler (no
flattening of FEATURE_NOT_ENABLED), the destructive-under-json rule, the
auto-merge escalations, the warnings channel, and the renderers' branch points.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, ErrorCode, FeatureNotEnabledError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig

runner = CliRunner()
ALIAS = "prod"


def _store(config_dir: Path, *, active_branch: int | None = None) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        ALIAS,
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="999-token",
            project_name="Prod",
            project_id=10,
            active_branch_id=active_branch,
        ),
    )
    return store


def _run(args: list[str], store: ConfigStore, service: MagicMock, input: str | None = None) -> Any:
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.MergeRequestService") as MockService,
    ):
        MockStore.return_value = store
        MockService.return_value = service
        return runner.invoke(app, args, input=input)


def _json(result: Any) -> dict[str, Any]:
    payload = json.loads(result.output)
    return payload


def _row(
    mr_id: int = 7, state: str = "development", branch_from: int | None = 123, **extra: Any
) -> dict[str, Any]:
    """An enriched service row (what list/find/get_merge_request_row return)."""
    derived = {"development": "in_development", "published": "merged", "canceled": "closed"}.get(
        state, state
    )
    return {
        "alias": ALIAS,
        "id": mr_id,
        "state": state,
        "title": "Add sales pipeline",
        "description": "",
        "creator": {"id": 42, "name": "Martin"},
        "reviewers": [],
        "approvals": [],
        "branches": {"branchFromId": branch_from, "branchIntoId": 1},
        "merge": {"mergedAt": None, "mergerId": None, "mergerName": ""},
        "createdAt": "2026-09-01T10:00:00+0200",
        "externalId": "",
        "autoMergeStrategy": "none",
        "autoMergeAt": None,
        "derived_state": derived,
        "allowed_actions": ["request_review", "merge", "update", "resolve_conflicts"],
        **extra,
    }


@pytest.fixture
def service() -> MagicMock:
    svc = MagicMock()
    svc.find_merge_request_for_branch.return_value = _row()
    svc.get_merge_request_row.return_value = _row()
    return svc


# ---------------------------------------------------------------------------
# Target resolution -- the chain every command shares
# ---------------------------------------------------------------------------


class TestTargetResolution:
    def test_explicit_id_is_used_as_is(self, tmp_path, service) -> None:
        service.list_conflicts.return_value = {
            "alias": ALIAS,
            "merge_request_id": 7,
            "count": 0,
            "conflicts": [],
        }
        result = _run(
            ["--json", "merge-request", "conflicts", "--project", ALIAS, "--merge-request-id", "7"],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 0, result.output
        service.list_conflicts.assert_called_once_with(ALIAS, 7)
        service.find_merge_request_for_branch.assert_not_called()
        data = _json(result)["data"]
        assert data["merge_request_id"] == 7
        assert data["resolved_from_branch"] is False

    def test_short_alias_id(self, tmp_path, service) -> None:
        service.list_conflicts.return_value = {
            "alias": ALIAS,
            "merge_request_id": 9,
            "count": 0,
            "conflicts": [],
        }
        result = _run(
            ["--json", "mr", "conflicts", "--project", ALIAS, "--id", "9"],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 0, result.output
        service.list_conflicts.assert_called_once_with(ALIAS, 9)

    def test_omitted_id_resolves_active_branch_then_its_mr(self, tmp_path, service) -> None:
        service.list_conflicts.return_value = {
            "alias": ALIAS,
            "merge_request_id": 7,
            "count": 0,
            "conflicts": [],
        }
        result = _run(
            ["--json", "merge-request", "conflicts", "--project", ALIAS],
            _store(tmp_path, active_branch=123),
            service,
        )
        assert result.exit_code == 0, result.output
        service.find_merge_request_for_branch.assert_called_once_with(ALIAS, 123)
        service.list_conflicts.assert_called_once_with(ALIAS, 7)
        data = _json(result)["data"]
        assert data["resolved_from_branch"] is True
        assert data["branch_from_id"] == 123

    def test_explicit_branch_wins_over_active(self, tmp_path, service) -> None:
        service.list_conflicts.return_value = {
            "alias": ALIAS,
            "merge_request_id": 7,
            "count": 0,
            "conflicts": [],
        }
        result = _run(
            ["--json", "merge-request", "conflicts", "--project", ALIAS, "--branch", "555"],
            _store(tmp_path, active_branch=123),
            service,
        )
        assert result.exit_code == 0, result.output
        service.find_merge_request_for_branch.assert_called_once_with(ALIAS, 555)

    def test_both_flags_is_exit_2_not_silent_precedence(self, tmp_path, service) -> None:
        result = _run(
            [
                "--json",
                "merge-request",
                "conflicts",
                "--project",
                ALIAS,
                "--merge-request-id",
                "7",
                "--branch",
                "123",
            ],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 2
        assert _json(result)["error"]["code"] == ErrorCode.INVALID_ARGUMENT
        service.find_merge_request_for_branch.assert_not_called()
        service.list_conflicts.assert_not_called()

    def test_no_id_no_branch_no_active_is_exit_5_with_branch_use_hint(
        self, tmp_path, service
    ) -> None:
        result = _run(
            ["--json", "merge-request", "conflicts", "--project", ALIAS], _store(tmp_path), service
        )
        assert result.exit_code == 5
        err = _json(result)["error"]
        assert err["code"] == ErrorCode.CONFIG_ERROR
        assert "branch use" in err["message"]

    def test_human_mode_reports_the_resolution(self, tmp_path, service) -> None:
        service.list_conflicts.return_value = {
            "alias": ALIAS,
            "merge_request_id": 7,
            "count": 0,
            "conflicts": [],
        }
        result = _run(
            ["merge-request", "conflicts", "--project", ALIAS],
            _store(tmp_path, active_branch=123),
            service,
        )
        assert result.exit_code == 0, result.output
        assert "Resolved merge request #7 from branch 123" in result.output

    def test_json_mode_does_not_print_the_info_lines(self, tmp_path, service) -> None:
        service.list_conflicts.return_value = {
            "alias": ALIAS,
            "merge_request_id": 7,
            "count": 0,
            "conflicts": [],
        }
        result = _run(
            ["--json", "merge-request", "conflicts", "--project", ALIAS],
            _store(tmp_path, active_branch=123),
            service,
        )
        assert result.exit_code == 0, result.output
        _json(result)  # stdout is pure JSON


# ---------------------------------------------------------------------------
# One error handler -- FEATURE_NOT_ENABLED must survive on every command
# ---------------------------------------------------------------------------


_READ_COMMANDS = [
    ["merge-request", "detail", "--project", ALIAS],
    ["merge-request", "conflicts", "--project", ALIAS],
    [
        "merge-request",
        "diff",
        "--project",
        ALIAS,
        "--component-id",
        "keboola.ex-db",
        "--config-id",
        "1",
    ],
]


class TestErrorHandler:
    @pytest.mark.parametrize("args", _READ_COMMANDS, ids=lambda a: a[1])
    def test_feature_not_enabled_from_the_resolver_keeps_its_code(
        self, tmp_path, service, args
    ) -> None:
        # The resolver runs the feature pre-flight on its no-match path (PR #703,
        # O002), so FeatureNotEnabledError now surfaces from READS with an omitted
        # id -- exactly where a copied `except ConfigError -> CONFIG_ERROR` idiom
        # would flatten it.
        service.find_merge_request_for_branch.side_effect = FeatureNotEnabledError(
            "Merge requests are not enabled on this project"
        )
        result = _run(["--json", *args], _store(tmp_path, active_branch=123), service)
        assert result.exit_code == 5, result.output
        assert _json(result)["error"]["code"] == ErrorCode.FEATURE_NOT_ENABLED

    def test_plain_config_error_is_config_error_exit_5(self, tmp_path, service) -> None:
        service.list_merge_requests.side_effect = ConfigError("boom")
        result = _run(
            ["--json", "merge-request", "list", "--project", ALIAS], _store(tmp_path), service
        )
        assert result.exit_code == 5
        assert _json(result)["error"]["code"] == ErrorCode.CONFIG_ERROR

    def test_api_error_maps_through_the_house_table(self, tmp_path, service) -> None:
        service.list_merge_requests.side_effect = KeboolaApiError(
            message="denied", status_code=403, error_code=ErrorCode.ACCESS_DENIED, retryable=False
        )
        result = _run(
            ["--json", "merge-request", "list", "--project", ALIAS], _store(tmp_path), service
        )
        assert result.exit_code == 1
        assert _json(result)["error"]["code"] == ErrorCode.ACCESS_DENIED

    def test_not_found_from_the_resolver(self, tmp_path, service) -> None:
        service.find_merge_request_for_branch.side_effect = KeboolaApiError(
            message="Branch 123 has no merge request",
            status_code=404,
            error_code=ErrorCode.NOT_FOUND,
            retryable=False,
        )
        result = _run(
            ["--json", "merge-request", "detail", "--project", ALIAS],
            _store(tmp_path, active_branch=123),
            service,
        )
        assert result.exit_code == 1
        assert _json(result)["error"]["code"] == ErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestList:
    def test_json_passthrough(self, tmp_path, service) -> None:
        service.list_merge_requests.return_value = {
            "alias": ALIAS,
            "count": 1,
            "merge_requests": [_row()],
        }
        result = _run(
            ["--json", "merge-request", "list", "--project", ALIAS], _store(tmp_path), service
        )
        assert result.exit_code == 0, result.output
        service.list_merge_requests.assert_called_once_with(ALIAS, state=None)
        assert _json(result)["data"]["count"] == 1

    def test_state_filter_passes_through(self, tmp_path, service) -> None:
        service.list_merge_requests.return_value = {
            "alias": ALIAS,
            "count": 0,
            "merge_requests": [],
            "state_filter": "merged",
        }
        result = _run(
            ["--json", "merge-request", "list", "--project", ALIAS, "--state", "merged"],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 0, result.output
        service.list_merge_requests.assert_called_once_with(ALIAS, state="merged")

    def test_unknown_state_is_exit_2_before_any_call(self, tmp_path, service) -> None:
        result = _run(
            ["--json", "merge-request", "list", "--project", ALIAS, "--state", "develpment"],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 2
        err = _json(result)["error"]
        assert err["code"] == ErrorCode.INVALID_ARGUMENT
        assert "in_development" in err["message"]  # the accepted list is spelled out
        service.list_merge_requests.assert_not_called()

    def test_human_table_uses_derived_state_and_server_order(self, tmp_path, service) -> None:
        service.list_merge_requests.return_value = {
            "alias": ALIAS,
            "count": 2,
            "merge_requests": [
                _row(9, "published", branch_from=None, title="Newer"),
                _row(7, "development", title="Older"),
            ],
        }
        result = _run(["merge-request", "list", "--project", ALIAS], _store(tmp_path), service)
        assert result.exit_code == 0, result.output
        assert "merged" in result.output and "published" not in result.output
        assert result.output.index("Newer") < result.output.index("Older")
        assert "—" in result.output  # null branchFromId on the merged row

    def test_optional_columns_appear_only_when_populated(self, tmp_path, service) -> None:
        service.list_merge_requests.return_value = {
            "alias": ALIAS,
            "count": 1,
            "merge_requests": [_row(createdAt=None)],
        }
        result = _run(["merge-request", "list", "--project", ALIAS], _store(tmp_path), service)
        assert "External ID" not in result.output
        assert "Merged by" not in result.output
        assert "Created" not in result.output

    def test_markup_in_a_title_is_escaped(self, tmp_path, service) -> None:
        service.list_merge_requests.return_value = {
            "alias": ALIAS,
            "count": 1,
            "merge_requests": [_row(title="Fix [bold] parsing [/]")],
        }
        result = _run(["merge-request", "list", "--project", ALIAS], _store(tmp_path), service)
        assert result.exit_code == 0, result.output
        assert "[bold]" in result.output

    def test_empty_featureless_project_says_so(self, tmp_path, service) -> None:
        service.list_merge_requests.return_value = {
            "alias": ALIAS,
            "count": 0,
            "merge_requests": [],
            "feature_enabled": False,
        }
        result = _run(["merge-request", "list", "--project", ALIAS], _store(tmp_path), service)
        assert "not enabled" in result.output
        assert "No merge requests" not in result.output

    def test_empty_with_feature_says_no_merge_requests(self, tmp_path, service) -> None:
        service.list_merge_requests.return_value = {
            "alias": ALIAS,
            "count": 0,
            "merge_requests": [],
            "feature_enabled": True,
        }
        result = _run(["merge-request", "list", "--project", ALIAS], _store(tmp_path), service)
        assert "No merge requests" in result.output


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------


def _detail(**extra: Any) -> dict[str, Any]:
    return {
        **_row(),
        "merge_blockers": [],
        "mergeable": True,
        "viewer": {"is_creator": True, "has_approved": False},
        "changeLog": {},
        "conflicts": [],
        "conflicts_count": 0,
        **extra,
    }


class TestDetail:
    def test_json_and_activity_log_flag(self, tmp_path, service) -> None:
        service.get_merge_request.return_value = _detail()
        result = _run(
            [
                "--json",
                "merge-request",
                "detail",
                "--project",
                ALIAS,
                "--id",
                "7",
                "--activity-log",
            ],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 0, result.output
        service.get_merge_request.assert_called_once_with(ALIAS, 7, include_activity_log=True)
        # explicit id: no row fetch, no branch lookup
        service.get_merge_request_row.assert_not_called()
        service.find_merge_request_for_branch.assert_not_called()

    def test_human_renders_readiness_viewer_and_next_steps(self, tmp_path, service) -> None:
        service.get_merge_request.return_value = _detail()
        result = _run(
            ["merge-request", "detail", "--project", ALIAS, "--id", "7"], _store(tmp_path), service
        )
        assert result.exit_code == 0, result.output
        assert "Mergeable" in result.output
        assert "you created this merge request" in result.output
        assert "you have approved" not in result.output  # False renders as nothing
        assert "merge-request merge" in result.output  # hint-next from allowed_actions
        assert "empty until the merge request is sent for review" in result.output

    def test_blockers_and_armed_auto_merge_render(self, tmp_path, service) -> None:
        service.get_merge_request.return_value = _detail(
            mergeable=False,
            merge_blockers=["conflicts", "approvals"],
            conflicts=[
                {
                    "componentId": "keboola.ex-db",
                    "configurationId": "1",
                    "isDeleted": False,
                    "message": "changed on both",
                }
            ],
            conflicts_count=1,
            autoMergeStrategy="immediately",
        )
        result = _run(
            ["merge-request", "detail", "--project", ALIAS, "--id", "7"], _store(tmp_path), service
        )
        assert "Blocked by" in result.output and "conflicts (1)" in result.output
        assert "armed" in result.output and "immediately" in result.output
        assert "keboola.ex-db" in result.output

    def test_viewer_none_flags_render_nothing(self, tmp_path, service) -> None:
        service.get_merge_request.return_value = _detail(
            viewer={"is_creator": None, "has_approved": None}
        )
        result = _run(
            ["merge-request", "detail", "--project", ALIAS, "--id", "7"], _store(tmp_path), service
        )
        assert "You:" not in result.output


# ---------------------------------------------------------------------------
# conflicts
# ---------------------------------------------------------------------------


class TestConflicts:
    def test_table_and_hint_point_at_the_first_conflict(self, tmp_path, service) -> None:
        service.list_conflicts.return_value = {
            "alias": ALIAS,
            "merge_request_id": 7,
            "count": 2,
            "conflicts": [
                {
                    "componentId": "keboola.ex-db",
                    "configurationId": "111",
                    "isDeleted": True,
                    "message": "m1",
                },
                {
                    "componentId": "keboola.wr-db",
                    "configurationId": "222",
                    "isDeleted": False,
                    "message": "m2",
                },
            ],
        }
        result = _run(
            ["merge-request", "conflicts", "--project", ALIAS, "--id", "7"],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 0, result.output
        assert "Deleted in branch" in result.output  # the flag is the DEV side's
        assert (
            "--component-id keboola.ex-db" in result.output and "--config-id 111" in result.output
        )

    def test_no_conflicts(self, tmp_path, service) -> None:
        service.list_conflicts.return_value = {
            "alias": ALIAS,
            "merge_request_id": 7,
            "count": 0,
            "conflicts": [],
        }
        result = _run(
            ["merge-request", "conflicts", "--project", ALIAS, "--id", "7"],
            _store(tmp_path),
            service,
        )
        assert "No conflicts" in result.output


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def _diff_result(**extra: Any) -> dict[str, Any]:
    return {
        "alias": ALIAS,
        "merge_request_id": 7,
        "component_id": "keboola.ex-db",
        "config_id": "111",
        "branch_id": 123,
        "onto_version": 9,
        "ours_deleted": False,
        "theirs_deleted": False,
        "changes": [
            {
                "path": "configuration.limit",
                "changed_by": "both",
                "agreed": False,
                "base": 100,
                "ours": 500,
                "theirs": 250,
            },
            {
                "path": "configuration.timeout",
                "changed_by": "theirs",
                "base": 30,
                "ours": 30,
                "theirs": 60,
            },
            {
                "path": "configuration.flag",
                "changed_by": "ours",
                "base": True,
                "ours": False,
                "theirs": True,
            },
            {
                "path": "configuration.x",
                "changed_by": "both",
                "agreed": True,
                "base": 1,
                "ours": 2,
                "theirs": 2,
            },
        ],
        "resolution_candidate": {
            "name": "My config",
            "description": None,
            "isDisabled": False,
            "configuration": {"limit": 500},
            "rows": [],
        },
        "diff": {},
        **extra,
    }


_DIFF_ARGS = [
    "merge-request",
    "diff",
    "--project",
    ALIAS,
    "--id",
    "7",
    "--component-id",
    "keboola.ex-db",
    "--config-id",
    "111",
]


class TestDiff:
    def test_three_sections_plus_agreed(self, tmp_path, service) -> None:
        service.get_config_diff.return_value = _diff_result()
        result = _run(_DIFF_ARGS, _store(tmp_path), service)
        assert result.exit_code == 0, result.output
        service.get_config_diff.assert_called_once_with(ALIAS, 7, "keboola.ex-db", "111")
        for heading in (
            "Both changed -- decide",
            "agreed",
            "Only you changed",
            "Only production changed",
        ):
            assert heading in result.output

    def test_deleted_side_renders_recommendation_and_no_sections(self, tmp_path, service) -> None:
        # Since #703 finding #4 a null side yields zero rows -- the flags ARE the content.
        service.get_config_diff.return_value = _diff_result(
            changes=[],
            theirs_deleted=True,
            resolution_candidate={
                "name": "n",
                "description": None,
                "isDisabled": False,
                "configuration": {},
                "rows": [],
            },
        )
        result = _run(_DIFF_ARGS, _store(tmp_path), service)
        assert result.exit_code == 0, result.output
        assert "Production deleted this configuration" in result.output
        assert "--take delete" in result.output and "--take ours" in result.output
        for heading in (
            "Both changed",
            "Only you changed",
            "Only production changed",
            "No changes",
        ):
            assert heading not in result.output

    def test_no_rows_and_no_flags_means_the_conflict_cleared(self, tmp_path, service) -> None:
        service.get_config_diff.return_value = _diff_result(changes=[])
        result = _run(_DIFF_ARGS, _store(tmp_path), service)
        assert "cleared" in result.output
        assert "No changes" not in result.output

    def test_output_writes_the_candidate_verbatim(self, tmp_path, service) -> None:
        service.get_config_diff.return_value = _diff_result()
        target = tmp_path / "resolved.json"
        result = _run(["--json", *_DIFF_ARGS, "--output", str(target)], _store(tmp_path), service)
        assert result.exit_code == 0, result.output
        written = json.loads(target.read_text())
        assert written == _diff_result()["resolution_candidate"]
        assert "description" in written and written["description"] is None  # explicit null survives
        assert _json(result)["data"]["output_path"] == str(target)

    def test_output_refuses_when_nothing_to_prefill(self, tmp_path, service) -> None:
        service.get_config_diff.return_value = _diff_result(
            changes=[], ours_deleted=True, resolution_candidate=None
        )
        target = tmp_path / "resolved.json"
        result = _run(["--json", *_DIFF_ARGS, "--output", str(target)], _store(tmp_path), service)
        assert result.exit_code == 2
        assert "--take delete" in _json(result)["error"]["message"]
        assert not target.exists()

    def test_unknown_format_is_exit_2(self, tmp_path, service) -> None:
        result = _run([*_DIFF_ARGS, "--format", "wide"], _store(tmp_path), service)
        assert result.exit_code == 2
        service.get_config_diff.assert_not_called()

    def test_long_values_elide_unless_full(self, tmp_path, service) -> None:
        long_value = "x" * 200
        changes = [
            {
                "path": "configuration.blob",
                "changed_by": "ours",
                "base": "",
                "ours": long_value,
                "theirs": "",
            }
        ]
        service.get_config_diff.return_value = _diff_result(changes=changes)
        store = _store(tmp_path)
        short = _run(_DIFF_ARGS, store, service)
        assert "…" in short.output and "--format full" in short.output
        full = _run([*_DIFF_ARGS, "--format", "full"], store, service)
        assert "Long values elided" not in full.output
        # folded, not cropped: every character of the value reaches the terminal
        assert full.output.count("x") >= 200
