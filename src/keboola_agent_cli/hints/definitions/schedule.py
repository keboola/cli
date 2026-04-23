"""Hint definitions for schedule commands + the flow list --with-schedules variant."""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

HintRegistry.register(
    CommandHint(
        cli_command="schedule.list",
        description="List all keboola.scheduler configurations across one or many projects",
        steps=[
            HintStep(
                comment="Fetch every component with its configs in a single round-trip",
                client=ClientCall(
                    method="list_components_with_configs",
                    args={
                        "branch_id": "{branch}",
                    },
                    result_var="components",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="ScheduleService",
                    service_module="schedule_service",
                    method="list_schedules",
                    args={
                        "aliases": "{project}",
                        "enabled_only": "{enabled_only}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Filter the response to entries where component id == 'keboola.scheduler'.",
            "configuration.target.componentId / configurationId hold the parent reference -- "
            "join with the same payload to look up parent names without N+1 API calls.",
            "configuration.schedule.state is the string 'enabled' or 'disabled'.",
            "Service returns {'schedules': [...], 'errors': [...]}; each schedule row has "
            "project_alias, schedule_id, schedule_name, parent_component_id, parent_config_id, "
            "parent_name, cron, timezone, enabled.",
        ],
    )
)


HintRegistry.register(
    CommandHint(
        cli_command="schedule.detail",
        description="Fetch a single keboola.scheduler config plus its parent config name",
        steps=[
            HintStep(
                comment="Fetch the scheduler config itself",
                client=ClientCall(
                    method="get_config_detail",
                    args={
                        "component_id": '"keboola.scheduler"',
                        "config_id": "{schedule_id}",
                        "branch_id": "{branch}",
                    },
                    result_var="schedule",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="ScheduleService",
                    service_module="schedule_service",
                    method="get_schedule_detail",
                    args={
                        "alias": "{project}",
                        "schedule_id": "{schedule_id}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Read configuration.target.componentId / configurationId from the response to "
            "fetch the parent config's display name with a second get_config_detail call.",
            "Orphaned schedules (parent deleted) keep the schedule detail available; the "
            "service surfaces parent_name='' rather than raising.",
        ],
    )
)


HintRegistry.register(
    CommandHint(
        cli_command="schedule.find",
        description="Audit schedules by cron-window and/or not-run-since filters",
        steps=[
            HintStep(
                comment="Collect every component+config payload in one call",
                client=ClientCall(
                    method="list_components_with_configs",
                    args={
                        "branch_id": "{branch}",
                    },
                    result_var="components",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="ScheduleService",
                    service_module="schedule_service",
                    method="find_schedules",
                    args={
                        "aliases": "{project}",
                        "cron_window": "{cron_window}",
                        "not_run_since_days": "{not_run_since}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
            HintStep(
                comment=(
                    "For each matching schedule, find the most recent job "
                    "for the parent component+config (not_run_since filter)"
                ),
                client=ClientCall(
                    method="list_jobs",
                    args={
                        "component_id": "target.componentId",
                        "config_id": "target.configurationId",
                        "limit": "1",
                    },
                    result_var="latest_jobs",
                    result_hint="list[dict]",
                ),
            ),
        ],
        notes=[
            "cron-window matcher is an hour-field approximation: it parses only the "
            "cron 'hour' field. Minute-level precision is best-effort -- see "
            "references/gotchas.md for the full semantics.",
            "not-run-since compares against the latest job's startTime (fallback createdTime). "
            "A parent with no jobs at all counts as stale.",
            "Filters combine with AND. Passing neither is equivalent to schedule list + "
            "last_run_at column.",
        ],
    )
)


# Enrichment flag on flow list -- reuses the flow.list hint pattern but documents
# the extra round-trip that populates schedules[] per flow.
HintRegistry.register(
    CommandHint(
        cli_command="flow.list-with-schedules",
        description="List flows enriched with the cron schedules that target them",
        steps=[
            HintStep(
                comment="Existing flow list: fetch orchestrator + flow configs",
                client=ClientCall(
                    method="list_component_configs",
                    args={
                        "component_id": '"keboola.orchestrator"',
                        "branch_id": "{branch}",
                    },
                    result_var="orchestrator_configs",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="FlowService",
                    service_module="flow_service",
                    method="list_flows",
                    args={
                        "aliases": "{project}",
                        "with_schedules": "True",
                        "branch_id": "{branch}",
                    },
                ),
            ),
            HintStep(
                comment=(
                    "One additional list_component_configs per project fetches every "
                    "keboola.scheduler config; join by target.configurationId in memory"
                ),
                client=ClientCall(
                    method="list_component_configs",
                    args={
                        "component_id": '"keboola.scheduler"',
                        "branch_id": "{branch}",
                    },
                    result_var="scheduler_configs",
                    result_hint="list[dict]",
                ),
            ),
        ],
        notes=[
            "The --with-schedules flag adds one keboola.scheduler list call per project "
            "(NOT per flow) -- the join happens in memory.",
            "Each flow row gains a 'schedules' key with a list of "
            "{schedule_id, cron, timezone, enabled} entries. Flows without a schedule "
            "keep schedules=[].",
        ],
    )
)
