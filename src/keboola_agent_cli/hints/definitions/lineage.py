"""Hint definitions for lineage commands."""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

# ── lineage show ───────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="lineage.show",
        description="Show cross-project data lineage via bucket sharing",
        steps=[
            HintStep(
                comment="List buckets with linked-bucket metadata",
                client=ClientCall(
                    method="list_buckets",
                    args={"include": '"linkedBuckets"'},
                    result_var="buckets",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="LineageService",
                    service_module="lineage_service",
                    method="get_lineage",
                    args={"aliases": "{project}"},
                ),
            ),
        ],
        notes=[
            "Client layer returns raw buckets — analyze sharing metadata yourself.",
            "Service layer queries all projects in parallel and builds "
            "data flow edges with deduplication.",
        ],
    )
)
