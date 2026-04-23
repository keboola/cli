"""Flow (orchestrator + conditional flow) lifecycle service.

Provides CRUD for keboola.orchestrator and keboola.flow configurations,
plus schedule bind/unbind via keboola.scheduler component configs.

Flows are semantic sugar over the Storage API config layer -- no separate
HTTP client is needed.  Schedules are stored as keboola.scheduler configs
whose ``target`` points at the flow.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..errors import ErrorCode, KeboolaApiError
from ..models import ProjectConfig
from .base import BaseService

logger = logging.getLogger(__name__)

FLOW_COMPONENT_IDS: tuple[str, ...] = ("keboola.orchestrator", "keboola.flow")
SCHEDULER_COMPONENT_ID = "keboola.scheduler"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_configuration(raw: Any) -> dict[str, Any]:
    """Return a parsed configuration dict regardless of whether raw is str or dict."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw or {}


def _count_phases_tasks(body: dict[str, Any]) -> tuple[int, int]:
    """Return (phase_count, task_count) from a flow configuration body."""
    return len(body.get("phases", [])), len(body.get("tasks", []))


def _validate_dag(phases: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> list[str]:
    """Validate phase dependency DAG for cycles and bad references.

    Uses Kahn's topological sort to detect cycles.  Returns a list of
    human-readable error strings; empty list means valid.
    """
    errors: list[str] = []
    phase_ids = {p.get("id") for p in phases if p.get("id") is not None}

    # Check dependsOn references
    for phase in phases:
        for dep_id in phase.get("dependsOn", []):
            if dep_id not in phase_ids:
                errors.append(f"Phase '{phase.get('id')}' depends on unknown phase '{dep_id}'")

    # Check task phase references
    for task in tasks:
        phase_ref = task.get("phase")
        if phase_ref is not None and phase_ref not in phase_ids:
            errors.append(f"Task '{task.get('id', '?')}' references unknown phase '{phase_ref}'")

    if errors:
        return errors

    # Kahn's algorithm for cycle detection
    in_degree: dict[Any, int] = {p.get("id"): 0 for p in phases if p.get("id") is not None}
    adj: dict[Any, list[Any]] = {p.get("id"): [] for p in phases if p.get("id") is not None}
    for phase in phases:
        pid = phase.get("id")
        if pid is None:
            continue
        for dep_id in phase.get("dependsOn", []):
            if dep_id in adj:
                adj[dep_id].append(pid)
                in_degree[pid] += 1

    queue = [pid for pid, deg in in_degree.items() if deg == 0]
    visited = 0
    while queue:
        node = queue.pop(0)
        visited += 1
        for neighbor in adj.get(node, []):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if visited != len(phase_ids):
        errors.append("Phase dependency graph contains a cycle")

    return errors


def _collect_schedules_by_parent(
    client: Any, branch_id: int | None
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Fetch every keboola.scheduler config for a project and group by target.

    Returns a dict keyed by ``(parent_component_id, parent_config_id)`` so
    ``list_flows`` can look up schedules per flow with a single in-memory
    lookup. Used by the ``--with-schedules`` enrichment path.

    A missing scheduler component (404 NOT_FOUND) yields an empty map.
    """
    try:
        all_sched = client.list_component_configs(SCHEDULER_COMPONENT_ID, branch_id=branch_id)
    except KeboolaApiError as exc:
        if exc.error_code == "NOT_FOUND":
            return {}
        raise

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for sched in all_sched:
        body = _parse_configuration(sched.get("configuration"))
        target = body.get("target") or {}
        sched_info = body.get("schedule") or {}

        parent_key = (
            str(target.get("componentId", "")),
            str(target.get("configurationId", "")),
        )
        if not parent_key[0] or not parent_key[1]:
            continue

        state_raw = sched_info.get("state")
        if isinstance(state_raw, dict):
            enabled = bool(state_raw.get("enabled", False))
        else:
            enabled = str(state_raw).lower() == "enabled"

        grouped.setdefault(parent_key, []).append(
            {
                "schedule_id": str(sched.get("id", "")),
                "cron": str(sched_info.get("cronTab", "")),
                "timezone": str(sched_info.get("timezone", "UTC")),
                "enabled": enabled,
            }
        )
    return grouped


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class FlowService(BaseService):
    """Business logic for flow (keboola.orchestrator + keboola.flow) CRUD.

    All schedule operations use keboola.scheduler component configs --
    no separate Scheduler Service HTTP client required.
    """

    # ── list ────────────────────────────────────────────────────────

    def list_flows(
        self,
        aliases: list[str] | None = None,
        branch_id: int | None = None,
        with_schedules: bool = False,
    ) -> dict[str, Any]:
        """List all flows across projects (both component IDs).

        When ``with_schedules`` is True, each flow row is enriched with a
        ``schedules`` list pulled from the same project's
        ``keboola.scheduler`` configs. The enrichment costs **one** extra
        ``list_component_configs`` call per project (NOT per flow) -- the
        join happens in memory by (component_id, config_id) key.

        Args:
            aliases: Project aliases to query; None means every project.
            branch_id: Dev-branch override for single-project fan-out.
            with_schedules: When True, populate ``schedules`` on each row.

        Returns:
            Dict with keys:
                - "flows": list of flow dicts (project_alias, component_id,
                  config_id, name, description, is_disabled, and
                  ``schedules`` when ``with_schedules`` is True)
                - "errors": list of error dicts
        """
        projects = self.resolve_projects(aliases)

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            client = self._client_factory(project.stack_url, project.token)
            effective_branch = branch_id or project.active_branch_id
            try:
                flows: list[dict[str, Any]] = []
                for comp_id in FLOW_COMPONENT_IDS:
                    try:
                        configs = client.list_component_configs(comp_id, branch_id=effective_branch)
                    except KeboolaApiError as exc:
                        # 404 = component not installed; skip gracefully
                        if exc.error_code == "NOT_FOUND":
                            continue
                        raise
                    for cfg in configs:
                        flow_row: dict[str, Any] = {
                            "project_alias": alias,
                            "component_id": comp_id,
                            "config_id": str(cfg.get("id", "")),
                            "name": cfg.get("name", ""),
                            "description": cfg.get("description", ""),
                            "is_disabled": cfg.get("isDisabled", False),
                        }
                        if with_schedules:
                            flow_row["schedules"] = []
                        flows.append(flow_row)

                # One extra list call per project, then a map-join in memory.
                if with_schedules and flows:
                    schedules_by_parent = _collect_schedules_by_parent(client, effective_branch)
                    for flow_row in flows:
                        key = (flow_row["component_id"], flow_row["config_id"])
                        flow_row["schedules"] = schedules_by_parent.get(key, [])

                return (alias, flows, True)
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

        successes, errors = self._run_parallel(projects, worker)

        all_flows: list[dict[str, Any]] = []
        for _, flows, _ in successes:
            all_flows.extend(flows)
        all_flows.sort(key=lambda f: (f["project_alias"], f["component_id"], f["name"].lower()))
        errors.sort(key=lambda e: e.get("project_alias", ""))

        return {"flows": all_flows, "errors": errors}

    # ── detail ──────────────────────────────────────────────────────

    def get_flow_detail(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Return full flow detail including phases, tasks, and schedule info.

        Raises:
            ConfigError: If alias is not found.
            KeboolaApiError: On API failure.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            detail = client.get_config_detail(component_id, config_id, branch_id=effective_branch)
        finally:
            client.close()

        body = _parse_configuration(detail.get("configuration"))
        phases = body.get("phases", [])
        tasks = body.get("tasks", [])

        detail["project_alias"] = alias
        detail["branch_id"] = effective_branch
        detail["phases"] = phases
        detail["tasks"] = tasks
        detail["phase_count"] = len(phases)
        detail["task_count"] = len(tasks)
        return detail

    # ── create ──────────────────────────────────────────────────────

    def create_flow(
        self,
        alias: str,
        component_id: str,
        name: str,
        description: str = "",
        phases: list[dict[str, Any]] | None = None,
        tasks: list[dict[str, Any]] | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a new flow configuration.

        Args:
            alias: Project alias.
            component_id: 'keboola.flow' (default) or 'keboola.orchestrator'.
            name: Flow name.
            description: Optional description.
            phases: Phase definitions (validated for DAG correctness).
            tasks: Task definitions.
            branch_id: Dev branch override.

        Raises:
            KeboolaApiError: On API failure or DAG validation error
                (error_code='INVALID_FLOW_DAG').
        """
        phases = phases or []
        tasks = tasks or []

        if phases:
            dag_errors = _validate_dag(phases, tasks)
            if dag_errors:
                raise KeboolaApiError(
                    message=f"Flow DAG validation failed: {'; '.join(dag_errors)}",
                    status_code=400,
                    error_code=ErrorCode.INVALID_FLOW_DAG,
                    retryable=False,
                )

        configuration: dict[str, Any] = {"phases": phases, "tasks": tasks}

        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            result = client.create_config(
                component_id=component_id,
                name=name,
                configuration=configuration,
                description=description,
                branch_id=effective_branch,
            )
        finally:
            client.close()

        result["project_alias"] = alias
        result["branch_id"] = effective_branch
        result["phase_count"] = len(phases)
        result["task_count"] = len(tasks)
        return result

    # ── update ──────────────────────────────────────────────────────

    def update_flow(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        name: str | None = None,
        description: str | None = None,
        phases: list[dict[str, Any]] | None = None,
        tasks: list[dict[str, Any]] | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Update an existing flow configuration.

        When both phases and tasks are provided, validates the DAG before writing.
        When only one is provided, the other is fetched from the current config.

        Raises:
            KeboolaApiError: On API failure or DAG validation error.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            configuration: dict[str, Any] | None = None
            if phases is not None or tasks is not None:
                current = client.get_config_detail(
                    component_id, config_id, branch_id=effective_branch
                )
                current_body = _parse_configuration(current.get("configuration"))
                merged_phases = phases if phases is not None else current_body.get("phases", [])
                merged_tasks = tasks if tasks is not None else current_body.get("tasks", [])

                if merged_phases:
                    dag_errors = _validate_dag(merged_phases, merged_tasks)
                    if dag_errors:
                        raise KeboolaApiError(
                            message=f"Flow DAG validation failed: {'; '.join(dag_errors)}",
                            status_code=400,
                            error_code=ErrorCode.INVALID_FLOW_DAG,
                            retryable=False,
                        )

                configuration = dict(current_body)
                configuration["phases"] = merged_phases
                configuration["tasks"] = merged_tasks

            result = client.update_config(
                component_id=component_id,
                config_id=config_id,
                name=name,
                description=description,
                configuration=configuration,
                change_description="Updated via kbagent flow update",
                branch_id=effective_branch,
            )
        finally:
            client.close()

        result["project_alias"] = alias
        result["branch_id"] = effective_branch
        return result

    # ── delete ──────────────────────────────────────────────────────

    def delete_flow(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Delete a flow configuration.

        Does NOT automatically remove associated keboola.scheduler configs.
        Use remove_flow_schedule() first if needed.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            client.delete_config(
                component_id=component_id,
                config_id=config_id,
                branch_id=effective_branch,
            )
        finally:
            client.close()

        return {
            "status": "deleted",
            "project_alias": alias,
            "component_id": component_id,
            "config_id": config_id,
            "branch_id": effective_branch,
        }

    # ── schedule ────────────────────────────────────────────────────

    def list_flow_schedules(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """List keboola.scheduler configs that target this flow.

        Fetches all keboola.scheduler configs and filters by
        target.componentId + target.configurationId.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            try:
                all_sched = client.list_component_configs(
                    SCHEDULER_COMPONENT_ID, branch_id=effective_branch
                )
            except KeboolaApiError as exc:
                if exc.error_code == "NOT_FOUND":
                    all_sched = []
                else:
                    raise
        finally:
            client.close()

        schedules: list[dict[str, Any]] = []
        for sched in all_sched:
            body = _parse_configuration(sched.get("configuration"))
            target = body.get("target") or {}
            if target.get("componentId") == component_id and str(
                target.get("configurationId", "")
            ) == str(config_id):
                sched_info = body.get("schedule") or {}
                schedules.append(
                    {
                        "schedule_id": str(sched.get("id", "")),
                        "name": sched.get("name", ""),
                        "cron_tab": sched_info.get("cronTab", ""),
                        "timezone": sched_info.get("timezone", "UTC"),
                        "state": sched_info.get("state", "disabled"),
                    }
                )

        return {
            "project_alias": alias,
            "component_id": component_id,
            "config_id": config_id,
            "schedules": schedules,
        }

    def set_flow_schedule(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        cron_tab: str,
        timezone: str = "UTC",
        enabled: bool = True,
        schedule_name: str | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Upsert a keboola.scheduler config that targets this flow.

        If a schedule already exists for this flow it is updated in-place
        (idempotent). If none exists a new one is created. This prevents
        duplicate schedules when called repeatedly.

        The schedule is stored as a keboola.scheduler configuration whose
        ``target`` points at the flow component + config.

        Args:
            alias: Project alias.
            component_id: Flow component ID.
            config_id: Flow configuration ID.
            cron_tab: Cron expression (e.g. '0 6 * * *').
            timezone: IANA timezone (default 'UTC').
            enabled: Whether the schedule is active.
            schedule_name: Optional scheduler config name.
            branch_id: Dev branch override.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            if not schedule_name:
                try:
                    detail = client.get_config_detail(
                        component_id, config_id, branch_id=effective_branch
                    )
                    schedule_name = f"{detail.get('name', config_id)} (Schedule)"
                except KeboolaApiError:
                    schedule_name = f"{config_id} (Schedule)"

            configuration = {
                "schedule": {
                    "cronTab": cron_tab,
                    "timezone": timezone,
                    "state": "enabled" if enabled else "disabled",
                },
                "target": {
                    "mode": "run",
                    "componentId": component_id,
                    "configurationId": config_id,
                },
            }

            # Upsert: update existing schedule if one exists
            try:
                existing = client.list_component_configs(
                    SCHEDULER_COMPONENT_ID, branch_id=effective_branch
                )
            except KeboolaApiError as exc:
                if exc.error_code == "NOT_FOUND":
                    existing = []
                else:
                    raise

            existing_id: str | None = None
            for sched in existing:
                body = _parse_configuration(sched.get("configuration"))
                target = body.get("target") or {}
                if target.get("componentId") == component_id and str(
                    target.get("configurationId", "")
                ) == str(config_id):
                    existing_id = str(sched.get("id", ""))
                    break

            if existing_id:
                result = client.update_config(
                    component_id=SCHEDULER_COMPONENT_ID,
                    config_id=existing_id,
                    name=schedule_name,
                    configuration=configuration,
                    branch_id=effective_branch,
                )
                status = "updated"
            else:
                result = client.create_config(
                    component_id=SCHEDULER_COMPONENT_ID,
                    name=schedule_name,
                    configuration=configuration,
                    branch_id=effective_branch,
                )
                status = "created"
        finally:
            client.close()

        return {
            "status": status,
            "project_alias": alias,
            "schedule_id": str(result.get("id", existing_id or "")),
            "schedule_name": schedule_name,
            "component_id": component_id,
            "config_id": config_id,
            "cron_tab": cron_tab,
            "timezone": timezone,
            "state": "enabled" if enabled else "disabled",
            "branch_id": effective_branch,
        }

    def remove_flow_schedule(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Delete all keboola.scheduler configs that target this flow.

        Idempotent: if no schedules exist, returns deleted_count=0.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            try:
                all_sched = client.list_component_configs(
                    SCHEDULER_COMPONENT_ID, branch_id=effective_branch
                )
            except KeboolaApiError as exc:
                if exc.error_code == "NOT_FOUND":
                    all_sched = []
                else:
                    raise

            deleted: list[str] = []
            errors: list[str] = []
            for sched in all_sched:
                body = _parse_configuration(sched.get("configuration"))
                target = body.get("target") or {}
                if target.get("componentId") == component_id and str(
                    target.get("configurationId", "")
                ) == str(config_id):
                    sched_id = str(sched.get("id", ""))
                    try:
                        client.delete_config(
                            SCHEDULER_COMPONENT_ID, sched_id, branch_id=effective_branch
                        )
                        deleted.append(sched_id)
                    except KeboolaApiError as exc:
                        errors.append(f"{sched_id}: {exc.message}")
        finally:
            client.close()

        if errors and not deleted:
            raise KeboolaApiError(
                message=f"Failed to delete schedules: {'; '.join(errors)}",
                status_code=0,
                error_code=ErrorCode.SCHEDULE_DELETE_FAILED,
                retryable=False,
            )

        return {
            "status": "removed",
            "project_alias": alias,
            "component_id": component_id,
            "config_id": config_id,
            "deleted_schedule_ids": deleted,
            "deleted_count": len(deleted),
            "branch_id": effective_branch,
        }
