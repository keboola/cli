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
- Duplicate ``name`` on POST → **500** with exception ``"Failed to create meta
  object"``. We normalize this to :data:`ErrorCode.ALREADY_EXISTS`.
- Error envelope has top-level ``error``, ``code``, ``exception``, ``status``,
  ``context.path``, and an ``errors[]`` list for 422 validation failures.
"""

import logging
from typing import Any, Literal

from . import __version__
from .errors import ErrorCode, KeboolaApiError
from .http_base import BaseHttpClient

logger = logging.getLogger(__name__)


SemanticType = Literal[
    "semantic-model",
    "semantic-dataset",
    "semantic-metric",
    "semantic-relationship",
    "semantic-constraint",
    "semantic-glossary",
]


SEMANTIC_TYPES: tuple[str, ...] = (
    "semantic-model",
    "semantic-dataset",
    "semantic-metric",
    "semantic-relationship",
    "semantic-constraint",
    "semantic-glossary",
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

    def __init__(self, stack_url: str, token: str) -> None:
        self._stack_url = stack_url.rstrip("/")
        base_url = self._derive_service_url(self._stack_url, "metastore")
        headers = {
            "X-StorageApi-Token": token,
            "User-Agent": f"keboola-agent-cli/{__version__}",
        }
        super().__init__(base_url=base_url, token=token, headers=headers)

    def __enter__(self) -> "MetastoreClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

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

        Normalizes the "duplicate name → 500" quirk into a clean
        :data:`ErrorCode.ALREADY_EXISTS`.
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
            # The metastore returns 500 (not 409/422) when the name already
            # exists in the model. Surface a clean ALREADY_EXISTS instead of
            # the raw 500 so command-layer error mapping can land it on the
            # right exit code.
            if exc.status_code == 500 and "Failed to create meta object" in exc.message:
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

    def delete_item(self, item_type: SemanticType, item_id: str) -> None:
        """Delete an item by its UUID. Returns silently on 204.

        Raises :class:`KeboolaApiError` with ``error_code=NOT_FOUND`` on 404.
        """
        self._do_request("DELETE", f"/api/v1/repository/{item_type}/{item_id}")
