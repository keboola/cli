"""Job service - business logic for Queue API jobs.

Covers listing (multi-project parallel retrieval + filtering), detail
fetch, creation with optional wait, termination, and variable-values
resolution. Stays agnostic of CLI and HTTP transport details.
"""

import logging
import time
from typing import Any

from ..constants import (
    DEFAULT_JOB_LIMIT,
    DEFAULT_JOB_MODE,
    DEFAULT_JOB_SORT_BY,
    DEFAULT_JOB_SORT_ORDER,
    DEFAULT_LOG_TAIL_LINES,
    DEFAULT_POLL_STRATEGY,
    JOB_IDEMPOTENCY_FILENAME,
    JOB_TERMINATE_GRACE_SECONDS,
    JOB_TERMINATE_POLL_INTERVAL,
    KILLABLE_JOB_STATUSES,
    ORCHESTRATION_JOB_COMPONENTS,
    VALID_JOB_MODES,
    VALID_POLL_STRATEGIES,
)
from ..errors import ErrorCode, KeboolaApiError
from ..models import ProjectConfig
from .base import BaseService
from .job_idempotency_store import JobIdempotencyStore, run_idempotent_job

logger = logging.getLogger(__name__)

# Terminal statuses for which we surface a log-tail.
_LOG_TAIL_STATUSES: frozenset[str] = frozenset({"error", "warning", "terminated"})

# Time-valued Queue API sort fields; compare lexicographically as ISO 8601
# strings, so plain string ordering already yields chronological order.
_TIME_SORT_FIELDS: frozenset[str] = frozenset({"startTime", "endTime", "createdTime"})


class _DescStr:
    """Wraps a string so an ascending tuple-key sort yields descending order.

    ``list.sort`` only exposes one ``reverse`` flag for the whole key, but
    our key tuple mixes a direction-agnostic "is this value missing"
    marker (always ascending, so missing rows land last regardless of
    ``sort_order``) with a direction-sensitive field value. Numeric fields
    handle that by negating the number for descending order; strings
    (timestamps, non-numeric ids) can't be negated, so they get wrapped
    in this instead -- it inverts ``__lt__`` so plain ascending sort of
    the wrapper produces descending string order.
    """

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    def __lt__(self, other: "_DescStr") -> bool:
        return other.value < self.value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _DescStr) and self.value == other.value


def _job_sort_key(job: dict[str, Any], sort_by: str, sort_order: str) -> tuple[int, Any, str, str]:
    """Build a sort key so a single ``list.sort(key=...)`` call yields the
    globally-correct chronological (or field-appropriate) merge order
    across all queried projects.

    Returns a 4-tuple ``(missing_flag, primary, project_alias, id)``:

    - ``missing_flag``: ``0`` when the requested field has a usable value,
      ``1`` when it is absent/None/uncoercible. Always compared ascending
      (this key tuple is never sorted with ``reverse=True``), so missing
      rows sort LAST for both "asc" and "desc" -- a job without
      ``startTime`` (e.g. still ``waiting``) is "not yet comparable", not
      "the oldest job".
    - ``primary``: the field's own value, pre-transformed (negated for
      numeric fields, wrapped in :class:`_DescStr` for string fields) so
      that a single ascending sort already reflects ``sort_order``.
    - ``project_alias`` / ``id``: the deterministic tiebreak. It resolves
      ties on equal field values AND orders the missing-value group
      itself (whose ``primary`` slot is a fixed placeholder).

    Never raises: Queue API rows are untyped dicts, so every field access
    is defensively coerced.
    """
    desc = sort_order == "desc"
    alias = str(job.get("project_alias", ""))
    id_tiebreak = str(job.get("id", ""))

    if sort_by == "durationSeconds":
        raw = job.get("durationSeconds")
        try:
            numeric = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            numeric = None
        if numeric is None:
            return (1, 0.0, alias, id_tiebreak)
        return (0, -numeric if desc else numeric, alias, id_tiebreak)

    if sort_by == "id":
        raw = job.get("id")
        if raw is None or raw == "":
            return (1, (1, ""), alias, id_tiebreak)
        try:
            numeric_id = float(raw)
        except (TypeError, ValueError):
            # Non-numeric id: string fallback, grouped after the numeric
            # group via the leading 1 marker (mixed id types never need
            # to compare against each other directly).
            return (0, (1, _DescStr(str(raw)) if desc else str(raw)), alias, id_tiebreak)
        return (0, (0, -numeric_id if desc else numeric_id), alias, id_tiebreak)

    if sort_by in _TIME_SORT_FIELDS:
        raw = job.get(sort_by)
        if not raw:
            return (1, "", alias, id_tiebreak)
        text = str(raw)
        return (0, _DescStr(text) if desc else text, alias, id_tiebreak)

    # Defensive fallback for an unexpected sort_by (should not happen --
    # commands/job.py validates against JOB_SORT_FIELDS before this is
    # ever called). Treat like a generic missing/string field so we
    # degrade to the tiebreak order instead of crashing.
    raw = job.get(sort_by)
    if raw is None:
        return (1, "", alias, id_tiebreak)
    text = str(raw)
    return (0, _DescStr(text) if desc else text, alias, id_tiebreak)


def _safe_fetch_log_tail(client: Any, job: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Fetch the last ``limit`` events for a job; never raises.

    Resolves ``runId`` from the job dict (falls back to ``id`` on legacy
    records where Queue v2 makes them equal). Storage Events API returns
    newest -> oldest, which we keep as-is: a "tail" display wants the
    most recent events first, and callers can reverse for chronology if
    they prefer.

    Log-tail capture is a convenience surface; failing the whole command
    because the events endpoint blipped would obscure the real underlying
    error. We log the secondary failure at debug level and return an empty
    list so callers can safely attach it to their result payload.
    """
    if limit <= 0:
        return []
    run_id = str(job.get("runId") or job.get("id") or "")
    if not run_id:
        # Defensive: malformed job dict with neither runId nor id. Log so
        # a real API regression doesn't get masked by the silent return.
        logger.debug(
            "log tail fetch skipped: job has no runId or id (keys=%s)",
            sorted(job.keys()),
        )
        return []
    try:
        events = client.fetch_job_events(run_id, limit=limit)
    except KeboolaApiError as exc:
        logger.debug(
            "fetch_job_events(%s) failed (%s): %s; surfacing empty logTail",
            run_id,
            exc.error_code,
            exc.message,
        )
        return []
    except Exception as exc:  # defensive: do not let tail fetch tank the run
        logger.debug("fetch_job_events(%s) raised %r; surfacing empty logTail", run_id, exc)
        return []
    # Storage Events API returns newest -> oldest today, but we do not
    # rely on that: sort defensively by `created` DESC so a future API
    # ordering change cannot silently invert the tail. Events without a
    # `created` key sort last (they lose the race to timestamped peers).
    events_list = list(events)
    events_list.sort(key=lambda e: e.get("created") or "", reverse=True)
    # Cap at limit in case the server ignored ?limit.
    return events_list[:limit]


def _enrich_with_tail(
    base_details: dict[str, Any] | None,
    tail: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return a fresh details dict with ``logTail`` merged in, or None if empty.

    We never mutate a caught exception's ``.details`` in place; we construct
    a new dict and let the caller wrap it in a new KeboolaApiError. That
    keeps exception identity clean and makes the chain explicit via
    ``raise ... from exc``.
    """
    if not tail and not base_details:
        return None
    merged: dict[str, Any] = dict(base_details or {})
    if tail:
        merged["logTail"] = tail
    return merged or None


def _terminate_and_wait(
    client: Any,
    job_id: str,
    grace_seconds: float,
) -> dict[str, Any] | None:
    """Issue kill_job and poll briefly for terminal status.

    Returns the final job dict if termination was observed within
    ``grace_seconds``; returns ``None`` if the kill call itself failed
    (network, auth, race against remote terminal state). Never raises --
    the caller decides how to surface the failure.

    Idempotent under concurrent kill attempts: if the user runs
    ``kbagent job terminate --job-id X`` while ``job run --wait`` is
    auto-cancelling X on timeout, both paths land on Queue API's
    ``POST /jobs/{id}/kill``. The second caller sees HTTP 400
    "not in killable states" (or HTTP 500 with body 404 for already-final
    jobs); the KeboolaApiError branch below still succeeds as long as a
    follow-up GET confirms isFinished=True. No data loss, no double-kill
    error surfaced.
    """
    try:
        client.kill_job(job_id)
    except KeboolaApiError as exc:
        # If the job is already terminal, kill returns 400/500 -- honour
        # that as "cancelled successfully" when a follow-up GET confirms.
        logger.debug("kill_job(%s) raised %s: %s", job_id, exc.error_code, exc.message)
        try:
            job = client.get_job_detail(job_id)
        except Exception:
            logger.debug("terminate: kill failed AND GET failed for %s; giving up", job_id)
            return None
        if job.get("isFinished"):
            return job
        logger.debug(
            "terminate: kill failed, GET succeeded but job %s still not terminal "
            "(status=%r); returning None (remote may still be running)",
            job_id,
            job.get("status"),
        )
        return None
    except Exception as exc:
        logger.debug("kill_job(%s) raised %r; giving up", job_id, exc)
        return None

    deadline = time.monotonic() + grace_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            job = client.get_job_detail(job_id)
        except Exception:
            time.sleep(min(JOB_TERMINATE_POLL_INTERVAL, remaining))
            continue
        if job.get("isFinished"):
            return job
        # Cap the sleep so we never overshoot the grace deadline by a
        # full poll interval (caller may be latency-sensitive).
        time.sleep(min(JOB_TERMINATE_POLL_INTERVAL, max(0.0, deadline - time.monotonic())))

    # Remote still wasn't terminal within the grace window; return whatever
    # the last GET saw so callers can surface the actual state.
    try:
        final = client.get_job_detail(job_id)
    except Exception:
        logger.debug("terminate: grace window exhausted and final GET failed for %s", job_id)
        return None
    logger.debug(
        "terminate: grace window exhausted; job %s status=%r isFinished=%r",
        job_id,
        final.get("status"),
        final.get("isFinished"),
    )
    return final


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
        offset: int = 0,
        sort_by: str = DEFAULT_JOB_SORT_BY,
        sort_order: str = DEFAULT_JOB_SORT_ORDER,
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
                offset=offset,
                sort_by=sort_by,
                sort_order=sort_order,
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
        offset: int = 0,
        sort_by: str = DEFAULT_JOB_SORT_BY,
        sort_order: str = DEFAULT_JOB_SORT_ORDER,
    ) -> dict[str, Any]:
        """List jobs across one or multiple projects.

        Queries each resolved project's Queue API for jobs in parallel,
        aggregates results into a unified list. Per-project errors are
        collected but do not stop other projects from being queried.

        Each project's page is already sorted server-side by ``sort_by``/
        ``sort_order``, but merging several already-sorted pages by simple
        concatenation would leave the aggregate grouped by project instead
        of globally ordered. The merged ``jobs`` list is therefore
        re-sorted client-side by the SAME ``sort_by``/``sort_order`` the
        caller asked for -- e.g. the default ``startTime desc`` gives a
        single chronological feed interleaved across every project, which
        is what both the CLI's multi-project ``job list`` and the
        cross-project "All Jobs" ``serve`` view need. A job missing the
        requested field (e.g. a ``waiting`` job has no ``startTime`` yet)
        sorts LAST regardless of ``sort_order`` -- it is "not yet
        comparable", not "the oldest job". Ties (including the entire
        missing-value group) break deterministically on
        ``(project_alias, str(id))``. See ``_job_sort_key``.

        Args:
            aliases: Project aliases to query. None means all projects.
            component_id: Optional filter by component ID.
            config_id: Optional filter by config ID.
            status: Optional filter by job status.
            limit: Max number of jobs per project (1-500, default 50).
            offset: Number of jobs to skip per project, for pagination.
            sort_by: Queue API field to sort by (see JOB_SORT_FIELDS).
            sort_order: "asc" or "desc".

        Returns:
            Dict with keys:
                - "jobs": list of job dicts with project_alias added,
                  globally sorted by sort_by/sort_order (missing values last)
                - "errors": list of error dicts with project_alias,
                  error_code, message

        Raises:
            ConfigError: If a specified alias is not found (before querying).
        """
        projects = self.resolve_projects(aliases)

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            return self._fetch_project_jobs(
                alias,
                project,
                component_id,
                config_id,
                status,
                limit,
                offset,
                sort_by,
                sort_order,
            )

        successes, errors = self._run_parallel(projects, worker)

        # Flatten jobs from all successful projects
        all_jobs: list[dict[str, Any]] = []
        for _alias, jobs, _ok in successes:
            all_jobs.extend(jobs)

        # Merge into one globally-ordered feed. Each project's Queue API
        # call already sorted its own page server-side, but naively
        # concatenating those pages leaves the aggregate grouped by
        # project instead of chronological (or whatever sort_by asks
        # for) -- see _job_sort_key for the missing-value / tiebreak
        # semantics.
        all_jobs.sort(key=lambda j: _job_sort_key(j, sort_by, sort_order))
        errors.sort(key=lambda e: e.get("project_alias", ""))

        return {"jobs": all_jobs, "errors": errors}

    def get_job_detail(
        self,
        alias: str,
        job_id: str,
        log_tail_lines: int = 0,
    ) -> dict[str, Any]:
        """Get detailed information about a specific job.

        Args:
            alias: Project alias to query.
            job_id: The job ID.
            log_tail_lines: When > 0, also fetch that many of the job's most
                recent events and attach them as ``logTail``. Off by default
                so a plain detail lookup stays a single API call; ``job run``
                attaches a tail on its own for terminal failures.

        Returns:
            Dict with the full job detail from the Queue API, plus a
            "project_alias" key (and ``logTail`` when requested).

        Raises:
            ConfigError: If the alias is not found.
            KeboolaApiError: If the API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            detail = client.get_job_detail(job_id)
            if log_tail_lines > 0:
                # Never raises -- a blip on the events endpoint must not hide
                # the job detail the caller actually asked for.
                detail["logTail"] = _safe_fetch_log_tail(client, detail, log_tail_lines)
        finally:
            client.close()

        detail["project_alias"] = alias
        if detail.get("component") in ORCHESTRATION_JOB_COMPONENTS:
            # A flow run with no matching cron schedule is not necessarily
            # manual -- table triggers and cross-project triggers also start
            # flows and neither appears in `schedule list`. That dead-end is
            # how issue #714's false "this flow has no trigger" happened, so
            # point the investigation at the command that answers it.
            detail["trigger_hint"] = (
                "If no cron schedule explains this run, check `kbagent flow triggers "
                f"--project {alias} --flow-id {detail.get('config', '')}` -- table "
                "triggers and cross-project triggers never appear in `schedule list`."
            )
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
        poll_strategy: str = DEFAULT_POLL_STRATEGY,
        log_tail_lines: int = DEFAULT_LOG_TAIL_LINES,
        mode: str = DEFAULT_JOB_MODE,
        idempotency_key: str | None = None,
        force_rerun: bool = False,
        only_flow_task_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create and optionally wait for a Queue API job.

        When the config has linked variables (``configuration.variables_id``),
        auto-resolve a ``variableValuesId`` via :meth:`resolve_variable_values_id`
        so the job runs against the deployed values row instead of empty
        strings. Pass ``variable_values_id`` to override, or ``no_variables=True``
        to skip the resolution entirely.

        Wait-mode behavior (``wait=True``):

        - Polls the Queue API using ``poll_strategy`` (default exponential,
          matching FIIA and the Go CLI cadence: 2s x 30 -> 5s x 48 -> 15s).
        - When the job lands in a terminal non-success state (``error``,
          ``warning``, ``terminated``) and ``log_tail_lines > 0``, fetches
          the job's events via ``fetch_job_events`` and attaches the last
          ``log_tail_lines`` as the ``logTail`` key on the returned dict.
        - If the local deadline elapses before the remote job finishes,
          issues ``kill_job`` to cancel the remote work, waits briefly for
          termination, and raises ``KeboolaApiError`` with
          ``error_code=ErrorCode.JOB_TIMEOUT_TERMINATED``. If the kill call itself
          fails we fall back to the original ``QUEUE_JOB_TIMEOUT`` error so
          the caller can tell "local gave up" from "remote was cancelled".

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
            poll_strategy: Wait cadence. One of VALID_POLL_STRATEGIES.
            log_tail_lines: Number of trailing events to surface on
                non-success terminal states. ``0`` disables the fetch.
            mode: Queue API job mode. One of ``VALID_JOB_MODES`` -- ``"run"``
                (default) writes to mapped output tables; ``"debug"`` runs the
                component but redirects the output to a Storage File tagged
                ``debug-<jobId>`` instead of into destination buckets, so the
                job can be safely re-run for diagnostics without touching
                production data.
            idempotency_key: Optional client-supplied de-duplication token
                (issue #427). When a prior run recorded under the same key is
                still running or finished non-failed, that job is returned
                instead of creating a duplicate -- so an interrupted, replayed
                build step is safe. A prior *failed* run is re-run. The Queue
                API has no server-side idempotency, so dedup is client-side and
                scoped to the local config-dir store (per machine). Reusing a key
                for a *different* component/config raises rather than return a
                wrong job.
            force_rerun: If True, ignore any recorded ``idempotency_key`` entry
                and always create a fresh job (overwriting the stored entry).
            only_flow_task_ids: Optional ``keboola.flow`` task-id allowlist for
                a partial flow run (issue #725). Resolved by
                ``FlowService.resolve_flow_task_ids``; passed through verbatim.
                Meaningless for a non-flow component -- the Queue API ignores
                it there.

        Returns:
            Job dict with ``project_alias``. If wait=True, returns the
            completed job, optionally with ``logTail`` attached; otherwise
            returns the initial job response.
        """
        if poll_strategy not in VALID_POLL_STRATEGIES:
            raise KeboolaApiError(
                message=(
                    f"Invalid poll_strategy {poll_strategy!r}. "
                    f"Expected one of: {sorted(VALID_POLL_STRATEGIES)}."
                ),
                status_code=0,
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        # Group enum-membership pre-flight checks together (poll_strategy + mode)
        # before the numeric/range checks below -- both are O(1) frozenset lookups
        # and they fail in the same shape (INVALID_ARGUMENT with a sorted list of
        # accepted values), so keeping them adjacent makes a typo's failure mode
        # symmetric for callers.
        if mode not in VALID_JOB_MODES:
            raise KeboolaApiError(
                message=(f"Invalid mode {mode!r}. Expected one of: {sorted(VALID_JOB_MODES)}."),
                status_code=0,
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        if log_tail_lines < 0:
            raise KeboolaApiError(
                message=f"log_tail_lines must be >= 0, got {log_tail_lines}.",
                status_code=0,
                error_code=ErrorCode.INVALID_ARGUMENT,
            )

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

            def _create() -> dict[str, Any]:
                return client.create_job(
                    component_id=component_id,
                    config_id=config_id,
                    config_row_ids=config_row_ids,
                    branch_id=branch_id,
                    variable_values_id=resolved_values_id,
                    mode=mode,
                    only_flow_task_ids=only_flow_task_ids,
                )

            store = self._default_idempotency_store() if idempotency_key else None
            job, replayed = run_idempotent_job(
                store=store,
                key=idempotency_key,
                component_id=component_id,
                config_id=config_id,
                branch_id=branch_id,
                force_rerun=force_rerun,
                create=_create,
                fetch=client.get_job_detail,
            )
            job_id = str(job.get("id", ""))

            if wait and job_id:
                try:
                    job = client.wait_for_queue_job(
                        job_id,
                        max_wait=timeout,
                        poll_strategy=poll_strategy,
                    )
                except KeboolaApiError as exc:
                    job = self._handle_wait_error(
                        client=client,
                        job_id=job_id,
                        exc=exc,
                        log_tail_lines=log_tail_lines,
                        timeout=timeout,
                    )
                else:
                    # Successful poll: still attach log tail for warning /
                    # terminated statuses (not raised; just surfaced).
                    status = str(job.get("status") or "")
                    if status in _LOG_TAIL_STATUSES and log_tail_lines > 0:
                        job["logTail"] = _safe_fetch_log_tail(client, job, log_tail_lines)
        finally:
            client.close()

        job["project_alias"] = alias
        if resolved_values_id:
            job["resolvedVariableValuesId"] = resolved_values_id
        if replayed:
            job["idempotent_replay"] = True
        return job

    def _default_idempotency_store(self) -> JobIdempotencyStore:
        """The config-dir-scoped dedup store for the CLI / service job-run path.

        In-process SDK consumers supply their own store (see
        ``keboola_agent_cli.Client``); the CLI path persists alongside
        ``config.json`` so a replayed `kbagent job run --idempotency-key` is
        deduplicated across invocations on the same machine.
        """
        return JobIdempotencyStore(self._config_store.config_dir / JOB_IDEMPOTENCY_FILENAME)

    def _handle_wait_error(
        self,
        client: Any,
        job_id: str,
        exc: KeboolaApiError,
        log_tail_lines: int,
        timeout: float,
    ) -> dict[str, Any]:
        """Handle a terminal wait_for_queue_job error.

        Two error classes land here:

        - ``QUEUE_JOB_FAILED`` -- remote job reached ``error`` status on its
          own. Fetch the log tail so the caller sees the last events and
          re-raise with the events attached to ``exc.details``.
        - ``QUEUE_JOB_TIMEOUT`` -- local deadline exceeded before the job
          finished. Issue ``kill_job`` to cancel the remote work, wait
          briefly for termination, fetch the log tail and re-raise with a
          distinct ``JOB_TIMEOUT_TERMINATED`` code so shells can tell
          "we cancelled the job" from "job failed on its own".

        We never swallow the error; on any failure path we re-raise so the
        command layer maps to the correct exit code.
        """
        code = exc.error_code
        if code == "QUEUE_JOB_FAILED":
            # GET the job to resolve runId (required for Storage events)
            # and to enrich the error details with the failed job payload.
            try:
                failed_job = client.get_job_detail(job_id)
            except Exception:
                failed_job = {"id": job_id}
            tail = _safe_fetch_log_tail(client, failed_job, log_tail_lines)
            details = _enrich_with_tail(exc.details, tail)
            raise KeboolaApiError(
                message=exc.message,
                status_code=exc.status_code,
                error_code=exc.error_code,
                retryable=exc.retryable,
                details=details or {},
            ) from exc

        if code == "QUEUE_JOB_TIMEOUT":
            cancelled = _terminate_and_wait(
                client, job_id, grace_seconds=JOB_TERMINATE_GRACE_SECONDS
            )
            tail = _safe_fetch_log_tail(client, cancelled or {"id": job_id}, log_tail_lines)
            if cancelled is not None:
                raise KeboolaApiError(
                    message=(
                        f"Queue job {job_id} exceeded the local --timeout "
                        f"({timeout:.0f}s); issued kill and observed "
                        f"status={cancelled.get('status')!r}."
                    ),
                    status_code=504,
                    error_code=ErrorCode.JOB_TIMEOUT_TERMINATED,
                    retryable=False,
                    details={"job": cancelled, "logTail": tail},
                ) from exc
            # kill failed: surface the original timeout but keep the tail.
            details = _enrich_with_tail(exc.details, tail)
            raise KeboolaApiError(
                message=exc.message,
                status_code=exc.status_code,
                error_code=exc.error_code,
                retryable=exc.retryable,
                details=details or {},
            ) from exc

        # Anything else (network, auth, a future Queue error code we
        # don't specialise) bubbles up unchanged so the command layer
        # maps it through map_error_to_exit_code().
        logger.debug(
            "run_job: unhandled wait error code=%s; bubbling up unchanged",
            code,
        )
        raise

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
                error_code=ErrorCode.NO_VARIABLE_ROWS,
            )

        # A row without a usable `id` would otherwise coerce to `""`, which
        # the HTTP client treats as "omit from Queue body" -- silently
        # submitting a job with empty variable bindings. Fail loud instead.
        first_row = rows[0] if isinstance(rows[0], dict) else {}
        first_row_id = first_row.get("id")
        if not first_row_id:
            raise KeboolaApiError(
                message=(
                    f"First row of variables config {variables_id} has no 'id' -- "
                    f"malformed Storage API response. Refusing to submit a job "
                    f"with empty variable bindings."
                ),
                status_code=0,
                error_code=ErrorCode.MALFORMED_VARIABLES_ROW,
            )
        return str(first_row_id)

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
