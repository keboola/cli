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


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def _created(**extra: Any) -> dict[str, Any]:
    return {**_row(), "branch_from_id": 123, "branch_into_id": 1, **extra}


class TestCreate:
    def test_create_from_active_branch(self, tmp_path, service) -> None:
        service.create_merge_request.return_value = _created()
        result = _run(
            ["--json", "merge-request", "create", "--project", ALIAS, "--title", "T"],
            _store(tmp_path, active_branch=123),
            service,
        )
        assert result.exit_code == 0, result.output
        kwargs = service.create_merge_request.call_args.kwargs
        assert kwargs["branch_from_id"] == 123 and kwargs["title"] == "T"
        assert kwargs["reviewer_ids"] is None  # never [] -- that would clear the set
        data = _json(result)["data"]
        assert data["merge_request_id"] == 7 and data["resolved_from_branch"] is True

    def test_reviewer_ids_pass_through_when_given(self, tmp_path, service) -> None:
        service.create_merge_request.return_value = _created()
        _run(
            [
                "--json",
                "merge-request",
                "create",
                "--project",
                ALIAS,
                "--title",
                "T",
                "--branch",
                "123",
                "--reviewer-id",
                "5",
                "--reviewer-id",
                "6",
            ],
            _store(tmp_path),
            service,
        )
        assert service.create_merge_request.call_args.kwargs["reviewer_ids"] == [5, 6]

    def test_no_branch_anywhere_is_exit_5(self, tmp_path, service) -> None:
        result = _run(
            ["--json", "merge-request", "create", "--project", ALIAS, "--title", "T"],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 5
        assert "branch use" in _json(result)["error"]["message"]
        service.create_merge_request.assert_not_called()

    @pytest.mark.parametrize(
        "flags",
        [
            ["--auto-merge-strategy", "sometimes"],
            ["--auto-merge-strategy", "scheduled"],  # missing --auto-merge-at
            ["--auto-merge-at", "2026-09-04T10:00:00Z"],  # at without scheduled
            ["--auto-merge-strategy", "immediately", "--auto-merge-at", "2026-09-04T10:00:00Z"],
        ],
    )
    def test_auto_merge_flag_pairing_is_validated(self, tmp_path, service, flags) -> None:
        result = _run(
            [
                "--json",
                "merge-request",
                "create",
                "--project",
                ALIAS,
                "--title",
                "T",
                "--branch",
                "123",
                *flags,
            ],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 2, result.output
        service.create_merge_request.assert_not_called()

    def test_arming_is_destructive_under_deny_destructive(self, tmp_path, service) -> None:
        result = _run(
            [
                "--json",
                "--deny-destructive",
                "merge-request",
                "create",
                "--project",
                ALIAS,
                "--title",
                "T",
                "--branch",
                "123",
                "--auto-merge-strategy",
                "immediately",
            ],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 6, result.output
        service.create_merge_request.assert_not_called()

    def test_disarmed_none_is_NOT_destructive(self, tmp_path, service) -> None:
        # `none` is the disarm -- escalating it would let --deny-destructive lock
        # a dangerous setting in place.
        service.create_merge_request.return_value = _created()
        result = _run(
            [
                "--json",
                "--deny-destructive",
                "merge-request",
                "create",
                "--project",
                ALIAS,
                "--title",
                "T",
                "--branch",
                "123",
                "--auto-merge-strategy",
                "none",
            ],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 0, result.output

    def test_arming_under_json_needs_an_explicit_branch(self, tmp_path, service) -> None:
        result = _run(
            [
                "--json",
                "merge-request",
                "create",
                "--project",
                ALIAS,
                "--title",
                "T",
                "--auto-merge-strategy",
                "immediately",
            ],
            _store(tmp_path, active_branch=123),
            service,
        )
        assert result.exit_code == 2
        assert "--branch" in _json(result)["error"]["message"]
        service.create_merge_request.assert_not_called()

    def test_arming_prompts_in_human_mode_and_warns_after(self, tmp_path, service) -> None:
        service.create_merge_request.return_value = _created(autoMergeStrategy="immediately")
        args = [
            "merge-request",
            "create",
            "--project",
            ALIAS,
            "--title",
            "T",
            "--branch",
            "123",
            "--auto-merge-strategy",
            "immediately",
        ]
        store = _store(tmp_path)
        aborted = _run(args, store, service, input="n\n")
        assert aborted.exit_code == 0 and "Aborted" in aborted.output
        service.create_merge_request.assert_not_called()
        confirmed = _run(args, store, service, input="y\n")
        assert confirmed.exit_code == 0, confirmed.output
        assert "Arm auto-merge" in confirmed.output
        assert "Auto-merge is armed (immediately)" in confirmed.output
        service.create_merge_request.assert_called_once()

    def test_yes_skips_the_arming_prompt(self, tmp_path, service) -> None:
        service.create_merge_request.return_value = _created()
        result = _run(
            [
                "merge-request",
                "create",
                "--project",
                ALIAS,
                "--title",
                "T",
                "--branch",
                "123",
                "--auto-merge-strategy",
                "immediately",
                "--yes",
            ],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 0, result.output
        assert "Continue?" not in result.output


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


class TestUpdate:
    def test_no_fields_is_exit_2(self, tmp_path, service) -> None:
        result = _run(
            ["--json", "merge-request", "update", "--project", ALIAS, "--id", "7"],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 2
        service.update_merge_request.assert_not_called()

    def test_empty_string_clears_description(self, tmp_path, service) -> None:
        service.update_merge_request.return_value = _row()
        result = _run(
            [
                "--json",
                "merge-request",
                "update",
                "--project",
                ALIAS,
                "--id",
                "7",
                "--description",
                "",
            ],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 0, result.output
        kwargs = service.update_merge_request.call_args.kwargs
        assert kwargs["description"] == ""
        assert kwargs["reviewer_ids"] is None and kwargs["title"] is None

    def test_arming_on_update_is_destructive_and_needs_explicit_target_under_json(
        self, tmp_path, service
    ) -> None:
        denied = _run(
            [
                "--json",
                "--deny-destructive",
                "merge-request",
                "update",
                "--project",
                ALIAS,
                "--id",
                "7",
                "--auto-merge-strategy",
                "immediately",
            ],
            _store(tmp_path / "a"),
            service,
        )
        assert denied.exit_code == 6
        implicit = _run(
            [
                "--json",
                "merge-request",
                "update",
                "--project",
                ALIAS,
                "--auto-merge-strategy",
                "immediately",
            ],
            _store(tmp_path / "b", active_branch=123),
            service,
        )
        assert implicit.exit_code == 2
        service.update_merge_request.assert_not_called()

    def test_disarming_needs_neither(self, tmp_path, service) -> None:
        service.update_merge_request.return_value = _row()
        result = _run(
            [
                "--json",
                "--deny-destructive",
                "merge-request",
                "update",
                "--project",
                ALIAS,
                "--auto-merge-strategy",
                "none",
            ],
            _store(tmp_path, active_branch=123),
            service,
        )
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# transitions: request-review / approve / request-changes
# ---------------------------------------------------------------------------


class TestTransitions:
    def test_request_review_unarmed_is_plain_write(self, tmp_path, service) -> None:
        service.request_review.return_value = _row(state="approved", derived_state="approved")
        result = _run(
            ["--json", "--deny-destructive", "merge-request", "request-review", "--project", ALIAS],
            _store(tmp_path, active_branch=123),
            service,
        )
        assert result.exit_code == 0, result.output
        service.request_review.assert_called_once_with(ALIAS, 7)
        # implicit path: the row came from find -- no extra fetch
        service.get_merge_request_row.assert_not_called()

    @pytest.mark.parametrize("command", ["request-review", "approve"])
    def test_armed_mr_escalates_to_destructive(self, tmp_path, service, command) -> None:
        service.find_merge_request_for_branch.return_value = _row(autoMergeStrategy="immediately")
        result = _run(
            [
                "--json",
                "--deny-destructive",
                "merge-request",
                command,
                "--project",
                ALIAS,
                "--branch",
                "123",
            ],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 6, result.output
        getattr(service, command.replace("-", "_")).assert_not_called()

    def test_armed_with_explicit_id_fetches_the_row_once(self, tmp_path, service) -> None:
        service.get_merge_request_row.return_value = _row(autoMergeStrategy="scheduled")
        service.request_review.return_value = _row(
            state="approved", derived_state="approved", autoMergeStrategy="scheduled"
        )
        result = _run(
            ["merge-request", "request-review", "--project", ALIAS, "--id", "7"],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 0, result.output
        service.get_merge_request_row.assert_called_once_with(ALIAS, 7)
        service.get_merge_request.assert_not_called()  # never the three-call detail
        assert "Auto-merge is armed (scheduled)" in result.output
        assert "on its next tick" in result.output  # state is approved

    def test_armed_implicit_target_under_json_exits_2_after_resolution(
        self, tmp_path, service
    ) -> None:
        # Deliberate: whether the call is destructive is only known from the row.
        service.find_merge_request_for_branch.return_value = _row(autoMergeStrategy="immediately")
        result = _run(
            ["--json", "merge-request", "request-review", "--project", ALIAS],
            _store(tmp_path, active_branch=123),
            service,
        )
        assert result.exit_code == 2
        msg = _json(result)["error"]["message"]
        assert "#7" in msg and "--merge-request-id 7" in msg
        service.find_merge_request_for_branch.assert_called_once()
        service.request_review.assert_not_called()

    def test_request_changes_never_escalates_and_caps_reason(self, tmp_path, service) -> None:
        service.find_merge_request_for_branch.return_value = _row(autoMergeStrategy="immediately")
        service.request_changes.return_value = _row()
        ok = _run(
            [
                "--json",
                "--deny-destructive",
                "merge-request",
                "request-changes",
                "--project",
                ALIAS,
                "--reason",
                "nope",
            ],
            _store(tmp_path / "a", active_branch=123),
            service,
        )
        assert ok.exit_code == 0, ok.output
        service.request_changes.assert_called_once_with(ALIAS, 7, reason="nope")
        too_long = _run(
            [
                "--json",
                "merge-request",
                "request-changes",
                "--project",
                ALIAS,
                "--id",
                "7",
                "--reason",
                "x" * 1001,
            ],
            _store(tmp_path / "b"),
            service,
        )
        assert too_long.exit_code == 2


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


def _merged() -> dict[str, Any]:
    return {
        "alias": ALIAS,
        "merge_request_id": 7,
        "branch_from_id": 123,
        "was_active": True,
        "job": {"id": 1},
        "state": "published",
        "derived_state": "merged",
        "message": "Merge request 7 merged into production. Source branch 123 is being deleted.",
    }


class TestMerge:
    def test_json_without_a_target_is_exit_2_before_any_lookup(self, tmp_path, service) -> None:
        result = _run(
            ["--json", "merge-request", "merge", "--project", ALIAS],
            _store(tmp_path, active_branch=123),
            service,
        )
        assert result.exit_code == 2
        assert "explicit target" in _json(result)["error"]["message"]
        service.find_merge_request_for_branch.assert_not_called()
        service.merge.assert_not_called()

    def test_json_with_branch_is_an_explicit_target(self, tmp_path, service) -> None:
        service.merge.return_value = _merged()
        result = _run(
            ["--json", "merge-request", "merge", "--project", ALIAS, "--branch", "123"],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 0, result.output
        service.merge.assert_called_once_with(ALIAS, 7)

    def test_human_keeps_the_fallback_and_prompts_with_branch_and_title(
        self, tmp_path, service
    ) -> None:
        service.merge.return_value = _merged()
        store = _store(tmp_path, active_branch=123)
        aborted = _run(["merge-request", "merge", "--project", ALIAS], store, service, input="n\n")
        assert aborted.exit_code == 0 and "Aborted" in aborted.output
        assert (
            "#7 'Add sales pipeline'" in aborted.output and "branch 123 deleted" in aborted.output
        )
        service.merge.assert_not_called()
        confirmed = _run(
            ["merge-request", "merge", "--project", ALIAS], store, service, input="y\n"
        )
        assert confirmed.exit_code == 0, confirmed.output
        assert "is being deleted" in confirmed.output
        service.merge.assert_called_once_with(ALIAS, 7)

    def test_deny_destructive_blocks_merge(self, tmp_path, service) -> None:
        result = _run(
            [
                "--json",
                "--deny-destructive",
                "merge-request",
                "merge",
                "--project",
                ALIAS,
                "--id",
                "7",
            ],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 6
        service.merge.assert_not_called()

    def test_post_merge_warnings_render(self, tmp_path, service) -> None:
        service.merge.return_value = {
            **_merged(),
            "warnings": ["Post-merge active-branch reset failed: disk full"],
        }
        result = _run(
            ["merge-request", "merge", "--project", ALIAS, "--id", "7", "--yes"],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 0, result.output
        assert "disk full" in result.output

    def test_merge_conflict_error_passes_through(self, tmp_path, service) -> None:
        service.merge.side_effect = KeboolaApiError(
            message="conflicts",
            status_code=409,
            error_code=ErrorCode.MR_MERGE_CONFLICT,
            retryable=False,
        )
        result = _run(
            ["--json", "merge-request", "merge", "--project", ALIAS, "--id", "7"],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 1
        assert _json(result)["error"]["code"] == ErrorCode.MR_MERGE_CONFLICT


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


_RESOLVE = [
    "merge-request",
    "resolve",
    "--project",
    ALIAS,
    "--id",
    "7",
    "--component-id",
    "keboola.ex-db",
    "--config-id",
    "111",
]


def _resolved(**extra: Any) -> dict[str, Any]:
    return {
        "alias": ALIAS,
        "merge_request_id": 7,
        "component_id": "keboola.ex-db",
        "config_id": "111",
        "branch_id": 123,
        "resolution": "ours",
        "onto_version": 9,
        "configuration": {"id": "111", "version": 10},
        **extra,
    }


class TestResolve:
    def test_take_passes_through(self, tmp_path, service) -> None:
        service.resolve_conflict.return_value = _resolved()
        result = _run(["--json", *_RESOLVE, "--take", "ours"], _store(tmp_path), service)
        assert result.exit_code == 0, result.output
        service.resolve_conflict.assert_called_once_with(
            ALIAS, 7, "keboola.ex-db", "111", take="ours", resolved=None, change_description=None
        )

    @pytest.mark.parametrize(
        "extra",
        [
            [],  # neither
            ["--take", "ours", "--resolved", "{}"],  # both
            ["--take", "mine"],  # unknown mode
            ["--resolved", "[1,2]"],  # not an object
            ["--resolved", "{not json"],  # malformed
        ],
    )
    def test_argument_shape_errors_are_exit_2(self, tmp_path, service, extra) -> None:
        result = _run(["--json", *_RESOLVE, *extra], _store(tmp_path), service)
        assert result.exit_code == 2, result.output
        service.resolve_conflict.assert_not_called()

    def test_resolved_from_file_is_parsed_and_forwarded(self, tmp_path, service) -> None:
        # The real round trip (diff --output -> resolve) is pinned one layer down:
        # test_merge_request_service.py::TestLayer1RfcWalkFollowUps. Here: argument parsing only.
        candidate = {
            "name": "n",
            "description": None,
            "isDisabled": False,
            "configuration": {"limit": 5},
            "rows": [],
        }
        path = tmp_path / "resolved.json"
        path.write_text(json.dumps(candidate))
        service.resolve_conflict.return_value = _resolved(resolution="custom")
        result = _run(
            [
                "--json",
                *_RESOLVE,
                "--resolved",
                f"@{path}",
                "--change-description",
                "merged by hand",
            ],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 0, result.output
        kwargs = service.resolve_conflict.call_args.kwargs
        assert kwargs["resolved"] == candidate and kwargs["take"] is None
        assert kwargs["change_description"] == "merged by hand"

    def test_service_warnings_render(self, tmp_path, service) -> None:
        service.resolve_conflict.return_value = _resolved(
            resolution="delete",
            warnings=["--change-description dropped: the delete tombstone cannot carry one"],
        )
        result = _run(
            [*_RESOLVE, "--take", "delete", "--change-description", "x"], _store(tmp_path), service
        )
        assert result.exit_code == 0, result.output
        assert "dropped" in result.output

    def test_resolve_on_an_armed_mr_escalates(self, tmp_path, service) -> None:
        service.get_merge_request_row.return_value = _row(
            autoMergeStrategy="immediately", state="approved"
        )
        result = _run(
            ["--json", "--deny-destructive", *_RESOLVE, "--take", "theirs"],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 6
        service.resolve_conflict.assert_not_called()

    def test_feature_not_enabled_from_resolve_keeps_its_code(self, tmp_path, service) -> None:
        service.resolve_conflict.side_effect = FeatureNotEnabledError("not enabled")
        result = _run(["--json", *_RESOLVE, "--take", "ours"], _store(tmp_path), service)
        assert result.exit_code == 5
        assert _json(result)["error"]["code"] == ErrorCode.FEATURE_NOT_ENABLED


class TestDiffOutputErrors:
    def test_unwritable_output_path_is_a_readable_exit_2(self, tmp_path, service) -> None:
        service.get_config_diff.return_value = _diff_result()
        target = tmp_path / "no-such-dir" / "resolved.json"
        result = _run(["--json", *_DIFF_ARGS, "--output", str(target)], _store(tmp_path), service)
        assert result.exit_code == 2, result.output
        assert "Cannot write --output" in _json(result)["error"]["message"]


class TestMergeRowFetch:
    def test_json_merge_with_explicit_id_does_not_fetch_the_row(self, tmp_path, service) -> None:
        service.merge.return_value = _merged()
        result = _run(
            ["--json", "merge-request", "merge", "--project", ALIAS, "--id", "7"],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 0, result.output
        service.get_merge_request_row.assert_not_called()
        assert _json(result)["data"]["branch_from_id"] == 123  # from merge()'s own result

    def test_human_merge_with_explicit_id_fetches_the_row_for_the_prompt(
        self, tmp_path, service
    ) -> None:
        service.merge.return_value = _merged()
        result = _run(
            ["merge-request", "merge", "--project", ALIAS, "--id", "7"],
            _store(tmp_path),
            service,
            input="y\n",
        )
        assert result.exit_code == 0, result.output
        service.get_merge_request_row.assert_called_once_with(ALIAS, 7)
        assert "'Add sales pipeline'" in result.output


class TestMergeConflictDetails:
    def _conflict_error(self, truncated: bool) -> KeboolaApiError:
        details: dict[str, Any] = {
            "api_error_code": "storage.mergeRequests.validation",
            "api_error_params": {
                "errors": [
                    {"componentId": "keboola.ex-db", "configurationId": "111"},
                    {"componentId": "keboola.wr-db", "configurationId": "[bold]2"},
                ]
            },
        }
        if truncated:
            details["api_error_params_truncated"] = True
        return KeboolaApiError(
            message="Configurations changed on both branches.",
            status_code=409,
            error_code=ErrorCode.MR_MERGE_CONFLICT,
            retryable=False,
            details=details,
        )

    def test_json_carries_details_through(self, tmp_path, service) -> None:
        service.merge.side_effect = self._conflict_error(truncated=True)
        result = _run(
            ["--json", "merge-request", "merge", "--project", ALIAS, "--id", "7"],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 1
        err = _json(result)["error"]
        assert err["details"]["api_error_params_truncated"] is True
        assert err["details"]["api_error_params"]["errors"][0]["configurationId"] == "111"

    def test_human_lists_conflicts_and_says_truncated(self, tmp_path, service) -> None:
        service.merge.side_effect = self._conflict_error(truncated=True)
        result = _run(
            ["merge-request", "merge", "--project", ALIAS, "--id", "7", "--yes"],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 1
        assert "keboola.ex-db/111" in result.output
        assert "[bold]2" in result.output  # escaped, not interpreted
        assert "list truncated" in result.output and "merge-request conflicts" in result.output

    def test_human_untruncated_has_no_truncation_line(self, tmp_path, service) -> None:
        service.merge.side_effect = self._conflict_error(truncated=False)
        result = _run(
            ["merge-request", "merge", "--project", ALIAS, "--id", "7", "--yes"],
            _store(tmp_path),
            service,
        )
        assert "keboola.wr-db" in result.output
        assert "list truncated" not in result.output


class TestOpusReviewFollowUps:
    """Pins for the Phase-5 self-review findings (docs/merge-requests-layer1.md)."""

    def test_explicit_id_detail_carries_branch_from_id_from_the_payload(
        self, tmp_path, service
    ) -> None:
        # M1: never `branch_from_id: null` beside `branches.branchFromId: 123`.
        service.get_merge_request.return_value = _detail()
        result = _run(
            ["--json", "merge-request", "detail", "--project", ALIAS, "--id", "7"],
            _store(tmp_path),
            service,
        )
        assert _json(result)["data"]["branch_from_id"] == 123

    def test_explicit_id_diff_and_conflicts_carry_branch_from_id(self, tmp_path, service) -> None:
        service.get_config_diff.return_value = _diff_result()
        diff = _run(["--json", *_DIFF_ARGS], _store(tmp_path / "a"), service)
        assert _json(diff)["data"]["branch_from_id"] == 123  # from the diff's branch_id
        service.list_conflicts.return_value = {
            "alias": ALIAS,
            "merge_request_id": 7,
            "count": 0,
            "conflicts": [],
        }
        conflicts = _run(
            ["--json", "merge-request", "conflicts", "--project", ALIAS, "--id", "7"],
            _store(tmp_path / "b"),
            service,
        )
        assert _json(conflicts)["data"]["branch_from_id"] == 123  # via the row tier
        service.get_merge_request_row.assert_called_once_with(ALIAS, 7)

    def test_armed_warning_is_human_only_and_not_in_the_payload(self, tmp_path, service) -> None:
        # L4: Layer 1 does not manufacture payload; --json reads autoMergeStrategy off the row.
        service.create_merge_request.return_value = _created(autoMergeStrategy="immediately")
        result = _run(
            [
                "--json",
                "merge-request",
                "create",
                "--project",
                ALIAS,
                "--title",
                "T",
                "--branch",
                "123",
                "--auto-merge-strategy",
                "immediately",
            ],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 0, result.output
        assert "warnings" not in _json(result)["data"]

    def test_hint_falls_back_to_the_raw_action_for_unknown_names(self, tmp_path, service) -> None:
        # M3: a server-serialised vocabulary (DMD-1988) must not make hint-next vanish.
        service.get_merge_request.return_value = _detail(allowed_actions=["requestReview"])
        result = _run(
            ["merge-request", "detail", "--project", ALIAS, "--id", "7"], _store(tmp_path), service
        )
        assert "requestReview" in result.output

    def test_output_wording_for_a_backend_envelope_hole(self, tmp_path, service) -> None:
        # L3/L5: a null candidate on a NON-deleted side is the service's warning, not "deleted in your branch".
        service.get_config_diff.return_value = _diff_result(
            resolution_candidate=None,
            warnings=[
                "The diff's ours side carries no isDisabled -- no resolution candidate could be prefilled."
            ],
        )
        result = _run(
            ["--json", *_DIFF_ARGS, "--output", str(tmp_path / "r.json")], _store(tmp_path), service
        )
        assert result.exit_code == 2
        msg = _json(result)["error"]["message"]
        assert "carries no isDisabled" in msg and "deleted in your branch" not in msg

    def test_resolved_pointing_at_a_directory_is_exit_2_not_a_traceback(
        self, tmp_path, service
    ) -> None:
        # L7: OSError from the file read is a usage error.
        result = _run(
            ["--json", *_RESOLVE, "--resolved", f"@{tmp_path}"], _store(tmp_path), service
        )
        assert result.exit_code == 2, result.output
        service.resolve_conflict.assert_not_called()

    def test_markup_in_wire_ids_does_not_crash_the_resolve_success_line(
        self, tmp_path, service
    ) -> None:
        # H2: the operation already landed server-side; a MarkupError afterwards would report failure.
        service.resolve_conflict.return_value = _resolved(component_id="k", config_id="[/x]")
        result = _run(
            [
                "merge-request",
                "resolve",
                "--project",
                ALIAS,
                "--id",
                "7",
                "--component-id",
                "k",
                "--config-id",
                "[/x]",
                "--take",
                "ours",
            ],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 0, result.output

    @pytest.mark.parametrize(
        "args",
        [
            ["merge-request", "update", "--project", ALIAS, "--title", "T"],
            ["merge-request", "request-review", "--project", ALIAS],
            ["merge-request", "approve", "--project", ALIAS],
            ["merge-request", "request-changes", "--project", ALIAS],
            ["merge-request", "merge", "--project", ALIAS, "--branch", "123"],
            [
                "merge-request",
                "resolve",
                "--project",
                ALIAS,
                "--component-id",
                "c",
                "--config-id",
                "1",
                "--take",
                "ours",
            ],
        ],
        ids=lambda a: a[1],
    )
    def test_feature_not_enabled_keeps_its_code_on_every_write(
        self, tmp_path, service, args
    ) -> None:
        # L2: one case per command, as the RFC promised.
        service.find_merge_request_for_branch.side_effect = FeatureNotEnabledError("not enabled")
        result = _run(["--json", *args], _store(tmp_path, active_branch=123), service)
        assert result.exit_code == 5, result.output
        assert _json(result)["error"]["code"] == ErrorCode.FEATURE_NOT_ENABLED

    def test_create_feature_not_enabled_keeps_its_code(self, tmp_path, service) -> None:
        service.create_merge_request.side_effect = FeatureNotEnabledError("not enabled")
        result = _run(
            [
                "--json",
                "merge-request",
                "create",
                "--project",
                ALIAS,
                "--title",
                "T",
                "--branch",
                "123",
            ],
            _store(tmp_path),
            service,
        )
        assert result.exit_code == 5
        assert _json(result)["error"]["code"] == ErrorCode.FEATURE_NOT_ENABLED


class TestDiffEmptyEnvelope:
    def test_no_rows_with_a_service_warning_does_not_claim_the_conflict_cleared(
        self, tmp_path, service
    ) -> None:
        service.get_config_diff.return_value = _diff_result(
            changes=[],
            resolution_candidate=None,
            warnings=[
                "The diff's ours side carries no name -- no resolution candidate could be prefilled."
            ],
        )
        result = _run(_DIFF_ARGS, _store(tmp_path), service)
        assert result.exit_code == 0, result.output
        assert "cleared" not in result.output
        assert "carries no name" in result.output
