"""Keboola API client with retry, timeouts, and token masking.

This is the only module that communicates with the Keboola Storage API
and the Keboola Queue API. All HTTP details, endpoint URLs, and error
mapping are encapsulated here.

Inherits shared retry/error logic from BaseHttpClient.
"""

import json
import logging
import time
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import httpx

from . import __version__
from .constants import (
    DEFAULT_JOB_LIMIT,
    DEFAULT_TIMEOUT,
    QUERY_JOB_MAX_WAIT,
    QUERY_JOB_POLL_INTERVAL,
    STORAGE_JOB_MAX_WAIT,
    STORAGE_JOB_POLL_INTERVAL,
)
from .errors import KeboolaApiError
from .http_base import BaseHttpClient
from .models import TokenVerifyResponse

logger = logging.getLogger(__name__)


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
            "User-Agent": f"keboola-agent-cli/{__version__}",
        }
        super().__init__(
            base_url=self._stack_url,
            token=token,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
        )
        self._queue_client: httpx.Client | None = None
        self._query_client: httpx.Client | None = None

    @property
    def _queue_base_url(self) -> str:
        """Derive Queue API base URL from the Storage API URL.

        Replaces 'connection.' with 'queue.' in the hostname.
        E.g. https://connection.keboola.com -> https://queue.keboola.com
        """
        parsed = urlparse(self._stack_url)
        hostname = parsed.hostname or ""
        queue_host = hostname.replace("connection.", "queue.", 1)
        if queue_host == hostname:
            logger.warning("Queue URL derivation did not change hostname: %s", hostname)
        return urlunparse(parsed._replace(netloc=queue_host))

    @property
    def _query_base_url(self) -> str:
        """Derive Query Service base URL from the Storage API URL.

        Replaces 'connection.' with 'query.' in the hostname.
        E.g. https://connection.keboola.com -> https://query.keboola.com
        """
        parsed = urlparse(self._stack_url)
        hostname = parsed.hostname or ""
        query_host = hostname.replace("connection.", "query.", 1)
        if query_host == hostname:
            logger.warning("Query URL derivation did not change hostname: %s", hostname)
        return urlunparse(parsed._replace(netloc=query_host))

    def close(self) -> None:
        """Close the underlying HTTP clients."""
        super().close()
        if self._queue_client is not None:
            self._queue_client.close()
        if self._query_client is not None:
            self._query_client.close()

    def __enter__(self) -> "KeboolaClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Execute a Storage API request with retry."""
        return self._do_request(method, path, **kwargs)

    def _queue_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Execute a Queue API request with retry. Lazily creates the queue client."""
        if self._queue_client is None:
            self._queue_client = httpx.Client(
                base_url=self._queue_base_url,
                timeout=DEFAULT_TIMEOUT,
                headers=self._client._headers.copy(),
            )
        return self._do_request(
            method,
            path,
            client=self._queue_client,
            base_url=self._queue_base_url,
            **kwargs,
        )

    def _query_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Execute a Query Service request with retry. Lazily creates the query client."""
        if self._query_client is None:
            self._query_client = httpx.Client(
                base_url=self._query_base_url,
                timeout=DEFAULT_TIMEOUT,
                headers=self._client._headers.copy(),
            )
        return self._do_request(
            method,
            path,
            client=self._query_client,
            base_url=self._query_base_url,
            **kwargs,
        )

    def verify_token(self) -> TokenVerifyResponse:
        """Verify the storage API token and retrieve project information.

        Returns:
            TokenVerifyResponse with project name, ID, and token description.

        Raises:
            KeboolaApiError: If token is invalid (401) or other API error.
        """
        response = self._request("GET", "/v2/storage/tokens/verify")
        data = response.json()

        return TokenVerifyResponse(
            token_id=str(data.get("id", "")),
            token_description=data.get("description", ""),
            project_id=data.get("owner", {}).get("id"),
            project_name=data.get("owner", {}).get("name", ""),
            owner_name=data.get("owner", {}).get("name", ""),
        )

    def list_components(self, component_type: str | None = None) -> list[dict[str, Any]]:
        """List components with their configurations.

        Args:
            component_type: Optional filter (extractor, writer, transformation, application).

        Returns:
            List of component dicts from the API.
        """
        params: dict[str, str] = {"include": "configuration"}
        if component_type:
            params["componentType"] = component_type

        response = self._request("GET", "/v2/storage/components", params=params)
        return response.json()

    def list_components_with_configs(self, branch_id: int | None = None) -> list[dict[str, Any]]:
        """List all components with full configuration bodies and rows.

        Makes a single API call to fetch everything needed for sync pull.
        Uses the include=configuration,rows parameter to get full config
        bodies and config rows in one request.

        Args:
            branch_id: If set, target a specific dev branch.

        Returns:
            List of component dicts, each containing a 'configurations' list
            with full config bodies and nested 'rows'.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        resp = self._request(
            "GET",
            f"{prefix}/components",
            params={"include": "configuration,rows"},
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

    def get_config_detail(self, component_id: str, config_id: str) -> dict[str, Any]:
        """Get detailed information about a specific configuration.

        Args:
            component_id: The component ID (e.g. keboola.ex-db-snowflake).
            config_id: The configuration ID.

        Returns:
            Configuration detail dict from the API.
        """
        safe_component_id = quote(component_id, safe="")
        safe_config_id = quote(config_id, safe="")
        response = self._request(
            "GET",
            f"/v2/storage/components/{safe_component_id}/configs/{safe_config_id}",
        )
        return response.json()

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

    def create_config(
        self,
        component_id: str,
        name: str,
        configuration: dict[str, Any],
        description: str = "",
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a new configuration for a component.

        POST /v2/storage/[branch/{id}/]components/{comp_id}/configs

        Args:
            component_id: Component identifier.
            name: Configuration name.
            configuration: Configuration body (parameters, storage, etc.).
            description: Optional description.
            branch_id: If set, target a specific dev branch.

        Returns:
            Created configuration dict including the assigned 'id'.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        resp = self._request(
            "POST",
            f"{prefix}/components/{quote(component_id)}/configs",
            data={
                "name": name,
                "description": description,
                "configuration": json.dumps(configuration),
            },
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
    ) -> dict[str, Any]:
        """Update an existing configuration.

        PUT /v2/storage/[branch/{id}/]components/{comp_id}/configs/{config_id}

        Only provided (non-None) fields are sent in the request.

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
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a new configuration row.

        POST /v2/storage/[branch/{id}/]components/{comp_id}/configs/{config_id}/rows

        Returns:
            Created row dict including the assigned 'id'.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        resp = self._request(
            "POST",
            f"{prefix}/components/{quote(component_id)}/configs/{quote(config_id)}/rows",
            data={
                "name": name,
                "description": description,
                "configuration": json.dumps(configuration),
            },
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
        change_description: str = "",
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Update an existing configuration row.

        PUT /v2/storage/[branch/{id}/]components/{comp_id}/configs/{config_id}/rows/{row_id}
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        data: dict[str, Any] = {}
        if name is not None:
            data["name"] = name
        if description is not None:
            data["description"] = description
        if configuration is not None:
            data["configuration"] = json.dumps(configuration)
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

    def _wait_for_storage_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """Poll a Storage API job until it reaches a terminal state.

        Branch create/delete are async operations that return a job object.
        This method polls until the job completes or fails.

        Args:
            job: Initial job response from POST/DELETE.

        Returns:
            Completed job dict (with results on success).

        Raises:
            KeboolaApiError: If the job fails or times out.
        """
        job_id = job.get("id")
        if job.get("status") in ("success", "error"):
            return job

        deadline = time.monotonic() + STORAGE_JOB_MAX_WAIT
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
                    error_code="STORAGE_JOB_FAILED",
                    retryable=False,
                )
        raise KeboolaApiError(
            message=f"Storage job {job_id} did not complete within {STORAGE_JOB_MAX_WAIT}s",
            status_code=504,
            error_code="STORAGE_JOB_TIMEOUT",
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

    def list_buckets(self, include: str | None = None) -> list[dict[str, Any]]:
        """List storage buckets with optional extended information.

        Args:
            include: Optional include parameter (e.g. "linkedBuckets" for sharing info).

        Returns:
            List of bucket dicts from the API.
        """
        params: dict[str, str] = {}
        if include:
            params["include"] = include
        response = self._request("GET", "/v2/storage/buckets", params=params)
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
        mode: str = "run",
    ) -> dict[str, Any]:
        """Create and run a Queue API job.

        Args:
            component_id: Component ID (e.g. keboola.sandboxes).
            config_id: Configuration ID.
            config_data: Optional runtime config data override.
            mode: Job mode (default: run).

        Returns:
            Job dict from the Queue API.
        """
        body: dict[str, Any] = {
            "component": component_id,
            "config": config_id,
            "mode": mode,
        }
        if config_data:
            body["configData"] = config_data
        response = self._queue_request("POST", "/jobs", json=body)
        return response.json()

    def wait_for_queue_job(self, job_id: str) -> dict[str, Any]:
        """Poll a Queue API job until it reaches a terminal state.

        Args:
            job_id: The Queue job ID.

        Returns:
            Completed job dict.

        Raises:
            KeboolaApiError: If the job fails or times out.
        """
        deadline = time.monotonic() + STORAGE_JOB_MAX_WAIT
        while time.monotonic() < deadline:
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
                        error_code="QUEUE_JOB_FAILED",
                        retryable=False,
                    )
                return job
            time.sleep(STORAGE_JOB_POLL_INTERVAL)

        raise KeboolaApiError(
            message=f"Queue job {job_id} did not complete within {STORAGE_JOB_MAX_WAIT}s",
            status_code=504,
            error_code="QUEUE_JOB_TIMEOUT",
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
    ) -> dict[str, Any]:
        """Create a workspace tied to a specific configuration.

        Args:
            branch_id: Branch ID (use main branch ID for production).
            component_id: Component ID (e.g. keboola.snowflake-transformation).
            config_id: Configuration ID.
            backend: Workspace backend.

        Returns:
            Workspace dict including connection credentials.
        """
        safe_component = quote(component_id, safe="")
        safe_config = quote(config_id, safe="")
        response = self._request(
            "POST",
            f"/v2/storage/branch/{branch_id}/components/{safe_component}/configs/{safe_config}/workspaces",
            json={"backend": backend},
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
    ) -> dict[str, Any]:
        """Load tables into a workspace (async operation).

        Args:
            workspace_id: Target workspace ID.
            tables: List of table load definitions, each with at minimum:
                - source: table ID (e.g. "in.c-bucket.table")
                - destination: target table name in workspace
            branch_id: Branch ID. Required for workspaces on dev branches.

        Returns:
            Completed storage job dict (polls until done).

        Raises:
            KeboolaApiError: If the load job fails or times out.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        body: dict[str, Any] = {"input": tables}
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
                error_msg = (
                    job.get("error", {}).get("message", "")
                    if isinstance(job.get("error"), dict)
                    else str(job.get("error", "Query execution failed"))
                )
                raise KeboolaApiError(
                    message=f"Query job failed: {error_msg}",
                    status_code=500,
                    error_code="QUERY_JOB_FAILED",
                    retryable=False,
                )
            time.sleep(QUERY_JOB_POLL_INTERVAL)

        raise KeboolaApiError(
            message=f"Query job {query_job_id} did not complete within {QUERY_JOB_MAX_WAIT}s",
            status_code=504,
            error_code="QUERY_JOB_TIMEOUT",
            retryable=True,
        )
