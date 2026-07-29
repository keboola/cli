"""Keboola Stream (Data Streams) API client with retry, timeouts, token masking.

This module talks to the Keboola **Stream control-plane API** for managing
Data Streams sources and sinks (list / create / detail / delete). The base URL
is derived from the Storage API stack URL by replacing 'connection.' with
'stream.' in the hostname (the same scheme used for 'ai.'/'queue.'), and the
request is authenticated with the per-project Storage API token
(``X-StorageApi-Token``) -- no manage token is involved.

Important: the OTLP *ingestion* endpoint (``stream-in.<region>/otlp/...``) is a
separate data-plane host and is NOT derived here -- it is returned by the API in
the source's ``otlp.url`` field. This client only speaks to the control plane.

Source-create and source-delete are asynchronous: the API returns a ``Task``
(202 Accepted); :meth:`wait_for_task` polls ``GET /v1/tasks/{taskId}`` until the
task reports ``isFinished``.

Inherits shared retry/error logic from :class:`BaseHttpClient`.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from .constants import (
    OTLP_BUCKET_PREFIX,
    OTLP_SINK_COLUMNS,
    OTLP_SINK_SIGNALS,
    STREAM_API_TIMEOUT,
    STREAM_TASK_POLL_INTERVAL,
    STREAM_TASK_TIMEOUT,
)
from .errors import ErrorCode, KeboolaApiError
from .http_base import BaseHttpClient

logger = logging.getLogger(__name__)


def stream_task_source_id(task: dict[str, Any]) -> str | None:
    """Extract the created ``sourceId`` from a finished create-source ``Task``.

    Shared by :class:`StreamService` (CLI path) and :class:`KeboolaClient`
    (importable path) so both read the async task result the same way.
    """
    outputs = task.get("outputs")
    if isinstance(outputs, dict):
        source_id = outputs.get("sourceId")
        if isinstance(source_id, str):
            return source_id
    return None


def provision_otlp_sinks(client: StreamClient, branch: str, source_id: str) -> None:
    """Create the standard logs/metrics/traces sinks for an OTLP source.

    The raw Stream ``POST /sources`` creates only the bare source; the three
    sinks are what make OTLP data actually land in Storage (bucket
    ``in.c-otlp-<sourceId>``), so both the ``kbagent stream`` CLI and the
    importable :meth:`KeboolaClient.create_stream_source` provision them here.

    Idempotent: only signals without an existing sink are created, so a re-run
    (or ``--if-not-exists`` against a half-provisioned source) heals the set
    rather than erroring on a conflict.
    """
    existing_signals: set[str] = set()
    for sink in client.list_sinks(branch, source_id).get("sinks", []):
        existing_signals.update(sink.get("allowedSignals") or [])
    columns = [dict(col) for col in OTLP_SINK_COLUMNS]
    for signal in OTLP_SINK_SIGNALS:
        if signal in existing_signals:
            continue
        table_id = f"{OTLP_BUCKET_PREFIX}{source_id}.{signal}"
        task = client.create_sink(
            branch,
            source_id,
            name=signal.capitalize(),
            table_id=table_id,
            columns=columns,
            allowed_signals=[signal],
        )
        client.wait_for_task(task)


class StreamClient(BaseHttpClient):
    """HTTP client for the Keboola Stream (Data Streams) control-plane API.

    Provides source CRUD, sink listing, and async-task polling, with the
    retry/backoff (429/5xx), timeouts, and token masking inherited from
    :class:`BaseHttpClient`.
    """

    SESSION_AUTH_FEATURE = "The Data Streams Service"

    def __init__(self, stack_url: str, token: str, *, http_auth: httpx.Auth | None = None) -> None:
        self._stack_url = stack_url.rstrip("/")
        stream_base_url = self._derive_service_url(self._stack_url, "stream")
        headers = {
            "X-StorageApi-Token": token,
            "Content-Type": "application/json",
        }
        super().__init__(
            base_url=stream_base_url,
            token=token,
            headers=headers,
            timeout=STREAM_API_TIMEOUT,
            http_auth=http_auth,
        )

    def __enter__(self) -> StreamClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------

    def list_sources(self, branch_id: str) -> dict[str, Any]:
        """List sources in a branch (``GET /v1/branches/{branch}/sources``)."""
        path = f"/v1/branches/{quote(branch_id, safe='')}/sources"
        response = self._do_request("GET", path)
        return response.json()

    def get_source(self, branch_id: str, source_id: str) -> dict[str, Any]:
        """Fetch one source (``GET /v1/branches/{branch}/sources/{id}``)."""
        path = f"/v1/branches/{quote(branch_id, safe='')}/sources/{quote(source_id, safe='')}"
        response = self._do_request("GET", path)
        return response.json()

    def create_source(
        self,
        branch_id: str,
        name: str,
        source_type: str,
        source_id: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create a source. Returns the async ``Task`` (poll with wait_for_task)."""
        path = f"/v1/branches/{quote(branch_id, safe='')}/sources"
        payload: dict[str, Any] = {"name": name, "type": source_type}
        if source_id is not None:
            payload["sourceId"] = source_id
        if description is not None:
            payload["description"] = description
        response = self._do_request("POST", path, json=payload)
        return response.json()

    def delete_source(self, branch_id: str, source_id: str) -> dict[str, Any]:
        """Delete a source. Returns the async ``Task`` (poll with wait_for_task)."""
        path = f"/v1/branches/{quote(branch_id, safe='')}/sources/{quote(source_id, safe='')}"
        response = self._do_request("DELETE", path)
        return response.json()

    # ------------------------------------------------------------------
    # Sinks
    # ------------------------------------------------------------------

    def list_sinks(self, branch_id: str, source_id: str) -> dict[str, Any]:
        """List a source's sinks (``GET .../sources/{id}/sinks``)."""
        path = f"/v1/branches/{quote(branch_id, safe='')}/sources/{quote(source_id, safe='')}/sinks"
        response = self._do_request("GET", path)
        return response.json()

    def create_sink(
        self,
        branch_id: str,
        source_id: str,
        *,
        name: str,
        table_id: str,
        columns: list[dict[str, Any]],
        allowed_signals: list[str] | None = None,
        sink_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a table sink on a source. Returns the async ``Task``.

        ``columns`` is the table mapping column list (see the Stream API
        ``TableColumn`` schema). ``allowed_signals`` restricts which OTLP signals
        route to this sink (logs/metrics/traces); omit to accept all.
        """
        path = f"/v1/branches/{quote(branch_id, safe='')}/sources/{quote(source_id, safe='')}/sinks"
        payload: dict[str, Any] = {
            "type": "table",
            "name": name,
            "table": {
                "type": "keboola",
                "tableId": table_id,
                "mapping": {"columns": columns},
            },
        }
        if allowed_signals is not None:
            payload["allowedSignals"] = allowed_signals
        if sink_id is not None:
            payload["sinkId"] = sink_id
        response = self._do_request("POST", path, json=payload)
        return response.json()

    # ------------------------------------------------------------------
    # Tasks (async create/delete)
    # ------------------------------------------------------------------

    def get_task(self, task_id: str) -> dict[str, Any]:
        """Fetch a task by id (``GET /v1/tasks/{taskId}``)."""
        path = f"/v1/tasks/{quote(task_id, safe='')}"
        response = self._do_request("GET", path)
        return response.json()

    def wait_for_task(
        self,
        task: dict[str, Any],
        timeout: float = STREAM_TASK_TIMEOUT,
        poll_interval: float = STREAM_TASK_POLL_INTERVAL,
    ) -> dict[str, Any]:
        """Poll a ``Task`` to completion and return the finished task.

        Accepts the Task dict returned by :meth:`create_source` /
        :meth:`delete_source`. Polls its canonical poll URL (the task's ``url``
        reduced to a path, falling back to ``/v1/tasks/{taskId}``) until
        ``isFinished`` is true, then raises :class:`KeboolaApiError` if the task
        failed.
        """
        if task.get("isFinished"):
            return self._check_task_result(task)

        poll_path = self._task_poll_path(task)
        deadline = time.monotonic() + timeout
        latest = task
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            response = self._do_request("GET", poll_path)
            latest = response.json()
            if latest.get("isFinished"):
                return self._check_task_result(latest)

        raise KeboolaApiError(
            message=(
                f"Stream task '{latest.get('taskId', '?')}' did not finish within "
                f"{timeout:.0f}s (last status: {latest.get('status', 'unknown')})"
            ),
            error_code=ErrorCode.TIMEOUT,
            retryable=True,
        )

    def _task_poll_path(self, task: dict[str, Any]) -> str:
        """Resolve the path to poll for ``task``.

        Prefers the task's ``url`` field reduced to a path relative to the
        Stream base; falls back to ``/v1/tasks/{taskId}``.
        """
        url = task.get("url")
        if isinstance(url, str) and url:
            parsed = urlparse(url)
            if parsed.path:
                return parsed.path
        task_id = task.get("taskId", "")
        return f"/v1/tasks/{quote(str(task_id), safe='')}"

    @staticmethod
    def _check_task_result(task: dict[str, Any]) -> dict[str, Any]:
        """Return a finished task, raising if it ended in error."""
        error = task.get("error")
        status = task.get("status")
        if error or status == "error":
            raise KeboolaApiError(
                message=f"Stream task failed: {error or status}",
                error_code=ErrorCode.API_ERROR,
            )
        return task
