"""Merge-request endpoints, exposed as ``client.merge_requests`` (DMD-1701).

This module holds four pieces (see docs/merge-requests-layer3-rfc.md, D10):

- ``StorageRequester``: the FUTURE transport interface -- public method names,
  defined today. The client-split RFC (draft PR #595; not in this tree yet)
  builds the real transport under this seam later; until then the adapter
  below satisfies it.
- ``_ClientRequester``: temporary Adapter delegating to ``_CoreClient``'s
  protected methods. Dies the day a real transport object exists.
- ``MergeRequests``: the endpoint-family namespace. It never sees the client,
  only the Protocol -- which keeps it unit-testable against a stub requester
  and makes the future transport swap a one-line change in the mixin.
- ``_MergeRequestsMixin``: exposes the namespace as a cached property on
  ``KeboolaClient``.

Unlike the flat endpoint-family mixins, new endpoint families are added as
namespaces depending on ``StorageRequester``; existing flat families stay
flat unless deliberately migrated (RFC, normative intent under D10).

Every endpoint here is project-level (``isAvailableInBranch: false``), so no
path is ever branch-prefixed -- do not copy the ``branch_id or production``
prefix idiom from the sibling mixins. And unlike ``configs.py``'s
form-encoded idiom, request bodies MUST be JSON (``json=``, real nested
objects): the backend validators require real types (``branchFromId`` is
``Assert\\Type('int')``), and form-encoded values stay strings and fail
validation.
"""

import functools
from typing import Any, Protocol

import httpx

from ..constants import MERGE_JOB_MAX_WAIT, STORAGE_JOB_MAX_WAIT
from ..errors import ErrorCode, KeboolaApiError
from ._core import _CoreClient

_BASE = "/v2/storage/merge-request"

# ``MergeRequests.list`` shadows the ``list`` builtin inside the class body
# (annotations there would resolve to the method), so class-scope annotations
# spell list types via these module-level aliases.
_DictList = list[dict[str, Any]]
_IntList = list[int]


class StorageRequester(Protocol):
    """The FUTURE transport interface -- public method names, defined today.

    Keep this minimal: grow it only when a method needs another transport
    capability; every method added is a promise the future transport must
    keep (see the client-split RFC, draft PR #595).
    """

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response: ...

    def wait_for_storage_job(
        self, job: dict[str, Any], max_wait: float = STORAGE_JOB_MAX_WAIT
    ) -> dict[str, Any]: ...


class _ClientRequester:
    """Temporary Adapter: satisfies the Protocol by delegating to the client.

    Dies the day a real transport object exists.
    """

    def __init__(self, client: _CoreClient) -> None:
        self._client = client

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self._client._request(method, path, **kwargs)

    def wait_for_storage_job(
        self, job: dict[str, Any], max_wait: float = STORAGE_JOB_MAX_WAIT
    ) -> dict[str, Any]:
        return self._client._wait_for_storage_job(job, max_wait=max_wait)


def _optional_mr_fields(
    *,
    description: str | None,
    reviewer_ids: _IntList | None,
    auto_merge_strategy: str | None,
    auto_merge_at: str | None,
    external_id: str | None,
) -> dict[str, Any]:
    """Build the optional-field part of a create/update body.

    Shared so the two bodies cannot drift: only provided (non-None) fields
    are included, under their wire (camelCase) names. Keyword-only by
    signature: four of the five parameters are ``str | None``, so a
    positional transposition would type-check cleanly and only surface as a
    backend 422 -- the one drift mode this helper otherwise cannot catch.
    """
    body: dict[str, Any] = {}
    if description is not None:
        body["description"] = description
    if reviewer_ids is not None:
        body["reviewerIds"] = reviewer_ids
    if auto_merge_strategy is not None:
        body["autoMergeStrategy"] = auto_merge_strategy
    if auto_merge_at is not None:
        body["autoMergeAt"] = auto_merge_at
    if external_id is not None:
        body["externalId"] = external_id
    return body


class MergeRequests:
    """Merge-request endpoints, exposed as ``client.merge_requests``.

    Non-SOX "Branches 2.0" flow (``branches-merge-requests``): create ->
    request review -> approve -> merge. All paths are project-level and never
    branch-prefixed; all bodies are JSON (module docstring). Returns are raw
    parsed JSON, as everywhere in ``client/``.

    The pre-flight feature check (``has_feature(FEATURE_BRANCHES_MERGE_REQUESTS)``)
    is deliberately NOT done here -- a missing feature surfaces as a 403
    byte-for-byte identical to a role denial, so only a Layer 2 pre-flight
    can word the error (RFC, D9).
    """

    def __init__(self, requester: StorageRequester) -> None:  # never sees the client
        self._requester = requester

    def list(self) -> _DictList:
        """List the project's merge requests.

        GET /v2/storage/merge-request

        The endpoint declares no query parameters, so any state filtering is
        necessarily client-side (Layer 2's job).
        """
        return self._requester.request("GET", _BASE).json()

    def get(self, merge_request_id: int, include_activity_log: bool = False) -> dict[str, Any]:
        """Get a merge request's detail.

        GET /v2/storage/merge-request/{id}[?include=activityLog]

        Args:
            merge_request_id: Merge request ID.
            include_activity_log: When True, the response embeds the MR's
                activity log (``include=activityLog``).
        """
        params: dict[str, str] = {}
        if include_activity_log:
            params["include"] = "activityLog"
        return self._requester.request("GET", f"{_BASE}/{merge_request_id}", params=params).json()

    def conflicts(self, merge_request_id: int) -> _DictList:
        """List the configurations conflicting between the MR's branches.

        GET /v2/storage/merge-request/{id}/conflicts
        """
        return self._requester.request("GET", f"{_BASE}/{merge_request_id}/conflicts").json()

    def create(
        self,
        branch_from_id: int,
        branch_into_id: int,
        title: str,
        description: str | None = None,
        reviewer_ids: _IntList | None = None,
        auto_merge_strategy: str | None = None,
        auto_merge_at: str | None = None,
        external_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a merge request from a dev branch into the default branch.

        POST /v2/storage/merge-request (201 on success). Only provided
        (non-None) optional fields are sent. Body is JSON -- ``branchFromId``
        / ``branchIntoId`` must arrive as JSON numbers, ``reviewerIds`` as an
        array of integers; form encoding fails validation.

        The backend rejects a non-default target branch and a source branch
        that already has an MR (one MR per source branch, ever) -- both as
        404, not 400.

        Args:
            branch_from_id: Source dev branch ID.
            branch_into_id: Target (default) branch ID.
            title: MR title.
            description: Optional MR description.
            reviewer_ids: Optional reviewer admin IDs (server de-duplicates).
            auto_merge_strategy: ``immediately`` | ``scheduled`` | ``none``.
            auto_merge_at: ISO 8601 date-time; required by the backend when
                ``auto_merge_strategy`` is ``scheduled``.
            external_id: Free-form external reference (max 255 chars), e.g.
                a ticket ID.
        """
        body: dict[str, Any] = {
            "branchFromId": branch_from_id,
            "branchIntoId": branch_into_id,
            "title": title,
        }
        body.update(
            _optional_mr_fields(
                description=description,
                reviewer_ids=reviewer_ids,
                auto_merge_strategy=auto_merge_strategy,
                auto_merge_at=auto_merge_at,
                external_id=external_id,
            )
        )
        return self._requester.request("POST", _BASE, json=body).json()

    def update(
        self,
        merge_request_id: int,
        title: str | None = None,
        description: str | None = None,
        reviewer_ids: _IntList | None = None,
        auto_merge_strategy: str | None = None,
        auto_merge_at: str | None = None,
        external_id: str | None = None,
    ) -> dict[str, Any]:
        """Update an existing merge request.

        PUT /v2/storage/merge-request/{id}

        Only provided (non-None) fields are sent, as JSON (module docstring).
        See ``create`` for the fields' meaning. ``None`` means "leave
        unchanged" -- and that is also all the API can express: server-side,
        an explicit JSON null and an absent key are indistinguishable
        (``?? null`` mapping + ``!== null`` update guards), so no field can
        be cleared to null through this endpoint. Calling with no fields set
        PUTs ``{}``, which the backend treats as a no-op returning the MR.
        """
        body = _optional_mr_fields(
            description=description,
            reviewer_ids=reviewer_ids,
            auto_merge_strategy=auto_merge_strategy,
            auto_merge_at=auto_merge_at,
            external_id=external_id,
        )
        if title is not None:
            body["title"] = title
        return self._requester.request("PUT", f"{_BASE}/{merge_request_id}", json=body).json()

    def request_review(self, merge_request_id: int) -> dict[str, Any]:
        """Move the MR from ``development`` to ``in_review``.

        PUT /v2/storage/merge-request/{id}/request-review (no body)
        """
        return self._requester.request("PUT", f"{_BASE}/{merge_request_id}/request-review").json()

    def approve(self, merge_request_id: int) -> dict[str, Any]:
        """Add the caller's approval to the MR.

        PUT /v2/storage/merge-request/{id}/approve (no body)
        """
        return self._requester.request("PUT", f"{_BASE}/{merge_request_id}/approve").json()

    def request_changes(self, merge_request_id: int, reason: str | None = None) -> dict[str, Any]:
        """Send the MR back to ``development`` to be revised.

        PUT /v2/storage/merge-request/{id}/request-changes

        Deliberately not named ``reject``: the transition is not terminal,
        the MR returns to ``development`` for another round.

        Args:
            merge_request_id: Merge request ID.
            reason: Optional reason, capped at 1000 characters server-side.
        """
        body: dict[str, Any] = {}
        if reason is not None:
            body["reason"] = reason
        return self._requester.request(
            "PUT", f"{_BASE}/{merge_request_id}/request-changes", json=body
        ).json()

    def merge(self, merge_request_id: int) -> dict[str, Any]:
        """Merge an approved MR into the default branch (waits for the job).

        PUT /v2/storage/merge-request/{id}/merge answers 202 with a Storage
        job; like every Storage-job method in ``client/`` this polls it to a
        terminal state and returns the completed job dict, whose ``results``
        carry the MR including its change log. The wait budget is
        ``MERGE_JOB_MAX_WAIT`` (following the ``IMPORT_JOB_MAX_WAIT`` /
        ``EXPORT_JOB_MAX_WAIT`` precedent) -- merging a many-config branch
        can legitimately outlive the default 60 s storage-job budget. A
        failed merge (the MR rolls back to ``approved``) raises
        ``STORAGE_JOB_FAILED`` instead of masquerading as success -- also
        when the 202 body itself already carries a terminal error, which the
        shared poller would return as-is; the MR state stays pollable via
        ``get()``.

        Caveat: a successful merge always also deletes the source branch,
        but that runs as a second job enqueued by the first, with no job
        handle returned -- the await covers the merge outcome only. After a
        successful return the changes are in production and the branch is
        doomed but may still briefly exist; callers must not assume either
        way.

        The merge 409 has four causes in two response shapes (three "not
        ready" cases carry ``storage.mergeRequests.notReadyToMerge``, a
        conflict does not); mapping them is Layer 2's concern.
        """
        response = self._requester.request("PUT", f"{_BASE}/{merge_request_id}/merge")
        job = self._requester.wait_for_storage_job(response.json(), max_wait=MERGE_JOB_MAX_WAIT)
        if job.get("status") == "error":
            # _wait_for_storage_job raises on a polled error but returns an
            # ALREADY-terminal initial body as-is; without this guard a fast
            # synchronous failure would come back as a normal return value.
            raise KeboolaApiError(
                message=job.get("error", {}).get("message", "Merge job failed"),
                status_code=500,
                error_code=ErrorCode.STORAGE_JOB_FAILED,
                retryable=False,
            )
        return job


class _MergeRequestsMixin(_CoreClient):
    """Exposes the merge-request namespace on ``KeboolaClient``.

    ``cached_property`` rather than the house attr-initialized-to-None lazy
    pattern because it needs no ``__init__`` change (the client defines no
    ``__slots__``). Neither the namespace nor the adapter owns an HTTP
    client, base URL, or token; the client<->namespace reference cycle is
    harmless because resources are released by the explicit ``close()``.
    """

    @functools.cached_property
    def merge_requests(self) -> MergeRequests:
        return MergeRequests(_ClientRequester(self))
