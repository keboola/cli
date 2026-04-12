"""Job listing service - business logic for listing jobs from Queue API.

Orchestrates multi-project job retrieval in parallel, filtering,
and aggregation without knowing about CLI or HTTP details.
"""

from typing import Any

from ..constants import DEFAULT_JOB_LIMIT
from ..errors import KeboolaApiError
from ..models import ProjectConfig
from .base import BaseService


class JobService(BaseService):
    """Business logic for listing Keboola jobs from the Queue API.

    Supports multi-project aggregation: queries multiple projects in parallel
    using ThreadPoolExecutor, collects results, and reports per-project errors
    without stopping others.

    Uses dependency injection for config_store and client_factory.
    """

    def _fetch_project_jobs(
        self,
        alias: str,
        project: ProjectConfig,
        component_id: str | None = None,
        config_id: str | None = None,
        status: str | None = None,
        limit: int = DEFAULT_JOB_LIMIT,
    ) -> tuple[str, list[dict[str, Any]], bool] | tuple[str, dict[str, str]]:
        """Fetch jobs for a single project (runs in a worker thread).

        Creates its own KeboolaClient, fetches jobs, and closes the client.
        Returns either (alias, jobs_list, True) on success or (alias, error_dict)
        on failure. The 3-tuple convention is required by _run_parallel().
        """
        client = self._client_factory(project.stack_url, project.token)
        try:
            jobs = client.list_jobs(
                component_id=component_id,
                config_id=config_id,
                status=status,
                limit=limit,
            )
            for job in jobs:
                job["project_alias"] = alias
            return (alias, jobs, True)
        except KeboolaApiError as exc:
            return (
                alias,
                {
                    "project_alias": alias,
                    "error_code": exc.error_code,
                    "message": exc.message,
                },
            )
        except Exception as exc:
            return (
                alias,
                {
                    "project_alias": alias,
                    "error_code": "UNEXPECTED_ERROR",
                    "message": str(exc),
                },
            )
        finally:
            client.close()

    def list_jobs(
        self,
        aliases: list[str] | None = None,
        component_id: str | None = None,
        config_id: str | None = None,
        status: str | None = None,
        limit: int = DEFAULT_JOB_LIMIT,
    ) -> dict[str, Any]:
        """List jobs across one or multiple projects.

        Queries each resolved project's Queue API for jobs in parallel,
        aggregates results into a unified list. Per-project errors are
        collected but do not stop other projects from being queried.

        Args:
            aliases: Project aliases to query. None means all projects.
            component_id: Optional filter by component ID.
            config_id: Optional filter by config ID.
            status: Optional filter by job status.
            limit: Max number of jobs per project (1-500, default 50).

        Returns:
            Dict with keys:
                - "jobs": list of job dicts with project_alias added
                - "errors": list of error dicts with project_alias,
                  error_code, message

        Raises:
            ConfigError: If a specified alias is not found (before querying).
        """
        projects = self.resolve_projects(aliases)

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            return self._fetch_project_jobs(alias, project, component_id, config_id, status, limit)

        successes, errors = self._run_parallel(projects, worker)

        # Flatten jobs from all successful projects
        all_jobs: list[dict[str, Any]] = []
        for _alias, jobs, _ok in successes:
            all_jobs.extend(jobs)

        # Sort for deterministic output
        all_jobs.sort(key=lambda j: (j.get("project_alias", ""), str(j.get("id", ""))))
        errors.sort(key=lambda e: e.get("project_alias", ""))

        return {"jobs": all_jobs, "errors": errors}

    def get_job_detail(
        self,
        alias: str,
        job_id: str,
    ) -> dict[str, Any]:
        """Get detailed information about a specific job.

        Args:
            alias: Project alias to query.
            job_id: The job ID.

        Returns:
            Dict with the full job detail from the Queue API,
            plus a "project_alias" key.

        Raises:
            ConfigError: If the alias is not found.
            KeboolaApiError: If the API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            detail = client.get_job_detail(job_id)
        finally:
            client.close()

        detail["project_alias"] = alias
        return detail

    def run_job(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        config_row_ids: list[str] | None = None,
        wait: bool = False,
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        """Create and optionally wait for a Queue API job.

        Args:
            alias: Project alias.
            component_id: Component ID to run.
            config_id: Configuration ID to run.
            config_row_ids: Optional row IDs (omit to run entire config).
            wait: If True, poll until job finishes or timeout.
            timeout: Max seconds to wait (only used when wait=True).

        Returns:
            Job dict with project_alias. If wait=True, returns the
            completed job; otherwise returns the initial job response.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            job = client.create_job(
                component_id=component_id,
                config_id=config_id,
                config_row_ids=config_row_ids,
            )
            job_id = str(job.get("id", ""))

            if wait and job_id:
                job = client.wait_for_queue_job(job_id, max_wait=timeout)
        finally:
            client.close()

        job["project_alias"] = alias
        return job
