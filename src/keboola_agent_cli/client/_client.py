"""Keboola API client with retry, timeouts, and token masking.

This is the only module that communicates with the Keboola Storage API
and the Keboola Queue API. All HTTP details, endpoint URLs, and error
mapping are encapsulated here.

Inherits shared retry/error logic from BaseHttpClient.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ..constants import (
    CLOUD_UPLOAD_ERROR_BODY_LIMIT,
    DEFAULT_GROUPED_JOBS_LIMIT,
    DEFAULT_JOB_LIMIT,
    DEFAULT_JOBS_PER_CONFIG,
    DEFAULT_POLL_STRATEGY,
    EXPORT_JOB_MAX_WAIT,
    FILE_DOWNLOAD_CHUNK_SIZE,
    FILE_DOWNLOAD_TIMEOUT,
    FILE_UPLOAD_TIMEOUT,
    IMPORT_JOB_MAX_WAIT,
    STORAGE_JOB_MAX_WAIT,
    VALID_POLL_STRATEGIES,
)
from ..errors import ErrorCode, KeboolaApiError
from ._core import _CoreClient
from ._transfer import (
    _assert_safe_download_url,
    _build_abs_upload_url,
    _CloudDownloader,
    _extract_cloud_error_code,
    _iter_poll_intervals,
    _IterBytesReader,
    _s3_signed_headers,
)
from .branches import _BranchesMixin
from .misc import _MiscMixin
from .query import _QueryMixin
from .stream import _StreamMixin
from .tokens import _TokensMixin
from .workspaces import _WorkspacesMixin

logger = logging.getLogger(__name__)


class KeboolaClient(
    _MiscMixin,
    _StreamMixin,
    _BranchesMixin,
    _TokensMixin,
    _QueryMixin,
    _WorkspacesMixin,
    _CoreClient,
):
    """HTTP client for the Keboola Storage API and Queue API.

    Provides methods to interact with Keboola endpoints with built-in
    retry logic (exponential backoff for 429/5xx), timeouts, and
    automatic token masking in error messages.

    Inherits _do_request() and _raise_api_error() from BaseHttpClient.
    """

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

    def create_table_snapshot(
        self,
        table_id: str,
        description: str | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a snapshot of a table (async, waits for completion).

        Snapshots capture the table's data, columns, primary key, and
        attributes at a point in time. A new table can later be created from
        the snapshot with :meth:`create_table_from_snapshot`.

        Args:
            table_id: Full ID of the table to snapshot (e.g. "in.c-bucket.table").
            description: Optional human-readable snapshot description.
            branch_id: If set, snapshot the table in a specific dev branch.

        Returns:
            Completed storage job results dict; contains the new snapshot "id".
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_id = quote(table_id, safe="")
        body: dict[str, Any] = {}
        if description is not None:
            body["description"] = description
        response = self._request("POST", f"{prefix}/tables/{safe_id}/snapshots", json=body)
        job = self._wait_for_storage_job(response.json())
        return job.get("results", {})

    def list_table_snapshots(
        self,
        table_id: str,
        limit: int | None = None,
        branch_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """List snapshots of a table (sync).

        Args:
            table_id: Full ID of the table (e.g. "in.c-bucket.table").
            limit: Optional maximum number of snapshots to return.
            branch_id: If set, list snapshots in a specific dev branch.

        Returns:
            List of snapshot dicts (id, createdTime, description, creatorToken, ...).
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_id = quote(table_id, safe="")
        params: dict[str, str] = {}
        if limit is not None:
            params["limit"] = str(limit)
        response = self._request(
            "GET", f"{prefix}/tables/{safe_id}/snapshots", params=params or None
        )
        return response.json()

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        """Get a single snapshot's detail by its ID (sync).

        Snapshot IDs are global (not table-scoped): the detail includes the
        source ``table`` object, so this is also how a bare snapshot ID is
        traced back to its origin table.

        Args:
            snapshot_id: Numeric snapshot ID.

        Returns:
            Snapshot dict (id, table, createdTime, description, ...).
        """
        safe_id = quote(str(snapshot_id), safe="")
        response = self._request("GET", f"/v2/storage/snapshots/{safe_id}")
        return response.json()

    def delete_snapshot(self, snapshot_id: str) -> None:
        """Delete a snapshot by its ID.

        The endpoint normally responds synchronously (204); a 202 job
        response is polled to completion for forward compatibility.

        Args:
            snapshot_id: Numeric snapshot ID.
        """
        safe_id = quote(str(snapshot_id), safe="")
        response = self._request("DELETE", f"/v2/storage/snapshots/{safe_id}")
        if response.status_code == 202:
            self._wait_for_storage_job(response.json())

    def create_table_from_snapshot(
        self,
        bucket_id: str,
        snapshot_id: str,
        name: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a NEW table from an existing snapshot (async, waits).

        Hits the classic ``tables-async`` import endpoint with a
        ``snapshotId`` source (the ``tables-definition`` endpoint used by
        :meth:`create_table` does not accept snapshots). The new table
        restores the snapshot's data, columns, and primary key. ``name`` is
        required: the API rejects an omitted/empty name ("Table create option
        \"name\" is required and cannot be empty", verified live 2026-07-22 --
        the reference PHP client's "fetched from snapshot" docblock is stale).

        Args:
            bucket_id: Destination bucket ID (e.g. "in.c-my-bucket").
            snapshot_id: Numeric ID of the source snapshot.
            name: Name for the new table (required by the API).
            branch_id: If set, create the table in a specific dev branch.

        Returns:
            Completed storage job results dict -- the created table (its "id"
            is the new full table ID).
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_id = quote(bucket_id, safe="")
        body: dict[str, Any] = {"snapshotId": snapshot_id, "name": name}
        response = self._request("POST", f"{prefix}/buckets/{safe_id}/tables-async", json=body)
        job = self._wait_for_storage_job(response.json())
        return job.get("results", {})

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
