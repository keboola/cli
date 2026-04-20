"""Job service - business logic for Queue API jobs.

Covers listing (multi-project parallel retrieval + filtering), detail
fetch, creation with optional wait, termination, and variable-values
resolution. Stays agnostic of CLI and HTTP transport details.
"""

from typing import Any

from ..constants import DEFAULT_JOB_LIMIT, KILLABLE_JOB_STATUSES
from ..errors import KeboolaApiError
from ..models import ProjectConfig
from .base import BaseService


class JobService(BaseService):
    """Business logic for Keboola jobs (list, detail, run, terminate).

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
        branch_id: int | None = None,
        variable_values_id: str | None = None,
        no_variables: bool = False,
    ) -> dict[str, Any]:
        """Create and optionally wait for a Queue API job.

        When the config has linked variables (``configuration.variables_id``),
        auto-resolve a ``variableValuesId`` via :meth:`resolve_variable_values_id`
        so the job runs against the deployed values row instead of empty
        strings. Pass ``variable_values_id`` to override, or ``no_variables=True``
        to skip the resolution entirely.

        Args:
            alias: Project alias.
            component_id: Component ID to run.
            config_id: Configuration ID to run.
            config_row_ids: Optional row IDs (omit to run entire config).
            wait: If True, poll until job finishes or timeout.
            timeout: Max seconds to wait (only used when wait=True).
            branch_id: Optional dev branch ID. When set, the job runs
                on that branch instead of the default (production) branch.
            variable_values_id: Optional explicit values row id. When set,
                bypasses auto-resolution and goes straight into the Queue
                body. Mutually exclusive with ``no_variables``.
            no_variables: If True, skip variable-values resolution entirely
                (useful for components that do not support variables, or
                when the caller intentionally wants empty-string binding).

        Returns:
            Job dict with project_alias. If wait=True, returns the
            completed job; otherwise returns the initial job response.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            resolved_values_id = variable_values_id
            if resolved_values_id is None and not no_variables:
                resolved_values_id = self.resolve_variable_values_id(
                    client=client,
                    component_id=component_id,
                    config_id=config_id,
                    branch_id=branch_id,
                )

            job = client.create_job(
                component_id=component_id,
                config_id=config_id,
                config_row_ids=config_row_ids,
                branch_id=branch_id,
                variable_values_id=resolved_values_id,
            )
            job_id = str(job.get("id", ""))

            if wait and job_id:
                job = client.wait_for_queue_job(job_id, max_wait=timeout)
        finally:
            client.close()

        job["project_alias"] = alias
        if resolved_values_id:
            job["resolvedVariableValuesId"] = resolved_values_id
        return job

    @staticmethod
    def resolve_variable_values_id(
        client: Any,
        component_id: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> str | None:
        """Resolve the variables values row id to pass to ``create_job``.

        Reads the parent config's ``configuration.variables_id`` to find the
        linked ``keboola.variables`` config. Prefers the explicit
        ``configuration.variables_values_id`` when set; otherwise falls back
        to the first row of the linked variables config (the default-row
        convention).

        Note the snake_case keys: Keboola stores the variables link at the
        root of the ``configuration`` body as ``variables_id`` /
        ``variables_values_id`` (matching the on-disk format written by
        ``VariablesService``), NOT nested under a ``runtime`` object.

        Returns:
            The values row id, or ``None`` if the parent config has no
            linked variables (no ``variables_id``).

        Raises:
            KeboolaApiError: ``NO_VARIABLE_ROWS`` when the linked variables
                config has zero rows (misconfiguration — the config exists
                but no values row has been set).
        """
        detail = client.get_config_detail(
            component_id=component_id,
            config_id=config_id,
            branch_id=branch_id,
        )
        configuration = detail.get("configuration") or {}
        variables_id = configuration.get("variables_id")
        if not variables_id:
            return None

        explicit_values_id = configuration.get("variables_values_id")
        if explicit_values_id:
            return str(explicit_values_id)

        rows = client.list_config_rows(
            component_id="keboola.variables",
            config_id=str(variables_id),
            branch_id=branch_id,
        )
        if not rows:
            raise KeboolaApiError(
                message=(
                    f"Linked variables config {variables_id} has no rows. "
                    f"Create one via `kbagent config variables-set` or pass "
                    f"`--no-variables` to skip resolution."
                ),
                status_code=0,
                error_code="NO_VARIABLE_ROWS",
            )
        return str(rows[0].get("id", ""))

    def resolve_job_ids_by_filter(
        self,
        alias: str,
        status: str | None = None,
        component_id: str | None = None,
        config_id: str | None = None,
        branch_id: int | None = None,
        limit: int = DEFAULT_JOB_LIMIT,
    ) -> list[dict[str, Any]]:
        """Resolve a set of jobs via filters for bulk operations (e.g. terminate).

        Returns the full job dicts (not just IDs) so callers can display context
        in dry-run output. branch_id is applied client-side because the Queue
        API /search/jobs endpoint does not accept a branch filter.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            jobs = client.list_jobs(
                component_id=component_id,
                config_id=config_id,
                status=status,
                limit=limit,
            )
        finally:
            client.close()

        if branch_id is not None:
            jobs = [j for j in jobs if str(j.get("branchId")) == str(branch_id)]

        return jobs

    def terminate_jobs(
        self,
        alias: str,
        job_ids: list[str],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Terminate one or more Queue API jobs.

        Partitions results into four buckets based on Queue API behavior:
        - killed: HTTP 200 from POST /jobs/{id}/kill (transition to terminating)
        - already_finished: HTTP 400 "not in one of killable states" (race condition
          between list and kill) OR HTTP 500 with body code=404 + GET confirms
          isFinished=True (Queue API inconsistency for terminal jobs)
        - not_found: HTTP 500 + body code=404 + GET returns 404
        - failed: anything else (auth, network, genuine server errors)

        Batch-tolerant: accumulates errors per job, one failure does not stop
        other kills.

        Args:
            alias: Project alias.
            job_ids: List of job IDs to terminate.
            dry_run: If True, only report what would be terminated.

        Returns:
            Dict with 'killed', 'already_finished', 'not_found', 'failed',
            'dry_run', 'project_alias', and optionally 'would_terminate'.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        if dry_run:
            return {
                "killed": [],
                "already_finished": [],
                "not_found": [],
                "failed": [],
                "would_terminate": list(job_ids),
                "dry_run": True,
                "project_alias": alias,
            }

        killed: list[dict[str, Any]] = []
        already_finished: list[dict[str, Any]] = []
        not_found: list[str] = []
        failed: list[dict[str, str]] = []

        client = self._client_factory(project.stack_url, project.token)
        try:
            for jid in job_ids:
                try:
                    result = client.kill_job(jid)
                    killed.append(
                        {
                            "id": jid,
                            "status": result.get("status"),
                            "desiredStatus": result.get("desiredStatus"),
                        }
                    )
                except KeboolaApiError as exc:
                    disposition = self._classify_kill_error(client, jid, exc)
                    if disposition["bucket"] == "already_finished":
                        already_finished.append({"id": jid, "reason": disposition["reason"]})
                    elif disposition["bucket"] == "not_found":
                        not_found.append(jid)
                    else:
                        failed.append({"id": jid, "error": exc.message})
        finally:
            client.close()

        return {
            "killed": killed,
            "already_finished": already_finished,
            "not_found": not_found,
            "failed": failed,
            "dry_run": False,
            "project_alias": alias,
        }

    def _classify_kill_error(
        self, client: Any, job_id: str, exc: KeboolaApiError
    ) -> dict[str, str]:
        """Map a kill_job error into one of {already_finished, not_found, failed}.

        Queue API returns HTTP 400 with "not in one of killable states" when the
        job is already terminal; for success/error/bogus IDs it returns HTTP 500
        with body code=404 (inconsistent), so we fall back to GET /jobs/{id} to
        distinguish "already done" from "really missing".
        """
        # Killable-state race: job transitioned to terminal between list and kill
        if exc.status_code == 400 and "killable states" in exc.message.lower():
            return {"bucket": "already_finished", "reason": "not_killable"}

        # 500/404 inconsistency: verify via GET
        if exc.status_code == 500 or exc.status_code == 404:
            try:
                job = client.get_job_detail(job_id)
                if job.get("isFinished"):
                    return {"bucket": "already_finished", "reason": "terminal_state"}
            except KeboolaApiError as get_exc:
                if get_exc.status_code == 404:
                    return {"bucket": "not_found", "reason": "missing"}

        return {"bucket": "failed", "reason": "error"}

    @staticmethod
    def filter_killable(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep only jobs whose current status is killable per Queue API contract."""
        return [j for j in jobs if j.get("status") in KILLABLE_JOB_STATUSES]
