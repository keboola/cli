"""Keboola API client with retry, timeouts, and token masking.

This is the only module that communicates with the Keboola Storage API
and the Keboola Queue API. All HTTP details, endpoint URLs, and error
mapping are encapsulated here.

Inherits shared retry/error logic from BaseHttpClient.
"""

import json
import logging
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .constants import (
    CLOUD_UPLOAD_ERROR_BODY_LIMIT,
    DEFAULT_GROUPED_JOBS_LIMIT,
    DEFAULT_JOB_LIMIT,
    DEFAULT_JOBS_PER_CONFIG,
    DEFAULT_POLL_STRATEGY,
    DEFAULT_TIMEOUT,
    EXPORT_JOB_MAX_WAIT,
    FILE_DOWNLOAD_CHUNK_SIZE,
    FILE_DOWNLOAD_TIMEOUT,
    FILE_UPLOAD_TIMEOUT,
    IMPORT_JOB_MAX_WAIT,
    JOB_POLL_CURVE,
    METADATA_NOT_FOUND,
    OAUTH_HOST,
    OAUTH_PATH,
    OTLP_BUCKET_PREFIX,
    QUERY_JOB_MAX_WAIT,
    QUERY_JOB_POLL_INTERVAL,
    QUERY_RESULTS_PAGE_SIZE,
    STORAGE_JOB_MAX_WAIT,
    STORAGE_JOB_POLL_INTERVAL,
    STREAM_DEFAULT_BRANCH,
    VALID_POLL_STRATEGIES,
)
from .errors import ErrorCode, KeboolaApiError
from .http_base import BaseHttpClient
from .models import TokenVerifyResponse
from .stream_client import StreamClient, provision_otlp_sinks, stream_task_source_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InlineQueryResult:
    """One statement's result fetched via the fast inline ``/results`` path."""

    columns: list[dict[str, Any]]  # [{"name", "type", "nullable"}]
    rows: list[list[Any]]  # row values, row-major; capped at the requested limit
    total_rows: int | None  # numberOfRows reported by the warehouse (full count)
    truncated: bool  # True when the warehouse has more rows than we fetched


def _collect_inline_results(
    client: "KeboolaClient",
    query_job_id: str,
    statement_id: str,
    limit: int,
) -> InlineQueryResult:
    """Page through ``GET .../results``, accumulating up to ``limit`` rows.

    The endpoint enforces ``100 <= pageSize <= 100000``, so we always request a
    fixed, valid ``QUERY_RESULTS_PAGE_SIZE`` page and cap the accumulated rows at
    ``limit`` locally -- deriving ``pageSize`` from a small ``limit`` (e.g. 5)
    would trip the API's minimum with a 400. A ``limit`` larger than one page is
    satisfied by walking ``offset``; we stop once the limit is reached (marking
    the result truncated) or when the warehouse runs out of rows.

    Lives in the client layer (not a service) because it is pure Query Service
    pagination over :meth:`KeboolaClient.get_query_results` -- no config, no
    business logic -- so both ``WorkspaceService`` and the public library facade
    (:mod:`keboola_agent_cli.lib`) can share it.
    """
    collected: list[list[Any]] = []
    columns: list[dict[str, Any]] = []
    total_rows: int | None = None
    offset = 0
    exhausted = False
    while len(collected) < limit:
        payload = client.get_query_results(
            query_job_id, statement_id, offset=offset, page_size=QUERY_RESULTS_PAGE_SIZE
        )
        if not columns:
            columns = payload.get("columns", []) or []
        if total_rows is None:
            total_rows = payload.get("numberOfRows")
        page_rows = payload.get("data", []) or []
        collected.extend(page_rows)
        # Last page: the warehouse returned fewer rows than a full page.
        if len(page_rows) < QUERY_RESULTS_PAGE_SIZE:
            exhausted = True
            break
        offset += len(page_rows)
        # Reached the reported total on a page boundary: stop without spending a
        # round-trip on the empty next page (e.g. total == a multiple of the
        # page size, limit larger than total).
        if total_rows is not None and offset >= total_rows:
            exhausted = True
            break

    rows = collected[:limit]
    if total_rows is not None:
        truncated = total_rows > len(rows)
    else:
        # The Query Service normally reports numberOfRows, but if it omits the
        # count we fall back to *how* the loop ended: stopping at the limit cap
        # without exhausting a full last page means there may be more rows. Bias
        # toward over-warning when the true count is unknown.
        truncated = not exhausted and len(collected) >= limit
    return InlineQueryResult(
        columns=columns,
        rows=rows,
        total_rows=total_rows,
        truncated=truncated,
    )


def _iter_poll_intervals(strategy: str) -> Iterator[float]:
    """Yield sleep intervals (seconds) for Queue job polling.

    Two strategies:

    - ``"exponential"`` walks ``JOB_POLL_CURVE``: each (interval, count)
      segment yields ``count`` copies of ``interval``; a segment with
      ``count == 0`` keeps yielding ``interval`` forever (valid only on
      the last segment).
    - ``"fixed"`` yields ``STORAGE_JOB_POLL_INTERVAL`` forever (legacy
      behavior preserved for opt-out via ``--poll-strategy fixed``).

    The deadline check in ``wait_for_queue_job`` stops iteration.
    """
    if strategy == "fixed":
        while True:
            yield STORAGE_JOB_POLL_INTERVAL
    for interval, count in JOB_POLL_CURVE:
        if count <= 0:
            while True:
                yield interval
        for _ in range(count):
            yield interval


class KeboolaClient(BaseHttpClient):
    """HTTP client for the Keboola Storage API and Queue API.

    Provides methods to interact with Keboola endpoints with built-in
    retry logic (exponential backoff for 429/5xx), timeouts, and
    automatic token masking in error messages.

    Inherits _do_request() and _raise_api_error() from BaseHttpClient.
    """

    def __init__(self, stack_url: str, token: str) -> None:
        self._stack_url = stack_url.rstrip("/")
        headers = {
            "X-StorageApi-Token": token,
        }
        super().__init__(
            base_url=self._stack_url,
            token=token,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
        )
        self._queue_client: httpx.Client | None = None
        self._query_client: httpx.Client | None = None
        self._encrypt_client: httpx.Client | None = None
        self._sync_actions_client: httpx.Client | None = None
        # Lazily built on first Data Streams call (per-device OTLP sources); the
        # Stream control plane is a sibling host reachable from this stack+token.
        self._stream_client: StreamClient | None = None
        # Cache of project feature flags. Populated lazily on first
        # has_feature() / get_project_features() call so we don't pay an
        # extra verify_token round-trip on every kbagent invocation, and
        # only when business logic actually needs to branch on a feature
        # (e.g. legacy fake-branch storage detection).
        self._features_cache: frozenset[str] | None = None

    @property
    def _queue_base_url(self) -> str:
        return self._derive_service_url(self._stack_url, "queue")

    @property
    def _query_base_url(self) -> str:
        return self._derive_service_url(self._stack_url, "query")

    @property
    def _encrypt_base_url(self) -> str:
        return self._derive_service_url(self._stack_url, "encryption")

    @property
    def _sync_actions_base_url(self) -> str:
        return self._derive_service_url(self._stack_url, "sync-actions")

    def close(self) -> None:
        """Close the underlying HTTP clients."""
        super().close()
        if self._queue_client is not None:
            self._queue_client.close()
        if self._query_client is not None:
            self._query_client.close()
        if self._encrypt_client is not None:
            self._encrypt_client.close()
        if self._sync_actions_client is not None:
            self._sync_actions_client.close()
        if self._stream_client is not None:
            self._stream_client.close()

    def __enter__(self) -> "KeboolaClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Execute a Storage API request with retry."""
        return self._do_request(method, path, **kwargs)

    def _get_or_create_sub_client(
        self,
        attr: str,
        base_url: str,
        headers: dict[str, str] | None = None,
    ) -> httpx.Client:
        """Return an existing sub-client or lazily create one.

        Args:
            attr: Instance attribute name (e.g. "_queue_client").
            base_url: Base URL for the sub-client.
            headers: Custom headers; defaults to the main client's headers.
        """
        client = getattr(self, attr)
        if client is None:
            client = httpx.Client(
                base_url=base_url,
                timeout=DEFAULT_TIMEOUT,
                headers=self._client._headers.copy() if headers is None else headers,
            )
            setattr(self, attr, client)
        return client

    def _queue_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Execute a Queue API request with retry."""
        client = self._get_or_create_sub_client("_queue_client", self._queue_base_url)
        return self._do_request(
            method, path, client=client, base_url=self._queue_base_url, **kwargs
        )

    def _query_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Execute a Query Service request with retry."""
        client = self._get_or_create_sub_client("_query_client", self._query_base_url)
        return self._do_request(
            method, path, client=client, base_url=self._query_base_url, **kwargs
        )

    def _encrypt_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Execute an Encryption API request with retry."""
        client = self._get_or_create_sub_client(
            "_encrypt_client", self._encrypt_base_url, headers={"Content-Type": "application/json"}
        )
        return self._do_request(
            method, path, client=client, base_url=self._encrypt_base_url, **kwargs
        )

    def encrypt_values(
        self,
        project_id: int,
        component_id: str,
        data: dict[str, str],
    ) -> dict[str, str]:
        """Encrypt secret values via the Keboola Encryption API.

        Sends a dict of {key: plaintext} and receives {key: encrypted}.
        Keys must start with '#'. Encrypted values start with 'KBC::ProjectSecure::'.

        Args:
            project_id: Keboola project numeric ID.
            component_id: Component identifier (e.g. 'keboola.ex-db-snowflake').
            data: Dict of secret keys to encrypt (e.g. {'#password': 'my-secret'}).

        Returns:
            Dict of {key: encrypted_value}.
        """
        response = self._encrypt_request(
            "POST",
            "/encrypt",
            params={"projectId": project_id, "componentId": component_id},
            json=data,
        )
        return response.json()

    def _sync_actions_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Execute a Sync Actions API request with retry.

        The Sync Actions service is a sibling host derived from the stack URL
        (``sync-actions.{stack-suffix}``); the sub-client inherits the main
        client's headers, so the ``X-StorageApi-Token`` auth carries over.
        """
        client = self._get_or_create_sub_client("_sync_actions_client", self._sync_actions_base_url)
        return self._do_request(
            method, path, client=client, base_url=self._sync_actions_base_url, **kwargs
        )

    def run_sync_action(
        self,
        component_id: str,
        action: str,
        config_data: dict[str, Any],
        branch_id: int | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Run a synchronous component action via the Sync Actions API.

        POSTs to ``/actions`` on the ``sync-actions.{stack-suffix}`` host.
        Valid action names are component-defined (surfaced as
        ``synchronous_actions`` in component metadata, e.g. ``testConnection``,
        ``getTables``); the API validates them server-side.

        Args:
            component_id: Component identifier (e.g. 'keboola.ex-db-mysql').
            action: Sync action name (freeform; component-defined).
            config_data: The configData payload (typically
                ``{"parameters": ..., "storage": ...}``). May carry secrets --
                never log it.
            branch_id: If set, sent as ``branchId``; omitted entirely for the
                production branch (the API treats an absent key as default).
            timeout: Optional per-request timeout in seconds (sync actions can
                run long, e.g. ``getTables`` against a large database).

        Returns:
            The action result verbatim (opaque dict or list; shape is
            action-specific).
        """
        body: dict[str, Any] = {
            "configData": config_data,
            "componentId": component_id,
            "action": action,
        }
        if branch_id is not None:
            body["branchId"] = branch_id
        request_kwargs: dict[str, Any] = {"json": body}
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        response = self._sync_actions_request("POST", "/actions", **request_kwargs)
        return response.json()

    def verify_token(self) -> TokenVerifyResponse:
        """Verify the storage API token and retrieve project information.

        Returns:
            TokenVerifyResponse with project name, ID, and token description.

        Raises:
            KeboolaApiError: If token is invalid (401) or other API error.
        """
        response = self._request("GET", "/v2/storage/tokens/verify")
        data = response.json()

        owner = data.get("owner", {})
        # /v2/storage/tokens/verify carries `organization` at the TOP level
        # (NOT nested under `owner` like I'd previously assumed -- three
        # rounds of broken backfill traced back to this mismatch). The
        # payload is minimal -- only `{"id": "73"}` on the GCP us-east4
        # stack -- so org name has to come from the Manage API path.
        org = data.get("organization") or {}
        org_id_raw = org.get("id")
        # Storage API serializes org id as a string ("73"); normalise to int
        # so callers and persisted ProjectConfig.org_id can keep its int
        # type without each consumer doing the cast.
        org_id: int | None
        try:
            org_id = int(org_id_raw) if org_id_raw is not None else None
        except (TypeError, ValueError):
            org_id = None
        response = TokenVerifyResponse(
            token_id=str(data.get("id", "")),
            token_description=data.get("description", ""),
            project_id=owner.get("id"),
            project_name=owner.get("name", ""),
            owner_name=owner.get("name", ""),
            default_backend=owner.get("defaultBackend", "snowflake"),
            features=owner.get("features", []),
            org_id=org_id,
            # Top-level `organization` block does NOT carry a name; that
            # field is Manage-API-only. Leave None and let the UI show
            # the id (e.g. "#73") as a fallback until `org setup` fills
            # in the human-readable name.
            org_name=None,
        )
        # Refresh the features cache on every successful verify so explicit
        # callers stay consistent with the cached view used by has_feature().
        self._features_cache = frozenset(response.features)
        return response

    def get_project_info(self) -> dict[str, Any]:
        """Return full project/token info from /v2/storage/tokens/verify.

        Unlike verify_token() which parses only a subset of fields into
        TokenVerifyResponse, this method returns the complete raw API response
        so callers can access all fields (features, limits, metrics, etc.).

        Returns:
            Full JSON response dict from /v2/storage/tokens/verify.

        Raises:
            KeboolaApiError: If token is invalid (401) or other API error.
        """
        response = self._request("GET", "/v2/storage/tokens/verify")
        return response.json()

    def create_short_lived_token(
        self,
        description: str,
        component_access: list[str],
        expires_in: int = 3600,
    ) -> dict[str, Any]:
        """Create a short-lived Storage API token restricted to a component.

        POST /v2/storage/tokens

        Args:
            description: Human-readable token description.
            component_access: List of component IDs this token may access.
            expires_in: Token lifetime in seconds (default: 3600 = 1 hour).

        Returns:
            Token dict from the API, including the 'token' field.
        """
        response = self._request(
            "POST",
            "/v2/storage/tokens",
            data={
                "description": description,
                "expiresIn": str(expires_in),
                "componentAccess[]": component_access,
            },
        )
        return response.json()

    def create_scoped_token(
        self,
        *,
        description: str,
        bucket_permissions: dict[str, str] | None = None,
        component_access: list[str] | None = None,
        can_read_all_file_uploads: bool = False,
        expires_in: int | None = None,
    ) -> dict[str, Any]:
        """Create a scoped Storage API token (``POST /v2/storage/tokens``).

        The general form of :meth:`create_short_lived_token`: instead of only a
        component allow-list it also expresses **bucket** permissions, so a
        caller can mint the narrow "upload Files + write one sink bucket,
        expiring, nothing else" token a capture device needs (Keboola's
        single-bucket-write pattern).

        Note on Files upload: a Files upload (``POST /v2/storage/files/prepare``)
        is a generic Storage write available to any valid Storage token -- it is
        **not** gated by ``componentAccess`` or ``canReadAllFileUploads``. Grant
        ``bucket_permissions={sink_bucket: "write"}`` for the sink write;
        ``can_read_all_file_uploads`` only widens *reading* files uploaded by
        *other* tokens (a device sees its own uploads regardless).

        The acting token must carry ``canManageTokens`` (the API rejects the
        create otherwise -- surfaced as an ``ACCESS_DENIED`` :class:`KeboolaApiError`
        with the token masked). The returned dict is the raw API response; its
        ``token`` field is a **one-time** secret reveal -- persist only ``id``
        (for :meth:`delete_token` / :meth:`refresh_token`) and ``expires``.

        Args:
            description: Human-readable token description (per-device label).
            bucket_permissions: ``{bucketId: "read" | "write"}`` grants.
            component_access: Component IDs the token may run (often empty for a
                capture device that only uploads Files + streams OTLP).
            can_read_all_file_uploads: If True the token may read files uploaded
                by other tokens (default False = only its own uploads).
            expires_in: Lifetime in seconds; ``None`` = never expires.
        """
        data: dict[str, Any] = {"description": description}
        if expires_in is not None:
            data["expiresIn"] = str(expires_in)
        if can_read_all_file_uploads:
            # Storage API reads this form field as truthy; omit it (=> default
            # false) rather than sending "0" so an unset scope stays minimal.
            data["canReadAllFileUploads"] = "1"
        for bucket_id, permission in (bucket_permissions or {}).items():
            data[f"bucketPermissions[{bucket_id}]"] = permission
        if component_access:
            data["componentAccess[]"] = component_access
        response = self._request("POST", "/v2/storage/tokens", data=data)
        return response.json()

    def delete_token(self, token_id: str) -> None:
        """Revoke a Storage API token immediately (``DELETE /v2/storage/tokens/{id}``).

        Returns 204 and the token stops authenticating at once. Only a
        **non-master** token can be deleted (the API refuses to delete the master
        token). Use this for active per-device revocation instead of waiting for
        the token to expire.
        """
        self._request("DELETE", f"/v2/storage/tokens/{quote(str(token_id), safe='')}")

    def refresh_token(self, token_id: str) -> dict[str, Any]:
        """Rotate a Storage API token (``POST /v2/storage/tokens/{id}/refresh``).

        Generates a **new** token value and returns the updated token dict; the
        **old** token string becomes immediately invalid (rotation, not
        additive), so every place using it must be updated. The token id is
        stable across a refresh.
        """
        response = self._request(
            "POST", f"/v2/storage/tokens/{quote(str(token_id), safe='')}/refresh"
        )
        return response.json()

    # ------------------------------------------------------------------
    # Data Streams (per-device OTLP sources) -- delegate to StreamClient
    # ------------------------------------------------------------------

    def _get_stream_client(self) -> StreamClient:
        """Lazily build (and cache) a :class:`StreamClient` over this stack+token.

        The Stream control plane lives on a sibling host (``stream.<region>``)
        and authenticates with the same Storage token, so it is reachable from
        the same ``(stack_url, token)`` this client already holds.
        """
        if self._stream_client is None:
            self._stream_client = StreamClient(stack_url=self._stack_url, token=self._token)
        return self._stream_client

    @staticmethod
    def _stream_source_detail(
        source: dict[str, Any], branch_id: str, sink_bucket_id: str | None
    ) -> dict[str, Any]:
        """Normalise a raw Stream ``source`` object into the returned detail dict.

        Flattens the OTLP block so the caller gets ``otlp_url`` (with the ingest
        secret embedded, **unmasked** -- the lib layer hands it to the device
        once and never persists it) without digging into ``source["otlp"]``. The
        raw ``source`` is echoed under ``source`` so nothing is lost.
        """
        otlp = source.get("otlp") or {}
        source_id = source.get("sourceId", "")
        return {
            "id": source_id,
            "source_id": source_id,
            "name": source.get("name", ""),
            "type": source.get("type", ""),
            "description": source.get("description", ""),
            "branch_id": branch_id,
            "otlp_url": otlp.get("url", ""),
            "otlp_secret": otlp.get("secret", ""),
            "base_endpoint": otlp.get("baseUrl", ""),
            "sink_bucket_id": sink_bucket_id,
            "source": source,
        }

    def create_stream_source(
        self,
        name: str,
        *,
        source_type: str = "otlp",
        description: str = "",
        branch_id: str = STREAM_DEFAULT_BRANCH,
        provision_sinks: bool = True,
    ) -> dict[str, Any]:
        """Create a per-device OTLP (or HTTP) stream source and return its detail.

        Async under the hood: the Stream API returns a 202 ``Task`` which is
        polled to completion here (the caller sees one blocking call). For an
        ``otlp`` source with ``provision_sinks`` (default) the logs/metrics/traces
        sinks and the ``in.c-otlp-<sourceId>`` sink bucket are auto-created so
        OTLP data actually lands in Storage -- and so a scoped device token can be
        granted ``write`` on that bucket. Pass ``provision_sinks=False`` for a
        bare source.

        Per-device sources are the unit of isolated event-plane revocation:
        delete one device's source (:meth:`delete_stream_source`) without
        rotating a secret shared with other devices.

        Returns a dict with the flattened endpoint: ``id`` / ``source_id``,
        ``otlp_url`` (secret **unmasked**), ``otlp_secret``, ``sink_bucket_id``
        (``None`` when no sinks were provisioned), and the raw ``source``.
        Requires a Storage token privileged to manage Data Streams.
        """
        stream = self._get_stream_client()
        task = stream.create_source(
            branch_id, name=name, source_type=source_type, description=description or None
        )
        finished = stream.wait_for_task(task)
        source_id = stream_task_source_id(finished) or name
        sink_bucket_id: str | None = None
        if provision_sinks and source_type == "otlp":
            provision_otlp_sinks(stream, branch_id, source_id)
            sink_bucket_id = f"{OTLP_BUCKET_PREFIX}{source_id}"
        source = stream.get_source(branch_id, source_id)
        return self._stream_source_detail(source, branch_id, sink_bucket_id)

    @staticmethod
    def _sink_bucket_from_sinks(sinks_raw: dict[str, Any]) -> str | None:
        """Return the bucket of the source's first table sink, or None if it has none.

        Derived from the ACTUAL sinks rather than assumed from the source type,
        so a source created with ``provision_sinks=False`` (or outside kbagent)
        truthfully reports ``None`` instead of a bucket that does not exist.
        """
        for sink in sinks_raw.get("sinks", []):
            table_id = (sink.get("table") or {}).get("tableId", "")
            if "." in table_id:
                return table_id.rsplit(".", 1)[0]
        return None

    def get_stream_source(
        self, source_id: str, *, branch_id: str = STREAM_DEFAULT_BRANCH
    ) -> dict[str, Any]:
        """Fetch one stream source's detail (endpoint + secret + sink bucket).

        ``sink_bucket_id`` reflects the source's ACTUAL sinks (``None`` when it
        has none -- e.g. created with ``provision_sinks=False`` or outside
        kbagent), so a caller never scopes a device token to a bucket that does
        not exist.
        """
        stream = self._get_stream_client()
        source = stream.get_source(branch_id, source_id)
        sinks = stream.list_sinks(branch_id, source.get("sourceId", source_id))
        return self._stream_source_detail(source, branch_id, self._sink_bucket_from_sinks(sinks))

    def list_stream_sources(
        self, *, branch_id: str = STREAM_DEFAULT_BRANCH
    ) -> list[dict[str, Any]]:
        """List the project's stream sources (raw source objects; find-or-create by name)."""
        raw = self._get_stream_client().list_sources(branch_id)
        return list(raw.get("sources", []))

    def delete_stream_source(
        self, source_id: str, *, branch_id: str = STREAM_DEFAULT_BRANCH
    ) -> None:
        """Delete a stream source (per-device revocation) -- async task polled to completion."""
        stream = self._get_stream_client()
        task = stream.delete_source(branch_id, source_id)
        stream.wait_for_task(task)

    def global_search(
        self,
        query: str,
        project_id: int,
        types: list[str] | None = None,
        branch_type: str = "production",
        branch_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
        regex: bool = False,
    ) -> dict[str, Any]:
        """Search for items by name across the project using the Storage API global-search endpoint.

        Calls GET /v2/storage/global-search with the given query and optional type filters.
        This performs textual (name-based) search only — it does not scan configuration bodies.
        Results are scoped to the single project identified by ``project_id``.

        Args:
            query: Search string to match against item names.
            project_id: Numeric Keboola project ID (required by the API).
            types: Optional list of item types to filter results. Supported values:
                   ``bucket``, ``table``, ``flow``, ``transformation``, ``configuration``,
                   ``configuration-row``, ``workspace``, ``shared-code``.
                   If None or empty, all types are returned.
            branch_type: ``"production"`` (default) or ``"development"``.
            branch_id: Required when ``branch_type="development"``; ignored otherwise.
            limit: Maximum number of results to return (default 50, max 100).
            offset: Pagination offset (default 0).
            regex: When True, run the query as a case-insensitive whole-term
                   regular expression over entity names (Storage API
                   ``mode=regex``). Omitted from the request otherwise.

        Returns:
            Raw API response dict with keys ``"all"`` (total count) and
            ``"items"`` (list of matching item dicts).

        Raises:
            KeboolaApiError: On API errors (auth, network, rate limits).
        """
        params: dict[str, Any] = {
            "query": query,
            "projectIds[]": project_id,
            "limit": limit,
            "offset": offset,
        }
        if types:
            params["types[]"] = types
        if regex:
            params["mode"] = "regex"
        if branch_type == "development" and branch_id is not None:
            params["branchTypes[]"] = "development"
            params["branchIds[]"] = branch_id
        else:
            params["branchTypes[]"] = "production"

        response = self._request("GET", "/v2/storage/global-search", params=params)
        return response.json()

    def get_oauth_url(
        self,
        component_id: str,
        config_id: str,
        redirect_url: str | None = None,
    ) -> str:
        """Generate an OAuth authorization URL for a component configuration.

        Creates a short-lived, component-scoped Storage API token and builds
        the URL the user must open to grant OAuth access.

        Args:
            component_id: The component ID (e.g. 'keboola.ex-google-drive').
            config_id: The configuration ID to authorize.
            redirect_url: Optional URL the OAuth wizard returns to after the
                flow completes (passed as the ``returnUrl`` query param).

        Returns:
            The full OAuth authorization URL as a string.
        """
        from urllib.parse import urlencode, urlunsplit

        token_response = self.create_short_lived_token(
            description=f"Short-lived token for OAuth URL - {component_id}/{config_id}",
            component_access=[component_id],
            expires_in=3600,
        )
        sapi_token = token_response["token"]

        query: dict[str, str] = {"token": sapi_token, "sapiUrl": self._stack_url}
        if redirect_url:
            query["returnUrl"] = redirect_url
        query_params = urlencode(query)
        fragment = f"/{component_id}/{config_id}"

        return urlunsplit(("https", OAUTH_HOST, OAUTH_PATH, query_params, fragment))

    def get_project_features(self) -> frozenset[str]:
        """Return the project's feature flags, fetching once per client lifetime.

        Calls ``verify_token()`` lazily on first request and caches the result.
        Subsequent calls do not trigger HTTP. The cache lives for the life of
        the ``KeboolaClient`` instance, which is one CLI invocation -- short
        enough that staleness across feature toggles is not a practical risk.
        """
        if self._features_cache is None:
            self.verify_token()
        # _features_cache is non-None here: verify_token() always sets it (or
        # raises on auth/network failure, which propagates to the caller).
        assert self._features_cache is not None
        return self._features_cache

    def has_feature(self, feature: str) -> bool:
        """True if the project owner has ``feature`` enabled.

        Convenience wrapper over ``get_project_features()`` for code paths
        that branch on a single flag (e.g. ``"storage-branches"``).
        """
        return feature in self.get_project_features()

    def list_components(
        self,
        component_type: str | None = None,
        branch_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """List components with their configurations.

        Args:
            component_type: Optional filter (extractor, writer, transformation, application).
            branch_id: If set, list components from a specific dev branch.

        Returns:
            List of component dicts from the API.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        params: dict[str, str] = {"include": "configuration"}
        if component_type:
            params["componentType"] = component_type

        response = self._request("GET", f"{prefix}/components", params=params)
        return response.json()

    def list_components_with_configs(
        self,
        branch_id: int | None = None,
        component_type: str | None = None,
        include_state: bool = False,
    ) -> list[dict[str, Any]]:
        """List all components with full configuration bodies and rows.

        Makes a single API call to fetch everything needed for sync pull and
        for deep search (row-level configuration). Uses the
        include=configuration,rows parameter to get full config bodies and
        config rows in one request. When ``include_state`` is True, the
        response also embeds each configuration's runtime ``state`` dict
        (same data as ``get_config_state``) so bulk-state retrieval stays a
        single request instead of N+1. Also used by the bulk-detail caller
        in ``ConfigService`` when ``--with-state`` is set on
        ``config detail`` without a specific ``--config-id``.

        Args:
            branch_id: If set, target a specific dev branch.
            component_type: Optional filter (extractor, writer, transformation,
                application). Passed to the API as ``componentType``.
            include_state: When True, adds ``state`` to the ``include``
                resource list so each returned configuration carries its
                runtime state dict.

        Returns:
            List of component dicts, each containing a 'configurations' list
            with full config bodies and nested 'rows'.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        include_parts = ["configuration", "rows"]
        if include_state:
            include_parts.append("state")
        params: dict[str, str] = {"include": ",".join(include_parts)}
        if component_type:
            params["componentType"] = component_type
        resp = self._request(
            "GET",
            f"{prefix}/components",
            params=params,
        )
        return resp.json()

    def list_component_configs(
        self,
        component_id: str,
        branch_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """List all configurations for a specific component.

        Args:
            component_id: Component identifier (e.g. 'keboola.sandboxes').
            branch_id: If set, target a specific dev branch.

        Returns:
            List of configuration dicts (id, name, description, etc.).
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        resp = self._request(
            "GET",
            f"{prefix}/components/{quote(component_id, safe='')}/configs",
        )
        return resp.json()

    def list_config_rows(
        self,
        component_id: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """List all rows for a specific configuration.

        Args:
            component_id: Component identifier (e.g. 'keboola.ex-http').
            config_id: Configuration ID.
            branch_id: If set, target a specific dev branch.

        Returns:
            List of config row dicts.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        resp = self._request(
            "GET",
            f"{prefix}/components/{quote(component_id)}/configs/{quote(config_id)}/rows",
        )
        return resp.json()

    def get_config_row(
        self,
        component_id: str,
        config_id: str,
        row_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Get a single configuration row by ID.

        Args:
            component_id: Component identifier.
            config_id: Configuration ID.
            row_id: Row ID.
            branch_id: If set, target a specific dev branch.

        Returns:
            Row detail dict from the API.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        resp = self._request(
            "GET",
            f"{prefix}/components/{quote(component_id)}/configs/{quote(config_id)}/rows/{quote(row_id)}",
        )
        return resp.json()

    def get_config_detail(
        self,
        component_id: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Get detailed information about a specific configuration.

        Args:
            component_id: The component ID (e.g. keboola.ex-db-snowflake).
            config_id: The configuration ID.
            branch_id: If set, get detail from a specific dev branch.

        Returns:
            Configuration detail dict from the API.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_component_id = quote(component_id, safe="")
        safe_config_id = quote(config_id, safe="")
        response = self._request(
            "GET",
            f"{prefix}/components/{safe_component_id}/configs/{safe_config_id}",
        )
        return response.json()

    def get_config_state(
        self,
        component_id: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Get the runtime state dict of a specific configuration.

        Convenience wrapper over
        ``get_config_detail(...).get("state", {})``: Storage API does not
        expose a standalone ``GET .../state`` resource (production returns
        404, branch-scoped returns 501 Not Implemented), so the state is
        only served inline as a field inside the configuration detail
        response. This wrapper is retained for API discoverability, but
        callers that already have a detail response should read ``state``
        from it directly instead of issuing this second identical request
        -- the service layer's single-mode ``--with-state`` does exactly
        that (see ``ConfigService.get_config_detail``).

        For bulk state retrieval across many configs, prefer the
        ``include=state`` query param on
        ``list_components_with_configs(include="configuration,rows,state")``
        -- one request serves every config's state instead of N requests.

        Args:
            component_id: The component ID (e.g. keboola.ex-db-snowflake).
            config_id: The configuration ID.
            branch_id: If set, fetch state from a specific dev branch.

        Returns:
            The state dict (empty ``{}`` when the config has no saved state).
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_component_id = quote(component_id, safe="")
        safe_config_id = quote(config_id, safe="")
        response = self._request(
            "GET",
            f"{prefix}/components/{safe_component_id}/configs/{safe_config_id}",
        )
        body = response.json()
        state = body.get("state")
        return state if isinstance(state, dict) else {}

    def list_config_folder_metadata(self, branch_id: int) -> dict[str, str]:
        """Fetch folder names for all configurations via metadata search.

        Uses the search/component-configurations endpoint to find configs
        with ``KBC.configuration.folderName`` metadata.

        Note: This endpoint requires a branch ID (branch-only route).

        Args:
            branch_id: Branch ID (required — use default branch for production).

        Returns:
            Dict mapping ``"{component_id}/{config_id}"`` to folder name.
        """
        prefix = f"/v2/storage/branch/{branch_id}"
        resp = self._request(
            "GET",
            f"{prefix}/search/component-configurations",
            params={
                "metadataKeys[]": "KBC.configuration.folderName",
                "include": "filteredMetadata",
            },
        )
        folder_map: dict[str, str] = {}
        for item in resp.json():
            comp_id = item.get("idComponent", "")
            config_id = str(item.get("configurationId", ""))
            meta = next(
                (m for m in item.get("metadata", []) if m["key"] == "KBC.configuration.folderName"),
                None,
            )
            if meta:
                folder_map[f"{comp_id}/{config_id}"] = meta["value"]
        return folder_map

    def list_config_metadata(
        self,
        component_id: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """List metadata entries on a configuration.

        GET /v2/storage/[branch/{b}/]components/{c}/configs/{id}/metadata
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        response = self._request(
            "GET",
            f"{prefix}/components/{quote(component_id, safe='')}/configs/{quote(config_id, safe='')}/metadata",
        )
        return response.json()

    def set_config_metadata(
        self,
        component_id: str,
        config_id: str,
        entries: list[tuple[str, str]],
        branch_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Bulk-set metadata key/value pairs on a configuration.

        POST /v2/storage/[branch/{b}/]components/{c}/configs/{id}/metadata
        Same PHP-style indexed form as set_branch_metadata.
        """
        form: dict[str, str] = {}
        for i, (key, value) in enumerate(entries):
            form[f"metadata[{i}][key]"] = key
            form[f"metadata[{i}][value]"] = value
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        response = self._request(
            "POST",
            f"{prefix}/components/{quote(component_id, safe='')}/configs/{quote(config_id, safe='')}/metadata",
            data=form,
        )
        return response.json()

    def delete_config_metadata(
        self,
        component_id: str,
        config_id: str,
        metadata_id: int | str,
        branch_id: int | None = None,
    ) -> None:
        """Delete a single metadata entry on a configuration by its numeric ID.

        DELETE /v2/storage/[branch/{b}/]components/{c}/configs/{id}/metadata/{mid}
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        self._request(
            "DELETE",
            f"{prefix}/components/{quote(component_id, safe='')}/configs/{quote(config_id, safe='')}/metadata/{metadata_id}",
        )

    def create_config(
        self,
        component_id: str,
        name: str,
        configuration: dict[str, Any],
        description: str = "",
        branch_id: int | None = None,
        is_disabled: bool = False,
    ) -> dict[str, Any]:
        """Create a new configuration for a component.

        POST /v2/storage/[branch/{id}/]components/{comp_id}/configs

        Args:
            component_id: Component identifier.
            name: Configuration name.
            configuration: Configuration body (parameters, storage, etc.).
            description: Optional description.
            branch_id: If set, target a specific dev branch.
            is_disabled: When True, the configuration is created in disabled
                state (mirrors ``create_config_row``).

        Returns:
            Created configuration dict including the assigned 'id'.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        data: dict[str, Any] = {
            "name": name,
            "description": description,
            "configuration": json.dumps(configuration),
        }
        if is_disabled:
            data["isDisabled"] = "1"
        resp = self._request(
            "POST",
            f"{prefix}/components/{quote(component_id)}/configs",
            data=data,
        )
        return resp.json()

    def update_config(
        self,
        component_id: str,
        config_id: str,
        name: str | None = None,
        configuration: dict[str, Any] | None = None,
        description: str | None = None,
        change_description: str = "",
        branch_id: int | None = None,
        is_disabled: bool | None = None,
    ) -> dict[str, Any]:
        """Update an existing configuration.

        PUT /v2/storage/[branch/{id}/]components/{comp_id}/configs/{config_id}

        Only provided (non-None) fields are sent in the request.
        ``is_disabled=None`` leaves the remote enabled/disabled state
        untouched (mirrors ``update_config_row``).

        Returns:
            Updated configuration dict.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        data: dict[str, Any] = {}
        if name is not None:
            data["name"] = name
        if description is not None:
            data["description"] = description
        if configuration is not None:
            data["configuration"] = json.dumps(configuration)
        if is_disabled is not None:
            data["isDisabled"] = "1" if is_disabled else "0"
        if change_description:
            data["changeDescription"] = change_description
        resp = self._request(
            "PUT",
            f"{prefix}/components/{quote(component_id)}/configs/{quote(config_id)}",
            data=data,
        )
        return resp.json()

    def create_config_row(
        self,
        component_id: str,
        config_id: str,
        name: str,
        configuration: dict[str, Any],
        description: str = "",
        is_disabled: bool = False,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a new configuration row.

        POST /v2/storage/[branch/{id}/]components/{comp_id}/configs/{config_id}/rows

        Args:
            component_id: The component ID.
            config_id: The parent configuration ID.
            name: Row name.
            configuration: Row-level configuration dict.
            description: Optional row description.
            is_disabled: When True, the row is created in disabled state and
                excluded from job runs until re-enabled.
            branch_id: Optional dev branch ID.

        Returns:
            Created row dict including the assigned 'id'.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        data: dict[str, Any] = {
            "name": name,
            "description": description,
            "configuration": json.dumps(configuration),
        }
        if is_disabled:
            data["isDisabled"] = "1"
        resp = self._request(
            "POST",
            f"{prefix}/components/{quote(component_id)}/configs/{quote(config_id)}/rows",
            data=data,
        )
        return resp.json()

    def update_config_row(
        self,
        component_id: str,
        config_id: str,
        row_id: str,
        name: str | None = None,
        configuration: dict[str, Any] | None = None,
        description: str | None = None,
        is_disabled: bool | None = None,
        change_description: str = "",
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Update an existing configuration row.

        PUT /v2/storage/[branch/{id}/]components/{comp_id}/configs/{config_id}/rows/{row_id}

        Args:
            is_disabled: When True, disable the row; when False, enable it;
                when None, leave the current state unchanged.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        data: dict[str, Any] = {}
        if name is not None:
            data["name"] = name
        if description is not None:
            data["description"] = description
        if configuration is not None:
            data["configuration"] = json.dumps(configuration)
        if is_disabled is not None:
            data["isDisabled"] = "1" if is_disabled else "0"
        if change_description:
            data["changeDescription"] = change_description
        resp = self._request(
            "PUT",
            f"{prefix}/components/{quote(component_id)}/configs/{quote(config_id)}/rows/{quote(row_id)}",
            data=data,
        )
        return resp.json()

    def delete_config_row(
        self,
        component_id: str,
        config_id: str,
        row_id: str,
        branch_id: int | None = None,
    ) -> None:
        """Delete a configuration row.

        DELETE /v2/storage/[branch/{id}/]components/{comp_id}/configs/{config_id}/rows/{row_id}
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        self._request(
            "DELETE",
            f"{prefix}/components/{quote(component_id)}/configs/{quote(config_id)}/rows/{quote(row_id)}",
        )

    def _wait_for_storage_job(
        self,
        job: dict[str, Any],
        max_wait: float = STORAGE_JOB_MAX_WAIT,
    ) -> dict[str, Any]:
        """Poll a Storage API job until it reaches a terminal state.

        Args:
            job: Initial job response from POST/DELETE.
            max_wait: Maximum seconds to wait (default: STORAGE_JOB_MAX_WAIT).

        Returns:
            Completed job dict (with results on success).

        Raises:
            KeboolaApiError: If the job fails or times out.
        """
        job_id = job.get("id")
        if job.get("status") in ("success", "error"):
            return job

        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            time.sleep(STORAGE_JOB_POLL_INTERVAL)
            response = self._request("GET", f"/v2/storage/jobs/{job_id}")
            job = response.json()
            status = job.get("status")
            if status == "success":
                return job
            if status == "error":
                error_msg = job.get("error", {}).get("message", "Storage job failed")
                raise KeboolaApiError(
                    message=error_msg,
                    status_code=500,
                    error_code=ErrorCode.STORAGE_JOB_FAILED,
                    retryable=False,
                )
        raise KeboolaApiError(
            message=f"Storage job {job_id} did not complete within {max_wait}s",
            status_code=504,
            error_code=ErrorCode.STORAGE_JOB_TIMEOUT,
            retryable=True,
        )

    def create_dev_branch(self, name: str, description: str = "") -> dict[str, Any]:
        """Create a new development branch (waits for async job to complete).

        The Storage API returns an async job. This method polls until the job
        completes and returns the branch data from the job results.

        Args:
            name: Branch name.
            description: Optional branch description.

        Returns:
            Branch dict with id, name, description, created, etc.

        Raises:
            KeboolaApiError: If the API call or job fails.
        """
        body: dict[str, str] = {"name": name}
        if description:
            body["description"] = description
        response = self._request("POST", "/v2/storage/dev-branches", json=body)
        job = self._wait_for_storage_job(response.json())
        return job.get("results", {})

    def delete_dev_branch(self, branch_id: int) -> None:
        """Delete a development branch (waits for async job to complete).

        Args:
            branch_id: The branch ID to delete.

        Raises:
            KeboolaApiError: If the API call or job fails.
        """
        response = self._request("DELETE", f"/v2/storage/dev-branches/{branch_id}")
        self._wait_for_storage_job(response.json())

    def list_dev_branches(self) -> list[dict[str, Any]]:
        """List development branches for the project.

        Returns:
            List of branch dicts from the API.
        """
        response = self._request("GET", "/v2/storage/dev-branches")
        return response.json()

    def list_branch_metadata(self, branch_id: int | str = "default") -> list[dict[str, Any]]:
        """List metadata entries on a branch.

        GET /v2/storage/branch/{id}/metadata

        Args:
            branch_id: Branch ID or the literal "default" for the main branch.

        Returns:
            List of metadata dicts with keys: id, key, value, provider, timestamp.
        """
        response = self._request("GET", f"/v2/storage/branch/{branch_id}/metadata")
        return response.json()

    def set_branch_metadata(
        self,
        entries: list[tuple[str, str]],
        branch_id: int | str = "default",
    ) -> list[dict[str, Any]]:
        """Bulk-set metadata key/value pairs on a branch.

        POST /v2/storage/branch/{id}/metadata

        Keboola's endpoint expects PHP-style array indices in the
        form-urlencoded body, e.g.::

            metadata[0][key]=KBC.projectDescription
            metadata[0][value]=My project

        httpx's ``data=`` accepts a mapping of str -> str and URL-encodes it.
        Since each ``metadata[i][...]`` key is unique per index, a plain dict
        preserves both ordering (Python 3.7+) and Keboola's expected shape.

        Args:
            entries: Ordered list of ``(key, value)`` metadata tuples.
            branch_id: Branch ID or the literal "default" for the main branch.

        Returns:
            List of metadata dicts created/updated by the API.
        """
        form: dict[str, str] = {}
        for i, (key, value) in enumerate(entries):
            form[f"metadata[{i}][key]"] = key
            form[f"metadata[{i}][value]"] = value
        response = self._request(
            "POST",
            f"/v2/storage/branch/{branch_id}/metadata",
            data=form,
        )
        return response.json()

    def delete_branch_metadata(
        self,
        metadata_id: int | str,
        branch_id: int | str = "default",
    ) -> None:
        """Delete a single metadata entry on a branch by its numeric ID.

        DELETE /v2/storage/branch/{id}/metadata/{metadataId}

        Args:
            metadata_id: ID of the metadata entry (from ``list_branch_metadata``).
            branch_id: Branch ID or the literal "default" for the main branch.
        """
        self._request(
            "DELETE",
            f"/v2/storage/branch/{branch_id}/metadata/{metadata_id}",
        )

    def get_branch_metadata_value(
        self,
        key: str,
        branch_id: int | str = "default",
    ) -> str | None | object:
        """Return the value for a single metadata key on a branch, or None if absent.

        Convenience wrapper around ``list_branch_metadata`` that filters by key.

        Args:
            key: Metadata key to look up (e.g. "KBC.projectDescription").
            branch_id: Branch ID or the literal "default" for the main branch.

        Returns:
            The string value if the key exists (may be None if the API stored null),
            or ``METADATA_NOT_FOUND`` sentinel if the key is not present.
        """
        for entry in self.list_branch_metadata(branch_id=branch_id):
            if entry.get("key") == key:
                return entry.get("value")
        return METADATA_NOT_FOUND

    def list_buckets(
        self, include: str | None = None, branch_id: int | None = None
    ) -> list[dict[str, Any]]:
        """List storage buckets with optional extended information.

        Args:
            include: Optional include parameter (e.g. "linkedBuckets" for sharing info).
            branch_id: If set, list buckets from a specific dev branch.

        Returns:
            List of bucket dicts from the API.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        params: dict[str, str] = {}
        if include:
            params["include"] = include
        response = self._request("GET", f"{prefix}/buckets", params=params)
        return response.json()

    def list_buckets_with_metadata(self) -> list[dict[str, Any]]:
        """List storage buckets with metadata included.

        Returns:
            List of bucket dicts with metadata fields.
        """
        return self.list_buckets(include="metadata")

    def list_bucket_metadata(
        self,
        bucket_id: str,
        branch_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """List metadata entries on a single storage bucket.

        GET /v2/storage/[branch/{b}/]buckets/{id}/metadata

        Args:
            bucket_id: Bucket ID (e.g. 'in.c-db').
            branch_id: If set, target a specific dev branch.

        Returns:
            List of metadata dicts (id/key/value/provider/timestamp).
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_id = quote(bucket_id, safe="")
        response = self._request("GET", f"{prefix}/buckets/{safe_id}/metadata")
        return response.json()

    def set_bucket_metadata(
        self,
        bucket_id: str,
        entries: list[tuple[str, str]],
        branch_id: int | None = None,
        provider: str = "user",
    ) -> list[dict[str, Any]]:
        """Upsert metadata key/value pairs on a storage bucket.

        POST /v2/storage/buckets/{id}/metadata

        Uses the same PHP-style array form encoding as ``set_branch_metadata``.

        Args:
            bucket_id: Bucket ID (e.g. 'in.c-db').
            entries: Ordered list of ``(key, value)`` metadata tuples.
            branch_id: If set, target a specific dev branch.
            provider: Metadata provider. Defaults to ``"user"`` for
                CLI-originated descriptions; pass ``"system"`` for reserved
                ``KBC.*`` keys (e.g. ``KBC.createdBy.branch.id``) -- the API
                rejects user-provider writes on that namespace.

        Returns:
            Full metadata list for the bucket after the upsert.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_id = quote(bucket_id, safe="")
        form: dict[str, str] = {"provider": provider}
        for i, (key, value) in enumerate(entries):
            form[f"metadata[{i}][key]"] = key
            form[f"metadata[{i}][value]"] = value
        response = self._request("POST", f"{prefix}/buckets/{safe_id}/metadata", data=form)
        return response.json()

    def set_table_metadata(
        self,
        table_id: str,
        entries: list[tuple[str, str]],
        branch_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Upsert metadata key/value pairs on a storage table.

        POST /v2/storage/tables/{id}/metadata

        Provider is always ``"user"`` for CLI-originated descriptions.
        Column-level descriptions use the namespaced key convention
        ``KBC.column.{colname}.description`` stored at table-metadata level
        (Keboola Storage API does not expose a user-writable column-metadata
        endpoint; ``columnMetadata`` is populated exclusively by components).

        Args:
            table_id: Full table ID (e.g. "in.c-bucket.table").
            entries: Ordered list of ``(key, value)`` metadata tuples.
            branch_id: If set, target a specific dev branch.

        Returns:
            Full metadata list for the table after the upsert.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_id = quote(table_id, safe="")
        form: dict[str, str] = {"provider": "user"}
        for i, (key, value) in enumerate(entries):
            form[f"metadata[{i}][key]"] = key
            form[f"metadata[{i}][value]"] = value
        response = self._request("POST", f"{prefix}/tables/{safe_id}/metadata", data=form)
        return response.json()

    def get_bucket_detail(
        self,
        bucket_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Get detailed information about a storage bucket.

        Returns full bucket metadata including sharing/linked info
        (sourceBucket, sourceTable with project references).

        Args:
            bucket_id: Bucket ID (e.g. 'in.c-db').
            branch_id: If set, target a specific dev branch.

        Returns:
            Bucket detail dict from the API.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_id = quote(bucket_id, safe="")
        response = self._request("GET", f"{prefix}/buckets/{safe_id}")
        return response.json()

    def get_table_detail(
        self,
        table_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Get detailed information about a storage table.

        Args:
            table_id: Full table ID (e.g. "in.c-bucket.table").
            branch_id: If set, target a specific dev branch.

        Returns:
            Table detail dict including columns, metadata, bucket info.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_id = quote(table_id, safe="")
        response = self._request("GET", f"{prefix}/tables/{safe_id}")
        return response.json()

    def list_tables(
        self,
        bucket_id: str | None = None,
        branch_id: int | None = None,
        include: str | None = None,
    ) -> list[dict[str, Any]]:
        """List storage tables, optionally filtered by bucket.

        Args:
            bucket_id: If set, list tables only from this bucket.
            branch_id: If set, target a specific dev branch.
            include: Optional include parameter (e.g. 'columns').

        Returns:
            List of table dicts from the API.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        params: dict[str, str] = {}
        if include:
            params["include"] = include
        if bucket_id:
            safe_id = quote(bucket_id, safe="")
            response = self._request("GET", f"{prefix}/buckets/{safe_id}/tables", params=params)
        else:
            response = self._request("GET", f"{prefix}/tables", params=params)
        return response.json()

    # ------------------------------------------------------------------
    # Bucket sharing & linking
    # ------------------------------------------------------------------

    def list_shared_buckets(self, include: str | None = None) -> list[dict[str, Any]]:
        """List buckets shared into the current project's organization.

        GET /v2/storage/shared-buckets

        Args:
            include: Optional include parameter (e.g. "metadata").

        Returns:
            List of shared bucket dicts.
        """
        params: dict[str, str] = {}
        if include:
            params["include"] = include
        response = self._request("GET", "/v2/storage/shared-buckets", params=params)
        return response.json()

    def share_bucket(
        self,
        bucket_id: str,
        sharing_type: str,
        target_project_ids: list[int] | None = None,
        target_users: list[str] | None = None,
    ) -> dict[str, Any]:
        """Enable sharing on a bucket (async, waits for completion).

        Args:
            bucket_id: Bucket ID to share (e.g. "out.c-data").
            sharing_type: One of "organization", "organization-project",
                          "selected-projects", "selected-users".
            target_project_ids: Required for "selected-projects" type.
            target_users: Required for "selected-users" type (email addresses).

        Returns:
            Completed storage job dict.

        Raises:
            KeboolaApiError: If the share operation fails (e.g. 403 for non-master token).
        """
        safe_id = quote(bucket_id, safe="")

        endpoint_map = {
            "organization": f"/v2/storage/buckets/{safe_id}/share-organization",
            "organization-project": f"/v2/storage/buckets/{safe_id}/share-organization-project",
            "selected-projects": f"/v2/storage/buckets/{safe_id}/share-to-projects",
            "selected-users": f"/v2/storage/buckets/{safe_id}/share-to-users",
        }

        endpoint = endpoint_map.get(sharing_type)
        if not endpoint:
            raise KeboolaApiError(
                message=f"Invalid sharing type: '{sharing_type}'. "
                f"Valid types: {', '.join(endpoint_map.keys())}",
                status_code=400,
                error_code=ErrorCode.INVALID_SHARING_TYPE,
                retryable=False,
            )

        data: dict[str, Any] = {}
        if sharing_type == "selected-projects" and target_project_ids:
            data["targetProjectIds"] = [str(pid) for pid in target_project_ids]
        elif sharing_type == "selected-users" and target_users:
            data["targetUsers"] = target_users

        response = self._request("POST", endpoint, params={"async": "true"}, data=data)
        return self._wait_for_storage_job(response.json())

    def change_sharing_type(
        self,
        bucket_id: str,
        sharing_type: str,
    ) -> dict[str, Any]:
        """Change the sharing type of an already-shared bucket (async).

        PUT /v2/storage/buckets/{bucket_id}/share

        Args:
            bucket_id: Bucket ID.
            sharing_type: "organization" or "organization-project".

        Returns:
            Completed storage job dict.
        """
        safe_id = quote(bucket_id, safe="")
        response = self._request(
            "PUT",
            f"/v2/storage/buckets/{safe_id}/share",
            json={"sharing": sharing_type},
            params={"async": "true"},
        )
        return self._wait_for_storage_job(response.json())

    def unshare_bucket(self, bucket_id: str) -> dict[str, Any]:
        """Disable sharing on a bucket (async, waits for completion).

        DELETE /v2/storage/buckets/{bucket_id}/share

        Prerequisite: no linked buckets exist in other projects.

        Returns:
            Completed storage job dict.
        """
        safe_id = quote(bucket_id, safe="")
        response = self._request(
            "DELETE",
            f"/v2/storage/buckets/{safe_id}/share",
            params={"async": "true"},
        )
        return self._wait_for_storage_job(response.json())

    def link_bucket(
        self,
        source_project_id: int,
        source_bucket_id: str,
        name: str,
        stage: str = "in",
    ) -> dict[str, Any]:
        """Link a shared bucket from another project (async, waits for completion).

        POST /v2/storage/buckets (with sourceProjectId + sourceBucketId)

        Args:
            source_project_id: Project ID that owns the shared bucket.
            source_bucket_id: Bucket ID in the source project.
            name: Display name for the linked bucket in this project.
            stage: Bucket stage ("in" or "out"). Defaults to "in".

        Returns:
            Completed storage job dict with linked bucket info in results.
        """
        response = self._request(
            "POST",
            "/v2/storage/buckets",
            params={"async": "true"},
            data={
                "stage": stage,
                "name": name,
                "displayName": name,
                "sourceProjectId": source_project_id,
                "sourceBucketId": source_bucket_id,
            },
        )
        return self._wait_for_storage_job(response.json())

    def delete_bucket(
        self, bucket_id: str, force: bool = False, branch_id: int | None = None
    ) -> dict[str, Any]:
        """Delete a bucket (async, waits for completion).

        Used for unlinking shared buckets or deleting regular buckets.

        Args:
            bucket_id: Bucket ID to delete.
            force: If True, delete even if bucket contains tables.
            branch_id: If set, target a specific dev branch.

        Returns:
            Completed storage job dict.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_id = quote(bucket_id, safe="")
        params: dict[str, str] = {"async": "true"}
        if force:
            params["force"] = "true"
        response = self._request("DELETE", f"{prefix}/buckets/{safe_id}", params=params)
        return self._wait_for_storage_job(response.json())

    def create_bucket(
        self,
        stage: str,
        name: str,
        description: str | None = None,
        backend: str | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a new storage bucket (sync).

        Args:
            stage: Bucket stage — "in" or "out".
            name: Bucket name slug (e.g. "my-bucket").
            description: Optional description.
            backend: Optional backend type (e.g. "snowflake", "bigquery").
            branch_id: If set, create bucket in a specific dev branch.

        Returns:
            New bucket dict from the API.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        body: dict[str, str] = {"stage": stage, "name": name}
        if description is not None:
            body["description"] = description
        if backend is not None:
            body["backend"] = backend
        response = self._request("POST", f"{prefix}/buckets", json=body)
        return response.json()

    def create_table(
        self,
        bucket_id: str,
        name: str,
        columns: list[dict[str, Any]] | None = None,
        primary_key: list[str] | None = None,
        branch_id: int | None = None,
        source: dict[str, Any] | None = None,
        time_partitioning: dict[str, Any] | None = None,
        range_partitioning: dict[str, Any] | None = None,
        clustering: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new table with typed columns (async, waits for completion).

        Hits the typed ``tables-definition`` endpoint. Exactly one of ``columns``
        or ``source`` is expected (the caller enforces this): with ``columns`` an
        empty table is created from the definition; with ``source`` (BigQuery
        only) the new table's schema is derived from the source table and its
        rows are copied into the requested partition/clustering layout.

        Args:
            bucket_id: Target bucket ID (e.g. "in.c-my-bucket").
            name: Table name.
            columns: List of column dicts with "name" and "definition.type" keys,
                     e.g. [{"name": "id", "definition": {"type": "INTEGER"}}].
                     Omitted when ``source`` is set (forbidden together).
            primary_key: Optional list of column names for the primary key.
            branch_id: If set, create table in a specific dev branch.
            source: Optional ``{"tableId": str, "branchId"?: int}`` to copy the new
                    table from (BigQuery only). Forbidden together with ``columns``.
            time_partitioning: Optional ``{"type": str, "field"?: str,
                    "expirationMs"?: str}`` (BigQuery). Mutually exclusive with
                    ``range_partitioning``.
            range_partitioning: Optional ``{"field": str, "range": {"start": str,
                    "end": str, "interval": str}}`` (BigQuery).
            clustering: Optional ``{"fields": list[str]}`` (BigQuery).

        Returns:
            Completed storage job results dict.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_id = quote(bucket_id, safe="")
        body: dict[str, Any] = {
            "name": name,
            "primaryKeysNames": primary_key or [],
        }
        # Send exactly one of columns / source. The Storage API rejects supplying
        # both; the service layer enforces this before we get here.
        if columns is not None:
            body["columns"] = columns
        if source is not None:
            body["source"] = source
        if time_partitioning is not None:
            body["timePartitioning"] = time_partitioning
        if range_partitioning is not None:
            body["rangePartitioning"] = range_partitioning
        if clustering is not None:
            body["clustering"] = clustering
        response = self._request("POST", f"{prefix}/buckets/{safe_id}/tables-definition", json=body)
        job = self._wait_for_storage_job(response.json())
        return job.get("results", {})

    def prepare_file_upload(
        self,
        name: str,
        size_bytes: int,
        tags: list[str] | None = None,
        is_permanent: bool = False,
        notify: bool = False,
    ) -> dict[str, Any]:
        """Register a file with the Storage API and get a presigned upload URL.

        Step 1 of the async table upload flow.

        Args:
            name: Filename (e.g. "data.csv").
            size_bytes: File size in bytes.
            tags: Optional list of tags to assign to the file.
            is_permanent: If True, file is not auto-deleted after 15 days.
            notify: If True, send notification on upload completion.

        Returns:
            File resource dict including 'id' (fileId), 'url', 'uploadParams',
            and 'gcsUploadParams' (present on GCP stacks; contains bearer token
            and GCS bucket/key for direct PUT upload).
        """
        # federationToken=1 is required on newer stacks (AWS, Azure) to get
        # cloud-native credentials instead of deprecated presigned POST fields.
        body: dict[str, Any] = {"name": name, "sizeBytes": size_bytes, "federationToken": "1"}
        if is_permanent:
            body["isPermanent"] = "1"
        if notify:
            body["notify"] = "1"
        if tags:
            for i, tag in enumerate(tags):
                body[f"tags[{i}]"] = tag
        response = self._request("POST", "/v2/storage/files/prepare", data=body)
        return response.json()

    def _upload_to_cloud(
        self,
        upload_info: dict[str, Any],
        file_path: str,
    ) -> None:
        """Upload a file to cloud storage using credentials from files/prepare.

        Four upload paths based on what the API returns:

        GCP stack (``gcsUploadParams`` present):
            PUT to ``https://storage.googleapis.com/{bucket}/{key}`` with an
            OAuth2 ``Authorization: Bearer`` header.

        Azure stack (``absUploadParams`` present):
            PUT to ABS container URL constructed from SASConnectionString
            with ``x-ms-blob-type: BlockBlob`` header.

        AWS stack with federation (``uploadParams.credentials`` present):
            PUT to ``https://{bucket}.s3.{region}.amazonaws.com/{key}``
            with AWS SigV4 signed headers.

        Legacy S3 presigned POST (``uploadParams`` without credentials):
            Multipart form POST — deprecated on newer stacks.

        Args:
            upload_info: Full response dict from prepare_file_upload().
            file_path: Local path to the file.
        """
        p = Path(file_path)

        gcs_params = upload_info.get("gcsUploadParams")
        abs_params = upload_info.get("absUploadParams")
        upload_params = upload_info.get("uploadParams") or {}

        if gcs_params:
            # GCP: PUT via GCS JSON API with short-lived OAuth2 bearer token
            bucket = gcs_params["bucket"]
            key = gcs_params["key"]
            access_token = gcs_params["access_token"]
            upload_url = f"https://storage.googleapis.com/{bucket}/{key}"
            with p.open("rb") as fh, httpx.Client(timeout=FILE_UPLOAD_TIMEOUT) as http:
                response = http.put(
                    upload_url,
                    content=fh,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            success_codes = (200,)
        elif abs_params:
            # Azure Blob Storage: PUT with write-capable SAS from absUploadParams
            upload_url = _build_abs_upload_url(abs_params)
            with p.open("rb") as fh, httpx.Client(timeout=FILE_UPLOAD_TIMEOUT) as http:
                response = http.put(
                    upload_url,
                    content=fh,
                    headers={"x-ms-blob-type": "BlockBlob"},
                )
            success_codes = (200, 201)
        elif upload_params.get("credentials"):
            # AWS S3 with federation token: PUT with SigV4 signed headers
            creds = upload_params["credentials"]
            bucket = upload_params["bucket"]
            key = upload_params["key"]
            region = upload_info.get("region", "us-east-1")
            upload_url = f"https://{bucket}.s3.{region}.amazonaws.com/{key}"
            with p.open("rb") as fh:
                file_bytes = fh.read()
            headers = _s3_signed_headers(
                upload_url, creds, region, method="PUT", payload=file_bytes
            )
            with httpx.Client(timeout=FILE_UPLOAD_TIMEOUT) as http:
                response = http.put(upload_url, content=file_bytes, headers=headers)
            success_codes = (200,)
        elif upload_params:
            # Legacy S3 presigned POST: multipart form — uploadParams first, file last
            url = upload_info["url"]
            with httpx.Client(timeout=FILE_UPLOAD_TIMEOUT) as http:
                form_fields: list[tuple[str, Any]] = [
                    (k, (None, str(v))) for k, v in upload_params.items()
                ]
                with p.open("rb") as fh:
                    form_fields.append(("file", (p.name, fh, "application/octet-stream")))
                    response = http.post(url, files=form_fields)
            success_codes = (200, 204)
        else:
            # Fallback: signed URL PUT (no extra auth needed)
            url = upload_info["url"]
            with p.open("rb") as fh, httpx.Client(timeout=FILE_UPLOAD_TIMEOUT) as http:
                response = http.put(url, content=fh)
            success_codes = (200, 201)

        if response.status_code not in success_codes:
            # The provider's error body names the exact denial (service
            # account, missing permission) -- essential when diagnosing e.g.
            # a platform-side IAM misconfiguration -- but it may embed signed
            # URLs, so the full text goes to the DEBUG log only; the raised
            # message carries just the short whitelisted error code.
            logger.debug(
                "Cloud storage error response (HTTP %d): %s",
                response.status_code,
                response.text[:CLOUD_UPLOAD_ERROR_BODY_LIMIT],
            )
            provider_code = _extract_cloud_error_code(response)
            code_suffix = f", {provider_code}" if provider_code else ""
            raise KeboolaApiError(
                message=(f"Cloud storage upload failed (HTTP {response.status_code}{code_suffix})"),
                status_code=response.status_code,
                error_code=ErrorCode.UPLOAD_FAILED,
                retryable=False,
            )

    def import_table_async(
        self,
        table_id: str,
        file_id: int,
        incremental: bool = False,
        delimiter: str = ",",
        enclosure: str = '"',
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Trigger async import of a pre-uploaded file into a table (step 3).

        Polls until the import job completes (up to IMPORT_JOB_MAX_WAIT seconds).

        Args:
            table_id: Target table ID (e.g. "in.c-my-bucket.my-table").
            file_id: File ID returned by prepare_file_upload().
            incremental: If True, append rows; if False, full load.
            delimiter: CSV column delimiter.
            enclosure: CSV value enclosure character.
            branch_id: If set, target a specific dev branch.

        Returns:
            Completed import job dict.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_id = quote(table_id, safe="")
        body: dict[str, str] = {
            "dataFileId": str(file_id),
            "incremental": "1" if incremental else "0",
            "delimiter": delimiter,
            "enclosure": enclosure,
        }
        response = self._request("POST", f"{prefix}/tables/{safe_id}/import-async", data=body)
        return self._wait_for_storage_job(response.json(), max_wait=IMPORT_JOB_MAX_WAIT)

    def upload_table(
        self,
        table_id: str,
        file_path: str,
        incremental: bool = False,
        delimiter: str = ",",
        enclosure: str = '"',
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Upload a CSV file into an existing table (async, waits for completion).

        Uses the file-first async flow to support files up to 5 GB:
        1. Register file with Storage API → get presigned cloud upload URL
        2. Upload file bytes directly to cloud storage (GCP bearer token, S3 presigned POST, or signed URL PUT)
        3. Trigger import-async job → poll until complete

        Args:
            table_id: Target table ID (e.g. "in.c-my-bucket.my-table").
            file_path: Local path to the CSV file.
            incremental: If True, append rows; if False (default), full load.
            delimiter: CSV column delimiter (default ",").
            enclosure: CSV value enclosure character (default '"').
            branch_id: If set, target a specific dev branch.

        Returns:
            Import results dict with importedRowsCount, warnings, etc.
        """
        p = Path(file_path)
        size_bytes = p.stat().st_size
        upload_info = self.prepare_file_upload(name=p.name, size_bytes=size_bytes)
        file_id = upload_info["id"]
        self._upload_to_cloud(upload_info, file_path)
        job = self.import_table_async(
            table_id=table_id,
            file_id=file_id,
            incremental=incremental,
            delimiter=delimiter,
            enclosure=enclosure,
            branch_id=branch_id,
        )
        return job.get("results", {})

    def delete_table(
        self,
        table_id: str,
        branch_id: int | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Delete a storage table (async, waits for completion).

        Args:
            table_id: Full table ID (e.g. "in.c-bucket.table").
            branch_id: If set, target a specific dev branch.
            force: If True, cascade-delete the table and all its aliases.

        Returns:
            Completed storage job dict.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_id = quote(table_id, safe="")
        params: dict[str, str] = {"async": "true"}
        if force:
            params["force"] = "true"
        response = self._request("DELETE", f"{prefix}/tables/{safe_id}", params=params)
        return self._wait_for_storage_job(response.json())

    def truncate_table(
        self,
        table_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Truncate a storage table (delete all rows; preserve schema).

        The Storage API requires the ``allowTruncate=1`` safety opt-in to
        confirm the caller intends to remove every row when no filter
        clauses are sent. The endpoint is inherently asynchronous on
        every branch -- it always returns ``HTTP 202`` with a queued
        storage job (``operationName: tableRowsDelete``), which
        ``_wait_for_storage_job`` polls to completion. Passing
        ``async=true`` is rejected by the API as an unknown field, so
        we do NOT send it (this is a deliberate departure from
        ``delete_table``'s contract -- see the truncate-table gotcha
        in plugins/.../gotchas.md for the live-API evidence).

        The table definition (columns, types, primary key, descriptions,
        sharing edges, and dependents) is preserved -- only the rows
        are removed.

        Args:
            table_id: Full table ID (e.g. "in.c-bucket.table").
            branch_id: If set, target a specific dev branch.

        Returns:
            Completed storage job dict.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_id = quote(table_id, safe="")
        params: dict[str, str] = {"allowTruncate": "1"}
        response = self._request("DELETE", f"{prefix}/tables/{safe_id}/rows", params=params)
        return self._wait_for_storage_job(response.json())

    def delete_column(
        self,
        table_id: str,
        column_name: str,
        branch_id: int | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Delete a column from a storage table (async, waits for completion).

        Args:
            table_id: Full table ID (e.g. "in.c-bucket.table").
            column_name: Name of the column to delete.
            branch_id: If set, target a specific dev branch.
            force: If True, also delete from aliased tables.

        Returns:
            Completed storage job dict.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_table_id = quote(table_id, safe="")
        safe_column = quote(column_name, safe="")
        params: dict[str, str] = {"async": "true"}
        if force:
            params["force"] = "true"
        response = self._request(
            "DELETE",
            f"{prefix}/tables/{safe_table_id}/columns/{safe_column}",
            params=params,
        )
        return self._wait_for_storage_job(response.json())

    def swap_tables(
        self,
        table_id: str,
        target_table_id: str,
        branch_id: int,
    ) -> dict[str, Any]:
        """Swap two storage tables (async, waits for completion; branch-scoped).

        Both tables exchange physical positions; aliases keep pointing at the
        same physical position and therefore expose the OTHER table's data
        after the swap. ``branch_id`` is mandatory (the swap is always scoped
        to a branch), but ANY branch works -- including the default/production
        branch. A default-branch swap is the supported way to retype a prod
        table, because dev-branch merge does not propagate storage schema.

        The API returns a queued storage job (``operationName: tableSwap``)
        which this method polls to completion before returning, mirroring
        ``delete_table`` semantics. (The PHP reference client returns the
        raw initial response, but the operation is asynchronous on every
        backend tested -- callers expect a finished swap on return.)

        Args:
            table_id: Full ID of the first table (e.g. "in.c-bucket.table").
            target_table_id: Full ID of the second table to swap with.
            branch_id: Development branch ID. Required by the API.

        Returns:
            Completed storage job dict.
        """
        prefix = f"/v2/storage/branch/{branch_id}"
        safe_id = quote(table_id, safe="")
        body = {"targetTableId": target_table_id}
        response = self._request("POST", f"{prefix}/tables/{safe_id}/swap", json=body)
        return self._wait_for_storage_job(response.json())

    def pull_table(self, table_id: str, branch_id: int) -> dict[str, Any]:
        """Pull (clone) a table from the default branch into a dev branch.

        On ``storage-branches`` projects a dev branch reads production tables
        transparently (copy-on-write) until the first write. Operations that
        mutate a table in the branch -- such as ``swap_tables`` or a column
        drop -- require a branch-local materialization of the table first;
        otherwise the Storage API reports the bucket as "not found" in the
        branch. This endpoint performs that materialization: it copies the
        table from the default (production) branch into the branch's isolated
        storage. It is the same call the platform issues on a branch's first
        write to a production table.

        The pull is one-way (default -> branch). The API returns a queued
        storage job which this method polls to completion before returning,
        mirroring ``swap_tables`` semantics.

        Args:
            table_id: Full ID of the table to pull (e.g. "in.c-bucket.table").
            branch_id: Target development branch ID. The source is always the
                default/production branch.

        Returns:
            Completed storage job dict.
        """
        prefix = f"/v2/storage/branch/{branch_id}"
        safe_id = quote(table_id, safe="")
        response = self._request("POST", f"{prefix}/tables/{safe_id}/pull")
        return self._wait_for_storage_job(response.json())

    def list_tables_with_metadata(self) -> list[dict[str, Any]]:
        """List all storage tables with columns and metadata.

        Returns:
            List of table dicts with columns, metadata, and bucket info.
        """
        return self.list_tables(include="columns,metadata,buckets")

    @staticmethod
    def _apply_table_filters(
        params: dict[str, Any],
        *,
        where_column: str | None = None,
        where_operator: str = "eq",
        where_values: list[str] | None = None,
        changed_since: str | None = None,
        changed_until: str | None = None,
    ) -> None:
        """Mutate ``params`` with Storage table export/preview filter clauses.

        Shared by :meth:`get_table_data_preview` and :meth:`export_table_async`
        so the ``whereColumn`` / ``whereOperator`` / ``whereValues[]`` and
        ``changedSince`` / ``changedUntil`` contract is identical across the
        sync-preview and async-export endpoints.

        Args:
            where_column: Column to filter on. Must be paired with ``where_values``.
            where_operator: ``"eq"`` or ``"neq"`` (only meaningful with a filter).
            where_values: Values the column is matched against (OR within the set).
            changed_since: Lower bound on import time -- a unix timestamp or a
                strtotime string like ``"-2 days"``.
            changed_until: Upper bound on import time (same formats).

        Raises:
            ValueError: On an invalid ``where_operator`` or a half-specified
                where-clause (a column without values, or values without a column).
        """
        if (where_column is None) != (where_values is None):
            raise ValueError(
                "where_column and where_values must be given together "
                "(the column to match and the values to match it against)."
            )
        if where_column is not None:
            if where_operator not in ("eq", "neq"):
                raise ValueError(f"where_operator must be 'eq' or 'neq', got {where_operator!r}.")
            params["whereColumn"] = where_column
            params["whereOperator"] = where_operator
            params["whereValues[]"] = where_values
        if changed_since is not None:
            params["changedSince"] = changed_since
        if changed_until is not None:
            params["changedUntil"] = changed_until

    def get_table_data_preview(
        self,
        table_id: str,
        limit: int = 100,
        columns: list[str] | None = None,
        *,
        where_column: str | None = None,
        where_operator: str = "eq",
        where_values: list[str] | None = None,
        changed_since: str | None = None,
        changed_until: str | None = None,
    ) -> str:
        """Get a CSV preview of table data.

        Args:
            table_id: Full table ID (e.g. "in.c-bucket.table").
            limit: Max number of rows to return.
            columns: Optional list of column names to export.
                     Storage API limits sync export to 30 columns max.
            where_column: Filter to rows where this column matches ``where_values``.
            where_operator: ``"eq"`` (default) or ``"neq"``.
            where_values: Values for the ``where_column`` filter.
            changed_since: Only rows imported since this time (unix ts / strtotime).
            changed_until: Only rows imported up to this time.

        Returns:
            CSV string with table data preview.
        """
        safe_id = quote(table_id, safe="")
        params: dict[str, Any] = {"limit": limit}
        if columns:
            params["columns"] = ",".join(columns)
        self._apply_table_filters(
            params,
            where_column=where_column,
            where_operator=where_operator,
            where_values=where_values,
            changed_since=changed_since,
            changed_until=changed_until,
        )
        response = self._request(
            "GET",
            f"/v2/storage/tables/{safe_id}/data-preview",
            params=params,
        )
        return response.text

    def export_table_async(
        self,
        table_id: str,
        columns: list[str] | None = None,
        limit: int | None = None,
        branch_id: int | None = None,
        file_type: str = "csv",
        *,
        where_column: str | None = None,
        where_operator: str = "eq",
        where_values: list[str] | None = None,
        changed_since: str | None = None,
        changed_until: str | None = None,
    ) -> dict[str, Any]:
        """Start an async table export and wait for completion.

        Args:
            table_id: Full table ID (e.g. "in.c-bucket.table").
            columns: Optional list of column names to export.
            limit: Optional max number of rows to export.
            branch_id: If set, target a specific dev branch.
            file_type: Output format, either "csv" (default) or "parquet".
                Parquet exports are always sliced and Snappy-compressed inside
                the parquet format (not gzipped at the slice level).
            where_column: Filter to rows where this column matches ``where_values``.
            where_operator: ``"eq"`` (default) or ``"neq"``.
            where_values: Values for the ``where_column`` filter.
            changed_since: Only rows imported since this time (unix ts / strtotime).
            changed_until: Only rows imported up to this time.

        Returns:
            Completed export job dict (results contain file info).
        """
        if file_type not in ("csv", "parquet"):
            raise ValueError(f"file_type must be 'csv' or 'parquet', got {file_type!r}")
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_id = quote(table_id, safe="")
        params: dict[str, Any] = {"fileType": file_type}
        if columns:
            params["columns"] = ",".join(columns)
        if limit is not None:
            params["limit"] = str(limit)
        self._apply_table_filters(
            params,
            where_column=where_column,
            where_operator=where_operator,
            where_values=where_values,
            changed_since=changed_since,
            changed_until=changed_until,
        )
        response = self._request(
            "POST",
            f"{prefix}/tables/{safe_id}/export-async",
            data=params,
        )
        return self._wait_for_storage_job(response.json(), max_wait=EXPORT_JOB_MAX_WAIT)

    def add_column(
        self,
        table_id: str,
        name: str,
        definition: dict[str, Any] | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Add a single column to an existing table (synchronous).

        Unlike ``delete_column`` (async storage job), the Storage API
        ``POST /tables/{id}/columns`` endpoint is synchronous and returns the
        updated table resource directly -- there is no job to poll.

        Args:
            table_id: Full table ID (e.g. "in.c-bucket.table").
            name: Name of the new column.
            definition: Optional typed-column definition for a typed table, e.g.
                ``{"type": "NUMBER", "length": "18,2", "nullable": False,
                "default": "0"}``. Omit for an untyped column.
            branch_id: If set, target a specific dev branch.

        Returns:
            The updated table resource dict from the API.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_id = quote(table_id, safe="")
        body: dict[str, Any] = {"name": name}
        if definition:
            body["definition"] = definition
        response = self._request("POST", f"{prefix}/tables/{safe_id}/columns", json=body)
        return response.json()

    def get_file_info(self, file_id: int, branch_id: int | None = None) -> dict[str, Any]:
        """Get file metadata including download URL.

        Args:
            file_id: Storage file ID (from export job results).
            branch_id: If set, query file from a specific dev branch scope.

        Returns:
            File resource dict with 'url', 'isSliced', 'sizeBytes', etc.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        response = self._request(
            "GET",
            f"{prefix}/files/{file_id}",
            params={"federationToken": "1"},
        )
        return response.json()

    def list_files(
        self,
        limit: int = 20,
        offset: int = 0,
        tags: list[str] | None = None,
        since_id: int | None = None,
        query: str | None = None,
        branch_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """List Storage Files with optional filtering.

        Args:
            limit: Max number of files to return.
            offset: Pagination offset.
            tags: Filter by tags (AND logic — all tags must match).
            since_id: Return only files with ID greater than this.
            query: Full-text search query on file name.
            branch_id: If set, list files from a specific dev branch.

        Returns:
            List of file resource dicts.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if tags:
            for i, tag in enumerate(tags):
                params[f"tags[{i}]"] = tag
        if since_id is not None:
            params["sinceId"] = since_id
        if query:
            params["q"] = query
        response = self._request("GET", f"{prefix}/files", params=params)
        return response.json()

    def upload_file(
        self,
        file_path: str,
        name: str | None = None,
        tags: list[str] | None = None,
        is_permanent: bool = False,
        notify: bool = False,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Upload a local file to Storage Files.

        Wraps prepare_file_upload + _upload_to_cloud into a single call.

        Args:
            file_path: Local path to the file to upload.
            name: Custom filename (defaults to local file basename).
            tags: Optional list of tags to assign.
            is_permanent: If True, file is not auto-deleted after 15 days.
            notify: If True, send notification on upload completion.
            branch_id: If set, upload to a specific dev branch.

        Returns:
            File resource dict with id, name, sizeBytes, tags, url.
        """
        p = Path(file_path)
        size_bytes = p.stat().st_size
        file_name = name or p.name
        upload_info = self.prepare_file_upload(
            name=file_name,
            size_bytes=size_bytes,
            tags=tags,
            is_permanent=is_permanent,
            notify=notify,
        )
        self._upload_to_cloud(upload_info, file_path)
        # Return file info (prepare response has the file metadata)
        return {
            "id": upload_info["id"],
            "name": upload_info.get("name", file_name),
            "sizeBytes": size_bytes,
            "tags": upload_info.get("tags", tags or []),
            "isPermanent": upload_info.get("isPermanent", is_permanent),
            "created": upload_info.get("created"),
        }

    def delete_file(self, file_id: int, branch_id: int | None = None) -> None:
        """Delete a Storage File.

        Args:
            file_id: Storage file ID.
            branch_id: If set, target a file in a specific dev branch scope.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        self._request("DELETE", f"{prefix}/files/{file_id}")

    def tag_file(self, file_id: int, tag: str, branch_id: int | None = None) -> None:
        """Add a tag to a Storage File.

        Args:
            file_id: Storage file ID.
            tag: Tag string to add.
            branch_id: If set, target a file in a specific dev branch scope.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        self._request("POST", f"{prefix}/files/{file_id}/tags", data={"tag": tag})

    def untag_file(self, file_id: int, tag: str, branch_id: int | None = None) -> None:
        """Remove a tag from a Storage File.

        Args:
            file_id: Storage file ID.
            tag: Tag string to remove.
            branch_id: If set, target a file in a specific dev branch scope.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_tag = quote(tag, safe="")
        self._request("DELETE", f"{prefix}/files/{file_id}/tags/{safe_tag}")

    def download_sliced_file(self, file_detail: dict[str, Any], output_path: str) -> int:
        """Download a sliced file by fetching manifest and concatenating slices.

        Handles S3 (SigV4 auth) and GCS (bearer token) providers.
        Decompresses gzipped slices transparently.

        Streams each slice chunk-by-chunk into a temp file and concatenates
        into ``output_path``. Peak RAM is O(chunk size), not O(slice size) —
        required for multi-GB tables on memory-constrained hosts (issue #187).

        The manifest `url` from file info is already a presigned URL (download
        directly). Manifest entries have cloud-native URLs (s3://, gs://) that
        need auth — we build HTTPS URLs from the s3Path/gcsPath credentials.

        Args:
            file_detail: Full file info dict from get_file_info()
                (must include provider credentials from federationToken=1).
            output_path: Local file path to write to.

        Returns:
            Number of bytes written.
        """
        import shutil
        import tempfile

        entries, base_url, downloader, _manifest_data = self._prepare_sliced_download(file_detail)

        # Stream each slice into a temp file, then copy-append into output.
        # Keeping per-slice temp files on disk (not in RAM) is the whole point.
        total = 0
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("wb") as out_fh:
            for entry in entries:
                entry_url = entry.get("url", "")
                slice_url = downloader.resolve_slice_url(base_url, entry_url, file_detail)
                is_gz = entry_url.split("?")[0].endswith(".gz")
                with tempfile.NamedTemporaryFile(
                    dir=out_path.parent, prefix=".slice-", delete=True
                ) as tmp:
                    downloader.stream_to_file(slice_url, tmp.name, decompress_gzip=is_gz)
                    tmp.seek(0)
                    shutil.copyfileobj(tmp, out_fh, length=FILE_DOWNLOAD_CHUNK_SIZE)
                    total += Path(tmp.name).stat().st_size

        return total

    def _prepare_sliced_download(
        self, file_detail: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], str, "_CloudDownloader", bytes]:
        """Fetch and parse the manifest, returning entries + download context.

        The manifest is small JSON (few KB even for TB tables), so loading it
        fully is fine. Entries are the per-slice URLs that callers iterate.

        Returns a 4-tuple: (entries, base_url, downloader, raw_manifest_bytes).
        The raw manifest is useful for callers that persist it next to slices.
        """
        import json as json_mod

        provider = file_detail.get("provider", "")
        downloader = _CloudDownloader.create(file_detail)

        _assert_safe_download_url(file_detail["url"])
        with httpx.Client(timeout=FILE_DOWNLOAD_TIMEOUT) as http:
            resp = http.get(file_detail["url"])
            resp.raise_for_status()
            manifest_data = resp.content

        manifest = json_mod.loads(manifest_data)
        entries = manifest.get("entries", [])
        if not entries:
            raise KeboolaApiError(
                message="Sliced file manifest has no entries",
                status_code=500,
                error_code=ErrorCode.EXPORT_EMPTY_MANIFEST,
                retryable=False,
            )

        logger.info("Downloading %d slices (provider=%s)", len(entries), provider)
        base_url = downloader.resolve_base_url(file_detail)
        return entries, base_url, downloader, manifest_data

    def download_sliced_file_to_dir(
        self, file_detail: dict[str, Any], output_dir: str
    ) -> dict[str, Any]:
        """Download a sliced file preserving each slice as a separate local file.

        Unlike download_sliced_file() which binary-concatenates slices, this
        writes every manifest entry into its own file under ``output_dir``.
        Required for formats like Parquet where each slice is a self-contained
        file with its own footer and cannot be safely concatenated.

        The original manifest is also written to ``output_dir/_manifest.json``
        so the slice set stays self-describing. The leading underscore follows
        the Hive/Spark/pyarrow convention that makes Parquet readers skip the
        file when scanning the directory as a dataset.

        Gzip-compressed slices (typical for CSV) are decompressed transparently
        and the ``.gz`` suffix is stripped from the written filename. Parquet
        slices are written as-is (Snappy compression lives inside the format).

        Args:
            file_detail: Full file info dict from get_file_info() with
                federationToken=1 provider credentials.
            output_dir: Directory to write slices into. Created if missing.

        Returns:
            Dict with ``output_dir``, ``slice_count``, ``total_bytes``, and
            ``slices`` (list of ``{path, size_bytes}``).
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        entries, base_url, downloader, manifest_data = self._prepare_sliced_download(file_detail)

        # Persist the manifest alongside slices for traceability.
        (out / "_manifest.json").write_bytes(manifest_data)

        slices: list[dict[str, Any]] = []
        total = 0

        for idx, entry in enumerate(entries):
            entry_url = entry.get("url", "")
            slice_url = downloader.resolve_slice_url(base_url, entry_url, file_detail)

            clean_url = entry_url.split("?")[0]
            basename = clean_url.rsplit("/", 1)[-1]
            is_gz = clean_url.endswith(".gz")
            if is_gz:
                basename = basename.removesuffix(".gz")
            if not basename:
                basename = f"part-{idx:05d}"

            slice_path = out / basename
            written = downloader.stream_to_file(slice_url, slice_path, decompress_gzip=is_gz)
            slices.append({"path": str(slice_path.resolve()), "size_bytes": written})
            total += written

        return {
            "output_dir": str(out.resolve()),
            "slice_count": len(slices),
            "total_bytes": total,
            "slices": slices,
        }

    def download_file(self, url: str, output_path: str) -> int:
        """Download a non-sliced file from a presigned URL.

        Streams the body chunk-by-chunk and decompresses gzip on the fly, so
        peak RAM stays at O(chunk size) even for multi-GB payloads (issue #187).

        Args:
            url: Presigned download URL from file info.
            output_path: Local file path to write to.

        Returns:
            Number of bytes written (post-decompression if the URL is gzipped).
        """
        import gzip
        import shutil

        _assert_safe_download_url(url)
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        is_gzipped = url.rstrip("?").split("?")[0].endswith(".gz")

        with (
            httpx.Client(timeout=FILE_DOWNLOAD_TIMEOUT) as http,
            http.stream("GET", url) as response,
        ):
            response.raise_for_status()
            source: Any = _IterBytesReader(response.iter_bytes(FILE_DOWNLOAD_CHUNK_SIZE))
            if is_gzipped:
                source = gzip.GzipFile(fileobj=source, mode="rb")
            with out_path.open("wb") as fh:
                shutil.copyfileobj(source, fh, length=FILE_DOWNLOAD_CHUNK_SIZE)

        return out_path.stat().st_size

    def list_jobs(
        self,
        component_id: str | None = None,
        config_id: str | None = None,
        status: str | None = None,
        limit: int = DEFAULT_JOB_LIMIT,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List jobs from the Queue API.

        Args:
            component_id: Optional filter by component ID.
            config_id: Optional filter by config ID (requires component_id).
            status: Optional filter by job status.
            limit: Max number of jobs to return (1-500).
            offset: Offset for pagination.

        Returns:
            List of job dicts from the Queue API.
        """
        params: dict[str, str | int] = {"limit": limit, "offset": offset}
        if component_id:
            params["component"] = component_id
        if config_id:
            params["config"] = config_id
        if status:
            params["status"] = status

        response = self._queue_request("GET", "/search/jobs", params=params)
        return response.json()

    def list_jobs_grouped(
        self,
        jobs_per_group: int = DEFAULT_JOBS_PER_CONFIG,
        limit: int = DEFAULT_GROUPED_JOBS_LIMIT,
        sort_by: str = "startTime",
        sort_order: str = "desc",
        created_time_from: str | None = None,
    ) -> list[dict[str, Any]]:
        """List jobs grouped by component+config from the Queue API.

        Uses GET /search/grouped-jobs to fetch the latest N jobs for each
        unique component+config combination in a single API call.

        Args:
            jobs_per_group: Max jobs per component+config group (1-500).
            limit: Max number of groups to return (1-500).
            sort_by: Sort field for jobs within each group.
            sort_order: Sort direction ("asc" or "desc").
            created_time_from: Optional ISO datetime filter (e.g. "2026-03-20T00:00:00Z").

        Returns:
            List of group dicts: [{"group": {"componentId": ..., "configId": ...}, "jobs": [...]}]
        """
        params: list[tuple[str, str]] = [
            ("groupBy[]", "componentId"),
            ("groupBy[]", "configId"),
            ("jobsPerGroup", str(jobs_per_group)),
            ("limit", str(limit)),
            ("sortBy", sort_by),
            ("sortOrder", sort_order),
        ]
        if created_time_from:
            params.append(("filters[createdTimeFrom]", created_time_from))

        response = self._queue_request("GET", "/search/grouped-jobs", params=params)
        return response.json()

    def get_job_detail(self, job_id: str) -> dict[str, Any]:
        """Get detailed information about a specific job from the Queue API.

        Args:
            job_id: The job ID.

        Returns:
            Job detail dict from the Queue API.
        """
        safe_job_id = quote(job_id, safe="")
        response = self._queue_request("GET", f"/jobs/{safe_job_id}")
        return response.json()

    # --- Queue Job Creation ---

    def create_job(
        self,
        component_id: str,
        config_id: str,
        config_data: dict[str, Any] | None = None,
        config_row_ids: list[str] | None = None,
        mode: str = "run",
        branch_id: int | None = None,
        variable_values_id: str | None = None,
    ) -> dict[str, Any]:
        """Create and run a Queue API job.

        Args:
            component_id: Component ID (e.g. keboola.sandboxes).
            config_id: Configuration ID.
            config_data: Optional runtime config data override.
            config_row_ids: Optional list of config row IDs to run
                (omit to run entire config).
            mode: Job mode (default: run).
            branch_id: Optional dev branch ID. When set, the job runs
                on that branch instead of the default (production) branch.
            variable_values_id: Optional id of a row in the linked
                ``keboola.variables`` config. When set, the Queue API binds
                the row's values to the job's `{{ variable }}` placeholders.
                Omit for configurations that have no linked variables.

        Returns:
            Job dict from the Queue API.
        """
        body: dict[str, Any] = {
            "component": component_id,
            "config": config_id,
            "mode": mode,
        }
        if branch_id is not None:
            body["branchId"] = str(branch_id)
        if config_data:
            body["configData"] = config_data
        if config_row_ids:
            body["configRowIds"] = config_row_ids
        if variable_values_id:
            body["variableValuesId"] = variable_values_id
        response = self._queue_request("POST", "/jobs", json=body)
        return response.json()

    def kill_job(self, job_id: str) -> dict[str, Any]:
        """Request termination of a running Queue API job.

        Sets the job's desiredStatus to "terminating"; the executor transitions
        the actual status asynchronously (waiting -> cancelled, processing ->
        terminating -> terminated). Poll get_job_detail until isFinished=True
        to observe the terminal state.

        Killable states per Queue API: created, waiting, processing. Calling
        kill on any other state returns HTTP 400 with a "not in one of killable
        states" message; callers that want idempotent behavior (e.g. bulk
        terminate after list_jobs under race conditions) should translate that
        into a no-op success at the service layer.
        """
        safe_job_id = quote(job_id, safe="")
        response = self._queue_request("POST", f"/jobs/{safe_job_id}/kill")
        return response.json()

    def fetch_job_events(self, run_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Fetch events emitted during a job's run.

        Wraps the Storage API's ``GET /v2/storage/events?runId={runId}``
        endpoint -- NOT a Queue API path. Queue jobs (Queue API v2) expose a
        ``runId`` on the job dict (typically equal to the job ``id``); the
        Storage Events API is the canonical event feed for the job. Returns
        the list in Storage API order (newest -> oldest; callers that want
        a chronological "tail" should reverse the slice).

        Args:
            run_id: The job's ``runId`` (``job["runId"]``; falls back to
                ``job["id"]`` on legacy records where they match).
            limit: Optional server-side event cap. Storage API default is
                about 100; pass an explicit value to cover long runs.

        Returns:
            List of event dicts. Each event typically has ``uuid``,
            ``event``, ``component``, ``message``, ``type``, ``created``,
            ``runId``, ``configurationId`` keys. Empty when the run emitted
            no events yet.
        """
        params: dict[str, Any] = {"runId": run_id}
        if limit is not None and limit > 0:
            params["limit"] = limit
        response = self._request("GET", "/v2/storage/events", params=params)
        payload = response.json()
        # Storage events returns a bare list. Tolerate a dict-wrapped
        # future shape defensively.
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("events"), list):
            return payload["events"]
        return []

    def wait_for_queue_job(
        self,
        job_id: str,
        max_wait: float = STORAGE_JOB_MAX_WAIT,
        poll_strategy: str = DEFAULT_POLL_STRATEGY,
    ) -> dict[str, Any]:
        """Poll a Queue API job until it reaches a terminal state.

        Uses the piecewise ``JOB_POLL_CURVE`` from constants for the
        ``"exponential"`` strategy (2s x 30 -> 5s x 48 -> 15s forever) and
        the legacy fixed ``STORAGE_JOB_POLL_INTERVAL`` for ``"fixed"``. The
        curve matches the cadence used by FIIA and the official
        ``keboola-as-code`` Go CLI.

        Args:
            job_id: The Queue job ID.
            max_wait: Maximum seconds to wait (default: STORAGE_JOB_MAX_WAIT).
            poll_strategy: "exponential" (default) or "fixed". Any other
                value raises ValueError before the first network call.

        Returns:
            Completed job dict.

        Raises:
            ValueError: If poll_strategy is not one of VALID_POLL_STRATEGIES.
            KeboolaApiError: If the job fails (QUEUE_JOB_FAILED) or the
                deadline elapses before the job finishes (QUEUE_JOB_TIMEOUT).
        """
        if poll_strategy not in VALID_POLL_STRATEGIES:
            # ValueError (not KeboolaApiError) because this is a programming
            # error -- the caller passed an invalid literal, not a bad API
            # response. JobService validates before reaching this layer, so
            # hitting this path from the CLI would be a bug in kbagent.
            raise ValueError(
                f"Invalid poll_strategy {poll_strategy!r}. "
                f"Expected one of: {sorted(VALID_POLL_STRATEGIES)}."
            )

        deadline = time.monotonic() + max_wait
        for interval in _iter_poll_intervals(poll_strategy):
            job = self.get_job_detail(job_id)
            if job.get("isFinished"):
                if job.get("status") == "error":
                    result = job.get("result", {})
                    error_msg = (
                        result.get("message", "Queue job failed")
                        if isinstance(result, dict)
                        else "Queue job failed"
                    )
                    raise KeboolaApiError(
                        message=f"Queue job {job_id} failed: {error_msg}",
                        status_code=500,
                        error_code=ErrorCode.QUEUE_JOB_FAILED,
                        retryable=False,
                    )
                return job

            # Cap the sleep so we never blow past the deadline by more than
            # one interval: trim to whatever time remains; if zero, break.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))

        raise KeboolaApiError(
            message=f"Queue job {job_id} did not complete within {max_wait}s",
            status_code=504,
            error_code=ErrorCode.QUEUE_JOB_TIMEOUT,
            retryable=True,
        )

    # --- Workspace CRUD ---

    def list_workspaces(self, branch_id: int | None = None) -> list[dict[str, Any]]:
        """List all workspaces in the project."""
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        response = self._request("GET", f"{prefix}/workspaces")
        return response.json()

    def get_workspace(self, workspace_id: int, branch_id: int | None = None) -> dict[str, Any]:
        """Get workspace details (note: password is NOT included)."""
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        response = self._request("GET", f"{prefix}/workspaces/{workspace_id}")
        return response.json()

    def delete_workspace(self, workspace_id: int, branch_id: int | None = None) -> None:
        """Delete a workspace (synchronous)."""
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        self._request("DELETE", f"{prefix}/workspaces/{workspace_id}")

    def reset_workspace_password(
        self, workspace_id: int, branch_id: int | None = None
    ) -> dict[str, Any]:
        """Reset workspace password. Returns new password."""
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        response = self._request("POST", f"{prefix}/workspaces/{workspace_id}/password")
        return response.json()

    def create_sandbox_config(
        self,
        name: str,
        description: str = "",
        backend_size: str = "small",
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a keboola.sandboxes configuration.

        This is needed to make workspaces visible in the Keboola UI.
        The UI only shows workspaces tied to a sandboxes config.

        Args:
            name: Human-readable name for the workspace.
            description: Optional description.
            backend_size: Backend size (small, medium, large).
            branch_id: Branch ID. If provided, creates config in that branch.

        Returns:
            Configuration dict with id, name, etc.
        """
        config = {
            "parameters": {
                "runtime": {"shared": False},
                "storage": {"input": {"tables": []}, "output": {"tables": []}},
                "parameters": {"id": "", "blocks": []},
                "backendSize": backend_size,
            },
            "runtime": {"shared": False},
        }
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        response = self._request(
            "POST",
            f"{prefix}/components/keboola.sandboxes/configs",
            data={
                "name": name,
                "description": description,
                "configuration": json.dumps(config),
            },
        )
        return response.json()

    def delete_config(
        self, component_id: str, config_id: str, branch_id: int | None = None
    ) -> None:
        """Delete a component configuration.

        Args:
            component_id: Component ID.
            config_id: Configuration ID.
            branch_id: Branch ID. If provided, deletes config in that branch.
        """
        safe_component = quote(component_id, safe="")
        safe_config = quote(config_id, safe="")
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        self._request(
            "DELETE",
            f"{prefix}/components/{safe_component}/configs/{safe_config}",
        )

    def create_config_workspace(
        self,
        branch_id: int,
        component_id: str,
        config_id: str,
        backend: str = "snowflake",
        login_type: str | None = None,
        public_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a workspace tied to a specific configuration.

        Args:
            branch_id: Branch ID (use main branch ID for production).
            component_id: Component ID (e.g. keboola.snowflake-transformation).
            config_id: Configuration ID.
            backend: Workspace backend.
            login_type: Optional Storage API loginType. Omitted when None.
            public_key: Optional public key for key-pair workspaces. Omitted when None.

        Returns:
            Workspace dict including connection credentials.
        """
        safe_component = quote(component_id, safe="")
        safe_config = quote(config_id, safe="")
        payload: dict[str, Any] = {"backend": backend}
        if login_type is not None:
            payload["loginType"] = login_type
        if public_key is not None:
            payload["publicKey"] = public_key

        response = self._request(
            "POST",
            f"/v2/storage/branch/{branch_id}/components/{safe_component}/configs/{safe_config}/workspaces",
            json=payload,
        )
        return response.json()

    def list_config_workspaces(
        self,
        branch_id: int,
        component_id: str,
        config_id: str,
    ) -> list[dict[str, Any]]:
        """List workspaces tied to a specific configuration."""
        safe_component = quote(component_id, safe="")
        safe_config = quote(config_id, safe="")
        response = self._request(
            "GET",
            f"/v2/storage/branch/{branch_id}/components/{safe_component}/configs/{safe_config}/workspaces",
        )
        return response.json()

    def load_workspace_tables(
        self,
        workspace_id: int,
        tables: list[dict[str, Any]],
        branch_id: int | None = None,
        preserve: bool = False,
    ) -> dict[str, Any]:
        """Load tables into a workspace (async operation).

        Args:
            workspace_id: Target workspace ID.
            tables: List of table load definitions, each with at minimum:
                - source: table ID (e.g. "in.c-bucket.table")
                - destination: target table name in workspace
            branch_id: Branch ID. Required for workspaces on dev branches.
            preserve: If True, keep existing tables in the workspace. Default is False
                (workspace is cleared before loading).

        Returns:
            Completed storage job dict (polls until done).

        Raises:
            KeboolaApiError: If the load job fails or times out.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        body: dict[str, Any] = {"input": tables, "preserve": preserve}
        response = self._request(
            "POST",
            f"{prefix}/workspaces/{workspace_id}/load",
            json=body,
        )
        return self._wait_for_storage_job(response.json())

    # --- Query Service ---

    def submit_query(
        self,
        branch_id: int,
        workspace_id: int,
        statements: list[str],
        transactional: bool = False,
    ) -> dict[str, Any]:
        """Submit SQL statements to the Query Service.

        Args:
            branch_id: Branch ID.
            workspace_id: Workspace ID.
            statements: List of SQL statements to execute.
            transactional: Whether to wrap in a transaction.

        Returns:
            Query job dict with id and status.
        """
        body: dict[str, Any] = {
            "statements": statements,
            "transactional": transactional,
        }
        response = self._query_request(
            "POST",
            f"/api/v1/branches/{branch_id}/workspaces/{workspace_id}/queries",
            json=body,
        )
        return response.json()

    def get_query_job(self, query_job_id: str) -> dict[str, Any]:
        """Get query job status."""
        response = self._query_request("GET", f"/api/v1/queries/{query_job_id}")
        return response.json()

    def export_query_results(
        self,
        query_job_id: str,
        statement_id: str,
        file_type: str = "csv",
    ) -> str:
        """Export query results as CSV (or other format).

        Returns:
            Raw CSV string of query results.
        """
        response = self._query_request(
            "GET",
            f"/api/v1/queries/{query_job_id}/{statement_id}/export",
            params={"fileType": file_type},
        )
        return response.text

    def get_query_results(
        self,
        query_job_id: str,
        statement_id: str,
        offset: int = 0,
        page_size: int = QUERY_RESULTS_PAGE_SIZE,
    ) -> dict[str, Any]:
        """Fetch a page of inline statement results from the Query Service.

        Unlike :meth:`export_query_results`, which materializes a CSV file via the
        warehouse UNLOAD path (slow), this reads the already-computed result set
        inline as JSON -- much faster for interactive queries. The endpoint is
        paginated; ``offset``/``page_size`` walk the result set.

        Args:
            query_job_id: The query job ID.
            statement_id: The statement ID within the job.
            offset: Row offset to start from (for pagination).
            page_size: Maximum rows to return in this page.

        Returns:
            Raw QueryResult dict, e.g.::

                {
                    "status": "completed",
                    "columns": [{"name": "id", "type": "INTEGER", "nullable": false}],
                    "data": [[1, "a"], [2, "b"]],
                    "numberOfRows": 2,
                }
        """
        response = self._query_request(
            "GET",
            f"/api/v1/queries/{query_job_id}/{statement_id}/results",
            params={"offset": offset, "pageSize": page_size},
        )
        return response.json()

    def get_query_history(
        self,
        branch_id: int,
        workspace_id: int,
    ) -> dict[str, Any]:
        """Get query history for a workspace."""
        response = self._query_request(
            "GET",
            f"/api/v1/branches/{branch_id}/workspaces/{workspace_id}/queries",
        )
        return response.json()

    def wait_for_query_job(self, query_job_id: str) -> dict[str, Any]:
        """Poll a Query Service job until it reaches a terminal state.

        Args:
            query_job_id: The query job ID.

        Returns:
            Completed query job dict.

        Raises:
            KeboolaApiError: If the query fails or times out.
        """
        deadline = time.monotonic() + QUERY_JOB_MAX_WAIT
        while time.monotonic() < deadline:
            job = self.get_query_job(query_job_id)
            status = job.get("status", "")
            if status == "completed":
                return job
            if status in ("error", "failed"):
                raise KeboolaApiError(
                    message=f"Query job failed: {_extract_query_job_error(job)}",
                    status_code=500,
                    error_code=ErrorCode.QUERY_JOB_FAILED,
                    retryable=False,
                )
            time.sleep(QUERY_JOB_POLL_INTERVAL)

        raise KeboolaApiError(
            message=f"Query job {query_job_id} did not complete within {QUERY_JOB_MAX_WAIT}s",
            status_code=504,
            error_code=ErrorCode.QUERY_JOB_TIMEOUT,
            retryable=True,
        )


# The Query Service surfaces BigQuery errors as a serialized object string, e.g.
#   {Location: "query"; Message: "Syntax error: Unexpected identifier ..."; Reason: "invalidQuery"}
# Pull out the human-readable `Message: "..."` part so a BigQuery failure reads
# like Snowflake's plain text instead of leaking the wrapper into the user's red
# error box. Mirrors keboola-mcp-server's `_BigQueryWorkspace._format_error_message`.
_BQ_ERROR_MESSAGE_RE = re.compile(r'Message:\s*"((?:[^"\\]|\\.)*)"')


def _unwrap_bigquery_error(message: str) -> str:
    """Extract the inner message from a serialized BigQuery Query-Service error.

    Snowflake errors are plain strings with no ``Message: "..."`` wrapper, so
    they pass through unchanged. Only the BigQuery object shape is rewritten.
    """
    if message and (match := _BQ_ERROR_MESSAGE_RE.search(message)):
        return match.group(1).replace('\\"', '"')
    return message


def _extract_query_job_error(job: dict[str, Any]) -> str:
    """Pull the most useful warehouse error message out of a failed Query Service job.

    The Query Service `/api/v1/queries/{id}` response for a failed job carries
    the actual Snowflake / BigQuery error inside ``statements[i].error`` as a
    plain string (e.g. "SQL compilation error:\\nFunction DATE_TRUNC does not
    support VARCHAR(10) argument type"). The top-level ``error`` field is
    usually ABSENT on failures — the previous extractor read only that and so
    emitted the useless "Query job failed: Query execution failed" message
    users were seeing in the SQL editor's red error box (#287).

    Strategy:
    1. Walk ``statements`` and collect every failed statement's error,
       prefixed with the statement index so multi-statement batches stay
       readable. Strings, dicts ({\"message\": "..."}), and unknown shapes
       are all handled.
    2. Fall back to top-level ``error`` (string OR dict-with-message) for
       legacy shapes that don't carry statement-level errors.
    3. Fall back to the original generic string only when neither is set,
       so the caller never sees an empty message.

    The returned string is meant to be embedded into a
    ``KeboolaApiError(message=f"Query job failed: ...")`` and ultimately
    surfaced to the user (and the AI fix-mode helper, which pivots its
    meta-prompt on the warehouse text).
    """

    def _as_text(err: Any) -> str:
        if isinstance(err, str):
            raw = err.strip()
        elif isinstance(err, dict):
            raw = ""
            for key in ("message", "error", "detail"):
                val = err.get(key)
                if isinstance(val, str) and val.strip():
                    raw = val.strip()
                    break
        else:
            raw = str(err).strip() if err is not None else ""
        # BigQuery wraps the real message in a serialized object; Snowflake plain
        # text passes through untouched.
        return _unwrap_bigquery_error(raw)

    statement_errors: list[str] = []
    for i, stmt in enumerate(job.get("statements") or []):
        if not isinstance(stmt, dict):
            continue
        if stmt.get("status") not in ("error", "failed"):
            continue
        text = _as_text(stmt.get("error"))
        if not text:
            continue
        # Single-statement queries don't need the "Statement 1:" prefix —
        # it adds visual noise in the editor's red box for the common case.
        prefix = "" if len(job.get("statements") or []) == 1 else f"Statement {i + 1}: "
        statement_errors.append(f"{prefix}{text}")

    if statement_errors:
        return "\n".join(statement_errors)

    top_level = _as_text(job.get("error"))
    if top_level:
        return top_level

    return "Query execution failed (no error details from Query Service)"


# ---------------------------------------------------------------------------
# Cloud storage upload helpers
# ---------------------------------------------------------------------------


def _build_abs_upload_url(abs_params: dict[str, Any]) -> str:
    """Build Azure Blob Storage upload URL from absUploadParams.

    Parses SASConnectionString to extract BlobEndpoint and SharedAccessSignature,
    then constructs: ``{BlobEndpoint}/{container}/{blobName}?{SAS}``.

    The ``url`` field in the API response is read-only (``sp=rl``).
    The write-capable SAS (``sp=rwl``) is only in ``absUploadParams``.

    Args:
        abs_params: The absUploadParams dict from files/prepare response.

    Returns:
        Full HTTPS URL with write-capable SAS token.
    """
    blob_name = abs_params["blobName"]
    container = abs_params["container"]
    sas_string = abs_params["absCredentials"]["SASConnectionString"]

    # Format: "BlobEndpoint=https://...;SharedAccessSignature=sv=2017-11-09&..."
    # partition("=") splits on first "=" only, preserving "=" in SAS values.
    parts: dict[str, str] = {}
    for segment in sas_string.split(";"):
        key, sep, value = segment.partition("=")
        if sep:
            parts[key] = value

    blob_endpoint = parts.get("BlobEndpoint", "").rstrip("/")
    sas = parts.get("SharedAccessSignature", "")

    return f"{blob_endpoint}/{container}/{blob_name}?{sas}"


# Strict charset: provider error codes are short alphanumeric tokens (GCS/S3
# "AccessDenied", Azure "AuthorizationFailure"). Anything else in the response
# -- signed URLs, credentials, free-form messages -- must never reach the
# user-facing error string.
_CLOUD_ERROR_CODE_RE = re.compile(r"<Code>([A-Za-z0-9._-]{1,64})</Code>")


def _extract_cloud_error_code(response: httpx.Response) -> str | None:
    """Best-effort short error code from a failed cloud-storage response.

    Azure surfaces it in the ``x-ms-error-code`` header; GCS and S3 return an
    XML body with a ``<Code>`` element. Returns ``None`` when neither matches.
    """
    header_code = response.headers.get("x-ms-error-code", "")
    if header_code and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", header_code):
        return header_code
    match = _CLOUD_ERROR_CODE_RE.search(response.text[:CLOUD_UPLOAD_ERROR_BODY_LIMIT])
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Cloud storage download helpers (S3 SigV4, GCS bearer, ABS signed URL)
# ---------------------------------------------------------------------------


def _assert_safe_download_url(url: str) -> None:
    """Reject a download URL whose host resolves to a non-public address.

    A malicious or compromised Storage API response can return a download URL
    pointing at the cloud instance-metadata endpoint (169.254.169.254) or
    localhost; because these fetches carry no Storage token and don't follow
    redirects, the residual SSRF still writes internal/credential data into the
    user's download file (GHSA-hjhx-mx7m-8xx2). We resolve the host and allow
    ONLY globally-routable (public) addresses plus the explicit BYOC private
    ranges below; everything else -- loopback, link-local (incl. the
    169.254.169.254 metadata endpoint), CGNAT 100.64.0.0/10, reserved,
    multicast, unspecified -- is refused.

    RFC1918 + IPv6-ULA *private* ranges are deliberately ALLOWED: BYOC /
    private-tenant Keboola deployments legitimately serve storage from private
    endpoints, and the high-value SSRF target (instance metadata) is link-local,
    not private. An allow-list (public OR explicit private) rather than a
    block-list of `ipaddress` predicates avoids gaps like CGNAT, which none of
    `is_loopback/is_link_local/is_reserved/is_multicast/is_unspecified` catch.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    byoc_private = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("fc00::/7"),  # IPv6 unique-local
    )

    host = urlparse(url).hostname
    if not host:
        raise KeboolaApiError(
            message=f"Refusing to download: URL has no host ({url!r}).",
            status_code=0,
            error_code=ErrorCode.INVALID_ARGUMENT,
            retryable=False,
        )
    try:
        resolved = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        # DNS failure is surfaced by the real fetch; don't mask it here.
        return
    for addr in resolved:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_global or any(ip in net for net in byoc_private):
            continue  # public host, or a BYOC private endpoint -- allowed
        raise KeboolaApiError(
            message=(
                f"Refusing to download from {host} -> {addr}: non-public address "
                f"(not a public host nor a BYOC private range). This indicates a "
                f"malicious or compromised Storage API response (possible SSRF, "
                f"e.g. the cloud instance-metadata endpoint)."
            ),
            status_code=0,
            error_code=ErrorCode.INVALID_ARGUMENT,
            retryable=False,
        )


class _IterBytesReader:
    """Adapt an httpx iter_bytes() iterator to a .read(n) file-like interface.

    shutil.copyfileobj and gzip.GzipFile both need a binary stream with
    read(size). httpx exposes an iterator instead, so we buffer the current
    chunk and hand out at most ``size`` bytes per read, refilling from the
    iterator as needed. The buffer holds at most one iterator chunk at a time
    (~1 MiB), so total memory stays bounded regardless of response size.
    """

    def __init__(self, chunks: Any) -> None:
        self._chunks = iter(chunks)
        self._buf = b""

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            pieces = [self._buf]
            self._buf = b""
            pieces.extend(self._chunks)
            return b"".join(pieces)
        while len(self._buf) < size:
            try:
                self._buf += next(self._chunks)
            except StopIteration:
                break
        out = self._buf[:size]
        self._buf = self._buf[size:]
        return out


class _CloudDownloader:
    """Abstraction for downloading from cloud storage using Keboola file credentials.

    Supports three cloud backends:
    - AWS S3: Uses SigV4 signing with temporary credentials
    - GCP GCS: Uses OAuth2 bearer token
    - Azure ABS: Uses presigned/SAS URLs
    """

    def __init__(self, provider: str, auth_fn: Any) -> None:
        self._provider = provider
        self._auth_fn = auth_fn

    @staticmethod
    def create(file_detail: dict[str, Any]) -> "_CloudDownloader":
        """Create a downloader from file detail response.

        Args:
            file_detail: Response from GET /v2/storage/files/{id}?federationToken=1.
        """
        provider = file_detail.get("provider", "")

        if provider == "aws":
            creds = file_detail.get("credentials", {})
            region = file_detail.get("region", "us-east-1")
            return _CloudDownloader(
                provider="aws",
                auth_fn=lambda url: _s3_signed_headers(url, creds, region),
            )
        elif provider == "gcp":
            gcs_creds = file_detail.get("gcsCredentials", {})
            token = gcs_creds.get("access_token", "")
            token_type = gcs_creds.get("token_type", "Bearer")
            return _CloudDownloader(
                provider="gcp",
                auth_fn=lambda _url: {"Authorization": f"{token_type} {token}"},
            )
        elif provider == "azure":
            # Azure: SAS token from absCredentials for authenticating slice downloads
            abs_creds = file_detail.get("absCredentials", {})
            sas_string = abs_creds.get("SASConnectionString", "")
            # Parse "BlobEndpoint=https://...;SharedAccessSignature=sv=..."
            sas_parts: dict[str, str] = {}
            for segment in sas_string.split(";"):
                key, sep, value = segment.partition("=")
                if sep:
                    sas_parts[key] = value
            blob_endpoint = sas_parts.get("BlobEndpoint", "").rstrip("/")
            sas = sas_parts.get("SharedAccessSignature", "")
            return _CloudDownloader(
                provider="azure",
                auth_fn=lambda _url, _be=blob_endpoint, _sas=sas: {
                    "_blob_endpoint": _be,
                    "_sas": _sas,
                },
            )
        else:
            # Other: presigned URLs, no extra auth needed
            return _CloudDownloader(provider=provider, auth_fn=lambda _url: {})

    def resolve_base_url(self, file_detail: dict[str, Any]) -> str:
        """Build the HTTPS base URL for downloading slices.

        Returns:
            Base HTTPS URL (e.g. "https://bucket.s3.region.amazonaws.com/key/prefix/").
        """
        if self._provider == "aws":
            s3_path = file_detail.get("s3Path", {})
            bucket = s3_path.get("bucket", "")
            key = s3_path.get("key", "")
            region = file_detail.get("region", "us-east-1")
            return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"
        elif self._provider == "gcp":
            gcs_path = file_detail.get("gcsPath", {})
            bucket = gcs_path.get("bucket", "")
            key = gcs_path.get("key", "")
            return f"https://storage.googleapis.com/{bucket}/{key}"
        elif self._provider == "azure":
            # Azure: base URL from absCredentials endpoint + container
            auth_info = self._auth_fn("")
            blob_endpoint = auth_info.get("_blob_endpoint", "")
            abs_path = file_detail.get("absPath", {})
            container = abs_path.get("container", "")
            return f"{blob_endpoint}/{container}/"
        else:
            # Other: entries should be full URLs
            return ""

    def resolve_slice_url(
        self,
        base_url: str,
        entry_url: str,
        file_detail: dict[str, Any],
    ) -> str:
        """Convert a manifest entry URL to a downloadable HTTPS URL.

        Manifest entries use cloud-native URLs (s3://bucket/key/slice.gz,
        azure://container/blob). This strips the cloud prefix and builds
        an HTTPS URL for download.

        Args:
            base_url: HTTPS base URL from resolve_base_url().
            entry_url: Raw entry URL from manifest (e.g. "s3://bucket/key/slice.gz").
            file_detail: Full file detail dict.

        Returns:
            Full HTTPS URL for the slice.
        """
        if self._provider == "aws":
            # entry_url: "s3://bucket/key/prefix/slice.csv.gz"
            # base_url: "https://bucket.s3.region.amazonaws.com/key/prefix/"
            s3_path = file_detail.get("s3Path", {})
            bucket = s3_path.get("bucket", "")
            key = s3_path.get("key", "")
            prefix = f"s3://{bucket}/{key}"
            relative = entry_url.removeprefix(prefix) if entry_url.startswith(prefix) else entry_url
            return base_url + relative
        elif self._provider == "gcp":
            # entry_url: "gs://bucket/key/prefix/slice.csv.gz"
            gcs_path = file_detail.get("gcsPath", {})
            bucket = gcs_path.get("bucket", "")
            key = gcs_path.get("key", "")
            prefix = f"gs://{bucket}/{key}"
            relative = entry_url.removeprefix(prefix) if entry_url.startswith(prefix) else entry_url
            return base_url + relative
        elif self._provider == "azure":
            # entry_url: "azure://account.blob.core.windows.net/container/blob.gz"
            # Replace azure:// with https:// and append SAS token
            auth_info = self._auth_fn("")
            sas = auth_info.get("_sas", "")
            if entry_url.startswith("azure://"):
                https_url = "https://" + entry_url[len("azure://") :]
                return f"{https_url}?{sas}"
            return entry_url
        else:
            # Other: entry URLs should be full HTTPS URLs
            return entry_url

    def _request_headers(self, url: str) -> dict[str, str]:
        """Resolve auth headers for a cloud URL.

        Azure stores metadata (endpoint, SAS) in the auth_fn result (keys
        prefixed with "_"); those are filtered out here. The SAS token itself
        is embedded into the URL by resolve_slice_url().
        """
        auth_result = self._auth_fn(url)
        return {k: v for k, v in auth_result.items() if not k.startswith("_")}

    def stream_to_file(self, url: str, dest: "Path | str", decompress_gzip: bool) -> int:
        """Stream a cloud URL directly to a local file in bounded-memory chunks.

        Used for slice downloads where the payload can be hundreds of MB per
        slice. Peak RAM is O(chunk size), not O(slice size), which is what
        makes multi-GB table exports survive on small VMs (see issue #187).

        Args:
            url: Full HTTPS URL (with auth baked in for Azure).
            dest: Local file path to write to.
            decompress_gzip: If True, wrap the response stream in gzip.GzipFile
                so the decompressed bytes are what lands on disk. Streaming
                gzip keeps both compressed and decompressed state bounded.

        Returns:
            Number of bytes written to ``dest`` (post-decompression if applicable).
        """
        import gzip
        import shutil

        _assert_safe_download_url(url)
        headers = self._request_headers(url)
        dest_path = Path(dest)
        with (
            httpx.Client(timeout=FILE_DOWNLOAD_TIMEOUT) as http,
            http.stream("GET", url, headers=headers) as response,
        ):
            response.raise_for_status()
            source: Any = _IterBytesReader(response.iter_bytes(FILE_DOWNLOAD_CHUNK_SIZE))
            if decompress_gzip:
                source = gzip.GzipFile(fileobj=source, mode="rb")
            with dest_path.open("wb") as fh:
                shutil.copyfileobj(source, fh, length=FILE_DOWNLOAD_CHUNK_SIZE)

        return dest_path.stat().st_size


def _s3_signed_headers(
    url: str,
    creds: dict[str, str],
    region: str,
    method: str = "GET",
    payload: bytes = b"",
) -> dict[str, str]:
    """Generate AWS SigV4 signed headers for an S3 request.

    Implements minimal AWS Signature Version 4 signing using only stdlib
    (hmac, hashlib, urllib.parse). No boto3/botocore dependency required.

    Args:
        url: Full S3 URL (https://bucket.s3.region.amazonaws.com/key).
        creds: Dict with AccessKeyId, SecretAccessKey, SessionToken.
        region: AWS region (e.g. "us-east-1").
        method: HTTP method (GET or PUT).
        payload: Request body bytes (empty for GET).

    Returns:
        Dict of headers to include in the request.
    """
    import datetime
    import hashlib
    import hmac
    from urllib.parse import unquote, urlparse

    access_key = creds["AccessKeyId"]
    secret_key = creds["SecretAccessKey"]
    session_token = creds.get("SessionToken", "")

    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path or "/"
    query = parsed.query or ""

    now = datetime.datetime.now(datetime.UTC)
    date_stamp = now.strftime("%Y%m%d")
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")

    service = "s3"
    scope = f"{date_stamp}/{region}/{service}/aws4_request"

    # Canonical request
    canonical_uri = quote(unquote(path), safe="/~")
    if query:
        params_list = sorted(query.split("&"))
        canonical_querystring = "&".join(params_list)
    else:
        canonical_querystring = ""

    headers_to_sign: dict[str, str] = {"host": host, "x-amz-date": amz_date}
    if session_token:
        headers_to_sign["x-amz-security-token"] = session_token

    signed_headers = ";".join(sorted(headers_to_sign.keys()))
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted(headers_to_sign.items()))

    payload_hash = hashlib.sha256(payload).hexdigest()

    canonical_request = "\n".join(
        [
            method,
            canonical_uri,
            canonical_querystring,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    # String to sign
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    # Signing key
    def _hmac_sha256(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _hmac_sha256(f"AWS4{secret_key}".encode(), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    k_signing = _hmac_sha256(k_service, "aws4_request")

    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    result: dict[str, str] = {
        "Authorization": authorization,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }
    if session_token:
        result["x-amz-security-token"] = session_token
    return result
