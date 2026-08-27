"""Tests for MergeRequestService and the status-derivation polyfill (DMD-1899).

The derivation tables are the canonical spec from the L2 RFC
(docs/merge-requests-layer2.md, "Derived status") and DMD-1988 -- a port of
the UI list badge. When Connection serializes the fields, the server-first
tests keep passing and the fallback tests get deleted with the fallbacks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.client.merge_requests import MergeRequests
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import (
    ConfigError,
    ErrorCode,
    FeatureNotEnabledError,
    KeboolaApiError,
)
from keboola_agent_cli.models import ProjectConfig, TokenVerifyResponse
from keboola_agent_cli.services.merge_request_service import (
    MergeRequestService,
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

    def test_approve_exists_only_in_in_review(self) -> None:
        # The approve transition's sole `from` place is in_review; from
        # `approved` the backend answers 422 (Opus wire review 2026-08-27).
        assert "approve" in derive_allowed_actions(_mr("in_review"))
        assert "approve" not in derive_allowed_actions(_mr("approved"))
        assert "approve" not in derive_allowed_actions(_mr("development"))

    def test_request_changes_in_review_and_approved(self) -> None:
        assert "request_changes" in derive_allowed_actions(_mr("in_review"))
        assert "request_changes" in derive_allowed_actions(_mr("approved"))

    def test_merge_not_offered_from_in_review(self) -> None:
        # in_review means approvals are insufficient by definition; the
        # moment they suffice the backend auto-transitions to approved.
        assert "merge" not in derive_allowed_actions(_mr("in_review"))

    def test_in_merge_offers_only_update(self) -> None:
        # The server blocks update only in the terminal states; an in_merge
        # MR is still updatable (metadata), nothing else is sensible.
        assert derive_allowed_actions(_mr("in_merge")) == ["update"]

    def test_terminal_states_offer_nothing(self) -> None:
        for state in ("published", "canceled"):
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
    # spec'd at the L3 seam this PR builds on (#556): a renamed or removed
    # client/namespace method fails these tests instead of silently keeping
    # them green against an interface that no longer exists.
    mock = MagicMock(spec=KeboolaClient)
    mock.merge_requests = MagicMock(spec=MergeRequests)
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


def _diff(
    base: dict[str, Any] | None,
    ours: dict[str, Any] | None,
    theirs: dict[str, Any] | None,
) -> dict[str, Any]:
    return {"base": base, "ours": ours, "theirs": theirs}


def _side(
    configuration: dict[str, Any],
    version: int = 5,
    is_deleted: bool = False,
    rows: list[dict[str, Any]] | None = None,
    **diff_extra: Any,
) -> dict[str, Any]:
    """One diff side in the verified wire shape (ConfigurationVersionResponse):
    version/isDeleted as side metadata, content nested under ``diff``."""
    return {
        "version": version,
        "isDeleted": is_deleted,
        "diff": {
            "name": "My config",
            "description": None,
            "changeDescription": "edited",
            "isDisabled": False,
            "configuration": configuration,
            "rows": rows if rows is not None else [],
            **diff_extra,
        },
    }


CONFLICT_ENTRY = {"componentId": "keboola.ex-db", "configurationId": "111"}


class TestGetConfigDiff:
    def test_classifies_ours_theirs_both(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(7, "development", branch_from=123)
        mock.get_config_diff.return_value = _diff(
            base=_side({"limit": 100, "timeout": 30, "flag": True}, version=3),
            ours=_side({"limit": 500, "timeout": 30, "flag": False}, version=4),
            theirs=_side({"limit": 250, "timeout": 60, "flag": True}, version=7),
        )
        result = _svc(store, factory).get_config_diff(ALIAS, 7, "keboola.ex-db", "111")
        # The branch is derived from the MR, never caller-supplied -- the L3
        # diff call must receive branchFromId (123), and the result echoes it.
        mock.get_config_diff.assert_called_once_with("keboola.ex-db", "111", 123)
        assert result["branch_id"] == 123
        assert result["merge_request_id"] == 7
        by_path = {c["path"]: c for c in result["changes"]}
        assert by_path["configuration.limit"] == {
            "path": "configuration.limit",
            "changed_by": "both",
            "agreed": False,
            "base": 100,
            "ours": 500,
            "theirs": 250,
        }
        assert by_path["configuration.timeout"]["changed_by"] == "theirs"
        # The side that did NOT touch the path still holds the base value.
        assert by_path["configuration.timeout"]["ours"] == 30
        assert by_path["configuration.flag"]["changed_by"] == "ours"
        assert "agreed" not in by_path["configuration.flag"]  # only on `both` rows
        assert result["onto_version"] == 7
        assert result["ours_deleted"] is False
        assert result["theirs_deleted"] is False

    def test_identical_change_on_both_sides_is_agreed(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(7, "development", branch_from=123)
        mock.get_config_diff.return_value = _diff(
            base=_side({"limit": 100}, version=3),
            ours=_side({"limit": 500}, version=4),
            theirs=_side({"limit": 500}, version=7),
        )
        result = _svc(store, factory).get_config_diff(ALIAS, 7, "keboola.ex-db", "111")
        (change,) = result["changes"]
        assert change["changed_by"] == "both"
        assert change["agreed"] is True

    def test_removed_key_shows_none_not_base(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(7, "development", branch_from=123)
        mock.get_config_diff.return_value = _diff(
            base=_side({"secret": "old"}, version=3),
            ours=_side({}, version=4),  # ours REMOVED the key
            theirs=_side({"secret": "new"}, version=7),
        )
        result = _svc(store, factory).get_config_diff(ALIAS, 7, "keboola.ex-db", "111")
        (change,) = result["changes"]
        assert change["changed_by"] == "both"
        assert change["agreed"] is False
        assert change["ours"] is None  # removed, never the base value
        assert change["theirs"] == "new"

    def test_deleted_side_is_flagged_not_a_path(self, store, client_factory) -> None:
        # Deletion is side metadata (isDeleted, top-level on the wire), not a
        # content path -- it must surface as a boolean, or the "Only you /
        # Only production" rendering would hide the most consequential
        # difference.
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(7, "development", branch_from=123)
        mock.get_config_diff.return_value = _diff(
            base=_side({"limit": 100}, version=3),
            ours=_side({"limit": 100}, version=4),
            theirs=_side({"limit": 100}, version=7, is_deleted=True),
        )
        result = _svc(store, factory).get_config_diff(ALIAS, 7, "keboola.ex-db", "111")
        assert result["theirs_deleted"] is True
        assert result["ours_deleted"] is False
        assert result["changes"] == []  # identical content, no paths

    def test_null_side_reports_none_deleted_flag(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(7, "development", branch_from=123)
        mock.get_config_diff.return_value = _diff(
            base=None,
            ours=None,  # never existed on this side
            theirs=_side({"a": 2}, version=7),
        )
        result = _svc(store, factory).get_config_diff(ALIAS, 7, "keboola.ex-db", "111")
        assert result["ours_deleted"] is None
        by_path = {c["path"]: c for c in result["changes"]}
        assert by_path["configuration"]["changed_by"] == "theirs"

    def test_change_description_is_not_content(self, store, client_factory) -> None:
        # changeDescription is a per-version commit message; two sides always
        # differ there and it is not something a resolution decides.
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(7, "development", branch_from=123)
        mock.get_config_diff.return_value = _diff(
            base=_side({"a": 1}, version=3, changeDescription="base msg"),
            ours=_side({"a": 1}, version=4, changeDescription="ours msg"),
            theirs=_side({"a": 1}, version=7, changeDescription="theirs msg"),
        )
        result = _svc(store, factory).get_config_diff(ALIAS, 7, "keboola.ex-db", "111")
        assert result["changes"] == []

    def test_rows_compare_wholesale(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(7, "development", branch_from=123)
        mock.get_config_diff.return_value = _diff(
            base=_side({}, version=3, rows=[{"id": "r1"}]),
            ours=_side({}, version=4, rows=[{"id": "r1"}, {"id": "r2"}]),
            theirs=_side({}, version=7, rows=[{"id": "r1"}]),
        )
        result = _svc(store, factory).get_config_diff(ALIAS, 7, "keboola.ex-db", "111")
        (change,) = result["changes"]
        assert change["path"] == "rows"
        assert change["changed_by"] == "ours"


class TestResolveConflict:
    # Sentinel: "keep the fixture's default side" -- distinct from an
    # explicit None, which means "the side does not exist".
    _DEFAULT_SIDE: Any = object()

    def _arm(
        self,
        mock: MagicMock,
        ours: Any = _DEFAULT_SIDE,
        theirs: Any = _DEFAULT_SIDE,
    ) -> None:
        mock.merge_requests.get.return_value = _wire_mr(7, "development", branch_from=123)
        mock.merge_requests.conflicts.return_value = [CONFLICT_ENTRY]
        mock.get_config_diff.return_value = _diff(
            base=_side({"limit": 100}, version=3),
            ours=_side({"limit": 500}, version=4) if ours is self._DEFAULT_SIDE else ours,
            theirs=(_side({"limit": 250}, version=7) if theirs is self._DEFAULT_SIDE else theirs),
        )
        mock.rebase_config.return_value = {"id": "111", "version": 8}
        mock.rebase_config_delete.return_value = {"id": "111", "version": 8, "isDeleted": True}

    def test_branch_is_derived_from_the_mr_never_supplied(self, store, client_factory) -> None:
        # Finding #1 of the PR review: a caller-supplied branch could point
        # the rebase at a branch the conflict-set guard never checked.
        factory, mock = client_factory
        self._arm(mock)
        result = _svc(store, factory).resolve_conflict(
            ALIAS, 7, "keboola.ex-db", "111", take="ours"
        )
        mock.get_config_diff.assert_called_once_with("keboola.ex-db", "111", 123)
        args = mock.rebase_config.call_args.args
        assert args == ("keboola.ex-db", "111", 123)
        assert result["branch_id"] == 123

    def test_closed_mr_cannot_be_resolved(self, store, client_factory) -> None:
        factory, mock = client_factory
        self._arm(mock)
        mock.merge_requests.get.return_value = _wire_mr(7, "published", branch_from=None)
        with pytest.raises(KeboolaApiError) as exc_info:
            _svc(store, factory).resolve_conflict(ALIAS, 7, "keboola.ex-db", "111", take="ours")
        assert exc_info.value.error_code == ErrorCode.VALIDATION_ERROR
        mock.rebase_config.assert_not_called()

    def test_take_ours_rebases_dev_content_onto_theirs_version(self, store, client_factory) -> None:
        factory, mock = client_factory
        self._arm(mock)
        result = _svc(store, factory).resolve_conflict(
            ALIAS, 7, "keboola.ex-db", "111", take="ours"
        )
        kwargs = mock.rebase_config.call_args.kwargs
        assert kwargs["version"] == 7  # theirs.version, never ours'
        assert kwargs["configuration"] == {"limit": 500}
        assert kwargs["name"] == "My config"  # from the side's diff envelope
        assert result["resolution"] == "ours"
        assert result["onto_version"] == 7

    def test_take_theirs_rebases_production_content(self, store, client_factory) -> None:
        factory, mock = client_factory
        self._arm(mock)
        _svc(store, factory).resolve_conflict(ALIAS, 7, "keboola.ex-db", "111", take="theirs")
        kwargs = mock.rebase_config.call_args.kwargs
        assert kwargs["configuration"] == {"limit": 250}
        assert kwargs["version"] == 7

    def test_take_delete_sends_the_tombstone(self, store, client_factory) -> None:
        factory, mock = client_factory
        self._arm(mock)
        result = _svc(store, factory).resolve_conflict(
            ALIAS, 7, "keboola.ex-db", "111", take="delete"
        )
        mock.rebase_config_delete.assert_called_once_with("keboola.ex-db", "111", 123, version=7)
        assert result["resolution"] == "delete"

    def test_take_ours_of_a_deleted_side_becomes_delete(self, store, client_factory) -> None:
        factory, mock = client_factory
        self._arm(mock, ours=_side({}, version=4, is_deleted=True))
        result = _svc(store, factory).resolve_conflict(
            ALIAS, 7, "keboola.ex-db", "111", take="ours"
        )
        assert result["resolution"] == "delete"
        mock.rebase_config.assert_not_called()

    def test_take_theirs_of_a_deleted_side_becomes_delete(self, store, client_factory) -> None:
        # "Production deleted it, dev changed it" is a live conflict shape --
        # symmetric with the ours mirror (review finding #3).
        factory, mock = client_factory
        self._arm(mock, theirs=_side({}, version=7, is_deleted=True))
        result = _svc(store, factory).resolve_conflict(
            ALIAS, 7, "keboola.ex-db", "111", take="theirs"
        )
        assert result["resolution"] == "delete"
        mock.rebase_config_delete.assert_called_once_with("keboola.ex-db", "111", 123, version=7)

    def test_take_side_without_content_keys_blames_the_server_not_the_caller(
        self, store, client_factory
    ) -> None:
        # The side's diff envelope is server-produced with all content keys
        # required -- a hole is a backend contract violation and the message
        # must point at the manual path, not lecture the caller about a body
        # they never supplied (review finding #2).
        factory, mock = client_factory
        self._arm(mock, theirs={"version": 7, "isDeleted": False, "diff": {"name": "x"}})
        with pytest.raises(KeboolaApiError) as exc_info:
            _svc(store, factory).resolve_conflict(ALIAS, 7, "keboola.ex-db", "111", take="theirs")
        assert exc_info.value.error_code == ErrorCode.VALIDATION_ERROR
        assert "theirs side carries no" in exc_info.value.message
        assert "resolved body" in exc_info.value.message  # names the workaround

    def test_missing_theirs_version_is_refused(self, store, client_factory) -> None:
        factory, mock = client_factory
        self._arm(mock, theirs=None)
        with pytest.raises(KeboolaApiError) as exc_info:
            _svc(store, factory).resolve_conflict(ALIAS, 7, "keboola.ex-db", "111", take="ours")
        assert exc_info.value.error_code == ErrorCode.VALIDATION_ERROR
        assert "theirs side" in exc_info.value.message

    def test_custom_body_must_spell_out_replaced_content(self, store, client_factory) -> None:
        factory, mock = client_factory
        self._arm(mock)
        with pytest.raises(ConfigError) as exc_info:
            _svc(store, factory).resolve_conflict(
                ALIAS, 7, "keboola.ex-db", "111", resolved={"name": "n"}
            )
        assert "rows" in str(exc_info.value) and "configuration" in str(exc_info.value)
        mock.rebase_config.assert_not_called()

    def test_custom_body_rebases_verbatim(self, store, client_factory) -> None:
        factory, mock = client_factory
        self._arm(mock)
        body = {"name": "merged", "rows": [], "configuration": {"limit": 300}}
        result = _svc(store, factory).resolve_conflict(
            ALIAS, 7, "keboola.ex-db", "111", resolved=body, change_description="3-way"
        )
        kwargs = mock.rebase_config.call_args.kwargs
        assert kwargs["configuration"] == {"limit": 300}
        assert kwargs["change_description"] == "3-way"
        assert result["resolution"] == "custom"

    def test_exactly_one_mode_required(self, store, client_factory) -> None:
        factory, _ = client_factory
        svc = _svc(store, factory)
        with pytest.raises(ConfigError):
            svc.resolve_conflict(ALIAS, 7, "c", "1")
        with pytest.raises(ConfigError):
            svc.resolve_conflict(ALIAS, 7, "c", "1", take="ours", resolved={})
        with pytest.raises(ConfigError):
            svc.resolve_conflict(ALIAS, 7, "c", "1", take="mine")

    def test_config_outside_conflict_set_is_refused(self, store, client_factory) -> None:
        factory, mock = client_factory
        self._arm(mock)
        with pytest.raises(KeboolaApiError) as exc_info:
            _svc(store, factory).resolve_conflict(ALIAS, 7, "keboola.other", "999", take="ours")
        assert exc_info.value.error_code == ErrorCode.VALIDATION_ERROR
        mock.rebase_config.assert_not_called()


class TestListConflicts:
    def test_returns_count_and_raw_entries(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.conflicts.return_value = [CONFLICT_ENTRY]
        result = _svc(store, factory).list_conflicts(ALIAS, 7)
        assert result["count"] == 1
        assert result["conflicts"] == [CONFLICT_ENTRY]


class TestReviewFollowUps:
    """Regression tests for the PR #703 review findings."""

    def test_string_wire_branch_id_still_matches_find(self, store, client_factory) -> None:
        # Finding #6: MR payload ids mix int and str; a string-serialized
        # branchFromId must not defeat the branch->MR resolver.
        factory, mock = client_factory
        mr = _wire_mr(1)
        mr["branches"]["branchFromId"] = "123"
        mock.merge_requests.list.return_value = [mr]
        result = _svc(store, factory).find_merge_request_for_branch(ALIAS, 123)
        assert result["id"] == 1

    def test_string_wire_branch_id_still_resets_active_branch(
        self, store, client_factory, monkeypatch
    ) -> None:
        factory, mock = client_factory
        mr = _wire_mr(7, "approved")
        mr["branches"]["branchFromId"] = "123"
        mock.merge_requests.get.return_value = mr
        mock.merge_requests.merge.return_value = {"id": 5, "status": "success", "results": {}}
        monkeypatch.setattr(
            "keboola_agent_cli.sync.branch_mapping.cleanup_branch_id_from_mapping",
            lambda branch_id: None,
        )
        store.set_project_branch(ALIAS, 123)
        result = _svc(store, factory).merge(ALIAS, 7)
        assert result["was_active"] is True
        assert result["branch_from_id"] == 123  # coerced to int
        assert store.get_project(ALIAS).active_branch_id is None

    def test_unknown_state_filter_is_refused_with_the_vocabulary(
        self, store, client_factory
    ) -> None:
        factory, mock = client_factory
        with pytest.raises(ConfigError) as exc_info:
            _svc(store, factory).list_merge_requests(ALIAS, state="mereged")
        assert "merged" in str(exc_info.value)  # names the accepted values
        mock.merge_requests.list.assert_not_called()

    def test_no_default_branch_is_a_readable_error(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.list_dev_branches.return_value = [DEV_BRANCH]  # no isDefault anywhere
        with pytest.raises(KeboolaApiError) as exc_info:
            _svc(store, factory).create_merge_request(ALIAS, 123, "My MR")
        assert "no default branch" in exc_info.value.message
        mock.merge_requests.create.assert_not_called()

    def test_server_viewer_skips_the_verify_token_call(self, store, client_factory) -> None:
        # Once DMD-1988 serializes `viewer`, the polyfill's cost (one
        # verify_token round-trip per detail) must disappear with it.
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(
            7, "development", viewer={"isCreator": True, "hasApproved": False}
        )
        mock.merge_requests.conflicts.return_value = []
        detail = _svc(store, factory).get_merge_request(ALIAS, 7)
        mock.verify_token.assert_not_called()
        assert detail["viewer"] == {"is_creator": True, "has_approved": False}


class TestOpusWireReviewFollowUps:
    """Regression tests for the Opus wire-truth review (2026-08-27)."""

    def test_conflict_409_matches_the_validation_code(self, store, client_factory) -> None:
        # The conflict 409 DOES carry a machine code
        # (storage.mergeRequests.validation) plus the conflicting configs in
        # params.errors -- both must survive into the remapped error.
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(7, "approved")
        conflict_params = {
            "errors": [{"componentId": "c", "configurationId": "1", "isDeleted": False}]
        }
        mock.merge_requests.merge.side_effect = KeboolaApiError(
            message="Merge request 7 cannot be merged.",
            status_code=409,
            error_code=ErrorCode.API_ERROR,
            details={
                "api_error_code": "storage.mergeRequests.validation",
                "api_error_params": conflict_params,
            },
        )
        with pytest.raises(KeboolaApiError) as exc_info:
            _svc(store, factory).merge(ALIAS, 7)
        assert exc_info.value.error_code == ErrorCode.MR_MERGE_CONFLICT
        assert exc_info.value.details["api_error_params"] == conflict_params

    def test_unknown_409_code_passes_through_unmapped(self, store, client_factory) -> None:
        # A future backend 409 with a different code must NOT be confidently
        # mislabeled as a merge conflict.
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(7, "approved")
        mock.merge_requests.merge.side_effect = KeboolaApiError(
            message="something else entirely",
            status_code=409,
            error_code=ErrorCode.API_ERROR,
            details={"api_error_code": "storage.somethingElse.entirely"},
        )
        with pytest.raises(KeboolaApiError) as exc_info:
            _svc(store, factory).merge(ALIAS, 7)
        assert exc_info.value.error_code == ErrorCode.API_ERROR

    def test_null_name_on_a_take_side_is_a_contract_violation(self, store, client_factory) -> None:
        # The diff envelope declares name nullable, but the rebase validator
        # requires a non-empty string -- a null must not sail through the
        # presence check into a server 400.
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(7, "development", branch_from=123)
        mock.merge_requests.conflicts.return_value = [CONFLICT_ENTRY]
        side = _side({"limit": 250}, version=7)
        side["diff"]["name"] = None
        mock.get_config_diff.return_value = _diff(
            base=_side({"limit": 100}, version=3),
            ours=_side({"limit": 500}, version=4),
            theirs=side,
        )
        with pytest.raises(KeboolaApiError) as exc_info:
            _svc(store, factory).resolve_conflict(ALIAS, 7, "keboola.ex-db", "111", take="theirs")
        assert exc_info.value.error_code == ErrorCode.VALIDATION_ERROR
        assert "name" in exc_info.value.message
        mock.rebase_config.assert_not_called()

    def test_empty_server_viewer_falls_back_to_local_derivation(
        self, store, client_factory
    ) -> None:
        # Copilot review: a `viewer: {}` (or one with foreign keys) must NOT
        # skip verify_token -- the skip predicate and derive_viewer's
        # server-field predicate are one function, so they cannot disagree.
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(7, "development", viewer={})
        mock.merge_requests.conflicts.return_value = []
        detail = _svc(store, factory).get_merge_request(ALIAS, 7)
        mock.verify_token.assert_called_once()
        # admin_id=42 == creator -> locally derived, not None/None
        assert detail["viewer"] == {"is_creator": True, "has_approved": False}

    def test_sox_project_gets_the_sox_refusal_not_a_generic_one(
        self, store, client_factory
    ) -> None:
        # A SOX project (protected-default-branch, no branches-merge-requests)
        # is refused as deliberate CLI policy -- the message must say so, not
        # suggest enabling a feature the project deliberately does not have.
        factory, mock = client_factory
        mock.has_feature.side_effect = lambda f: f == "protected-default-branch"
        with pytest.raises(FeatureNotEnabledError) as exc_info:
            _svc(store, factory).create_merge_request(ALIAS, 123, "My MR")
        assert "SOX" in str(exc_info.value)
        assert exc_info.value.error_code == ErrorCode.FEATURE_NOT_ENABLED
        mock.merge_requests.create.assert_not_called()

    def test_plain_project_gets_the_enable_hint(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.has_feature.return_value = False
        with pytest.raises(ConfigError) as exc_info:
            _svc(store, factory).create_merge_request(ALIAS, 123, "My MR")
        assert "not enabled" in str(exc_info.value)
        assert "SOX" not in str(exc_info.value)


class TestLayer1FindingsFollowUps:
    """Regression tests for tasks/dmd-1899-findings-from-layer1.md."""

    def test_every_enriched_return_carries_allowed_actions(self, store, client_factory) -> None:
        # Finding #2: a --json consumer of create/transitions must be able to
        # answer "what can I do next" without a second call.
        factory, mock = client_factory
        mock.list_dev_branches.return_value = [{"id": 1, "isDefault": True}]
        mock.merge_requests.create.return_value = _wire_mr(9, "development")
        created = _svc(store, factory).create_merge_request(ALIAS, 123, "My MR")
        assert created["allowed_actions"] == [
            "request_review",
            "merge",
            "update",
            "resolve_conflicts",
        ]

        mock.merge_requests.request_review.return_value = _wire_mr(9, "approved")
        submitted = _svc(store, factory).request_review(ALIAS, 9)
        assert "merge" in submitted["allowed_actions"]

        mock.merge_requests.list.return_value = [_wire_mr(9, "published")]
        rows = _svc(store, factory).list_merge_requests(ALIAS)["merge_requests"]
        assert rows[0]["allowed_actions"] == []

    def test_empty_list_reports_feature_enabled_flag(self, store, client_factory) -> None:
        # Finding #6: 200 + [] on a project without the feature must be
        # tellable from a genuinely empty project.
        factory, mock = client_factory
        mock.merge_requests.list.return_value = []
        mock.has_feature.return_value = False
        result = _svc(store, factory).list_merge_requests(ALIAS)
        assert result["count"] == 0
        assert result["feature_enabled"] is False

    def test_non_empty_list_does_not_spend_the_feature_call(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.merge_requests.list.return_value = [_wire_mr(1)]
        result = _svc(store, factory).list_merge_requests(ALIAS)
        assert "feature_enabled" not in result
        mock.has_feature.assert_not_called()

    def test_diff_on_a_closed_mr_is_refused(self, store, client_factory) -> None:
        # Finding #1: the branch comes from the MR; a published/canceled MR
        # has none (FK nulled it), so the diff is refused readably.
        factory, mock = client_factory
        mock.merge_requests.get.return_value = _wire_mr(7, "published", branch_from=None)
        with pytest.raises(KeboolaApiError) as exc_info:
            _svc(store, factory).get_config_diff(ALIAS, 7, "keboola.ex-db", "111")
        assert exc_info.value.error_code == ErrorCode.VALIDATION_ERROR
        mock.get_config_diff.assert_not_called()
