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

    def __init__(self, stack_url: str, token: str, *, http_auth: httpx.Auth | None = None) -> None:
        self._stack_url = stack_url.rstrip("/")
        headers: dict[str, str] = {}
        if http_auth is None:
            headers["X-StorageApi-Token"] = token
        # When http_auth is set, the token is a bearer-session sentinel, not a
        # real Storage token -- an empty/sentinel value must never go on the
        # wire as X-StorageApi-Token, so the header is omitted entirely and
        # BearerAuth stamps Authorization: Bearer on every request instead.
        super().__init__(
            base_url=self._stack_url,
            token=token,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
            http_auth=http_auth,
        )
        self._queue_client: httpx.Client | None = None
        self._query_client: httpx.Client | None = None
        self._encrypt_client: httpx.Client | None = None
        self._sync_actions_client: httpx.Client | None = None
        self._billing_client: httpx.Client | None = None
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

    @property
    def _billing_base_url(self) -> str:
        return self._derive_service_url(self._stack_url, "billing")

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
        if self._billing_client is not None:
            self._billing_client.close()
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
                # Propagate the bearer-auth hook so queue / query / encryption /
                # sync-actions sub-clients don't silently fall back to no auth
                # when the main client is running in session (bearer) mode.
                auth=self._http_auth,
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

    def _billing_get(self, path: str, **kwargs: Any) -> httpx.Response:
        """Execute a read-only Billing API request with retry.

        The billing service is a sibling host derived from the stack URL
        (``billing.{stack-suffix}``); the sub-client inherits the main
        client's headers, so the ``X-StorageApi-Token`` auth carries over. On
        stacks without Pay-As-You-Go the host may not resolve at all (DNS
        failure) -- callers should feature-gate with ``has_feature()`` before
        reaching this method rather than relying on the resulting error.

        Deliberately NOT shaped like its ``_queue_request`` /
        ``_sync_actions_request`` siblings, which take an arbitrary ``method``:
        the billing service exposes ``POST /credits``, which charges real money
        by triggering an automatic top-up. Hardcoding the verb here means a
        future caller cannot construct that request through this dispatcher at
        all -- a guarantee in the signature, which no source-scanning test can
        match. If a write to the billing service is ever wanted, it needs its
        own method, its own review, and its own confirmation flow.
        """
        client = self._get_or_create_sub_client("_billing_client", self._billing_base_url)
        return self._do_request(
            "GET", path, client=client, base_url=self._billing_base_url, **kwargs
        )

    def _wait_for_storage_job(
        self,
        job: dict[str, Any],
        max_wait: float = STORAGE_JOB_MAX_WAIT,
    ) -> dict[str, Any]:
        """Poll a Storage API job until it reaches a terminal state.

        The terminal state is evaluated in ONE place for the caller's initial
        body and for every polled body alike -- the loop checks before it
        fetches. Keep it that way: this used to be two checks (an early return
        before the loop plus a second check inside it) and they drifted, so an
        already-terminal ERROR initial body was returned as-is instead of
        raising. Every call site either returns the job or its ``results``, so
        that surfaced as a silent empty success. Same shape as the sibling
        pollers ``wait_for_queue_job`` / ``wait_for_query_job``.

        Args:
            job: Initial job response from POST/DELETE. May already be
                terminal (the Storage API can fail fast, never returning
                ``waiting``), in which case no request is made at all.
            max_wait: Maximum seconds to wait (default: STORAGE_JOB_MAX_WAIT).

        Returns:
            Completed job dict (with results on success).

        Raises:
            KeboolaApiError: If the job fails or times out.
        """
        job_id = job.get("id")
        deadline = time.monotonic() + max_wait
        while True:
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
            # Checked before sleeping, so an exhausted budget never costs a
            # poll interval.
            if time.monotonic() >= deadline:
                break
            time.sleep(STORAGE_JOB_POLL_INTERVAL)
            job = self._request("GET", f"/v2/storage/jobs/{job_id}").json()

        raise KeboolaApiError(
            message=f"Storage job {job_id} did not complete within {max_wait}s",
            status_code=504,
            error_code=ErrorCode.STORAGE_JOB_TIMEOUT,
            retryable=True,
        )
