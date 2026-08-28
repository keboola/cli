"""Keboola Metastore API client for the semantic layer.

Communicates with the Keboola Metastore at ``metastore.{stack-suffix}`` (derived
from the Storage API stack URL by replacing ``connection.`` with ``metastore.``
in the hostname). Same ``X-StorageApi-Token`` credential as the Storage API.

Inherits shared retry, timeout, and error handling from :class:`BaseHttpClient`.

Verified contract (probed 2026-05-14 against e2e-1143):

- ``GET  /api/v1/repository/{type}`` → 200 with body ``{"data": [item, ...]}``.
- ``POST /api/v1/repository/{type}`` → 201 with body ``{"data": {type, id,
  attributes, meta}}``. Envelope: ``{name, data, branch, schemaVersion, scope}``.
- ``DELETE /api/v1/repository/{type}/{id}`` → 204 empty body. Missing ID → 404
  with the standard error envelope.
- Duplicate ``name`` on POST → **409 Conflict** with message ``"Object with
  this name already exists in this project"`` (after go-monorepo PR #513).
  Legacy metastore deployments still return **500** with exception ``"Failed
  to create meta object"``. We normalize both into
  :data:`ErrorCode.ALREADY_EXISTS`.
- Error envelope has top-level ``error``, ``code``, ``exception``, ``status``,
  ``context.path``, and an ``errors[]`` list for 422 validation failures.

Scope/target-project contract (grounded in go-monorepo commit ``e4f62941``,
services/metastore -- Linear PSGO-140; read from source, **not yet probed
live**):

- Every item's ``scope`` (``"project"`` | ``"organization"`` | ``"targeted"``),
  ``targetProjectIds``, and ``scopeElevationRequestedAt`` live under the
  response's ``meta`` block, not ``attributes`` (server:
  ``MetaObjectResponse.JSONAPIMeta``).
- ``POST`` accepts ``scope``/``targetProjectIds`` in the create envelope.
  ``PUT /{id}`` (plain update) does **not** -- its server-side request struct
  (``MetaObjectUpdatePutRequest``) has no such fields, so scope/grants can
  only be set at creation or through the dedicated endpoints below.
- Elevating to organization scope is ``PATCH /{id}`` with body
  ``{"scope": "organization"}`` **only** -- it cannot be combined with a
  name/data change in the same request, is one-way (no downgrade endpoint
  exists), and requires the organization-admin role.
- ``PUT /{id}/target-projects`` **replaces the whole grant set** (not
  additive); only valid for an object created with ``scope="targeted"``.
  204 on success, no body.
- ``PUT``/``DELETE /{id}/scope-elevation-request`` self-service "request a
  step-up" flag: empty body, 200 with the updated item. Idempotent.
- ``GET /{type}/organization`` lists organization-visible items across
  projects; supports the same generic ``field[op]=value`` filter query
  language as every other list endpoint, plus bare ``limit=N``/``offset=N``.
"""

import logging
import re
from typing import Any, Literal

import httpx

from .errors import ErrorCode, KeboolaApiError
from .http_base import BaseHttpClient

logger = logging.getLogger(__name__)


# Pre-PSGO-282, the metastore's auth middleware collapsed EVERY project-scope
# resolution failure into this one opaque 401 string (go-monorepo
# ``services/metastore/internal/middleware/auth.go``, ``resolveProjectScope``
# -- the underlying error was logged server-side and discarded), because
# ``NewProjectDeps`` was called without ``WithoutMasterToken()``: unlike the
# Storage API, the metastore accepted ONLY a master (project admin) Storage
# token and every valid non-master token landed here (issue #711;
# A/B-verified live on us-east4.gcp: non-master token -> this 401 on every
# call, master token on the same stack -> 200).
#
# Fixed server-side in go-monorepo#596 (PSGO-282, merged 2026-08-31): reads
# now work with any valid, non-disabled, non-expired Storage token, writes
# still require the token to belong to a project admin, and a 401 now relays
# the Storage API's real error message instead of this generic string. A
# fixed metastore will not emit this exact phrase again -- this
# reclassification is kept only as a safety net for a deployment that has
# not rolled the fix out yet (or an older regional stack); the message below
# no longer claims every call needs a master token.
_PROJECT_SCOPE_401_EXCEPTION = "Failed to create project scope"

# Re-extracts the ``[exceptionId: ...]`` suffix `_raise_api_error` appended,
# so the reclassified message keeps the handle Keboola support traces by.
_EXCEPTION_ID_SUFFIX = re.compile(r" \[exceptionId: [^\]]+\]")


SemanticType = Literal[
    "semantic-model",
    "semantic-dataset",
    "semantic-metric",
    "semantic-relationship",
    "semantic-constraint",
    "semantic-glossary",
    "semantic-reference-data",
]


SEMANTIC_TYPES: tuple[str, ...] = (
    "semantic-model",
    "semantic-dataset",
    "semantic-metric",
    "semantic-relationship",
    "semantic-constraint",
    "semantic-glossary",
    "semantic-reference-data",
)


ObjectScope = Literal["project", "organization", "targeted"]

# Envelope fields kept constant across every POST (per metastore contract).
_ENVELOPE_BRANCH = "main"
# 1.1.0, not 1.0.0: every semantic-* schema's x-metastore.scope.supported is
# ["project"] ONLY at 1.0.0 -- 1.1.0 is what adds "organization"/"targeted"
# (go-monorepo migrations/schema/semantic-*_schema_1.1.0.json, diffed against
# 1.0.0 at commit e4f62941: purely additive x-metastore.acl/scope blocks, no
# `data` schema change, so this is safe for every existing caller). Sending
# scope="organization"/"targeted" against 1.0.0 gets a clean 400
# ErrScopeNotSupported from prepareCreateSchema -- the server resolves the
# EXACT version string sent, never silently upgrades it.
_ENVELOPE_SCHEMA_VERSION = "1.1.0"
_DEFAULT_SCOPE: ObjectScope = "project"


class MetastoreClient(BaseHttpClient):
    """HTTP client for the Keboola Metastore (semantic layer repository).

    Provides minimal verb-level primitives that the
    :class:`SemanticLayerService` composes into business operations. This
    client deliberately stays thin: no business logic, no model resolution,
    no in-memory caching. All such concerns live in the service layer.
    """

    SESSION_AUTH_FEATURE = "The Metastore Service (semantic layer)"

    def __init__(self, stack_url: str, token: str) -> None:
        self._stack_url = stack_url.rstrip("/")
        base_url = self._derive_service_url(self._stack_url, "metastore")
        headers = {
            "X-StorageApi-Token": token,
        }
        super().__init__(base_url=base_url, token=token, headers=headers)

    def __enter__(self) -> "MetastoreClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _do_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Funnel every metastore call through the project-scope-401 diagnosis.

        A 401 carrying ``"Failed to create project scope"`` was the
        metastore's pre-PSGO-282 master-token gate rejecting a valid
        non-master token (see :data:`_PROJECT_SCOPE_401_EXCEPTION`), not a
        bad credential and not a server fault to escalate -- so it is
        reclassified from the generic 401 mapping into
        :data:`ErrorCode.MISSING_MASTER_TOKEN`, mirroring the
        ``token create`` / ``config oauth-url`` pre-flight guards (#599).
        Kept at the request funnel (rather than one try/except per verb
        method) so no endpoint can miss it, but since PSGO-282 a fixed
        metastore only 401s this way on a WRITE against a non-admin token
        (reads succeed for any valid token) -- the remedy below reflects
        that, not a blanket master-token requirement.
        """
        try:
            return super()._do_request(method, path, **kwargs)
        except KeboolaApiError as exc:
            if exc.status_code != 401 or _PROJECT_SCOPE_401_EXCEPTION not in exc.message:
                raise
            id_match = _EXCEPTION_ID_SUFFIX.search(exc.message)
            id_suffix = id_match.group(0) if id_match else ""
            raise KeboolaApiError(
                message=(
                    f"The Metastore API (semantic layer) rejected the request with "
                    f"HTTP 401 {_PROJECT_SCOPE_401_EXCEPTION!r}. This is the "
                    f"metastore's project-admin gate: writes need a token whose "
                    f"Storage Admin role is `admin` on this project (a master "
                    f"token qualifies, but so does any other project-admin "
                    f"user's token); reads work with any valid token "
                    f"(token: {self._masked_token}). Check `kbagent project "
                    f"info` -> is_master_token, or register a project-admin "
                    f"token (`kbagent project edit --token ...`) to use "
                    f"semantic-layer write commands.{id_suffix}"
                ),
                status_code=exc.status_code,
                error_code=ErrorCode.MISSING_MASTER_TOKEN,
                retryable=False,
            ) from exc

    # ------------------------------------------------------------------
    # Primitive verb methods
    # ------------------------------------------------------------------

    def list_items(
        self,
        item_type: SemanticType,
        model_uuid: str | None = None,
    ) -> list[dict[str, Any]]:
        """List all items of ``item_type`` and (optionally) filter by model.

        Returns the **raw item shape**: ``{"type", "id", "attributes",
        "meta"}``. Callers typically only want ``attributes`` plus ``id``;
        we keep the full shape so audit fields stay reachable.

        Filtering: client-side on ``attributes.modelUUID == model_uuid``. The
        server's ``?modelId=`` query param works in the probe but sl-builder
        reports it as historically unreliable — defensive filter wins.
        """
        response = self._do_request("GET", f"/api/v1/repository/{item_type}")
        body = response.json()
        items: list[dict[str, Any]] = body.get("data", []) if isinstance(body, dict) else []
        if model_uuid is None:
            return items
        return [i for i in items if (i.get("attributes") or {}).get("modelUUID") == model_uuid]

    def get_schema(self, item_type: SemanticType, version: str | None = None) -> dict[str, Any]:
        """Fetch the JSON Schema (or version listing) for a semantic object type.

        ``GET /api/v1/schema/{item_type}[/{version}]``. The schema is
        **server-emitted** so it always matches the deployed metastore
        version — never a hand-rolled static copy (it would drift the moment
        the metastore evolves). Live behavior (verified 2026-07): the bare
        endpoint returns a ``{"versions": [...]}`` listing with NO schema
        body; the actual JSON Schema lives at ``/{version}``. The service
        layer resolves the default version. No ``{"data": ...}`` envelope on
        either form, so the body is passed through verbatim.
        """
        path = f"/api/v1/schema/{item_type}/{version}" if version else f"/api/v1/schema/{item_type}"
        response = self._do_request("GET", path)
        body = response.json()
        if not isinstance(body, dict):
            raise KeboolaApiError(
                message=(
                    f"Unexpected metastore schema response format for "
                    f"{item_type!r} (expected a JSON object)."
                ),
                status_code=response.status_code,
                error_code=ErrorCode.API_ERROR,
                retryable=False,
            )
        return body

    def get_item(self, item_type: SemanticType, item_id: str) -> dict[str, Any]:
        """Fetch a single item by its UUID.

        Raises :class:`KeboolaApiError` with ``error_code=NOT_FOUND`` on 404.
        """
        response = self._do_request("GET", f"/api/v1/repository/{item_type}/{item_id}")
        body = response.json()
        return body.get("data", body) if isinstance(body, dict) else body

    def post_item(
        self,
        item_type: SemanticType,
        name: str,
        data: dict[str, Any],
        *,
        scope: ObjectScope = _DEFAULT_SCOPE,
        target_project_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """Create an item. Returns the server's stored representation.

        ``data`` is the inner ``attributes`` payload (including ``modelUUID``
        for non-model types). The outer envelope is added here.

        ``scope`` defaults to ``"project"`` (today's only behavior, unchanged
        for every existing caller). ``target_project_ids`` is only meaningful
        with ``scope="targeted"``; the server rejects the combination
        otherwise (``meta_object_repository.go`` ``Create``: not a typed
        sentinel error there, so it may surface as a 500 rather than a clean
        400 -- checked client-side below instead of relying on that). Neither
        can be changed later via :meth:`put_item` -- use
        :meth:`elevate_to_organization` / :meth:`put_target_projects`.

        Normalizes the duplicate-name conflict into a clean
        :data:`ErrorCode.ALREADY_EXISTS`, accepting both shapes the metastore
        has used: HTTP 409 (post go-monorepo PR #513) and HTTP 500 with
        ``"Failed to create meta object"`` (legacy / pre-fix deployments).
        """
        if scope not in ("project", "organization", "targeted"):
            raise KeboolaApiError(
                message=f"scope must be one of 'project'|'organization'|'targeted', got {scope!r}.",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
        if target_project_ids and scope != "targeted":
            raise KeboolaApiError(
                message=(
                    f"target_project_ids is only valid with scope='targeted', got scope={scope!r}."
                ),
                error_code=ErrorCode.VALIDATION_ERROR,
            )
        envelope: dict[str, Any] = {
            "name": name,
            "data": data,
            "branch": _ENVELOPE_BRANCH,
            "schemaVersion": _ENVELOPE_SCHEMA_VERSION,
            "scope": scope,
        }
        if target_project_ids is not None:
            envelope["targetProjectIds"] = target_project_ids
        try:
            response = self._do_request(
                "POST",
                f"/api/v1/repository/{item_type}",
                json=envelope,
            )
        except KeboolaApiError as exc:
            # Surface a clean ALREADY_EXISTS so command-layer error mapping
            # lands it on the right exit code. Two server-side shapes are
            # accepted because the metastore fix rolls out per-stack:
            #   * post go-monorepo PR #513: 409 Conflict (any 409 on this
            #     endpoint is by construction a uniqueness violation -- see
            #     services/metastore/api/handlers/repository_errors.go).
            #   * legacy / pre-fix:        500 with "Failed to create meta
            #     object" in the body (gated on the substring so unrelated
            #     500s -- e.g. a DB outage -- still surface as API_ERROR and
            #     stay retryable).
            is_duplicate = exc.status_code == 409 or (
                exc.status_code == 500 and "Failed to create meta object" in exc.message
            )
            if is_duplicate:
                raise KeboolaApiError(
                    message=(
                        f"{item_type} with name {name!r} already exists in the "
                        "target model. Use `edit` to update, or `remove` first."
                    ),
                    status_code=exc.status_code,
                    error_code=ErrorCode.ALREADY_EXISTS,
                    retryable=False,
                ) from exc
            raise
        body = response.json()
        return body.get("data", body) if isinstance(body, dict) else body

    def put_item(
        self,
        item_type: SemanticType,
        item_id: str,
        name: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Replace an item in place via ``PUT`` (revisioned update).

        Unlike the DELETE+POST pattern the higher-level ``edit`` operations
        use, ``PUT`` updates the record in place and increments
        ``meta.revision`` server-side, preserving the metastore's revision
        history. ``data`` is the inner ``attributes`` payload; the outer
        envelope is added here.

        Deliberately carries no ``scope``/``targetProjectIds`` -- the
        server's request struct for this endpoint (``MetaObjectUpdatePutRequest``)
        has no such fields, so scope/grants are untouched by a plain PUT.
        Use :meth:`elevate_to_organization` / :meth:`put_target_projects`.

        Raises :class:`KeboolaApiError` with ``error_code=NOT_FOUND`` on 404.
        """
        envelope = {
            "name": name,
            "data": data,
            "branch": _ENVELOPE_BRANCH,
            "schemaVersion": _ENVELOPE_SCHEMA_VERSION,
        }
        response = self._do_request(
            "PUT",
            f"/api/v1/repository/{item_type}/{item_id}",
            json=envelope,
        )
        body = response.json()
        return body.get("data", body) if isinstance(body, dict) else body

    def delete_item(self, item_type: SemanticType, item_id: str) -> None:
        """Delete an item by its UUID. Returns silently on 204.

        Raises :class:`KeboolaApiError` with ``error_code=NOT_FOUND`` on 404.
        """
        self._do_request("DELETE", f"/api/v1/repository/{item_type}/{item_id}")

    # ------------------------------------------------------------------
    # Scope / target-project primitives (PSGO-140)
    # ------------------------------------------------------------------

    def elevate_to_organization(self, item_type: SemanticType, item_id: str) -> dict[str, Any]:
        """Step an item up from project/targeted scope to organization scope.

        ``PATCH /{id}`` with ``{"scope": "organization"}`` only -- the server
        rejects combining a scope change with name/data in the same request,
        so this method sends nothing else. One-way: there is no downgrade
        endpoint. Requires the caller to hold the organization-admin role;
        a caller without it gets ``ACCESS_DENIED`` (403).
        """
        response = self._do_request(
            "PATCH",
            f"/api/v1/repository/{item_type}/{item_id}",
            json={"scope": "organization"},
        )
        body = response.json()
        return body.get("data", body) if isinstance(body, dict) else body

    def put_target_projects(
        self,
        item_type: SemanticType,
        item_id: str,
        target_project_ids: list[int],
    ) -> None:
        """Replace the full set of projects granted access to a targeted-scope item.

        This is a **replace**, not a merge -- an empty list clears every
        grant. Only the owning project or an organization admin may call
        this; only valid for an item created with ``scope="targeted"``
        (the server 400s otherwise, including for organization-scoped
        items, where targeting is meaningless). 204 on success, no body.
        """
        self._do_request(
            "PUT",
            f"/api/v1/repository/{item_type}/{item_id}/target-projects",
            json={"targetProjectIds": target_project_ids},
        )

    def request_scope_elevation(self, item_type: SemanticType, item_id: str) -> dict[str, Any]:
        """Flag a project-scoped item as awaiting an org-admin's step-up decision.

        Owner-only, idempotent (repeating just refreshes the timestamp). No
        request body. Returns the updated item.
        """
        response = self._do_request(
            "PUT",
            f"/api/v1/repository/{item_type}/{item_id}/scope-elevation-request",
        )
        body = response.json()
        return body.get("data", body) if isinstance(body, dict) else body

    def withdraw_scope_elevation(self, item_type: SemanticType, item_id: str) -> dict[str, Any]:
        """Clear a pending scope-elevation request. Idempotent no-op if none is pending."""
        response = self._do_request(
            "DELETE",
            f"/api/v1/repository/{item_type}/{item_id}/scope-elevation-request",
        )
        body = response.json()
        return body.get("data", body) if isinstance(body, dict) else body

    def list_organization_items(
        self,
        item_type: SemanticType,
        *,
        pending_elevation_only: bool = False,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """List organization-visible items of ``item_type`` across projects.

        ``GET /{type}/organization``. ``pending_elevation_only`` applies the
        server's documented discovery filter for an org-admin's queue
        (``scope_elevation_requested_at[not][null]=true``); the generic
        filter/limit/offset query language is shared with every other list
        endpoint in this API.
        """
        params: dict[str, str] = {}
        if pending_elevation_only:
            params["scope_elevation_requested_at[not][null]"] = "true"
        if limit is not None:
            params["limit"] = str(limit)
        if offset is not None:
            params["offset"] = str(offset)
        response = self._do_request(
            "GET",
            f"/api/v1/repository/{item_type}/organization",
            params=params or None,
        )
        body = response.json()
        return body.get("data", []) if isinstance(body, dict) else []
