"""Hint definitions for component commands (list, detail)."""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

# ── component list ─────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="component.list",
        description="List available components",
        steps=[
            HintStep(
                comment="List components",
                client=ClientCall(
                    method="list_components",
                    args={"component_type": "{type}"},
                    result_var="components",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="ComponentService",
                    service_module="component_service",
                    method="list_components",
                    args={
                        "aliases": "{project}",
                        "component_type": "{type}",
                        "query": "{query}",
                    },
                ),
            ),
        ],
        notes=["With --query, service uses the AI search API for smart matching."],
    )
)

# ── component detail ───────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="component.detail",
        description="Show component detail with schema summary",
        steps=[
            HintStep(
                comment="Get component detail via AI Service API",
                client=ClientCall(
                    method="get_component_detail",
                    args={"component_id": "{component_id}"},
                    result_var="detail",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="ComponentService",
                    service_module="component_service",
                    method="get_component_detail",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                    },
                ),
            ),
        ],
        notes=[
            "Uses the AI Service API (ai.{stack}) for enriched schema information.",
            "Client layer: use AiServiceClient, not KeboolaClient.",
        ],
    )
)
