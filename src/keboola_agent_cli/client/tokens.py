"""Token verification, project info, scoped tokens and feature flags.

Extracted verbatim from the former single-file ``client.py`` (issue #520).
"""

from typing import Any
from urllib.parse import quote

from ..models import TokenVerifyResponse
from ._core import _CoreClient


class _TokensMixin(_CoreClient):
    """Token verification, project info, scoped tokens and feature flags."""

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
