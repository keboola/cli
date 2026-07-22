"""Query Service: workspace SQL submission, results and history.

Extracted verbatim from the former single-file ``client.py`` (issue #520).
"""

import time
from typing import Any

from ..constants import (
    QUERY_JOB_MAX_WAIT,
    QUERY_JOB_POLL_INTERVAL,
    QUERY_RESULTS_PAGE_SIZE,
)
from ..errors import ErrorCode, KeboolaApiError
from ._core import _CoreClient
from ._transfer import _extract_query_job_error


class _QueryMixin(_CoreClient):
    """Query Service: workspace SQL submission, results and history."""

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

    def get_query_results(
        self,
        query_job_id: str,
        statement_id: str,
        offset: int = 0,
        page_size: int = QUERY_RESULTS_PAGE_SIZE,
    ) -> dict[str, Any]:
        """Fetch a page of inline statement results from the Query Service.

        Unlike :meth:`export_query_results`, which materializes a CSV file via the
        warehouse UNLOAD path (slow), this reads the already-computed result set
        inline as JSON -- much faster for interactive queries. The endpoint is
        paginated; ``offset``/``page_size`` walk the result set.

        Args:
            query_job_id: The query job ID.
            statement_id: The statement ID within the job.
            offset: Row offset to start from (for pagination).
            page_size: Maximum rows to return in this page.

        Returns:
            Raw QueryResult dict, e.g.::

                {
                    "status": "completed",
                    "columns": [{"name": "id", "type": "INTEGER", "nullable": false}],
                    "data": [[1, "a"], [2, "b"]],
                    "numberOfRows": 2,
                }
        """
        response = self._query_request(
            "GET",
            f"/api/v1/queries/{query_job_id}/{statement_id}/results",
            params={"offset": offset, "pageSize": page_size},
        )
        return response.json()

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
                raise KeboolaApiError(
                    message=f"Query job failed: {_extract_query_job_error(job)}",
                    status_code=500,
                    error_code=ErrorCode.QUERY_JOB_FAILED,
                    retryable=False,
                )
            time.sleep(QUERY_JOB_POLL_INTERVAL)

        raise KeboolaApiError(
            message=f"Query job {query_job_id} did not complete within {QUERY_JOB_MAX_WAIT}s",
            status_code=504,
            error_code=ErrorCode.QUERY_JOB_TIMEOUT,
            retryable=True,
        )
