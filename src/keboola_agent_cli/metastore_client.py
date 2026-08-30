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
"""

import logging
import re
from typing import Any, Literal

import httpx

from .errors import ErrorCode, KeboolaApiError
from .http_base import BaseHttpClient

logger = logging.getLogger(__name__)


# The metastore's auth middleware collapses EVERY project-scope resolution
# failure into this one opaque 401 string (go-monorepo
# ``services/metastore/internal/middleware/auth.go``, ``resolveProjectScope``
# -- the underlying error is logged server-side and discarded). The dominant
# cause is its master-token gate: ``NewProjectDeps`` is called without
# ``WithoutMasterToken()``, so unlike the Storage API the metastore accepts
# ONLY a master (project admin) Storage token, and every valid non-master
# token lands here (issue #711; A/B-verified live on us-east4.gcp: non-master
# token -> this 401 on every call, master token on the same stack -> 200).
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


# Envelope fields kept constant across every POST (per metastore contract).
_ENVELOPE_BRANCH = "main"
_ENVELOPE_SCHEMA_VERSION = "1.0.0"
_ENVELOPE_SCOPE = "project"


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

        A 401 carrying ``"Failed to create project scope"`` is the metastore's
        master-token gate rejecting a valid non-master token (see
        :data:`_PROJECT_SCOPE_401_EXCEPTION`), not a bad credential and not a
        server fault to escalate -- so it is reclassified from the generic
        401 mapping into :data:`ErrorCode.MISSING_MASTER_TOKEN` with the
        actual remedy, mirroring the ``token create`` / ``config oauth-url``
        pre-flight guards (#599). Kept at the request funnel (rather than one
        try/except per verb method) so no endpoint can miss it.
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
                    f"HTTP 401 {_PROJECT_SCOPE_401_EXCEPTION!r}. Unlike the Storage "
                    f"API, the metastore accepts only a MASTER (project admin) "
                    f"Storage token, and this is how it answers a valid non-master "
                    f"token (token: {self._masked_token}). Check "
                    f"`kbagent project info` -> is_master_token, and register a "
                    f"master token (`kbagent project edit --token ...`) to use "
                    f"semantic-layer commands.{id_suffix}"
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
    ) -> dict[str, Any]:
        """Create an item. Returns the server's stored representation.

        ``data`` is the inner ``attributes`` payload (including ``modelUUID``
        for non-model types). The outer envelope is added here.

        Normalizes the duplicate-name conflict into a clean
        :data:`ErrorCode.ALREADY_EXISTS`, accepting both shapes the metastore
        has used: HTTP 409 (post go-monorepo PR #513) and HTTP 500 with
        ``"Failed to create meta object"`` (legacy / pre-fix deployments).
        """
        envelope = {
            "name": name,
            "data": data,
            "branch": _ENVELOPE_BRANCH,
            "schemaVersion": _ENVELOPE_SCHEMA_VERSION,
            "scope": _ENVELOPE_SCOPE,
        }
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
        envelope is added here (identical shape to :meth:`post_item`).

        Raises :class:`KeboolaApiError` with ``error_code=NOT_FOUND`` on 404.
        """
        envelope = {
            "name": name,
            "data": data,
            "branch": _ENVELOPE_BRANCH,
            "schemaVersion": _ENVELOPE_SCHEMA_VERSION,
            "scope": _ENVELOPE_SCOPE,
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
