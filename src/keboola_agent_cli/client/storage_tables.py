"""Storage buckets, tables, snapshots and bucket sharing/linking.

Extracted verbatim from the former single-file ``client.py`` (issue #520).
"""

import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ..constants import (
    CLOUD_UPLOAD_ERROR_BODY_LIMIT,
    EXPORT_JOB_MAX_WAIT,
    FILE_UPLOAD_TIMEOUT,
    IMPORT_JOB_MAX_WAIT,
)
from ..errors import ErrorCode, KeboolaApiError
from ._core import _CoreClient
from ._transfer import (
    _build_abs_upload_url,
    _extract_cloud_error_code,
    _s3_signed_headers,
)

logger = logging.getLogger(__name__)


class _StorageTablesMixin(_CoreClient):
    """Storage buckets, tables, snapshots and bucket sharing/linking."""

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

        The flat ``KBC.column.{colname}.description`` table-metadata key this
        method used to carry column descriptions is LEGACY (pre-0.88.0, issue
        #624): nothing reads it except this CLI -- neither the Keboola UI nor
        the MCP server -- and a metadata write never reaches the native column
        description field. ``update_table_definition`` supersedes it. The key
        convention survives only so ``describe_migrate`` and the table-detail
        read fallback can still find entries written by older versions.

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

    def update_table_definition(
        self,
        table_id: str,
        columns: list[dict[str, Any]] | None = None,
        description: str | None = None,
        description_set: bool = False,
        is_description_system_managed: bool | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Update a table's native definition (async, waits for the storage job).

        PUT /v2/storage/branch/{branch}/tables/{id}/definition

        This is the endpoint the web UI writes column descriptions with, and
        the only one whose values the backend mirrors into ``columnMetadata``
        (``KBC.description``) -- so a single write here is visible to the UI,
        to the MCP server and to the underlying backend (Snowflake COMMENT /
        BigQuery description). The mirroring is one-way: a metadata POST never
        travels back into the native field (issue #624).

        The definition endpoint is branch-scoped only; production uses the
        literal branch ref ``"default"`` (matching the web UI's client).

        Args:
            table_id: Full table ID (e.g. "in.c-bucket.table").
            columns: Column entries ``{"name": str, "description": str | None}``;
                a ``None`` description clears that column's description. Omitted
                from the body entirely when ``None``.
            description: New table-level description. Only sent when
                ``description_set`` is True.
            description_set: Distinguishes "clear the table description"
                (``description=None`` + this flag) from "don't touch it"
                (flag False), which a bare ``None`` cannot express.
            is_description_system_managed: When False, marks the descriptions
                as user-authored so the next component run's Output Mapping
                does not overwrite them. Omitted from the body when ``None``.
            branch_id: If set, target a specific dev branch.

        Returns:
            Completed storage job dict.

        Raises:
            KeboolaApiError: If the storage job fails or times out.
        """
        branch_ref = str(branch_id) if branch_id else "default"
        safe_id = quote(table_id, safe="")
        body: dict[str, Any] = {}
        if columns is not None:
            body["columns"] = columns
        if description_set:
            body["description"] = description
        if is_description_system_managed is not None:
            body["isDescriptionSystemManaged"] = is_description_system_managed
        response = self._request(
            "PUT",
            f"/v2/storage/branch/{branch_ref}/tables/{safe_id}/definition",
            json=body,
        )
        return self._wait_for_storage_job(response.json())

    def delete_table_metadata(
        self,
        table_id: str,
        metadata_id: int | str,
        branch_id: int | None = None,
    ) -> None:
        """Delete a single metadata entry on a storage table by its numeric ID.

        DELETE /v2/storage/[branch/{b}/]tables/{id}/metadata/{metadataId}

        Synchronous (204). Used to retire legacy flat
        ``KBC.column.{colname}.description`` entries once their value has been
        written through ``update_table_definition`` -- leaving them behind
        would resurrect a description the user later cleared (issue #624).

        Args:
            table_id: Full table ID (e.g. "in.c-bucket.table").
            metadata_id: ID of the metadata entry (from the table detail).
            branch_id: If set, target a specific dev branch.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_id = quote(table_id, safe="")
        self._request("DELETE", f"{prefix}/tables/{safe_id}/metadata/{metadata_id}")

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
