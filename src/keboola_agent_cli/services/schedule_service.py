"""Schedule discovery and audit service.

Provides fleet-wide read-only queries over ``keboola.scheduler`` configurations
across one or many projects, joined in memory with the parent component+config
so callers can answer the cross-project question "which flows/orchestrations
are scheduled on cron triggers?" without N+1 API calls.

Schedules are stored as regular Storage API configurations of the
``keboola.scheduler`` component -- the same shape written by
``kbagent flow schedule``. Each config body follows::

    configuration.target = {"componentId": "...", "configurationId": "..."}
    configuration.schedule.cronTab = "0 6 * * *"
    configuration.schedule.timezone = "Europe/Prague"
    configuration.schedule.state = "enabled"   # or "disabled"

This read/audit path needs no Scheduler Service HTTP client -- everything
reuses ``KeboolaClient.list_component_configs`` + ``get_config_detail`` +
``list_jobs`` from the Storage and Queue APIs. (The WRITE path is different:
making a schedule actually fire requires registering it with the Scheduler
Service -- see ``SchedulerClient`` and ``FlowService.set_flow_schedule``.)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from ..errors import ConfigError, KeboolaApiError
from ..models import ProjectConfig
from .base import BaseService

logger = logging.getLogger(__name__)

SCHEDULER_COMPONENT_ID = "keboola.scheduler"

# ---------------------------------------------------------------------------
# Helpers (internal)
# ---------------------------------------------------------------------------


def _parse_configuration(raw: Any) -> dict[str, Any]:
    """Return a parsed configuration dict regardless of whether raw is str or dict."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw or {}


def _extract_schedule_fields(cfg: dict[str, Any]) -> dict[str, Any]:
    """Project a raw scheduler config dict into the canonical schedule row.

    Returns a dict with the keys the CLI exposes downstream:
    schedule_id, schedule_name, parent_component_id, parent_config_id,
    cron, timezone, enabled.

    ``parent_name`` is left unset here -- the service joins it separately
    once the parent config metadata has been fetched.
    """
    body = _parse_configuration(cfg.get("configuration"))
    target = body.get("target") or {}
    sched = body.get("schedule") or {}

    state_raw = sched.get("state")
    # keboola.scheduler stores state as the string "enabled"/"disabled".
    # Treat the string form canonically; fall back to dict.enabled for
    # forward compatibility if the API ever switches shape.
    if isinstance(state_raw, dict):
        enabled = bool(state_raw.get("enabled", False))
    else:
        enabled = str(state_raw).lower() == "enabled"

    return {
        "schedule_id": str(cfg.get("id", "")),
        "schedule_name": cfg.get("name", ""),
        "parent_component_id": str(target.get("componentId", "")),
        "parent_config_id": str(target.get("configurationId", "")),
        "cron": str(sched.get("cronTab", "")),
        "timezone": str(sched.get("timezone", "UTC")),
        "enabled": enabled,
    }


# ---------------------------------------------------------------------------
# Cron window parsing (hour-field approximation)
# ---------------------------------------------------------------------------


# Minutes are validated strictly (00-59) even though the matcher later
# ignores them -- rejecting obviously malformed input like ``04:88`` at
# parse time yields a clearer error than silently accepting garbage.
_WINDOW_RE = re.compile(r"^\s*(\d{1,2}):([0-5][0-9])\s*-\s*(\d{1,2}):([0-5][0-9])\s*$")


def parse_cron_window(spec: str) -> tuple[int, int]:
    """Parse a cron-window spec like ``02:00-04:00`` into ``(start_hour, end_hour)``.

    Minutes in the spec are accepted syntactically (they match the user's
    mental model of "between 02:00 and 04:00") but are ignored by the
    matcher -- see :func:`cron_in_window`. Minute values outside 00-59
    are still rejected at parse time so obviously malformed inputs fail
    loudly rather than silently matching at hour granularity. The range
    is inclusive on both ends of the hour field.

    Raises:
        ValueError: on malformed input.
    """
    match = _WINDOW_RE.match(spec)
    if not match:
        raise ValueError(
            f"Invalid --cron-window '{spec}'. Expected format 'HH:MM-HH:MM' "
            "with HH in 0-23 and MM in 00-59 (e.g. '02:00-04:00')."
        )
    start_hour = int(match.group(1))
    end_hour = int(match.group(3))
    if not (0 <= start_hour <= 23) or not (0 <= end_hour <= 23):
        raise ValueError(f"Hour field out of range in --cron-window '{spec}'. Must be 0-23.")
    if end_hour < start_hour:
        raise ValueError(
            f"--cron-window '{spec}' has end hour before start hour. "
            "Wrap-around windows are not supported; split into two audits -- "
            f"e.g. '22:00-02:00' -> run once with '22:00-23:00' and once with "
            "'00:00-02:00', then union the results."
        )
    return start_hour, end_hour


def _expand_hour_field(field_: str) -> set[int] | None:
    """Expand a single cron hour-field token into the set of matching hours.

    Returns None if the field matches every hour (``*`` or ``*/1``) so callers
    can short-circuit. Returns an empty set if nothing parsed -- the caller
    must treat that as "do not match" rather than "match everything" to stay
    on the safe side for audit use-cases.
    """
    field_ = field_.strip()
    if field_ == "*":
        return None

    hours: set[int] = set()
    for token in field_.split(","):
        token = token.strip()
        if not token:
            continue
        step = 1
        body = token
        if "/" in token:
            body, step_s = token.split("/", 1)
            try:
                step = max(int(step_s), 1)
            except ValueError:
                return set()  # unparseable -> no match, fail safe
            if body == "" or body == "*":
                body = "0-23"

        if "-" in body:
            try:
                lo_s, hi_s = body.split("-", 1)
                lo = int(lo_s)
                hi = int(hi_s)
            except ValueError:
                return set()
            if lo < 0 or hi < 0 or lo > 23 or hi > 23 or hi < lo:
                return set()
            hours.update(range(lo, hi + 1, step))
        else:
            try:
                hours.add(int(body))
            except ValueError:
                return set()

    return hours


def cron_in_window(cron_tab: str, start_hour: int, end_hour: int) -> bool:
    """Return True if the cron expression fires only within ``[start_hour, end_hour]``.

    This is an **hour-field approximation**: the check only looks at the
    third cron field (hour). Minute precision is a best-effort compromise --
    cron minute patterns are rarely restrictive enough to change "which
    hour does this cron run in?" for realistic scheduling, and parsing
    full cron semantics would require an external dependency. For audit
    workflows (the motivating use-case on issue #195), hour-level accuracy
    is sufficient.

    - Hour field ``*`` (fires every hour) is treated as OUT of any bounded
      window because the schedule is not confined to the window.
    - Empty / unparseable cron -> returns False.
    - Any matched hour outside the window -> returns False.

    Args:
        cron_tab: Standard 5-field cron expression (minute hour day month weekday).
        start_hour: Inclusive window start (0-23).
        end_hour: Inclusive window end (0-23).

    Returns:
        True when all hours at which this cron fires fall inside the window.
    """
    if not cron_tab:
        return False

    parts = cron_tab.strip().split()
    if len(parts) < 5:
        return False

    hour_field = parts[1]
    expanded = _expand_hour_field(hour_field)
    if expanded is None:
        # '*' -- fires every hour; not confined to any bounded window.
        return False
    if not expanded:
        return False

    window = set(range(start_hour, end_hour + 1))
    return expanded.issubset(window)


# ---------------------------------------------------------------------------
# "not run since" helper (Queue API timestamp parsing)
# ---------------------------------------------------------------------------


def _parse_iso_timestamp(raw: str) -> datetime | None:
    """Parse an ISO-8601 timestamp (with or without ``Z``) into an aware datetime."""
    if not raw:
        return None
    candidate = raw.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def job_is_stale(
    latest_job_ts: str | None, threshold_days: int, *, now: datetime | None = None
) -> bool:
    """Return True when the latest job timestamp is older than ``threshold_days``.

    A missing timestamp (no job ever ran) also counts as stale -- the
    caller asked "show me configs where the parent has not run in N days"
    and "never ran" is the strongest form of that condition.

    Args:
        latest_job_ts: ISO-8601 timestamp of the most recent job, or None.
        threshold_days: Positive integer number of days.
        now: Optional clock override for determinism in tests.
    """
    if latest_job_ts is None:
        return True
    ts = _parse_iso_timestamp(latest_job_ts)
    if ts is None:
        return True
    reference = now or datetime.now(tz=UTC)
    return (reference - ts) > timedelta(days=threshold_days)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ScheduleService(BaseService):
    """Fleet-wide discovery for ``keboola.scheduler`` configurations.

    All methods are **read-only** and accumulate per-project errors in the
    ``errors`` field of the returned dict rather than aborting the entire
    multi-project fan-out.
    """

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------

    def list_schedules(
        self,
        aliases: list[str] | None = None,
        enabled_only: bool = False,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """List all schedule configurations across one, many, or all projects.

        Parent names are resolved in the same worker (single
        ``list_components_with_configs`` call) so the multi-project fan-out
        is an O(#projects) API round-trip rather than O(#schedules).

        Args:
            aliases: Project aliases to query. ``None`` / empty means every
                registered project.
            enabled_only: Drop schedules whose state is disabled.
            branch_id: Dev-branch ID; only valid when the caller supplied
                exactly one alias (same rule the CLI enforces).

        Returns:
            ``{"schedules": [...], "errors": [...]}``. Each schedule row has
            ``project_alias``, ``schedule_id``, ``schedule_name``,
            ``parent_component_id``, ``parent_config_id``, ``parent_name``,
            ``cron``, ``timezone``, ``enabled``.
        """
        projects = self.resolve_projects(aliases)

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            return self._fetch_project_schedules(
                alias,
                project,
                enabled_only=enabled_only,
                branch_id=branch_id,
            )

        successes, errors = self._run_parallel(projects, worker)

        schedules: list[dict[str, Any]] = []
        for result in successes:
            schedules.extend(result[1])
        schedules.sort(
            key=lambda s: (
                s.get("project_alias", ""),
                s.get("parent_component_id", ""),
                (s.get("parent_name") or "").lower(),
                s.get("schedule_id", ""),
            )
        )
        errors.sort(key=lambda e: e.get("project_alias", ""))

        return {"schedules": schedules, "errors": errors}

    # ------------------------------------------------------------------
    # detail
    # ------------------------------------------------------------------

    def get_schedule_detail(
        self,
        alias: str,
        schedule_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Return the full detail for a single schedule config.

        Performs two API calls: one ``get_config_detail`` on the
        ``keboola.scheduler`` config and a second one to resolve the
        parent's display name. The parent lookup is best-effort -- if it
        fails the schedule detail is still returned with ``parent_name``
        left empty.

        Raises:
            ConfigError: alias not registered.
            KeboolaApiError: schedule not found or permission denied.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            raw = client.get_config_detail(
                SCHEDULER_COMPONENT_ID, schedule_id, branch_id=effective_branch
            )
            schedule_row = _extract_schedule_fields(raw)

            parent_name = ""
            parent_component_id = schedule_row["parent_component_id"]
            parent_config_id = schedule_row["parent_config_id"]
            if parent_component_id and parent_config_id:
                try:
                    parent = client.get_config_detail(
                        parent_component_id, parent_config_id, branch_id=effective_branch
                    )
                    parent_name = parent.get("name", "")
                except KeboolaApiError as exc:
                    # Orphaned schedule (parent deleted) is a legitimate
                    # state -- surface it via parent_name="" rather than
                    # failing the whole detail lookup.
                    logger.debug(
                        "Parent config lookup failed for schedule %s (%s/%s): %s",
                        schedule_id,
                        parent_component_id,
                        parent_config_id,
                        exc,
                    )
        finally:
            client.close()

        return {
            "project_alias": alias,
            "branch_id": effective_branch,
            "schedule_id": schedule_row["schedule_id"],
            "schedule_name": schedule_row["schedule_name"],
            "parent_component_id": parent_component_id,
            "parent_config_id": parent_config_id,
            "parent_name": parent_name,
            "cron": schedule_row["cron"],
            "timezone": schedule_row["timezone"],
            "enabled": schedule_row["enabled"],
            "configuration": _parse_configuration(raw.get("configuration")),
            "version": raw.get("version"),
            "created": raw.get("created", ""),
            "change_description": raw.get("changeDescription", ""),
        }

    # ------------------------------------------------------------------
    # find (audit filters)
    # ------------------------------------------------------------------

    def find_schedules(
        self,
        aliases: list[str] | None = None,
        cron_window: str | None = None,
        not_run_since_days: int | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Audit filter combining cron-window + not-run-since constraints.

        Both filters are optional and combine with AND. Without either
        filter this is equivalent to :meth:`list_schedules` plus two
        always-present audit columns (``last_run_at`` and
        ``matches_cron_window``) that stay ``None`` until the
        corresponding filter opts in.

        Args:
            aliases: Project aliases to search (None = all).
            cron_window: String ``HH:MM-HH:MM``; pass None to skip the filter.
            not_run_since_days: Integer day-threshold; pass None to skip.
            branch_id: Dev-branch ID (single-project only).

        Raises:
            ConfigError: invalid cron-window or not-run-since-days value
                (validated at the service layer boundary -- commands never
                touch the parsing logic).

        Returns:
            ``{"schedules": [...], "errors": [...], "filters": {...}}``.
            Each row adds two audit columns next to the base schedule
            fields:

            - ``last_run_at`` -- ISO-8601 timestamp or ``None``. Populated
              only when ``not_run_since_days`` is set; otherwise ``None``
              to avoid N extra Queue API calls per project purely to
              populate an unrequested column. Pass
              ``not_run_since_days=0`` to force the lookup for every row.
            - ``matches_cron_window`` -- ``bool`` or ``None``. Populated
              only when ``cron_window`` is set; otherwise ``None`` so
              downstream consumers do not treat the column as a positive
              match signal when the filter was not evaluated.
        """
        hour_bounds: tuple[int, int] | None = None
        if cron_window is not None:
            try:
                hour_bounds = parse_cron_window(cron_window)
            except ValueError as exc:
                raise ConfigError(str(exc)) from None

        if not_run_since_days is not None and not_run_since_days < 0:
            raise ConfigError("--not-run-since must be a non-negative integer number of days.")

        projects = self.resolve_projects(aliases)

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            return self._find_in_project(
                alias,
                project,
                hour_bounds=hour_bounds,
                not_run_since_days=not_run_since_days,
                branch_id=branch_id,
            )

        successes, errors = self._run_parallel(projects, worker)

        schedules: list[dict[str, Any]] = []
        for result in successes:
            schedules.extend(result[1])
        schedules.sort(
            key=lambda s: (
                s.get("project_alias", ""),
                s.get("parent_component_id", ""),
                (s.get("parent_name") or "").lower(),
            )
        )
        errors.sort(key=lambda e: e.get("project_alias", ""))

        return {
            "schedules": schedules,
            "errors": errors,
            "filters": {
                "cron_window": cron_window,
                "not_run_since_days": not_run_since_days,
            },
        }

    # ------------------------------------------------------------------
    # Internal per-project workers
    # ------------------------------------------------------------------

    def _fetch_project_schedules(
        self,
        alias: str,
        project: ProjectConfig,
        enabled_only: bool,
        branch_id: int | None,
    ) -> tuple[Any, ...]:
        """Fetch + shape schedules for a single project.

        One ``list_components_with_configs`` round-trip gives us every
        config body plus the parent names we need to join -- no N+1.
        The response payload is proportional to the **entire project
        size**, not the schedule count. See ``schedule-workflow.md`` for
        the trade-off rationale.
        """
        effective_branch = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            components = client.list_components_with_configs(branch_id=effective_branch)

            scheduler_configs, parent_names = self._partition_components(components)

            schedules: list[dict[str, Any]] = []
            for cfg in scheduler_configs:
                row = _extract_schedule_fields(cfg)
                if enabled_only and not row["enabled"]:
                    continue

                parent_key = (row["parent_component_id"], row["parent_config_id"])
                row["project_alias"] = alias
                row["parent_name"] = parent_names.get(parent_key, "")
                schedules.append(row)

            return (alias, schedules, True)
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

    def _find_in_project(
        self,
        alias: str,
        project: ProjectConfig,
        hour_bounds: tuple[int, int] | None,
        not_run_since_days: int | None,
        branch_id: int | None,
    ) -> tuple[Any, ...]:
        """Apply audit filters inside a single project worker.

        Runs one ``list_components_with_configs`` to collect all
        schedule+parent data, then, when ``not_run_since_days`` is set,
        issues one ``list_jobs(limit=1)`` per unique parent (component+config)
        to establish the most recent job timestamp. That's at most
        ``#schedules`` extra round-trips per project, not
        ``#schedules * #projects``.

        Uses the same try/except/finally pattern as
        :meth:`_fetch_project_schedules` so the worker contract is
        symmetric: any exception short-circuits to the error-dict
        tuple, the client is always closed, and the caller sees the
        uniform ``(alias, payload, True?)`` shape.
        """
        effective_branch = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            components = client.list_components_with_configs(branch_id=effective_branch)

            scheduler_configs, parent_names = self._partition_components(components)

            rows: list[dict[str, Any]] = []
            for cfg in scheduler_configs:
                row = _extract_schedule_fields(cfg)
                parent_key = (row["parent_component_id"], row["parent_config_id"])
                row["project_alias"] = alias
                row["parent_name"] = parent_names.get(parent_key, "")
                # Columns are always present; population is gated on the
                # corresponding filter. ``None`` (rather than a hard-coded
                # ``True`` / ``""``) makes the "filter was not evaluated"
                # state explicit so downstream LLM/agent consumers do not
                # treat a missing evaluation as a positive match signal.
                row["matches_cron_window"] = None
                row["last_run_at"] = None

                if hour_bounds is not None:
                    row["matches_cron_window"] = cron_in_window(
                        row["cron"], hour_bounds[0], hour_bounds[1]
                    )

                rows.append(row)

            # Resolve last_run_at only for rows that might still match.
            # Skipped entirely when ``--not-run-since`` is inactive so we do
            # not pay N extra Queue API calls per project purely to populate
            # a column the caller did not ask for.
            if not_run_since_days is not None:
                for row in rows:
                    if hour_bounds is not None and not row["matches_cron_window"]:
                        # Will be filtered out below -- no need for the API call.
                        continue
                    row["last_run_at"] = self._fetch_latest_job_ts(
                        client,
                        row["parent_component_id"],
                        row["parent_config_id"],
                    )

            # Apply AND-filter
            filtered: list[dict[str, Any]] = []
            for row in rows:
                if hour_bounds is not None and not row["matches_cron_window"]:
                    continue
                if not_run_since_days is not None and not job_is_stale(
                    row["last_run_at"], not_run_since_days
                ):
                    continue
                filtered.append(row)

            return (alias, filtered, True)
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

    # ------------------------------------------------------------------
    # Helpers shared by both workers
    # ------------------------------------------------------------------

    @staticmethod
    def _partition_components(
        components: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[tuple[str, str], str]]:
        """Split the ``list_components_with_configs`` payload.

        Returns ``(scheduler_configs, parent_names)`` where ``parent_names``
        is a dict keyed by ``(component_id, config_id)`` (all stringified)
        so we can answer "what's the display name of the target this
        schedule points at?" without extra API calls.
        """
        scheduler_configs: list[dict[str, Any]] = []
        parent_names: dict[tuple[str, str], str] = {}

        for comp in components:
            comp_id = comp.get("id", "")
            for cfg in comp.get("configurations", []) or []:
                cfg_id = str(cfg.get("id", ""))
                if comp_id == SCHEDULER_COMPONENT_ID:
                    scheduler_configs.append(cfg)
                parent_names[(comp_id, cfg_id)] = cfg.get("name", "")

        return scheduler_configs, parent_names

    @staticmethod
    def _fetch_latest_job_ts(client: Any, component_id: str, config_id: str) -> str | None:
        """Return ISO-8601 timestamp of the most recent job for a parent config.

        Prefers ``startTime`` (when the job actually began executing) and
        falls back to ``createdTime`` (queue arrival) so the comparison
        stays meaningful even for jobs that never left the queue. Returns
        ``None`` if the parent has never had a job or if the Queue API
        call fails (failure is silent -- the audit use-case prefers
        "no information = stale" semantics, matching :func:`job_is_stale`).
        """
        if not component_id or not config_id:
            return None
        try:
            jobs = client.list_jobs(
                component_id=component_id,
                config_id=config_id,
                limit=1,
            )
        except KeboolaApiError as exc:
            logger.debug(
                "list_jobs failed for %s/%s while auditing schedules: %s",
                component_id,
                config_id,
                exc,
            )
            return None
        except Exception as exc:
            logger.debug(
                "Unexpected list_jobs failure for %s/%s: %s",
                component_id,
                config_id,
                exc,
            )
            return None
        if not jobs:
            return None
        latest = jobs[0]
        return latest.get("startTime") or latest.get("createdTime") or None


__all__ = [
    "SCHEDULER_COMPONENT_ID",
    "ScheduleService",
    "cron_in_window",
    "job_is_stale",
    "parse_cron_window",
]
