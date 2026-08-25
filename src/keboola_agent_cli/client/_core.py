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


def _storage_job_error_message(job: dict[str, Any]) -> str:
    """Best-effort human message out of a failed Storage job's ``error`` field.

    Written tolerantly on purpose. An API ``error`` field is not reliably a
    dict in this codebase's experience: the Metastore once answered with an
    int (``{"error": 422}``), which is why ``BaseHttpClient._raise_api_error``
    accepts ``error`` only when it is a non-empty string; the Queue poller
    guards its own ``result`` with ``isinstance``; and
    ``_extract_query_job_error`` handles strings, dicts and unknown shapes.
    Assuming a dict here would turn a failed job into an ``AttributeError``
    traceback instead of ``STORAGE_JOB_FAILED`` -- and since the terminal
    check now also sees the caller's initial response body, that shape would
    arrive from one more direction than before.
    """
    error = job.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        # A dict without a usable message falls through to the generic text
        # rather than rendering "None" or a raw dict repr at the user.
        if isinstance(message, str) and message:
            return message
    elif isinstance(error, str) and error:
        return error
    return "Storage job failed"


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
        self._notification_client: httpx.Client | None = None
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

    @property
    def _notification_base_url(self) -> str:
        return self._derive_service_url(self._stack_url, "notification")

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
        if self._notification_client is not None:
            self._notification_client.close()
        if self._stream_client is not None:
            self._stream_client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _request(
        self, method: str, path: str, retry_safe: bool | None = None, **kwargs: Any
    ) -> httpx.Response:
        """Execute a Storage API request with retry.

        ``retry_safe=False`` opts a single call out of the method-based retry
        rule -- see :meth:`BaseHttpClient._do_request`.
        """
        return self._do_request(method, path, retry_safe=retry_safe, **kwargs)

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

    def _notification_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Execute a Notification Service request with retry.

        The notification service is a sibling host derived from the stack URL
        (``notification.{stack-suffix}``, advertised in ``GET /v2/storage``);
        the sub-client inherits the main client's headers, so plain
        ``X-StorageApi-Token`` project auth carries over. The project the
        subscriptions belong to is resolved server-side from that token --
        there is no project-ID path segment or query parameter.

        Shaped like ``_queue_request`` (arbitrary ``method``) rather than the
        verb-locked ``_billing_get``: unlike ``POST /credits``, none of this
        service's endpoints spend money, and the create/delete subscription
        write path is a planned follow-up to the read-only audit surface.
        """
        client = self._get_or_create_sub_client("_notification_client", self._notification_base_url)
        return self._do_request(
            method, path, client=client, base_url=self._notification_base_url, **kwargs
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
        max_wait: float | None = None,
    ) -> dict[str, Any]:
        """Poll a Storage API job until it reaches a terminal state.

        The terminal state is evaluated in ONE place for the caller's initial
        body and for every polled body alike -- the loop checks before it
        fetches. Keep it that way: this used to be two checks (an early return
        before the loop plus a second check inside it) and they drifted, so an
        already-terminal ERROR initial body was returned as-is instead of
        raising. Every call site either returns the job or its ``results``, so
        that surfaced as a silent empty success.

        The check-then-fetch *shape* matches the sibling pollers
        ``wait_for_queue_job`` / ``wait_for_query_job``; the behaviour does not,
        and deliberately so -- this is not a parity claim. Two differences worth
        knowing: this poller recognises only ``success`` and ``error``, so any
        other terminal status the Storage API might report would exhaust the
        whole budget and surface as ``STORAGE_JOB_TIMEOUT`` (the queue poller
        keys off ``isFinished`` and ends on any terminal state), and the sleep
        here is not capped to the remaining budget, so a wait overshoots its
        deadline by up to one poll interval. Both predate this restructure.

        Args:
            job: Initial job response from the request that enqueued the job
                (POST, PUT or DELETE -- e.g. ``change_sharing_type`` enqueues
                with PUT). May already be terminal (the Storage API can fail
                fast, never returning ``waiting``), in which case no request
                is made at all.
            max_wait: Maximum seconds to wait. ``None`` (the default) means
                ``STORAGE_JOB_MAX_WAIT``. Callers whose jobs are legitimately
                slower pass their own budget -- e.g. a workspace load moving
                gigabytes (``WORKSPACE_LOAD_JOB_MAX_WAIT``).

        Returns:
            Completed job dict (with results on success).

        Raises:
            KeboolaApiError: If the job fails or times out.
        """
        budget = STORAGE_JOB_MAX_WAIT if max_wait is None else max_wait
        job_id = job.get("id")
        deadline = time.monotonic() + budget
        while True:
            status = job.get("status")
            if status == "success":
                return job
            if status == "error":
                raise KeboolaApiError(
                    message=_storage_job_error_message(job),
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
            # Naming the consequence matters: giving up locally does NOT stop
            # the job. It keeps running (and keeps consuming backend
            # resources) server-side, so "it timed out" must not be read as
            # "nothing happened" -- point the caller at where to check.
            message=(
                f"Storage job {job_id} did not complete within {budget}s. "
                "The job continues running server-side and keeps consuming backend "
                f"resources; check its status with GET /v2/storage/jobs/{job_id} "
                "or in the Keboola UI."
            ),
            status_code=504,
            error_code=ErrorCode.STORAGE_JOB_TIMEOUT,
            retryable=True,
        )
