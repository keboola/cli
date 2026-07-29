"""Shared HTTP plumbing for the Keboola client mixins.

``_CoreClient`` is the typed base every ``KeboolaClient`` endpoint-family mixin
inherits (issue #520). It holds the construction, request dispatch, sub-client
lifecycle, base-URL derivation and storage-job polling that the mixins call as
``self._request(...)`` / ``self._queue_request(...)`` / ``self.stack_url`` --
having them on a common typed base is what lets ``ty`` resolve those calls
inside each mixin with no ``# type: ignore``. The cooperative base is a single
diamond apex: ``_CoreClient.__init__`` runs once for the composed client.

Extracted verbatim from the former single-file ``client.py``.
"""

import time
from typing import Any, Self

import httpx

from ..constants import DEFAULT_TIMEOUT, STORAGE_JOB_MAX_WAIT, STORAGE_JOB_POLL_INTERVAL
from ..errors import ErrorCode, KeboolaApiError
from ..http_base import BaseHttpClient
from ..stream_client import StreamClient


class _CoreClient(BaseHttpClient):
    """Shared plumbing base for the Keboola client mixins."""

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

    def __enter__(self) -> Self:
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
