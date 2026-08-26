"""Tests for MergeRequestService and the status-derivation polyfill (DMD-1899).

The derivation tables are the canonical spec from the L2 RFC
(docs/merge-requests-layer2.md, "Derived status") and DMD-1988 -- a port of
the UI list badge. When Connection serializes the fields, the server-first
tests keep passing and the fallback tests get deleted with the fallbacks.
"""

from __future__ import annotations

from typing import Any

from keboola_agent_cli.services.merge_request_service import (
    derive_allowed_actions,
    derive_merge_blockers,
    derive_state,
    derive_viewer,
)

CREATOR = {"id": 42, "name": "Martin"}


def _mr(
    state: str = "development",
    reviewers: list[dict[str, Any]] | None = None,
    approvals: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": 7,
        "state": state,
        "creator": CREATOR,
        "reviewers": reviewers or [],
        "approvals": approvals or [],
        **extra,
    }


class TestDeriveState:
    def test_plain_states_map_to_client_vocabulary(self) -> None:
        assert derive_state(_mr("development")) == "in_development"
        assert derive_state(_mr("in_review")) == "in_review"
        assert derive_state(_mr("approved")) == "approved"
        assert derive_state(_mr("in_merge")) == "in_merge"
        assert derive_state(_mr("published")) == "merged"
        assert derive_state(_mr("canceled")) == "closed"

    def test_rejected_needs_a_non_creator_rejection_in_development(self) -> None:
        mr = _mr("development", reviewers=[{"id": 99, "name": "R", "status": "rejected"}])
        assert derive_state(mr) == "rejected"

    def test_creator_self_rejection_is_closed_not_rejected(self) -> None:
        # The UI "cancel" action reuses request-changes: the creator rejects
        # their own MR and it stays in development.
        mr = _mr("development", reviewers=[{"id": 42, "name": "Martin", "status": "rejected"}])
        assert derive_state(mr) == "closed"

    def test_rejection_outside_development_does_not_override(self) -> None:
        # A stale rejected reviewer entry must not relabel a re-submitted MR.
        mr = _mr("in_review", reviewers=[{"id": 99, "status": "rejected"}])
        assert derive_state(mr) == "in_review"

    def test_non_creator_rejection_wins_over_self_rejection(self) -> None:
        # RFC order: rejected is evaluated before closed.
        mr = _mr(
            "development",
            reviewers=[
                {"id": 42, "status": "rejected"},
                {"id": 99, "status": "rejected"},
            ],
        )
        assert derive_state(mr) == "rejected"

    def test_unknown_raw_state_passes_through(self) -> None:
        assert derive_state(_mr("some_future_state")) == "some_future_state"

    def test_server_field_wins_over_local_derivation(self) -> None:
        # Polyfill contract: once DMD-1988 serializes derivedState, the local
        # table must never contradict it.
        mr = _mr("development", derivedState="rejected")
        assert derive_state(mr) == "rejected"

    def test_reviewer_without_status_is_pending_not_rejection(self) -> None:
        mr = _mr("development", reviewers=[{"id": 99, "name": "R", "status": None}])
        assert derive_state(mr) == "in_development"


class TestDeriveMergeBlockers:
    def test_open_mr_without_conflicts_is_mergeable(self) -> None:
        assert derive_merge_blockers(_mr("development"), conflicts=[]) == []
        assert derive_merge_blockers(_mr("approved"), conflicts=[]) == []

    def test_conflicts_block(self) -> None:
        conflicts = [{"componentId": "keboola.snowflake-transformation", "configurationId": "1"}]
        assert derive_merge_blockers(_mr("development"), conflicts) == ["conflicts"]

    def test_in_review_blocks_on_approvals(self) -> None:
        assert derive_merge_blockers(_mr("in_review"), conflicts=[]) == ["approvals"]

    def test_concurrent_blockers_do_not_mask_each_other(self) -> None:
        conflicts = [{"componentId": "c", "configurationId": "1"}]
        assert derive_merge_blockers(_mr("in_review"), conflicts) == ["conflicts", "approvals"]

    def test_terminal_and_transient_states_block_on_state(self) -> None:
        for state in ("in_merge", "published", "canceled"):
            assert derive_merge_blockers(_mr(state), conflicts=[]) == ["state"]

    def test_none_conflicts_means_not_fetched_not_conflict_free(self) -> None:
        # List rows don't fetch conflicts; absence of data must not assert
        # "no conflicts".
        assert derive_merge_blockers(_mr("development"), conflicts=None) == []

    def test_rejected_mr_has_no_blocker(self) -> None:
        # It sits in development; a non-SOX merge from there succeeds
        # (auto skip_review). derived_state tells the story instead.
        mr = _mr("development", reviewers=[{"id": 99, "status": "rejected"}])
        assert derive_merge_blockers(mr, conflicts=[]) == []

    def test_server_field_wins(self) -> None:
        mr = _mr("development", mergeBlockers=["approvals"])
        assert derive_merge_blockers(mr, conflicts=[]) == ["approvals"]


class TestDeriveAllowedActions:
    def test_development_offers_submit_and_direct_merge(self) -> None:
        assert derive_allowed_actions(_mr("development")) == [
            "request_review",
            "merge",
            "update",
            "resolve_conflicts",
        ]

    def test_review_states_gate_approve_and_request_changes(self) -> None:
        # Mirrors the UI buttons: approve/request-changes only in
        # in_review|approved.
        assert "approve" in derive_allowed_actions(_mr("in_review"))
        assert "approve" in derive_allowed_actions(_mr("approved"))
        assert "approve" not in derive_allowed_actions(_mr("development"))

    def test_merge_not_offered_from_in_review(self) -> None:
        # in_review means approvals are insufficient by definition; the
        # moment they suffice the backend auto-transitions to approved.
        assert "merge" not in derive_allowed_actions(_mr("in_review"))

    def test_locked_and_terminal_states_offer_nothing(self) -> None:
        for state in ("in_merge", "published", "canceled"):
            assert derive_allowed_actions(_mr(state)) == []

    def test_unknown_state_offers_nothing(self) -> None:
        assert derive_allowed_actions(_mr("some_future_state")) == []

    def test_server_field_wins(self) -> None:
        mr = _mr("published", allowedActions=["merge"])
        assert derive_allowed_actions(mr) == ["merge"]


class TestDeriveViewer:
    def test_creator_is_flagged(self) -> None:
        viewer = derive_viewer(_mr(), admin_id=42)
        assert viewer == {"is_creator": True, "has_approved": False}

    def test_approver_is_flagged(self) -> None:
        # approverId is a string on the wire while admin ids are ints -- the
        # comparison must not care (the UI does String(id) for the same
        # reason).
        mr = _mr("in_review", approvals=[{"approverId": "77", "approverName": "R"}])
        viewer = derive_viewer(mr, admin_id=77)
        assert viewer == {"is_creator": False, "has_approved": True}

    def test_no_admin_identity_means_unknown_not_false(self) -> None:
        # A scoped token has no admin block; None is honest, False would lie.
        viewer = derive_viewer(_mr(), admin_id=None)
        assert viewer == {"is_creator": None, "has_approved": None}

    def test_approval_without_approver_id_is_ignored(self) -> None:
        mr = _mr("in_review", approvals=[{"approverId": None, "approverName": "gone"}])
        assert derive_viewer(mr, admin_id=42)["has_approved"] is False

    def test_server_field_wins(self) -> None:
        mr = _mr(viewer={"isCreator": False, "hasApproved": True})
        assert derive_viewer(mr, admin_id=42) == {"is_creator": False, "has_approved": True}


# -- Service-level tests ------------------------------------------------------

from pathlib import Path  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

from keboola_agent_cli.config_store import ConfigError, ConfigStore  # noqa: E402
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError  # noqa: E402
from keboola_agent_cli.models import ProjectConfig, TokenVerifyResponse  # noqa: E402
from keboola_agent_cli.services.merge_request_service import (  # noqa: E402
    MergeRequestService,
)

STACK_URL = "https://connection.keboola.com"
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
ALIAS = "prod"


def _verify_response(admin_id: int | None = 42) -> TokenVerifyResponse:
    return TokenVerifyResponse(
        token_id="901",
        token_description="test",
        project_id=10,
        project_name="Prod",
        owner_name="Prod",
        admin_id=admin_id,
    )


@pytest.fixture
def store(tmp_config_dir: Path) -> ConfigStore:
    s = ConfigStore(config_dir=tmp_config_dir)
    s.add_project(
        ALIAS,
        ProjectConfig(stack_url=STACK_URL, token=TOKEN, project_name="Prod", project_id=10),
    )
    return s


@pytest.fixture
def client_factory() -> tuple[MagicMock, MagicMock]:
    mock = MagicMock()
    mock.verify_token.return_value = _verify_response()
    mock.has_feature.return_value = True
    factory = MagicMock(return_value=mock)
    return factory, mock


def _svc(store: ConfigStore, factory: MagicMock) -> MergeRequestService:
    return MergeRequestService(store, client_factory=factory)


def _wire_mr(
    mr_id: int = 7,
    state: str = "development",
    branch_from: int | None = 123,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": mr_id,
        "state": state,
        "title": "My MR",
        "creator": CREATOR,
        "reviewers": [],
        "approvals": [],
        "branches": {"branchFromId": branch_from, "branchIntoId": 1},
        **extra,
    }


class TestListMergeRequests:
    def test_rows_carry_derived_state(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.list.return_value = [
            _wire_mr(1, "published"),
            _wire_mr(2, "development"),
        ]
        result = _svc(store, factory).list_merge_requests(ALIAS)
        assert result["count"] == 2
        assert [mr["derived_state"] for mr in result["merge_requests"]] == [
            "merged",
            "in_development",
        ]
        mock.close.assert_called_once()

    def test_state_filter_matches_derived_vocabulary(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.list.return_value = [
            _wire_mr(1, "published"),
            _wire_mr(2, "development"),
        ]
        result = _svc(store, factory).list_merge_requests(ALIAS, state="merged")
        assert result["count"] == 1
        assert result["merge_requests"][0]["id"] == 1
        assert result["state_filter"] == "merged"

    def test_state_filter_matches_raw_state_too(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.list.return_value = [_wire_mr(1, "published")]
        result = _svc(store, factory).list_merge_requests(ALIAS, state="published")
        assert result["count"] == 1

    def test_rejected_filter_uses_reviewer_derivation(self, store, client_factory) -> None:
        factory, mock = client_factory
        rejected = _wire_mr(1, "development", reviewers=[{"id": 99, "status": "rejected"}])
        mock.merge_requests.list.return_value = [rejected, _wire_mr(2, "development")]
        result = _svc(store, factory).list_merge_requests(ALIAS, state="rejected")
        assert [mr["id"] for mr in result["merge_requests"]] == [1]


class TestFindMergeRequestForBranch:
    def test_finds_by_branch_from_id(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.list.return_value = [
            _wire_mr(1, branch_from=111),
            _wire_mr(2, branch_from=222),
        ]
        result = _svc(store, factory).find_merge_request_for_branch(ALIAS, 222)
        assert result["id"] == 2
        assert result["alias"] == ALIAS
        assert result["derived_state"] == "in_development"

    def test_branch_without_mr_raises_not_found_with_next_step(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.list.return_value = [_wire_mr(1, branch_from=111)]
        with pytest.raises(KeboolaApiError) as exc_info:
            _svc(store, factory).find_merge_request_for_branch(ALIAS, 999)
        assert exc_info.value.error_code == ErrorCode.NOT_FOUND
        assert "merge-request create" in exc_info.value.message


class TestGetMergeRequest:
    def test_open_mr_fetches_conflicts_and_derives_everything(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(7, "in_review")
        mock.merge_requests.conflicts.return_value = [{"componentId": "c", "configurationId": "1"}]
        detail = _svc(store, factory).get_merge_request(ALIAS, 7)
        assert detail["derived_state"] == "in_review"
        assert detail["merge_blockers"] == ["conflicts", "approvals"]
        assert detail["mergeable"] is False
        assert detail["allowed_actions"] == [
            "approve",
            "request_changes",
            "update",
            "resolve_conflicts",
        ]
        assert detail["viewer"] == {"is_creator": True, "has_approved": False}
        assert detail["conflicts_count"] == 1
        mock.merge_requests.conflicts.assert_called_once_with(7)

    def test_mergeable_open_mr(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(7, "approved")
        mock.merge_requests.conflicts.return_value = []
        detail = _svc(store, factory).get_merge_request(ALIAS, 7)
        assert detail["merge_blockers"] == []
        assert detail["mergeable"] is True
        assert detail["conflicts"] == []

    def test_closed_mr_skips_conflicts_and_is_not_mergeable(self, store, client_factory) -> None:
        # The source branch of a published/canceled MR is deleted; the
        # conflicts endpoint is moot there and must not be called.
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(7, "published", branch_from=None)
        detail = _svc(store, factory).get_merge_request(ALIAS, 7)
        mock.merge_requests.conflicts.assert_not_called()
        assert detail["merge_blockers"] == ["state"]
        assert detail["mergeable"] is False
        assert "conflicts" not in detail

    def test_activity_log_flag_passes_through(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(7, "published", branch_from=None)
        _svc(store, factory).get_merge_request(ALIAS, 7, include_activity_log=True)
        mock.merge_requests.get.assert_called_once_with(7, include_activity_log=True)

    def test_scoped_token_yields_unknown_viewer(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.verify_token.return_value = _verify_response(admin_id=None)
        mock.merge_requests.get.return_value = _wire_mr(7, "development")
        mock.merge_requests.conflicts.return_value = []
        detail = _svc(store, factory).get_merge_request(ALIAS, 7)
        assert detail["viewer"] == {"is_creator": None, "has_approved": None}


DEFAULT_BRANCH = {"id": 1, "name": "Main", "isDefault": True}
DEV_BRANCH = {"id": 123, "name": "feature", "isDefault": False}


class TestCreateMergeRequest:
    def test_targets_the_default_branch_automatically(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.list_dev_branches.return_value = [DEFAULT_BRANCH, DEV_BRANCH]
        mock.merge_requests.create.return_value = _wire_mr(9, "development", branch_from=123)
        result = _svc(store, factory).create_merge_request(ALIAS, 123, "My MR")
        kwargs = mock.merge_requests.create.call_args.kwargs
        assert kwargs["branch_from_id"] == 123
        assert kwargs["branch_into_id"] == 1
        assert result["branch_from_id"] == 123
        assert result["derived_state"] == "in_development"

    def test_refuses_the_default_branch_as_source(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.list_dev_branches.return_value = [DEFAULT_BRANCH, DEV_BRANCH]
        with pytest.raises(ConfigError) as exc_info:
            _svc(store, factory).create_merge_request(ALIAS, 1, "My MR")
        assert "default" in str(exc_info.value)
        mock.merge_requests.create.assert_not_called()

    def test_preflight_blocks_without_the_feature(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.has_feature.return_value = False
        with pytest.raises(ConfigError) as exc_info:
            _svc(store, factory).create_merge_request(ALIAS, 123, "My MR")
        assert "branches-merge-requests" in str(exc_info.value)
        mock.merge_requests.create.assert_not_called()

    def test_optional_fields_pass_through(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.list_dev_branches.return_value = [DEFAULT_BRANCH]
        mock.merge_requests.create.return_value = _wire_mr(9)
        _svc(store, factory).create_merge_request(
            ALIAS,
            123,
            "My MR",
            description="d",
            reviewer_ids=[5, 6],
            external_id="TICKET-1",
        )
        kwargs = mock.merge_requests.create.call_args.kwargs
        assert kwargs["reviewer_ids"] == [5, 6]
        assert kwargs["external_id"] == "TICKET-1"


class TestUpdateAndTransitions:
    def test_update_passes_fields_and_enriches(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.update.return_value = _wire_mr(7, "in_review")
        result = _svc(store, factory).update_merge_request(ALIAS, 7, title="New")
        assert mock.merge_requests.update.call_args.kwargs["title"] == "New"
        assert result["derived_state"] == "in_review"

    def test_request_review(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.request_review.return_value = _wire_mr(7, "approved")
        result = _svc(store, factory).request_review(ALIAS, 7)
        # Non-SOX default of 0 required approvals: lands straight in approved.
        assert result["derived_state"] == "approved"

    def test_approve(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.approve.return_value = _wire_mr(7, "approved")
        result = _svc(store, factory).approve(ALIAS, 7)
        assert result["derived_state"] == "approved"

    def test_request_changes_carries_reason(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.request_changes.return_value = _wire_mr(7, "development")
        _svc(store, factory).request_changes(ALIAS, 7, reason="fix the mapping")
        assert mock.merge_requests.request_changes.call_args.kwargs["reason"] == "fix the mapping"

    def test_every_write_runs_the_preflight(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.has_feature.return_value = False
        svc = _svc(store, factory)
        for call in (
            lambda: svc.update_merge_request(ALIAS, 7, title="x"),
            lambda: svc.request_review(ALIAS, 7),
            lambda: svc.approve(ALIAS, 7),
            lambda: svc.request_changes(ALIAS, 7),
        ):
            with pytest.raises(ConfigError):
                call()


class TestMerge:
    def _arm_merge(self, mock: MagicMock, branch_from: int | None = 123) -> None:
        mock.merge_requests.get.return_value = _wire_mr(7, "approved", branch_from=branch_from)
        mock.merge_requests.merge.return_value = {
            "id": 555,
            "status": "success",
            "results": {**_wire_mr(7, "published", branch_from=None)},
        }

    def test_happy_path_says_being_deleted_never_deleted(
        self, store, client_factory, monkeypatch
    ) -> None:
        factory, mock = client_factory
        self._arm_merge(mock)
        monkeypatch.setattr(
            "keboola_agent_cli.sync.branch_mapping.cleanup_branch_id_from_mapping",
            lambda branch_id: None,
        )
        result = _svc(store, factory).merge(ALIAS, 7)
        assert "is being deleted" in result["message"]
        assert "is deleted" not in result["message"].replace("is being deleted", "")
        assert result["branch_from_id"] == 123
        assert result["state"] == "published"
        assert result["derived_state"] == "merged"

    def test_active_branch_reset_only_when_it_was_the_merged_one(
        self, store, client_factory, monkeypatch
    ) -> None:
        factory, mock = client_factory
        self._arm_merge(mock)
        monkeypatch.setattr(
            "keboola_agent_cli.sync.branch_mapping.cleanup_branch_id_from_mapping",
            lambda branch_id: None,
        )
        store.set_project_branch(ALIAS, 999)  # a DIFFERENT branch is active
        result = _svc(store, factory).merge(ALIAS, 7)
        assert result["was_active"] is False
        assert store.get_project(ALIAS).active_branch_id == 999  # untouched

        store.set_project_branch(ALIAS, 123)  # the merged branch is active
        result = _svc(store, factory).merge(ALIAS, 7)
        assert result["was_active"] is True
        assert store.get_project(ALIAS).active_branch_id is None

    def test_sync_mapping_cleanup_is_reported(self, store, client_factory, monkeypatch) -> None:
        factory, mock = client_factory
        self._arm_merge(mock)
        monkeypatch.setattr(
            "keboola_agent_cli.sync.branch_mapping.cleanup_branch_id_from_mapping",
            lambda branch_id: {"project_root": "/x", "git_branches_unlinked": ["feat/a"]},
        )
        result = _svc(store, factory).merge(ALIAS, 7)
        assert result["mapping_cleanup"]["git_branches_unlinked"] == ["feat/a"]
        assert "feat/a" in result["message"]

    def test_cleanup_failure_degrades_to_warning_not_error(
        self, store, client_factory, monkeypatch
    ) -> None:
        factory, mock = client_factory
        self._arm_merge(mock)

        def boom(branch_id: int) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(
            "keboola_agent_cli.sync.branch_mapping.cleanup_branch_id_from_mapping", boom
        )
        result = _svc(store, factory).merge(ALIAS, 7)  # must NOT raise
        assert any("disk full" in w for w in result["cleanup_warnings"])

    def test_409_with_code_maps_to_not_ready_retryable(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(7, "approved")
        mock.merge_requests.merge.side_effect = KeboolaApiError(
            message="API error 409: Cannot merge, another merge request is processing.",
            status_code=409,
            error_code=ErrorCode.API_ERROR,
            details={"api_error_code": "storage.mergeRequests.notReadyToMerge"},
        )
        with pytest.raises(KeboolaApiError) as exc_info:
            _svc(store, factory).merge(ALIAS, 7)
        assert exc_info.value.error_code == ErrorCode.MR_NOT_READY_TO_MERGE
        assert exc_info.value.retryable is True

    def test_409_without_code_maps_to_conflict_with_next_step(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(7, "approved")
        mock.merge_requests.merge.side_effect = KeboolaApiError(
            message="API error 409: Configuration was changed in the default branch.",
            status_code=409,
            error_code=ErrorCode.API_ERROR,
        )
        with pytest.raises(KeboolaApiError) as exc_info:
            _svc(store, factory).merge(ALIAS, 7)
        assert exc_info.value.error_code == ErrorCode.MR_MERGE_CONFLICT
        assert exc_info.value.retryable is False
        assert "merge-request conflicts" in exc_info.value.message

    def test_non_409_errors_pass_through_unmapped(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(7, "approved")
        mock.merge_requests.merge.side_effect = KeboolaApiError(
            message="job failed",
            status_code=0,
            error_code=ErrorCode.STORAGE_JOB_FAILED,
        )
        with pytest.raises(KeboolaApiError) as exc_info:
            _svc(store, factory).merge(ALIAS, 7)
        assert exc_info.value.error_code == ErrorCode.STORAGE_JOB_FAILED

    def test_failed_merge_does_no_cleanup(self, store, client_factory, monkeypatch) -> None:
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(7, "approved")
        mock.merge_requests.merge.side_effect = KeboolaApiError(
            message="conflict", status_code=409, error_code=ErrorCode.API_ERROR
        )
        called: list[int] = []
        monkeypatch.setattr(
            "keboola_agent_cli.sync.branch_mapping.cleanup_branch_id_from_mapping",
            lambda branch_id: called.append(branch_id),
        )
        store.set_project_branch(ALIAS, 123)
        with pytest.raises(KeboolaApiError):
            _svc(store, factory).merge(ALIAS, 7)
        assert called == []
        assert store.get_project(ALIAS).active_branch_id == 123

    def test_preflight_blocks_without_the_feature(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.has_feature.return_value = False
        with pytest.raises(ConfigError):
            _svc(store, factory).merge(ALIAS, 7)
        mock.merge_requests.merge.assert_not_called()
