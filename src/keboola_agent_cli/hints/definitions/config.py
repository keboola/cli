"""Hint definitions for config commands (list, detail, search)."""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

# ── config list ────────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="config.list",
        description="List configurations from connected projects",
        steps=[
            HintStep(
                comment="List all components with their configurations",
                client=ClientCall(
                    method="list_components",
                    args={
                        "component_type": "{component_type}",
                        "branch_id": "{branch}",
                    },
                    result_var="components",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="ConfigService",
                    service_module="config_service",
                    method="list_configs",
                    args={
                        "aliases": "{project}",
                        "component_type": "{component_type}",
                        "component_id": "{component_id}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Each component in the response has a 'configurations' list.",
            "Service layer returns {'configs': [...], 'errors': [...]} with flattened results.",
        ],
    )
)

# ── config detail ──────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="config.detail",
        description="Show detailed information about a specific configuration",
        steps=[
            HintStep(
                comment="Get configuration detail",
                client=ClientCall(
                    method="get_config_detail",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "branch_id": "{branch}",
                    },
                    result_var="detail",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="ConfigService",
                    service_module="config_service",
                    method="get_config_detail",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
    )
)

# ── config search ──────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="config.search",
        description="Search through configuration bodies across projects",
        steps=[
            HintStep(
                comment="Search configurations for a pattern",
                client=ClientCall(
                    method="list_components",
                    args={
                        "component_type": "{component_type}",
                        "branch_id": "{branch}",
                    },
                    result_var="components",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="ConfigService",
                    service_module="config_service",
                    method="search_configs",
                    args={
                        "query": "{query}",
                        "aliases": "{project}",
                        "component_type": "{component_type}",
                        "component_id": "{component_id}",
                        "ignore_case": "{ignore_case}",
                        "use_regex": "{regex}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Client layer returns raw components — you need to search through "
            "configuration JSON bodies yourself.",
            "Service layer does the full-text search and returns "
            "{'matches': [...], 'errors': [...], 'stats': {...}}.",
        ],
    )
)
