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
from ..config_store import ConfigError
from ..constants import BRANCHES_MERGE_REQUESTS_FEATURE
from ..errors import ErrorCode, KeboolaApiError
from ..models import ProjectConfig
from .base import BaseService

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

# Mechanical action availability per raw state -- what the UI buttons gate on
# (approve/request-changes only in in_review|approved, send-for-review only in
# development; editing/rebasing is allowed in development, in_review and
# approved -- the dev branch is locked only while in_merge). `merge` appears
# for `development` because a non-SOX merge from there succeeds when approvals
# suffice (the backend auto-applies skip_review). Deliberately state-only:
# roles and features are the pre-flight's job client-side, and the backend can
# honor them once it serializes `allowedActions` (DMD-1988).
_ALLOWED_ACTIONS_BY_STATE: dict[str, tuple[str, ...]] = {
    "development": ("request_review", "merge", "update", "resolve_conflicts"),
    "in_review": ("approve", "request_changes", "update", "resolve_conflicts"),
    "approved": ("approve", "request_changes", "merge", "update", "resolve_conflicts"),
    "in_merge": (),
    "published": (),
    "canceled": (),
}


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
        raise ConfigError(
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
        (``published``, ...) keeps the row; matching is case-insensitive.
        """
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
                if (mr.get("branches") or {}).get("branchFromId") == branch_id:
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
            # verify_token is cheap (and refreshes the features cache); its
            # admin block anchors the viewer-relative flags. A scoped token
            # has no admin identity -> viewer flags are None, not False.
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
