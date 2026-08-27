"""Merge-request service -- MR lifecycle, status derivation, error mapping (DMD-1899).

Business logic for the future ``kbagent merge-request`` command group, over the
Layer 3 namespace ``client.merge_requests`` (shipped in #556) and the config
diff/rebase endpoints in ``client/configs.py``. Scope is the **non-SOX** flow
(``branches-merge-requests``); design record: ``docs/merge-requests-layer2.md``.

The module-level functions below are the **status-derivation polyfill**: the
derived vocabulary (``derived_state`` / ``merge_blockers`` / ``allowed_actions``
/ ``viewer``) belongs on the backend so the UI, this CLI, and the MCP tools
consume one evaluation instead of re-deriving it three times (the way GitHub
serializes ``mergeable_state`` / ``reviewDecision`` / ``viewer*``). Connection
tracks that as DMD-1988; until it lands, every function here reads the future
serialized field FIRST and only falls back to the local decision table --
delete the fallbacks when DMD-1988 ships. The canonical decision tables live
in the L2 RFC and in DMD-1988; the local logic is a port of the UI list badge
(``kbc-ui .../merge-requests/components/MergeRequestRow.tsx`` + ``helpers.ts``).
"""

from __future__ import annotations

import logging
from typing import Any

from ..client import KeboolaClient
from ..constants import BRANCHES_MERGE_REQUESTS_FEATURE
from ..errors import ConfigError, ErrorCode, FeatureNotEnabledError, KeboolaApiError
from ..json_utils import DiffEntry, compute_diff_entries
from ..models import ProjectConfig
from .base import BaseService, find_default_branch_id

logger = logging.getLogger(__name__)

# Raw lifecycle state -> derived_state, before the reviewer-based overrides.
# `published`/`canceled` get client-facing names matching the UI list badge
# ("Merged"/"Closed"); `in_merge` is named even though the UI badge omits it.
_DERIVED_STATE_BY_RAW: dict[str, str] = {
    "development": "in_development",
    "in_review": "in_review",
    "approved": "approved",
    "in_merge": "in_merge",
    "published": "merged",
    "canceled": "closed",
}

# Mechanical action availability per raw state, verified against the Symfony
# workflow (MergeRequestLifecycleStateMachine + guards; Opus wire review
# 2026-08-27):
# - `approve` exists ONLY in in_review (the transition's sole `from` place);
#   from `approved` the backend answers 422. Even in in_review it is further
#   gated by AddApprovalGuard (not the creator, not already approved,
#   required count not yet reached) -- which no state-only table can express.
#   With the non-SOX default of 0 required approvals, approve is 422 in every
#   state and in_review itself is unreachable (request-review jumps straight
#   to approved via skip_review).
# - `merge` appears for `development` because a non-SOX merge from there
#   succeeds when approvals suffice (the backend auto-applies skip_review).
# - `update` is blocked server-side only in the terminal states
#   (published/canceled) -- an in_merge MR is still updatable.
# - `resolve_conflicts` (diff+rebase) is allowed while the MR is open; the
#   in_merge branch lock is enforced on the SOX path only, but rebasing
#   mid-merge is pointless, so the table deliberately omits it there.
# Deliberately state-only: roles and features are the pre-flight's job
# client-side, and the backend can honor them once it serializes
# `allowedActions` (DMD-1988).
_ALLOWED_ACTIONS_BY_STATE: dict[str, tuple[str, ...]] = {
    "development": ("request_review", "merge", "update", "resolve_conflicts"),
    "in_review": ("approve", "request_changes", "update", "resolve_conflicts"),
    "approved": ("request_changes", "merge", "update", "resolve_conflicts"),
    "in_merge": ("update",),
    "published": (),
    "canceled": (),
}


# Everything `list_merge_requests`' --state filter accepts: the derived
# vocabulary plus the raw lifecycle states (the two overlap on purpose).
_STATE_FILTER_VOCABULARY: frozenset[str] = (
    frozenset(_DERIVED_STATE_BY_RAW) | frozenset(_DERIVED_STATE_BY_RAW.values()) | {"rejected"}
)


def _same_id(a: Any, b: Any) -> bool:
    """Compare two ids that may arrive as int or str (approverId is a string
    on the wire while creator.id and reviewer.id are numbers)."""
    return a is not None and b is not None and str(a) == str(b)


def _creator_id(mr: dict[str, Any]) -> Any:
    return (mr.get("creator") or {}).get("id")


def derive_state(mr: dict[str, Any]) -> str:
    """Derive the client-facing lifecycle state of a merge request.

    Server-first: prefers a serialized ``derivedState`` when Connection ships
    it (DMD-1988); the local fallback is the UI list badge's decision table,
    evaluated in order:

    - ``rejected``: ``development`` + a non-creator reviewer with
      ``status=rejected``.
    - ``closed``: ``canceled``, or ``development`` + the creator's
      self-rejection -- the UI "cancel" action reuses request-changes under
      the hood, so a self-closed MR keeps ``state=development``.
    - otherwise the raw state mapped through ``_DERIVED_STATE_BY_RAW``
      (an unknown raw state passes through unchanged, defensively).

    Reliability caveat (verified against Connection, Opus wire review
    2026-08-27): ``reviewers[].status`` is populated only within a review
    round anchored by an actual ``request_review`` event, and a non-reviewer's
    decision (the creator's included -- the creator can never BE a reviewer)
    is dropped whenever explicit reviewers exist. ``skip_review`` writes no
    activity event, so in a project with the non-SOX default of 0 required
    approvals every status is ``null`` and the ``rejected`` / self-``closed``
    overrides never fire -- the same blind spot the UI badge has, since this
    table is its port. The truth lives in the MR's activity log
    (``changes_requested`` events, un-shadowed and un-anchored); serializing a
    reliable ``derivedState`` from it is exactly what DMD-1988 asks Connection
    to do. The fallback here stays best-effort by design.
    """
    server = mr.get("derivedState")
    if isinstance(server, str) and server:
        return server

    state = mr.get("state") or ""
    creator_id = _creator_id(mr)
    non_creator_rejected = False
    creator_self_rejected = False
    for reviewer in mr.get("reviewers") or []:
        if reviewer.get("status") != "rejected":
            continue
        if _same_id(reviewer.get("id"), creator_id):
            creator_self_rejected = True
        else:
            non_creator_rejected = True

    if state == "development" and non_creator_rejected:
        return "rejected"
    if state == "canceled" or (state == "development" and creator_self_rejected):
        return "closed"
    return _DERIVED_STATE_BY_RAW.get(state, state)


def derive_merge_blockers(mr: dict[str, Any], conflicts: list[dict[str, Any]] | None) -> list[str]:
    """Derive what currently blocks ``merge`` -- a list, so concurrent
    blockers don't mask each other (unlike GitHub's single-valued
    ``mergeable_state``).

    Server-first (``mergeBlockers``, DMD-1988). Local fallback, in
    deterministic order:

    - ``conflicts``: the live conflicts list is non-empty. Pass ``None`` when
      conflicts were not fetched (list rows) -- absence of data is not
      absence of conflicts, so ``None`` simply skips the check.
    - ``approvals``: ``state == in_review`` -- the state machine collapses
      the requirement (insufficient approvals is the only way to sit there).
      No count is reported until Connection serializes it (DMD-1969).
    - ``state``: ``in_merge`` / ``published`` / ``canceled`` -- merge is not
      applicable.

    Purely informational, NOT a guard: the backend stays the authority via
    the merge 409 (conflicts are validated live on every attempt). Note a
    ``rejected`` MR has no blocker -- it sits in ``development`` and a
    non-SOX merge from there succeeds (auto skip_review); the story is told
    by ``derived_state``.
    """
    server = mr.get("mergeBlockers")
    if isinstance(server, list):
        return [str(blocker) for blocker in server]

    state = mr.get("state") or ""
    blockers: list[str] = []
    if conflicts:
        blockers.append("conflicts")
    if state == "in_review":
        blockers.append("approvals")
    if state in ("in_merge", "published", "canceled"):
        blockers.append("state")
    return blockers


def derive_allowed_actions(mr: dict[str, Any]) -> list[str]:
    """Derive which MR actions the current state mechanically allows.

    Server-first (``allowedActions``, DMD-1988). The local fallback is
    state-only (see ``_ALLOWED_ACTIONS_BY_STATE``); an unknown state yields
    an empty list rather than guessing.
    """
    server = mr.get("allowedActions")
    if isinstance(server, list):
        return [str(action) for action in server]
    return list(_ALLOWED_ACTIONS_BY_STATE.get(mr.get("state") or "", ()))


def derive_viewer(mr: dict[str, Any], admin_id: int | None) -> dict[str, bool | None]:
    """Derive the caller-relative flags: am I the creator, did I approve.

    ``admin_id`` is the caller's user id from ``verify_token`` (the response's
    ``admin`` block); a scoped token has none, in which case both flags are
    ``None`` -- honest "unknown", not ``False``. Server-first (``viewer``
    with ``isCreator``/``hasApproved``, DMD-1988). These flags are what turns
    a blocker into a next step: ``approvals`` + ``has_approved=True`` means
    "wait for the other reviewers", not "approve it".
    """
    server = mr.get("viewer")
    if isinstance(server, dict) and ("isCreator" in server or "hasApproved" in server):
        return {
            "is_creator": server.get("isCreator"),
            "has_approved": server.get("hasApproved"),
        }

    if admin_id is None:
        return {"is_creator": None, "has_approved": None}
    is_creator = _same_id(_creator_id(mr), admin_id)
    has_approved = any(
        _same_id(approval.get("approverId"), admin_id) for approval in mr.get("approvals") or []
    )
    return {"is_creator": is_creator, "has_approved": has_approved}


def _enrich_row(mr: dict[str, Any]) -> dict[str, Any]:
    """List-row enrichment: raw MR + derived_state (conflicts are not fetched
    per row, so no blockers here -- detail-level enrichment does that)."""
    return {**mr, "derived_state": derive_state(mr)}


class MergeRequestService(BaseService):
    """Business logic for the non-SOX merge-request lifecycle.

    Single-project operations over ``client.merge_requests`` (Layer 3, #556).
    Reads pass through with derived-status enrichment; writes run the
    ``branches-merge-requests`` pre-flight first, because a missing feature
    surfaces as a 403 byte-for-byte identical to a role denial and only a
    client-side check can word the real error.

    Uses dependency injection for config_store and client_factory.
    """

    # States whose source branch still exists and whose conflicts endpoint is
    # meaningful; for published/canceled MRs the branch is deleted and
    # branchFromId is null.
    _OPEN_STATES = ("development", "in_review", "approved")

    def _project(self, alias: str) -> ProjectConfig:
        return self.resolve_projects([alias])[alias]

    def _require_merge_requests_feature(self, client: KeboolaClient) -> None:
        """Refuse a write early when the project lacks the non-SOX MR feature.

        Server-side, the six MR writes accept `protected-default-branch` OR
        `branches-merge-requests` (only /rebase requires the latter), so this
        pre-flight fences off SOX projects ONLY as long as a SOX project
        never also carries `branches-merge-requests` -- and it is deliberately
        stricter than the server for a project with only
        `protected-default-branch` (SOX approvals semantics are out of
        kbagent's scope). See docs/merge-requests-layer2.md.
        """
        if client.has_feature(BRANCHES_MERGE_REQUESTS_FEATURE):
            return
        raise FeatureNotEnabledError(
            "Merge requests are not enabled on this project: the "
            f"'{BRANCHES_MERGE_REQUESTS_FEATURE}' feature is missing. Without this "
            "pre-flight the API would answer an unexplained 403 (identical to a role "
            "denial). Note: kbagent deliberately refuses SOX projects that carry only "
            "'protected-default-branch', although the server would accept some writes -- "
            "the SOX approvals flow is not supported by this CLI."
        )

    # -- Reads ----------------------------------------------------------------

    def list_merge_requests(self, alias: str, state: str | None = None) -> dict[str, Any]:
        """List the project's merge requests, each with ``derived_state``.

        ``state`` filters client-side (the endpoint declares no query
        parameters): a value matching either the derived vocabulary
        (``rejected``, ``merged``, ``closed``, ...) or a raw lifecycle state
        (``published``, ...) keeps the row; matching is case-insensitive. An
        unknown value is refused with the accepted list -- the vocabulary is
        closed and known here, and a typo returning a silent ``count: 0``
        would read as "no MRs".
        """
        if state is not None and state.lower() not in _STATE_FILTER_VOCABULARY:
            raise ConfigError(
                f"Unknown --state value {state!r}. Accepted values: "
                f"{', '.join(sorted(_STATE_FILTER_VOCABULARY))}."
            )
        project = self._project(alias)
        client = self._client_factory(project.stack_url, project.token)
        try:
            rows = [_enrich_row(mr) for mr in client.merge_requests.list()]
        finally:
            client.close()

        if state is not None:
            wanted = state.lower()
            rows = [
                mr
                for mr in rows
                if wanted in ((mr.get("state") or "").lower(), mr["derived_state"].lower())
            ]

        result: dict[str, Any] = {
            "alias": alias,
            "count": len(rows),
            "merge_requests": rows,
        }
        if state is not None:
            result["state_filter"] = state
        return result

    def find_merge_request_for_branch(self, alias: str, branch_id: int) -> dict[str, Any]:
        """Resolve a dev branch to its merge request (a branch has at most
        one MR, ever -- the backend's existence check has no state filter).

        The list endpoint cannot filter server-side, so this lists and
        matches ``branches.branchFromId`` client-side. Raises ``NOT_FOUND``
        when the branch has no MR, with the create command as the next step.
        """
        project = self._project(alias)
        client = self._client_factory(project.stack_url, project.token)
        try:
            for mr in client.merge_requests.list():
                if _same_id((mr.get("branches") or {}).get("branchFromId"), branch_id):
                    return {"alias": alias, **_enrich_row(mr)}
        finally:
            client.close()
        raise KeboolaApiError(
            message=(
                f"Branch {branch_id} has no merge request in project '{alias}'. "
                "Create one with `kbagent merge-request create`."
            ),
            status_code=404,
            error_code=ErrorCode.NOT_FOUND,
            retryable=False,
        )

    def get_merge_request(
        self,
        alias: str,
        merge_request_id: int,
        include_activity_log: bool = False,
    ) -> dict[str, Any]:
        """Get an MR's detail with the full derived status.

        On top of the raw payload: ``derived_state``, ``merge_blockers`` +
        ``mergeable``, ``allowed_actions``, ``viewer`` and -- for open MRs --
        the live ``conflicts`` list (skipped for published/canceled/in_merge,
        where the source branch is gone or locked and the readiness question
        is moot). The derivations are informational; the merge 409 stays the
        authority.
        """
        project = self._project(alias)
        client = self._client_factory(project.stack_url, project.token)
        try:
            mr = client.merge_requests.get(
                merge_request_id, include_activity_log=include_activity_log
            )
            conflicts: list[dict[str, Any]] | None = None
            if (mr.get("state") or "") in self._OPEN_STATES:
                conflicts = client.merge_requests.conflicts(merge_request_id)
            # The verify_token call exists only to anchor the viewer
            # polyfill (its admin block is the caller's identity). Note the
            # detail and conflicts endpoints themselves require an admin
            # token (MergeRequestVoter denies a token with no admin) -- a
            # scoped token fails above before viewer is ever derived; the
            # None-flags path stays as defense in depth. Once the
            # server serializes `viewer` (DMD-1988), derive_viewer never
            # reads admin_id -- so skip the call and its cost with it. A
            # scoped token has no admin identity -> flags are None, not
            # False.
            admin_id: int | None = None
            if not isinstance(mr.get("viewer"), dict):
                admin_id = client.verify_token().admin_id
        finally:
            client.close()

        blockers = derive_merge_blockers(mr, conflicts)
        detail: dict[str, Any] = {
            "alias": alias,
            **mr,
            "derived_state": derive_state(mr),
            "merge_blockers": blockers,
            "mergeable": not blockers and conflicts is not None,
            "allowed_actions": derive_allowed_actions(mr),
            "viewer": derive_viewer(mr, admin_id),
        }
        if conflicts is not None:
            detail["conflicts"] = conflicts
            detail["conflicts_count"] = len(conflicts)
        return detail

    # -- Writes: create / update / review transitions --------------------------

    def create_merge_request(
        self,
        alias: str,
        branch_from_id: int,
        title: str,
        description: str | None = None,
        reviewer_ids: list[int] | None = None,
        auto_merge_strategy: str | None = None,
        auto_merge_at: str | None = None,
        external_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a merge request from ``branch_from_id`` into the default branch.

        The target is always the default branch (the backend rejects any
        other), so the service resolves its id itself. The source branch is
        an explicit parameter -- Layer 1 resolves it via the house
        ``resolve_branch()`` idiom (``--branch`` -> ``active_branch_id`` ->
        readable error) and the output must state which branch the MR was
        created from. A source branch can have at most one MR, ever; a
        second create answers 404 server-side.
        """
        project = self._project(alias)
        client = self._client_factory(project.stack_url, project.token)
        try:
            self._require_merge_requests_feature(client)
            default_branch_id = self._default_branch_id(client, alias)
            if branch_from_id == default_branch_id:
                raise ConfigError(
                    f"Branch {branch_from_id} is the default (production) branch of "
                    f"project '{alias}'. A merge request merges a development branch "
                    "into it -- create one with `kbagent branch create`, or pass "
                    "--branch with a dev branch id."
                )
            mr = client.merge_requests.create(
                branch_from_id=branch_from_id,
                branch_into_id=default_branch_id,
                title=title,
                description=description,
                reviewer_ids=reviewer_ids,
                auto_merge_strategy=auto_merge_strategy,
                auto_merge_at=auto_merge_at,
                external_id=external_id,
            )
        finally:
            client.close()
        return {
            "alias": alias,
            "branch_from_id": branch_from_id,
            "branch_into_id": default_branch_id,
            **_enrich_row(mr),
        }

    def update_merge_request(
        self,
        alias: str,
        merge_request_id: int,
        title: str | None = None,
        description: str | None = None,
        reviewer_ids: list[int] | None = None,
        auto_merge_strategy: str | None = None,
        auto_merge_at: str | None = None,
        external_id: str | None = None,
    ) -> dict[str, Any]:
        """Update an MR's metadata. ``None`` = leave unchanged (the API cannot
        clear a field to null; an empty string clears description/externalId
        server-side)."""
        project = self._project(alias)
        client = self._client_factory(project.stack_url, project.token)
        try:
            self._require_merge_requests_feature(client)
            mr = client.merge_requests.update(
                merge_request_id,
                title=title,
                description=description,
                reviewer_ids=reviewer_ids,
                auto_merge_strategy=auto_merge_strategy,
                auto_merge_at=auto_merge_at,
                external_id=external_id,
            )
        finally:
            client.close()
        return {"alias": alias, **_enrich_row(mr)}

    def request_review(self, alias: str, merge_request_id: int) -> dict[str, Any]:
        """Move the MR from ``development`` to review.

        With the non-SOX default of 0 required approvals the MR lands
        straight in ``approved`` (the backend auto-applies finish_review) --
        and a merge from ``development`` skips this step entirely, so the
        happy path never needs it.
        """
        project = self._project(alias)
        client = self._client_factory(project.stack_url, project.token)
        try:
            self._require_merge_requests_feature(client)
            mr = client.merge_requests.request_review(merge_request_id)
        finally:
            client.close()
        return {"alias": alias, **_enrich_row(mr)}

    def approve(self, alias: str, merge_request_id: int) -> dict[str, Any]:
        """Add the caller's approval."""
        project = self._project(alias)
        client = self._client_factory(project.stack_url, project.token)
        try:
            self._require_merge_requests_feature(client)
            mr = client.merge_requests.approve(merge_request_id)
        finally:
            client.close()
        return {"alias": alias, **_enrich_row(mr)}

    def request_changes(
        self, alias: str, merge_request_id: int, reason: str | None = None
    ) -> dict[str, Any]:
        """Send the MR back to ``development`` (approvals are deleted).

        Also the closest thing to closing an MR: the backend has no cancel
        endpoint, and the UI's "cancel" is exactly this call made by the
        creator on their own MR (rendered as Closed; derived_state mirrors
        that). ``reason`` is capped at 1000 characters server-side.
        """
        project = self._project(alias)
        client = self._client_factory(project.stack_url, project.token)
        try:
            self._require_merge_requests_feature(client)
            mr = client.merge_requests.request_changes(merge_request_id, reason=reason)
        finally:
            client.close()
        return {"alias": alias, **_enrich_row(mr)}

    def _default_branch_id(self, client: KeboolaClient, alias: str) -> int:
        """Resolve the project's default branch id (the only legal MR target)."""
        branch_id = find_default_branch_id(client.list_dev_branches())
        if branch_id is not None:
            return branch_id
        raise KeboolaApiError(
            message=f"Project '{alias}' reports no default branch -- cannot target a merge request.",
            status_code=0,
            error_code=ErrorCode.API_ERROR,
            retryable=False,
        )

    # -- Merge ------------------------------------------------------------------

    _NOT_READY_CODE = "storage.mergeRequests.notReadyToMerge"
    # MergeValidationException's string code (ExceptionConverter serializes
    # it top-level as `code`, with the conflicting configs in `params.errors`).
    _CONFLICT_CODE = "storage.mergeRequests.validation"

    def merge(self, alias: str, merge_request_id: int) -> dict[str, Any]:
        """Merge the MR into the default branch and clean local references.

        Waits for the merge Storage job (Layer 3 awaits with
        MERGE_JOB_MAX_WAIT; a failed merge raises STORAGE_JOB_FAILED and the
        MR rolls back to ``approved``). Works straight from ``development``
        when approvals are satisfied -- the backend auto-applies skip_review.

        The merge 409 is remapped onto its two wire shapes (the RFC's
        decision; docs/error-codes.md). Both carry a machine string code
        (ExceptionConverter serializes it top-level as ``code``):

        - ``storage.mergeRequests.notReadyToMerge`` (project merge lock /
          wrong state / another MR processing) -> ``MR_NOT_READY_TO_MERGE``,
          retryable -- all three causes are transient.
        - ``storage.mergeRequests.validation`` is the conflict validation ->
          ``MR_MERGE_CONFLICT``, not retryable; the conflicting
          configurations arrive in the 409's own ``params.errors`` and are
          passed through in details. A code-less 409 is treated as a
          conflict too (older stacks), but a 409 with any OTHER code passes
          through unmapped -- never mislabeled as a conflict.

        A successful merge always also deletes the source branch -- as a
        second async job with no handle, so the result says the branch "is
        being deleted", never that it is gone. Local cleanup mirrors
        ``BranchService.delete_branch``: reset ``active_branch_id`` only if
        it pointed at the merged branch, and unlink any sync
        ``branch-mapping.json`` entry. Cleanup is best-effort -- the merge
        already happened, so a cleanup failure degrades to a warning in the
        result instead of failing the command.
        """
        from ..sync.branch_mapping import cleanup_branch_id_from_mapping

        project = self._project(alias)
        client = self._client_factory(project.stack_url, project.token)
        try:
            self._require_merge_requests_feature(client)
            # branchFromId is nullable once the MR is published -- capture it
            # from the pre-merge payload, not the post-merge one. Coerced to
            # int so the was_active comparison and the mapping cleanup below
            # cannot be defeated by a string-serialized wire id (the payload
            # mixes int and str ids -- the reason _same_id exists).
            raw_branch_from = (
                client.merge_requests.get(merge_request_id).get("branches") or {}
            ).get("branchFromId")
            branch_from_id = int(raw_branch_from) if raw_branch_from is not None else None
            try:
                job = client.merge_requests.merge(merge_request_id)
            except KeboolaApiError as exc:
                self._remap_merge_conflict(exc)
                raise
        finally:
            client.close()

        was_active = branch_from_id is not None and project.active_branch_id == branch_from_id
        mapping_cleanup: dict[str, Any] | None = None
        cleanup_warnings: list[str] = []
        try:
            if was_active:
                self._config_store.set_project_branch(alias, None)
            if branch_from_id is not None:
                mapping_cleanup = cleanup_branch_id_from_mapping(branch_from_id)
        except Exception as exc:
            # a failed local cleanup must not turn it into a failed command.
            logger.warning("Post-merge cleanup failed: %s", exc)
            cleanup_warnings.append(f"Post-merge cleanup failed: {exc}")

        results = job.get("results")
        mr_after: dict[str, Any] = results if isinstance(results, dict) else {}

        message_parts = [f"Merge request {merge_request_id} merged into production."]
        if branch_from_id is not None:
            message_parts.append(
                f"Source branch {branch_from_id} is being deleted (a separate async "
                "job -- it may still briefly exist)."
            )
        if was_active:
            message_parts.append("Active branch reset to main.")
        if mapping_cleanup:
            unlinked = ", ".join(mapping_cleanup["git_branches_unlinked"])
            message_parts.append(f"Unlinked git branch(es): {unlinked}.")

        result: dict[str, Any] = {
            "alias": alias,
            "merge_request_id": merge_request_id,
            "branch_from_id": branch_from_id,
            "was_active": was_active,
            "job": job,
            "message": " ".join(message_parts),
        }
        if mr_after.get("state"):
            result["state"] = mr_after["state"]
            result["derived_state"] = derive_state(mr_after)
        if mapping_cleanup:
            result["mapping_cleanup"] = mapping_cleanup
        if cleanup_warnings:
            result["cleanup_warnings"] = cleanup_warnings
        return result

    def _remap_merge_conflict(self, exc: KeboolaApiError) -> None:
        """Raise the RFC's dedicated error for a known merge 409; return for others.

        Only this call site knows the 409 came from the merge endpoint --
        which is why the mapping cannot live in http_base (see
        docs/merge-requests-layer2.md). Both shapes match on the top-level
        ``code`` of the error body (surfaced as ``details.api_error_code``);
        a code-less 409 falls back to the conflict interpretation for older
        stacks, but a 409 carrying any *other* code passes through unmapped
        rather than being confidently mislabeled a conflict.
        """
        if exc.status_code != 409:
            return
        code = exc.details.get("api_error_code")
        if code == self._NOT_READY_CODE:
            raise KeboolaApiError(
                message=(
                    f"{exc.message} The merge lock, MR state or a concurrently "
                    "processing merge request blocks the merge -- these are "
                    "transient; retry once it clears."
                ),
                status_code=409,
                error_code=ErrorCode.MR_NOT_READY_TO_MERGE,
                retryable=True,
                details=exc.details,
            ) from exc
        if code == self._CONFLICT_CODE or code is None:
            # The 409 already carries the conflicting configurations in
            # params.errors (surfaced as details.api_error_params) -- keep
            # them so the caller does not need a second round trip; the
            # conflicts command remains the way to re-inspect later.
            raise KeboolaApiError(
                message=(
                    f"{exc.message} Configurations changed on both branches. Inspect "
                    "them with `kbagent merge-request conflicts`, resolve each one, "
                    "then merge again (conflicts are re-validated live on every "
                    "attempt)."
                ),
                status_code=409,
                error_code=ErrorCode.MR_MERGE_CONFLICT,
                retryable=False,
                details=exc.details,
            ) from exc

    # -- Conflicts / diff / resolution --------------------------------------------

    # Content-bearing keys of a diff side's ``diff`` envelope -- what a
    # three-way comparison is about. Wire truth (ConfigurationDiffData,
    # verified against connection): each side serializes as
    # ``{version, isDeleted, diff: {name, description, changeDescription,
    # isDisabled, configuration, rows}}`` -- content nested under ``diff``,
    # version/deletion as side metadata. ``changeDescription`` is excluded:
    # it is a per-version commit message, not content to resolve (the rebase
    # takes its own ``change_description``).
    _DIFF_CONTENT_KEYS = ("name", "description", "configuration", "isDisabled", "rows")

    def list_conflicts(self, alias: str, merge_request_id: int) -> dict[str, Any]:
        """List the configurations conflicting between the MR's branches.

        Conflicts are computed live by the backend on every call (and on
        every merge attempt), so rebasing each listed config is sufficient --
        there is no MR-level re-validate step.
        """
        project = self._project(alias)
        client = self._client_factory(project.stack_url, project.token)
        try:
            conflicts = client.merge_requests.conflicts(merge_request_id)
        finally:
            client.close()
        return {
            "alias": alias,
            "merge_request_id": merge_request_id,
            "count": len(conflicts),
            "conflicts": conflicts,
        }

    def get_config_diff(
        self, alias: str, component_id: str, config_id: str, branch_id: int
    ) -> dict[str, Any]:
        """Three-way diff of one config, flattened to a per-path classification.

        No three panes: each touched path is tagged ``changed_by`` --
        ``ours`` (only the dev branch changed it), ``theirs`` (only
        production), or ``both``. A ``both`` row where the two sides agree on
        the identical value additionally carries ``agreed: true`` -- both
        sides moved, but there is nothing to decide; the actual conflict
        hotspots are the ``both`` rows without it. ``rows`` compares
        wholesale (row-level three-way diffing is not attempted).

        Deletions do not show up as paths: a soft-deleted side is flagged by
        the top-level ``ours_deleted`` / ``theirs_deleted`` booleans instead
        (``None`` = the side does not exist at all). ``onto_version`` is the
        default-branch version a rebase re-anchors onto -- the
        ``theirs.version`` trap spelled out once, here.
        """
        project = self._project(alias)
        client = self._client_factory(project.stack_url, project.token)
        try:
            diff = client.get_config_diff(component_id, config_id, branch_id)
        finally:
            client.close()
        theirs = diff.get("theirs") or {}
        ours = diff.get("ours")
        return {
            "alias": alias,
            "component_id": component_id,
            "config_id": config_id,
            "branch_id": branch_id,
            "onto_version": theirs.get("version"),
            "ours_deleted": bool(ours.get("isDeleted")) if ours is not None else None,
            "theirs_deleted": (
                bool(theirs.get("isDeleted")) if diff.get("theirs") is not None else None
            ),
            "changes": self._classify_three_way(diff),
            "diff": diff,
        }

    def _classify_three_way(self, diff: dict[str, Any]) -> list[dict[str, Any]]:
        """Intersect the two pairwise diffs (base->ours, base->theirs) per path."""

        def content(side: dict[str, Any] | None) -> dict[str, Any]:
            # The side's content lives in its nested ``diff`` envelope (wire
            # truth above); a null side contributes nothing.
            envelope = (side or {}).get("diff") or {}
            return {key: envelope[key] for key in self._DIFF_CONTENT_KEYS if key in envelope}

        base = content(diff.get("base"))
        ours_entries = {e.path: e for e in compute_diff_entries(base, content(diff.get("ours")))}
        theirs_entries = {
            e.path: e for e in compute_diff_entries(base, content(diff.get("theirs")))
        }

        def side_value(entry: DiffEntry | None, base_val: Any) -> Any:
            # A side that changed the path shows its own value (None when
            # it REMOVED the key -- never the base value); a side with no
            # entry did not touch the path and still holds the base.
            if entry is not None:
                return entry.new if entry.new_present else None
            return base_val

        changes: list[dict[str, Any]] = []
        for path in sorted(set(ours_entries) | set(theirs_entries)):
            ours_entry = ours_entries.get(path)
            theirs_entry = theirs_entries.get(path)
            reference = ours_entry or theirs_entry
            assert reference is not None  # path came from one of the two maps
            changed_by = (
                "both" if ours_entry and theirs_entry else ("ours" if ours_entry else "theirs")
            )
            base_value = reference.old if reference.old_present else None
            change: dict[str, Any] = {
                "path": path,
                "changed_by": changed_by,
                "base": base_value,
                "ours": side_value(ours_entry, base_value),
                "theirs": side_value(theirs_entry, base_value),
            }
            if changed_by == "both":
                # Identical independent changes are agreement, not a
                # conflict hotspot -- flag them so renderers can demote them.
                assert ours_entry is not None and theirs_entry is not None
                change["agreed"] = (
                    ours_entry.new_present == theirs_entry.new_present
                    and side_value(ours_entry, base_value) == side_value(theirs_entry, base_value)
                )
            changes.append(change)
        return changes

    def resolve_conflict(
        self,
        alias: str,
        merge_request_id: int,
        component_id: str,
        config_id: str,
        take: str | None = None,
        resolved: dict[str, Any] | None = None,
        change_description: str | None = None,
    ) -> dict[str, Any]:
        """Resolve one conflicting config by rebasing it (uniformly -- every
        mode goes through the rebase endpoint, per the RFC decision; the UI's
        reset-to-default alternative for take=theirs is DMD-1987).

        The branch is NOT a parameter: it is derived from the merge request
        itself (``branches.branchFromId``), so the conflict-set guard and the
        branch being written to can never disagree -- a caller-supplied
        branch id could point the rebase at an unrelated dev branch that the
        guard never checked (rebase REPLACES; that would be silent data
        loss).

        Modes (exactly one of ``take`` / ``resolved``):

        - ``take="ours"``: keep the dev-branch content, re-anchored onto the
          production version.
        - ``take="theirs"``: adopt the production content (the config stays
          in the MR's changeset; the merge then writes a content-no-op).
        - ``take="delete"``: the ``{"version": N, "diff": {}}`` tombstone.
          A take of a side whose ``isDeleted`` is true collapses to this
          resolution too -- "production deleted it, dev changed it" and its
          mirror are live conflict shapes.
        - ``resolved={...}``: a caller-authored three-way merge -- a FLAT
          body that must carry ``name``, ``rows`` and ``configuration``
          explicitly (rebase REPLACES; a missing key would silently wipe
          data, so it is refused instead of defaulted).

        The config must be in the MR's live conflict set; ``version`` is
        taken from the diff's ``theirs.version`` (the default-branch version
        being re-anchored onto). Rebasing every conflicting config makes the
        MR mergeable -- no re-validate step exists or is needed.
        """
        if (take is None) == (resolved is None):
            raise ConfigError("Pass exactly one of take=ours|theirs|delete or a resolved body.")
        if take is not None and take not in ("ours", "theirs", "delete"):
            raise ConfigError(f"Unknown take mode {take!r}: use ours, theirs or delete.")

        project = self._project(alias)
        client = self._client_factory(project.stack_url, project.token)
        try:
            self._require_merge_requests_feature(client)
            branch_id = self._branch_from_id_of(client, merge_request_id)
            self._require_in_conflict_set(client, merge_request_id, component_id, config_id)
            diff = client.get_config_diff(component_id, config_id, branch_id)
            theirs = diff.get("theirs") or {}
            onto_version = theirs.get("version")
            if onto_version is None:
                raise KeboolaApiError(
                    message=(
                        f"The diff of {component_id}/{config_id} has no theirs side -- "
                        "cannot determine the default-branch version to rebase onto."
                    ),
                    status_code=0,
                    error_code=ErrorCode.VALIDATION_ERROR,
                    retryable=False,
                )

            # Normalize the three sources of a replace body onto one flat
            # shape: a take side contributes its nested ``diff`` envelope, a
            # caller-authored body is already flat.
            body: dict[str, Any] | None = resolved
            resolution = "custom"
            if take == "delete":
                body, resolution = None, "delete"
            elif take is not None:
                side = diff.get("ours") if take == "ours" else diff.get("theirs")
                if side is None or side.get("isDeleted"):
                    # Taking a deleted (or never-existing) side IS the delete
                    # resolution -- symmetric for ours and theirs.
                    body, resolution = None, "delete"
                else:
                    body, resolution = side.get("diff") or {}, take

            if body is None:
                configuration = client.rebase_config_delete(
                    component_id, config_id, branch_id, version=onto_version
                )
            else:
                # `name` must also be non-empty: the diff envelope declares
                # it nullable, but the rebase validator requires a non-empty
                # trimmed string -- a null would sail through a bare presence
                # check straight into a server 400.
                missing = [key for key in ("name", "rows", "configuration") if key not in body]
                if "name" not in missing and not str(body.get("name") or "").strip():
                    missing.insert(0, "name")
                if missing:
                    if take is not None:
                        # The diff side's envelope is server-produced and its
                        # schema marks all content keys required -- a hole
                        # here is a backend contract violation, not caller
                        # error. Point at the manual path as the workaround.
                        raise KeboolaApiError(
                            message=(
                                f"The diff's {take} side carries no "
                                f"{', '.join(missing)} -- cannot compose a replace "
                                "body from it. Author the resolution manually and "
                                "pass it as a resolved body instead."
                            ),
                            status_code=0,
                            error_code=ErrorCode.VALIDATION_ERROR,
                            retryable=False,
                        )
                    raise ConfigError(
                        "A resolved body must spell out the full replaced content "
                        f"(rebase REPLACES): missing {', '.join(missing)}."
                    )
                configuration = client.rebase_config(
                    component_id,
                    config_id,
                    branch_id,
                    version=onto_version,
                    name=body["name"],
                    rows=body["rows"],
                    configuration=body["configuration"],
                    is_disabled=bool(body.get("isDisabled", False)),
                    description=body.get("description"),
                    change_description=change_description,
                )
        finally:
            client.close()

        return {
            "alias": alias,
            "merge_request_id": merge_request_id,
            "component_id": component_id,
            "config_id": config_id,
            "branch_id": branch_id,
            "resolution": resolution,
            "onto_version": onto_version,
            "configuration": configuration,
        }

    def _branch_from_id_of(self, client: KeboolaClient, merge_request_id: int) -> int:
        """The MR's source branch id -- the only branch a resolution may write to.

        The null check is best-effort, not airtight: `branchFromId` is nulled
        by the FK's ON DELETE SET NULL when the source branch row is deleted,
        and that deletion is a separate async job -- a freshly published MR
        can still carry the id for a while. Harmless: the rebase then fails
        server-side (the MR is no longer open), it just fails later.
        """
        mr = client.merge_requests.get(merge_request_id)
        branch_from_id = (mr.get("branches") or {}).get("branchFromId")
        if branch_from_id is None:
            raise KeboolaApiError(
                message=(
                    f"Merge request {merge_request_id} has no source branch (state: "
                    f"{mr.get('state', 'unknown')}) -- a published or canceled MR "
                    "cannot be resolved."
                ),
                status_code=0,
                error_code=ErrorCode.VALIDATION_ERROR,
                retryable=False,
            )
        return int(branch_from_id)

    def _require_in_conflict_set(
        self, client: KeboolaClient, merge_request_id: int, component_id: str, config_id: str
    ) -> None:
        """Refuse to rebase a config the MR does not list as conflicting."""
        for conflict in client.merge_requests.conflicts(merge_request_id):
            if conflict.get("componentId") == component_id and str(
                conflict.get("configurationId")
            ) == str(config_id):
                return
        raise KeboolaApiError(
            message=(
                f"{component_id}/{config_id} is not in merge request "
                f"{merge_request_id}'s conflict set -- nothing to resolve. "
                "See `kbagent merge-request conflicts` for the current set."
            ),
            status_code=0,
            error_code=ErrorCode.VALIDATION_ERROR,
            retryable=False,
        )
