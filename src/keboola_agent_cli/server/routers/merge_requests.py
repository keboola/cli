"""Merge-request endpoints -- the REST mirror of ``kbagent merge-request *``.

1:1 with the CLI group (CONTRIBUTING: every non-terminal command has a route),
plus one route the CLI hides behind an omitted ``--merge-request-id``:
``GET /{project}/by-branch/{branch_id}`` -- over HTTP there is no active-branch
idiom, so the branch->MR resolver is exposed directly (registered as the
serve-only ``merge-request.by-branch``). Skipped on purpose: ``diff --output
PATH`` writes to the host's disk; ``GET .../diff`` returns the same payload
(``resolution_candidate`` included) and the caller writes its own file.

**Every route enforces the permission policy** (``Depends(require_permission)``),
which most routers do not yet do. Here it is not optional: the CLI classifies
``merge`` as destructive and escalates arming auto-merge and the transitions
on an armed MR to destructive too (``permissions.FLAG_ESCALATIONS``); without
the same checks over HTTP that whole analysis would be decorative for
``serve`` callers. The static class is a route dependency; the state/flag-
derived escalations are evaluated in the route body, where the request body
(and, via one row GET, the MR's ``autoMergeStrategy``) is known. Design
record: ``docs/merge-requests-layer1.md``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ...errors import ErrorCode, KeboolaApiError
from ...permissions import PermissionEngine
from ...services.merge_request_service import (
    AUTO_MERGE_DISARMED,
    STATE_FILTER_VOCABULARY,
    TAKE_MODES,
    arms_auto_merge,
    validate_auto_merge_flags,
)
from ..dependencies import ServiceRegistry, get_permission_engine, get_registry, require_permission

router = APIRouter(prefix="/merge-requests", tags=["merge-requests"])

_REASON_MAX_LENGTH = 1000  # MergeRequestRejectRequest::REASON_MAX_LENGTH, same cap as the CLI


def _perm(operation: str) -> Any:
    return Depends(require_permission(f"merge-request.{operation}"))


def _invalid(message: str) -> KeboolaApiError:
    # INVALID_ARGUMENT is in app.py's _CALLER_REFUSAL_CODES -> HTTP 400, the
    # REST twin of the CLI's exit 2.
    return KeboolaApiError(
        message=message, status_code=400, error_code=ErrorCode.INVALID_ARGUMENT, retryable=False
    )


def _arming(strategy: str | None, at: str | None) -> bool:
    """400 on a bad strategy / pairing (the service owns the rule, so the CLI and
    this router cannot drift); return whether the body ARMS auto-merge."""
    problem = validate_auto_merge_flags(strategy, at)
    if problem:
        raise _invalid(problem)
    return arms_auto_merge(strategy)


def _escalate_if_armed(
    registry: ServiceRegistry,
    engine: PermissionEngine,
    project: str,
    merge_request_id: int,
    operation: str,
) -> None:
    """request-review / approve / resolve on an MR armed for auto-merge cause a
    production merge; apply the same state-derived escalation the CLI does.
    One row GET, never the three-call detail."""
    row = registry.merge_request.get_merge_request_row(project, merge_request_id)
    if (row.get("autoMergeStrategy") or AUTO_MERGE_DISARMED) != AUTO_MERGE_DISARMED:
        engine.check_or_raise(f"merge-request.{operation} --auto-merge-armed")


# -- Bodies ------------------------------------------------------------------------------------


class MergeRequestCreate(BaseModel):
    branch_from_id: int
    title: str
    description: str | None = None
    reviewer_ids: list[int] | None = None
    auto_merge_strategy: str | None = None
    auto_merge_at: str | None = None
    external_id: str | None = None


class MergeRequestUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    reviewer_ids: list[int] | None = None
    auto_merge_strategy: str | None = None
    auto_merge_at: str | None = None
    external_id: str | None = None


class RequestChanges(BaseModel):
    reason: str | None = None


class ResolveConflict(BaseModel):
    take: str | None = None
    resolved: dict[str, Any] | None = None
    change_description: str | None = None


# -- Reads -------------------------------------------------------------------------------------


@router.get("/{project}", summary="List merge requests", dependencies=[_perm("list")])
def list_merge_requests(
    project: str,
    state: str | None = Query(None, description="Client-side state filter"),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List the project's merge requests, newest first. Mirrors `kbagent merge-request list`."""
    if state is not None and state.lower() not in STATE_FILTER_VOCABULARY:
        raise _invalid(
            f"Unknown state {state!r}. Accepted: {', '.join(sorted(STATE_FILTER_VOCABULARY))}."
        )
    return registry.merge_request.list_merge_requests(project, state=state)


# Declared BEFORE /{project}/{merge_request_id}: FastAPI matches in order, and
# the int path type would only turn 'by-branch' into a 422 instead of a match.
@router.get(
    "/{project}/by-branch/{branch_id}",
    summary="Find the merge request of a branch",
    dependencies=[_perm("by-branch")],
)
def find_for_branch(
    project: str, branch_id: int, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """The branch->MR resolver the CLI hides behind an omitted --merge-request-id
    (a branch has at most one merge request, ever). Serve-only."""
    return registry.merge_request.find_merge_request_for_branch(project, branch_id)


@router.get(
    "/{project}/{merge_request_id}", summary="Merge request detail", dependencies=[_perm("detail")]
)
def get_merge_request(
    project: str,
    merge_request_id: int,
    activity_log: bool = Query(False, description="Include the activity log"),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Detail with derived status, blockers, viewer flags and live conflicts. Mirrors `kbagent merge-request detail`."""
    return registry.merge_request.get_merge_request(
        project, merge_request_id, include_activity_log=activity_log
    )


@router.get(
    "/{project}/{merge_request_id}/conflicts",
    summary="List conflicts",
    dependencies=[_perm("conflicts")],
)
def list_conflicts(
    project: str, merge_request_id: int, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Configurations changed on both sides, computed live. Mirrors `kbagent merge-request conflicts`."""
    return registry.merge_request.list_conflicts(project, merge_request_id)


@router.get(
    "/{project}/{merge_request_id}/diff/{component_id}/{config_id}",
    summary="Three-way diff of one conflicting configuration",
    dependencies=[_perm("diff")],
)
def get_config_diff(
    project: str,
    merge_request_id: int,
    component_id: str,
    config_id: str,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Per-path classification plus `resolution_candidate` (the CLI's `--output`
    content -- write it yourself). Mirrors `kbagent merge-request diff`."""
    return registry.merge_request.get_config_diff(
        project, merge_request_id, component_id, config_id
    )


# -- Writes ------------------------------------------------------------------------------------


@router.post("/{project}", summary="Create a merge request", dependencies=[_perm("create")])
def create_merge_request(
    project: str,
    body: MergeRequestCreate,
    registry: ServiceRegistry = Depends(get_registry),
    engine: PermissionEngine = Depends(get_permission_engine),
) -> dict[str, Any]:
    """Open a merge request from a dev branch into production. Arming auto-merge
    is destructive (a delayed production merge). Mirrors `kbagent merge-request create`."""
    if _arming(body.auto_merge_strategy, body.auto_merge_at):
        engine.check_or_raise("merge-request.create --auto-merge-strategy")
    return registry.merge_request.create_merge_request(
        project,
        branch_from_id=body.branch_from_id,
        title=body.title,
        description=body.description,
        reviewer_ids=body.reviewer_ids,
        auto_merge_strategy=body.auto_merge_strategy,
        auto_merge_at=body.auto_merge_at,
        external_id=body.external_id,
    )


@router.put(
    "/{project}/{merge_request_id}",
    summary="Update a merge request",
    dependencies=[_perm("update")],
)
def update_merge_request(
    project: str,
    merge_request_id: int,
    body: MergeRequestUpdate,
    registry: ServiceRegistry = Depends(get_registry),
    engine: PermissionEngine = Depends(get_permission_engine),
) -> dict[str, Any]:
    """Omitted fields stay; an empty string clears description/external_id;
    reviewer_ids replaces the set. Mirrors `kbagent merge-request update`."""
    if all(
        v is None
        for v in (
            body.title,
            body.description,
            body.reviewer_ids,
            body.auto_merge_strategy,
            body.auto_merge_at,
            body.external_id,
        )
    ):
        raise _invalid("Nothing to update: pass at least one field.")
    if _arming(body.auto_merge_strategy, body.auto_merge_at):
        engine.check_or_raise("merge-request.update --auto-merge-strategy")
    return registry.merge_request.update_merge_request(
        project,
        merge_request_id,
        title=body.title,
        description=body.description,
        reviewer_ids=body.reviewer_ids,
        auto_merge_strategy=body.auto_merge_strategy,
        auto_merge_at=body.auto_merge_at,
        external_id=body.external_id,
    )


@router.post(
    "/{project}/{merge_request_id}/request-review",
    summary="Send for review",
    dependencies=[_perm("request-review")],
)
def request_review(
    project: str,
    merge_request_id: int,
    registry: ServiceRegistry = Depends(get_registry),
    engine: PermissionEngine = Depends(get_permission_engine),
) -> dict[str, Any]:
    """On a 0-approval project this lands directly in `approved`. Destructive
    when the MR is armed for auto-merge. Mirrors `kbagent merge-request request-review`."""
    _escalate_if_armed(registry, engine, project, merge_request_id, "request-review")
    return registry.merge_request.request_review(project, merge_request_id)


@router.post(
    "/{project}/{merge_request_id}/approve", summary="Approve", dependencies=[_perm("approve")]
)
def approve(
    project: str,
    merge_request_id: int,
    registry: ServiceRegistry = Depends(get_registry),
    engine: PermissionEngine = Depends(get_permission_engine),
) -> dict[str, Any]:
    """Only from `in_review`; 422 on a 0-approval project. Destructive when the
    MR is armed for auto-merge. Mirrors `kbagent merge-request approve`."""
    _escalate_if_armed(registry, engine, project, merge_request_id, "approve")
    return registry.merge_request.approve(project, merge_request_id)


@router.post(
    "/{project}/{merge_request_id}/request-changes",
    summary="Request changes",
    dependencies=[_perm("request-changes")],
)
def request_changes(
    project: str,
    merge_request_id: int,
    body: RequestChanges | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Back to development, approvals removed; also the closest thing to closing.
    Mirrors `kbagent merge-request request-changes`."""
    reason = body.reason if body else None
    if reason is not None and len(reason) > _REASON_MAX_LENGTH:
        raise _invalid(f"reason is capped at {_REASON_MAX_LENGTH} characters (got {len(reason)}).")
    return registry.merge_request.request_changes(project, merge_request_id, reason=reason)


@router.post(
    "/{project}/{merge_request_id}/merge",
    summary="Merge into production",
    dependencies=[_perm("merge")],
)
def merge(
    project: str, merge_request_id: int, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Merges and deletes the source branch. SYNCHRONOUS: awaits the merge job for
    up to MERGE_JOB_MAX_WAIT (600 s) -- set your client/proxy timeout accordingly.
    Mirrors `kbagent merge-request merge`."""
    return registry.merge_request.merge(project, merge_request_id)


@router.post(
    "/{project}/{merge_request_id}/resolve/{component_id}/{config_id}",
    summary="Resolve one conflict",
    dependencies=[_perm("resolve")],
)
def resolve_conflict(
    project: str,
    merge_request_id: int,
    component_id: str,
    config_id: str,
    body: ResolveConflict,
    registry: ServiceRegistry = Depends(get_registry),
    engine: PermissionEngine = Depends(get_permission_engine),
) -> dict[str, Any]:
    """Exactly one of `take` (ours|theirs|delete) or `resolved` (the full replaced
    body -- start from the diff's `resolution_candidate`). Destructive when the
    MR is armed for auto-merge. Mirrors `kbagent merge-request resolve`."""
    if (body.take is None) == (body.resolved is None):
        raise _invalid("Pass exactly one of take (ours|theirs|delete) or resolved.")
    if body.take is not None and body.take not in TAKE_MODES:
        raise _invalid(f"Unknown take {body.take!r}: use {', '.join(TAKE_MODES)}.")
    _escalate_if_armed(registry, engine, project, merge_request_id, "resolve")
    return registry.merge_request.resolve_conflict(
        project,
        merge_request_id,
        component_id,
        config_id,
        take=body.take,
        resolved=body.resolved,
        change_description=body.change_description,
    )
