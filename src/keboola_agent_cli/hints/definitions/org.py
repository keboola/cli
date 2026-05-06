"""Hint definitions for org commands."""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

# ── org setup ──────────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="org.setup",
        description="Onboard organization projects into kbagent",
        steps=[
            HintStep(
                comment="List organization projects via Manage API",
                client=ClientCall(
                    method="list_organization_projects",
                    args={"org_id": "{org_id}"},
                    client_type="manage",
                    result_var="projects",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="OrgService",
                    service_module="org_service",
                    method="setup_organization",
                    args={
                        "stack_url": "{url}",
                        "org_id": "{org_id}",
                        "dry_run": "{dry_run}",
                    },
                ),
            ),
        ],
        notes=[
            "Uses the Manage API token (not Storage token). Default-deny on env "
            "since kbagent v0.28.0: pass top-level `--allow-env-manage-token` "
            "to read KBC_MANAGE_API_TOKEN, or run interactively (TTY hidden prompt).",
            "Service layer creates per-project tokens and registers them in CLI config.",
        ],
    )
)
