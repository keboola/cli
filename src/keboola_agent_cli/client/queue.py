"""Queue API: job listing, creation, termination, events and polling.

Extracted verbatim from the former single-file ``client.py`` (issue #520).
"""

import time
from typing import Any
from urllib.parse import quote

from ..constants import (
    DEFAULT_GROUPED_JOBS_LIMIT,
    DEFAULT_JOB_LIMIT,
    DEFAULT_JOB_SORT_BY,
    DEFAULT_JOB_SORT_ORDER,
    DEFAULT_JOBS_PER_CONFIG,
    DEFAULT_POLL_STRATEGY,
    STORAGE_JOB_MAX_WAIT,
    VALID_POLL_STRATEGIES,
)
from ..errors import ErrorCode, KeboolaApiError
from ._core import _CoreClient
from ._transfer import _iter_poll_intervals


class _QueueMixin(_CoreClient):
    """Queue API: job listing, creation, termination, events and polling."""

    def list_jobs(
        self,
        component_id: str | None = None,
        config_id: str | None = None,
        status: str | None = None,
        limit: int = DEFAULT_JOB_LIMIT,
        offset: int = 0,
        sort_by: str = DEFAULT_JOB_SORT_BY,
        sort_order: str = DEFAULT_JOB_SORT_ORDER,
    ) -> list[dict[str, Any]]:
        """List jobs from the Queue API.

        Args:
            component_id: Optional filter by component ID.
            config_id: Optional filter by config ID (requires component_id).
            status: Optional filter by job status.
            limit: Max number of jobs to return (1-500).
            offset: Offset for pagination.
            sort_by: Field to sort by (see JOB_SORT_FIELDS).
            sort_order: "asc" or "desc".

        Returns:
            List of job dicts from the Queue API.
        """
        params: dict[str, str | int] = {
            "limit": limit,
            "offset": offset,
            "sortBy": sort_by,
            "sortOrder": sort_order,
        }
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
