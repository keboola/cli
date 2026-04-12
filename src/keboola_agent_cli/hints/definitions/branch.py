"""Hint definitions for branch commands (list, create, delete)."""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

# ── branch list ────────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="branch.list",
        description="List development branches",
        steps=[
            HintStep(
                comment="List dev branches",
                client=ClientCall(
                    method="list_dev_branches",
                    args={},
                    result_var="branches",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="BranchService",
                    service_module="branch_service",
                    method="list_branches",
                    args={"aliases": "{project}"},
                ),
            ),
        ],
    )
)

# ── branch create ──────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="branch.create",
        description="Create a new development branch",
        steps=[
            HintStep(
                comment="Create dev branch",
                client=ClientCall(
                    method="create_dev_branch",
                    args={"name": "{name}", "description": "{description}"},
                    result_var="branch",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="BranchService",
                    service_module="branch_service",
                    method="create_branch",
                    args={
                        "alias": "{project}",
                        "name": "{name}",
                        "description": "{description}",
                    },
                ),
            ),
        ],
        notes=["Service layer also auto-activates the branch in CLI config."],
    )
)

# ── branch delete ──────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="branch.delete",
        description="Delete a development branch",
        steps=[
            HintStep(
                comment="Delete dev branch",
                client=ClientCall(
                    method="delete_dev_branch",
                    args={"branch_id": "{branch}"},
                    result_var="result",
                ),
                service=ServiceCall(
                    service_class="BranchService",
                    service_module="branch_service",
                    method="delete_branch",
                    args={"alias": "{project}", "branch_id": "{branch}"},
                ),
            ),
        ],
        notes=["Service layer auto-resets active branch if the deleted branch was active."],
    )
)
