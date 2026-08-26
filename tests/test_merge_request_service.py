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
