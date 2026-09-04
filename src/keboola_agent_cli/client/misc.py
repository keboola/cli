"""Cross-cutting endpoints: global search, OAuth URL, encryption, sync actions.

Extracted verbatim from the former single-file ``client.py`` (issue #520).
"""

import math
from typing import TYPE_CHECKING, Any

from ..constants import (
    OAUTH_HOST,
    OAUTH_PATH,
)
from ._core import _CoreClient

if TYPE_CHECKING:
    from ._client import KeboolaClient


class _MiscMixin(_CoreClient):
    """Cross-cutting endpoints: global search, OAuth URL, encryption, sync actions."""

    def trigger_event(
        self,
        *,
        component_id: str,
        message: str,
        event_type: str,
        params: dict[str, Any] | None = None,
        results: dict[str, Any] | None = None,
        duration: float | None = None,
        configuration_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Post a custom Storage event (POST /v2/storage/events).

        Connection stores the event under the name ``ext.<component_id>.<configuration_id>``
        (an empty ``configuration_id`` yields a trailing dot, e.g. ``ext.keboola.cli.``)
        and stamps it server-side with the caller's token + user agent. Used for
        per-invocation usage telemetry; callers treat it as fire-and-forget.

        Args:
            component_id: Registered component id (e.g. ``keboola.cli``).
            message: Human-readable event message.
            event_type: ``info`` / ``success`` / ``warn`` / ``error``.
            params: Optional JSON object matching the component's event schema.
            results: Optional JSON object (e.g. ``projectId`` / ``error``).
            duration: Optional processing duration in seconds; the events API
                ignores floats, so it is rounded up to whole seconds.
            configuration_id: Optional second name segment (interface discriminator).
            timeout: Optional short per-request timeout override.

        Returns:
            The API response dict (``{"id": ...}``).
        """
        payload: dict[str, Any] = {
            "component": component_id,
            "message": message,
            "type": event_type,
        }
        if configuration_id:
            payload["configurationId"] = configuration_id
        if params:
            payload["params"] = params
        if results:
            payload["results"] = results
        if duration is not None:
            payload["duration"] = math.ceil(duration)

        # One bounded attempt, never retried: a blocked or unreachable events
        # endpoint must fail fast, not stall the caller behind retry+backoff.
        request_kwargs: dict[str, Any] = {"json": payload, "max_attempts": 1}
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        response = self._request("POST", "/v2/storage/events", **request_kwargs)
        return response.json()

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
        self: "KeboolaClient",
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
