"""Hint definitions for flow commands."""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

HintRegistry.register(
    CommandHint(
        cli_command="flow.list",
        description="List all flows (keboola.orchestrator + keboola.flow) across projects",
        steps=[
            HintStep(
                comment="Fetch configs for both flow component IDs",
                client=ClientCall(
                    method="list_component_configs",
                    args={
                        "component_id": "keboola.orchestrator",
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
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Client layer: call list_component_configs for both 'keboola.orchestrator' and "
            "'keboola.flow' and merge the results.",
            "Service layer returns {'flows': [...], 'errors': [...]}.  "
            "Each flow dict has project_alias, component_id, config_id, name, description, is_disabled.",
        ],
    )
)

HintRegistry.register(
    CommandHint(
        cli_command="flow.detail",
        description="Show detailed flow information including phases and tasks",
        steps=[
            HintStep(
                comment="Fetch full flow configuration detail",
                client=ClientCall(
                    method="get_config_detail",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{flow_id}",
                        "branch_id": "{branch}",
                    },
                    result_var="detail",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="FlowService",
                    service_module="flow_service",
                    method="get_flow_detail",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{flow_id}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "configuration.phases and configuration.tasks hold the flow DAG.",
            "Service response adds phase_count, task_count top-level keys.",
        ],
    )
)

HintRegistry.register(
    CommandHint(
        cli_command="flow.new",
        description="Create a new flow configuration",
        steps=[
            HintStep(
                comment="Create a flow config (phases + tasks in configuration body)",
                client=ClientCall(
                    method="create_config",
                    args={
                        "component_id": "{component_id}",
                        "name": "{name}",
                        "configuration": '{"phases": [...], "tasks": [...]}',
                        "description": "{description}",
                        "branch_id": "{branch}",
                    },
                    result_var="result",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="FlowService",
                    service_module="flow_service",
                    method="create_flow",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "name": "{name}",
                        "description": "{description}",
                        "phases": "list[dict]",
                        "tasks": "list[dict]",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "component_id defaults to 'keboola.flow' (newer format) or use 'keboola.orchestrator'.",
            "DAG validation runs before the API call; INVALID_FLOW_DAG on cycle or bad ref.",
            "Run 'kbagent flow schema' to see the expected YAML format.",
        ],
    )
)

HintRegistry.register(
    CommandHint(
        cli_command="flow.schedule",
        description="Bind a cron schedule to a flow (creates a keboola.scheduler config)",
        steps=[
            HintStep(
                comment="Create a keboola.scheduler config targeting the flow",
                client=ClientCall(
                    method="create_config",
                    args={
                        "component_id": "keboola.scheduler",
                        "name": "{schedule_name}",
                        "configuration": (
                            '{"schedule": {"cronTab": "{cron}", "timezone": "{timezone}", '
                            '"state": "enabled"}, "target": {"mode": "run", '
                            '"componentId": "{component_id}", "configurationId": "{flow_id}"}}'
                        ),
                        "branch_id": "{branch}",
                    },
                    result_var="schedule",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="FlowService",
                    service_module="flow_service",
                    method="set_flow_schedule",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{flow_id}",
                        "cron_tab": "{cron}",
                        "timezone": "{timezone}",
                        "enabled": "{enabled}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Schedules are stored as keboola.scheduler component configs -- no separate HTTP client.",
            "configurationId in the target must be the flow's config ID (string).",
            "Upsert: if a schedule already exists for this flow it is updated; otherwise a new one is "
            "created. schedule-remove deletes all matching schedules.",
        ],
    )
)
