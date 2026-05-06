"""Hint definitions for project-level commands (project description, info)."""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

# ── project description-get ───────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="project.description-get",
        description="Read the Keboola dashboard project description",
        steps=[
            HintStep(
                comment="Read KBC.projectDescription on the default branch",
                client=ClientCall(
                    method="get_branch_metadata_value",
                    args={
                        "key": '"KBC.projectDescription"',
                        "branch_id": '"default"',
                    },
                    result_var="description",
                    result_hint="str | None",
                ),
                service=ServiceCall(
                    service_class="BranchService",
                    service_module="branch_service",
                    method="get_project_description",
                    args={"alias": "{project}"},
                ),
            ),
        ],
        notes=[
            "The dashboard reads project description from branch metadata, "
            "not from the Manage API or the branch description field.",
        ],
    )
)

# ── project description-set ───────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="project.description-set",
        description="Set the Keboola dashboard project description (markdown)",
        steps=[
            HintStep(
                comment="Write KBC.projectDescription on the default branch",
                client=ClientCall(
                    method="set_branch_metadata",
                    args={
                        "entries": '[("KBC.projectDescription", {description})]',
                        "branch_id": '"default"',
                    },
                    result_var="result",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="BranchService",
                    service_module="branch_service",
                    method="set_project_description",
                    args={
                        "alias": "{project}",
                        "description": "{description}",
                    },
                ),
            ),
        ],
        notes=[
            "Writes to the default branch metadata - always the main branch, "
            "regardless of any active dev branch.",
        ],
    )
)

# ── project info ──────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="project.info",
        description="Return full project metadata from /v2/storage/tokens/verify",
        steps=[
            HintStep(
                comment=(
                    "Fetch the full token-verify response; includes owner.features, "
                    "owner.limits, owner.metrics, owner.defaultBackend, token expiry."
                ),
                client=ClientCall(
                    method="get_project_info",
                    args={},
                    result_var="info",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="ProjectService",
                    service_module="project_service",
                    method="get_info",
                    args={"alias": "{project}"},
                ),
            ),
        ],
        notes=[
            "get_project_info() returns the raw API response dict; "
            "the service layer formats it into a stable structure.",
            "Features are in owner.features (list of strings). "
            "Limits and metrics are in owner.limits / owner.metrics (dicts).",
        ],
    )
)
