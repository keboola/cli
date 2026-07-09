"""Conditional flow (keboola.flow) lifecycle service.

Provides CRUD for keboola.flow (Conditional Flow) configurations, plus
schedule bind/unbind via keboola.scheduler component configs.

keboola.orchestrator support was dropped in 0.57.0; this service targets the
single component keboola.flow. Legacy orchestrator configs are still counted
(not listed) so the CLI can warn users why a flow "disappeared".

Flows are semantic sugar over the Storage API config layer -- no separate
HTTP client is needed.  Schedules are stored as keboola.scheduler configs
whose ``target`` points at the flow, and are additionally registered with
the Scheduler Service (via ``SchedulerClient``) -- the Storage config alone
does not make the cron trigger fire.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..ai_client import AiServiceClient
from ..config_store import ConfigStore
from ..errors import ErrorCode, KeboolaApiError
from ..models import ComponentDetail, ProjectConfig
from ..scheduler_client import SchedulerClient
from .base import BaseService, ClientFactory
from .flow_validation import find_unreachable_phases, validate_conditional_flow

logger = logging.getLogger(__name__)

FLOW_COMPONENT_ID = "keboola.flow"
LEGACY_FLOW_COMPONENT_ID = "keboola.orchestrator"
SCHEDULER_COMPONENT_ID = "keboola.scheduler"

AiClientFactory = Callable[[str, str], AiServiceClient]
SchedulerClientFactory = Callable[[str, str], SchedulerClient]


@dataclass(frozen=True)
class FlowSchemaFetch:
    """Outcome of fetching the live keboola.flow JSON Schema from the stack.

    ``schema`` holds the JSON Schema dict on success and is ``None`` when it
    could not be obtained; ``reason`` explains the failure (``None`` on
    success). A ``None`` schema must NOT block a write -- callers degrade to
    semantic-only validation and surface ``reason`` as a warning.
    """

    schema: dict[str, Any] | None
    reason: str | None


def default_ai_client_factory(stack_url: str, token: str) -> AiServiceClient:
    """Default factory: build an ``AiServiceClient`` for the given project."""
    return AiServiceClient(stack_url=stack_url, token=token)


def default_scheduler_client_factory(stack_url: str, token: str) -> SchedulerClient:
    """Default factory: build a ``SchedulerClient`` for the given project."""
    return SchedulerClient(stack_url=stack_url, token=token)


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


def _schedules_targeting_flow(
    all_sched: list[dict[str, Any]], config_id: str
) -> list[dict[str, Any]]:
    """Filter keboola.scheduler configs whose target is this keboola.flow config."""
    matching: list[dict[str, Any]] = []
    for sched in all_sched:
        body = _parse_configuration(sched.get("configuration"))
        target = body.get("target") or {}
        if target.get("componentId") == FLOW_COMPONENT_ID and str(
            target.get("configurationId", "")
        ) == str(config_id):
            matching.append(sched)
    return matching


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
        if exc.error_code == ErrorCode.NOT_FOUND:
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
    """Business logic for conditional flow (keboola.flow) CRUD.

    Schedules are stored as keboola.scheduler component configs AND
    registered with the Scheduler Service via ``scheduler_client_factory``
    -- writing the Storage config alone leaves the cron trigger dormant.

    The structural conditional-flow JSON Schema is fetched at runtime from the
    stack's component registry (AI Service ``configurationSchema`` for
    ``keboola.flow``) via ``ai_client_factory`` -- it is never bundled.
    """

    def __init__(
        self,
        config_store: ConfigStore,
        client_factory: ClientFactory | None = None,
        ai_client_factory: AiClientFactory | None = None,
        scheduler_client_factory: SchedulerClientFactory | None = None,
    ) -> None:
        super().__init__(config_store, client_factory)
        self._ai_client_factory = ai_client_factory or default_ai_client_factory
        self._scheduler_client_factory = (
            scheduler_client_factory or default_scheduler_client_factory
        )

    # ── schema fetch ─────────────────────────────────────────────────

    def _fetch_flow_schema(self, project: ProjectConfig) -> FlowSchemaFetch:
        """Fetch the live keboola.flow JSON Schema from the AI Service.

        Returns a ``FlowSchemaFetch`` with ``schema`` set on success, or
        ``schema=None`` + a ``reason`` when the schema cannot be obtained
        (network error, KeboolaApiError, malformed or empty schema). A ``None``
        schema must NOT block a write -- the caller degrades to semantic-only
        validation and surfaces ``reason`` as a warning.
        """
        ai_client = self._ai_client_factory(project.stack_url, project.token)
        try:
            raw = ai_client.get_component_detail(FLOW_COMPONENT_ID)
        except KeboolaApiError as exc:
            return FlowSchemaFetch(schema=None, reason=exc.message)
        except Exception as exc:
            # Intentionally broad: ANY schema-fetch failure must degrade to
            # semantic-only validation, never block the write. Narrowing to
            # OSError-style transport errors would miss httpx exceptions
            # (httpx.HTTPError does not subclass OSError) and re-raise them.
            return FlowSchemaFetch(schema=None, reason=str(exc))
        finally:
            ai_client.close()

        try:
            detail = ComponentDetail(**raw)
        except (TypeError, ValueError) as exc:
            return FlowSchemaFetch(
                schema=None, reason=f"component detail could not be parsed ({exc})"
            )

        schema = detail.configuration_schema
        if not schema:
            return FlowSchemaFetch(
                schema=None, reason="AI Service returned no configurationSchema for keboola.flow"
            )
        return FlowSchemaFetch(schema=schema, reason=None)

    def fetch_flow_schema(self, alias: str) -> FlowSchemaFetch:
        """Public schema fetch for a project alias (used by ``flow validate
        --project`` and ``flow schema --full --project``).

        Returns a ``FlowSchemaFetch`` (``schema`` set on success, or
        ``schema=None`` + ``reason`` on any failure) -- the caller decides how
        to surface the reason.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        return self._fetch_flow_schema(project)

    # ── list ────────────────────────────────────────────────────────

    def list_flows(
        self,
        aliases: list[str] | None = None,
        branch_id: int | None = None,
        with_schedules: bool = False,
    ) -> dict[str, Any]:
        """List conditional flows (keboola.flow) across projects.

        Only ``keboola.flow`` configs are returned. Legacy
        ``keboola.orchestrator`` configs are counted (not listed) and surfaced
        as ``legacy_orchestrator_count`` so the CLI can warn users why a flow
        "disappeared" (orchestrator support was dropped in 0.57.0).

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
                - "flows": list of keboola.flow dicts (project_alias,
                  component_id, config_id, name, description, is_disabled, and
                  ``schedules`` when ``with_schedules`` is True)
                - "errors": list of error dicts
                - "legacy_orchestrator_count": total legacy orchestrator
                  configs found across the queried projects (not listed)
        """
        projects = self.resolve_projects(aliases)

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            client = self._client_factory(project.stack_url, project.token)
            effective_branch = branch_id or project.active_branch_id
            try:
                flows: list[dict[str, Any]] = []
                try:
                    configs = client.list_component_configs(
                        FLOW_COMPONENT_ID, branch_id=effective_branch
                    )
                except KeboolaApiError as exc:
                    if exc.error_code == ErrorCode.NOT_FOUND:
                        configs = []
                    else:
                        raise
                for cfg in configs:
                    flow_row: dict[str, Any] = {
                        "project_alias": alias,
                        "component_id": FLOW_COMPONENT_ID,
                        "config_id": str(cfg.get("id", "")),
                        "name": cfg.get("name", ""),
                        "description": cfg.get("description", ""),
                        "is_disabled": cfg.get("isDisabled", False),
                    }
                    if with_schedules:
                        flow_row["schedules"] = []
                    flows.append(flow_row)

                # Count (do not list) legacy orchestrator configs so the CLI can warn.
                try:
                    legacy = client.list_component_configs(
                        LEGACY_FLOW_COMPONENT_ID, branch_id=effective_branch
                    )
                    legacy_count = len(legacy)
                except KeboolaApiError as exc:
                    if exc.error_code == ErrorCode.NOT_FOUND:
                        legacy_count = 0
                    else:
                        raise

                # One extra list call per project, then a map-join in memory.
                if with_schedules and flows:
                    schedules_by_parent = _collect_schedules_by_parent(client, effective_branch)
                    for flow_row in flows:
                        key = (flow_row["component_id"], flow_row["config_id"])
                        flow_row["schedules"] = schedules_by_parent.get(key, [])

                return (alias, flows, legacy_count)
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
        legacy_total = 0
        for _, flows, legacy_count in successes:
            all_flows.extend(flows)
            legacy_total += legacy_count
        all_flows.sort(key=lambda f: (f["project_alias"], f["name"].lower()))
        errors.sort(key=lambda e: e.get("project_alias", ""))

        return {
            "flows": all_flows,
            "errors": errors,
            "legacy_orchestrator_count": legacy_total,
        }

    # ── detail ──────────────────────────────────────────────────────

    def get_flow_detail(
        self,
        alias: str,
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
            detail = client.get_config_detail(
                FLOW_COMPONENT_ID, config_id, branch_id=effective_branch
            )
        finally:
            client.close()

        body = _parse_configuration(detail.get("configuration"))
        phases = body.get("phases", [])
        tasks = body.get("tasks", [])

        detail["project_alias"] = alias
        detail["component_id"] = FLOW_COMPONENT_ID
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
        name: str,
        description: str = "",
        phases: list[dict[str, Any]] | None = None,
        tasks: list[dict[str, Any]] | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a new conditional-flow (keboola.flow) configuration.

        Args:
            alias: Project alias.
            name: Flow name.
            description: Optional description.
            phases: Phase definitions (validated against the CF schema).
            tasks: Task definitions (validated against the CF schema).
            branch_id: Dev branch override.

        Raises:
            KeboolaApiError: On API failure or definition validation error
                (error_code='INVALID_FLOW_DEFINITION').
        """
        phases = phases or []
        tasks = tasks or []

        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch = branch_id or project.active_branch_id

        fetch = self._fetch_flow_schema(project)
        warnings: list[str] = []
        if fetch.schema is None:
            warnings.append(f"structural schema validation skipped: {fetch.reason}")

        definition_errors = validate_conditional_flow(phases, tasks, fetch.schema)
        if definition_errors:
            raise KeboolaApiError(
                message="Flow definition is invalid: " + "; ".join(definition_errors),
                status_code=400,
                error_code=ErrorCode.INVALID_FLOW_DEFINITION,
                retryable=False,
            )
        warnings.extend(
            f"Phase '{pid}' is unreachable from the entry phase"
            for pid in find_unreachable_phases(phases)
        )

        configuration: dict[str, Any] = {"phases": phases, "tasks": tasks}

        client = self._client_factory(project.stack_url, project.token)
        try:
            result = client.create_config(
                component_id=FLOW_COMPONENT_ID,
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
        result["warnings"] = warnings
        return result

    # ── update ──────────────────────────────────────────────────────

    def update_flow(
        self,
        alias: str,
        config_id: str,
        name: str | None = None,
        description: str | None = None,
        phases: list[dict[str, Any]] | None = None,
        tasks: list[dict[str, Any]] | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Update an existing conditional-flow (keboola.flow) configuration.

        When phases and/or tasks are provided, validation runs on the merged
        body (the unspecified side is fetched from the current config) so a
        half-config is never validated.

        Raises:
            KeboolaApiError: On API failure or definition validation error
                (error_code='INVALID_FLOW_DEFINITION').
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch = branch_id or project.active_branch_id

        warnings: list[str] = []
        client = self._client_factory(project.stack_url, project.token)
        try:
            configuration: dict[str, Any] | None = None
            if phases is not None or tasks is not None:
                current = client.get_config_detail(
                    FLOW_COMPONENT_ID, config_id, branch_id=effective_branch
                )
                current_body = _parse_configuration(current.get("configuration"))
                merged_phases = phases if phases is not None else current_body.get("phases", [])
                merged_tasks = tasks if tasks is not None else current_body.get("tasks", [])

                fetch = self._fetch_flow_schema(project)
                if fetch.schema is None:
                    warnings.append(f"structural schema validation skipped: {fetch.reason}")

                definition_errors = validate_conditional_flow(
                    merged_phases, merged_tasks, fetch.schema
                )
                if definition_errors:
                    raise KeboolaApiError(
                        message="Flow definition is invalid: " + "; ".join(definition_errors),
                        status_code=400,
                        error_code=ErrorCode.INVALID_FLOW_DEFINITION,
                        retryable=False,
                    )

                warnings.extend(
                    f"Phase '{pid}' is unreachable from the entry phase"
                    for pid in find_unreachable_phases(merged_phases)
                )

                configuration = dict(current_body)
                configuration["phases"] = merged_phases
                configuration["tasks"] = merged_tasks

            result = client.update_config(
                component_id=FLOW_COMPONENT_ID,
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
        result["warnings"] = warnings
        return result

    # ── delete ──────────────────────────────────────────────────────

    def delete_flow(
        self,
        alias: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Delete a conditional-flow (keboola.flow) configuration.

        Does NOT automatically remove associated keboola.scheduler configs.
        Use remove_flow_schedule() first if needed.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            client.delete_config(
                component_id=FLOW_COMPONENT_ID,
                config_id=config_id,
                branch_id=effective_branch,
            )
        finally:
            client.close()

        return {
            "status": "deleted",
            "project_alias": alias,
            "component_id": FLOW_COMPONENT_ID,
            "config_id": config_id,
            "branch_id": effective_branch,
        }

    # ── schedule ────────────────────────────────────────────────────

    def list_flow_schedules(
        self,
        alias: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """List keboola.scheduler configs that target this flow.

        Fetches all keboola.scheduler configs and filters by
        target.componentId == keboola.flow + target.configurationId.
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
                if exc.error_code == ErrorCode.NOT_FOUND:
                    all_sched = []
                else:
                    raise
        finally:
            client.close()

        schedules: list[dict[str, Any]] = []
        for sched in _schedules_targeting_flow(all_sched, config_id):
            sched_info = _parse_configuration(sched.get("configuration")).get("schedule") or {}
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
            "component_id": FLOW_COMPONENT_ID,
            "config_id": config_id,
            "schedules": schedules,
        }

    def set_flow_schedule(
        self,
        alias: str,
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
        ``target`` points at the keboola.flow component + config, then
        registered with the Scheduler Service so the cron trigger actually
        fires. An activation failure (e.g. token without the activation
        privilege) is non-fatal: the config stays written and the failure is
        reported via ``warnings`` + ``activated: False`` in the result.

        Args:
            alias: Project alias.
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
                        FLOW_COMPONENT_ID, config_id, branch_id=effective_branch
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
                    "componentId": FLOW_COMPONENT_ID,
                    "configurationId": config_id,
                },
            }

            # Upsert: update existing schedule if one exists
            try:
                existing = client.list_component_configs(
                    SCHEDULER_COMPONENT_ID, branch_id=effective_branch
                )
            except KeboolaApiError as exc:
                if exc.error_code == ErrorCode.NOT_FOUND:
                    existing = []
                else:
                    raise

            matching = _schedules_targeting_flow(existing, config_id)
            existing_id = str(matching[0].get("id", "")) if matching else None

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

        schedule_id = str(result.get("id", existing_id or ""))

        # The service re-reads the config on activation, so this also
        # deregisters a disabled schedule.
        warnings: list[str] = []
        activated = False
        with self._scheduler_client_factory(project.stack_url, project.token) as scheduler:
            try:
                scheduler.activate_schedule(schedule_id)
                activated = True
            except KeboolaApiError as exc:
                logger.warning("Scheduler Service activation failed: %s", exc.message)
                warnings.append(
                    f"Schedule config {schedule_id} was {status} but could not be "
                    f"activated on the Scheduler Service: {exc.message}. The cron "
                    "trigger will NOT fire until activation succeeds -- re-run this "
                    "command with a token that can manage schedules."
                )

        return {
            "status": status,
            "project_alias": alias,
            "schedule_id": schedule_id,
            "schedule_name": schedule_name,
            "component_id": FLOW_COMPONENT_ID,
            "config_id": config_id,
            "cron_tab": cron_tab,
            "timezone": timezone,
            "state": "enabled" if enabled else "disabled",
            "activated": activated,
            "branch_id": effective_branch,
            "warnings": warnings,
        }

    def remove_flow_schedule(
        self,
        alias: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Delete all keboola.scheduler configs that target this flow.

        Each schedule is first deregistered from the Scheduler Service (so
        the cron trigger stops firing), then its Storage config is deleted.
        A missing service-side registration is ignored; other deregistration
        failures are reported via ``warnings`` and do not block the Storage
        config deletion.

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
                if exc.error_code == ErrorCode.NOT_FOUND:
                    all_sched = []
                else:
                    raise

            deleted: list[str] = []
            errors: list[str] = []
            warnings: list[str] = []
            matching = _schedules_targeting_flow(all_sched, config_id)
            if matching:
                with self._scheduler_client_factory(project.stack_url, project.token) as scheduler:
                    for sched in matching:
                        sched_id = str(sched.get("id", ""))
                        try:
                            scheduler.remove_schedule(sched_id)
                        except KeboolaApiError as exc:
                            if exc.error_code != ErrorCode.NOT_FOUND:
                                logger.warning(
                                    "Scheduler Service deregistration failed: %s", exc.message
                                )
                                warnings.append(
                                    f"Schedule {sched_id} could not be deregistered from "
                                    f"the Scheduler Service: {exc.message}"
                                )
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
            "component_id": FLOW_COMPONENT_ID,
            "config_id": config_id,
            "deleted_schedule_ids": deleted,
            "deleted_count": len(deleted),
            "branch_id": effective_branch,
            "warnings": warnings,
        }
