"""Hint definitions for job commands (list, detail, run)."""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

# ── job list ───────────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="job.list",
        description="List recent jobs from the Queue API",
        steps=[
            HintStep(
                comment="List jobs",
                client=ClientCall(
                    method="list_jobs",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "status": "{status}",
                        "limit": "{limit}",
                    },
                    result_var="jobs",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="JobService",
                    service_module="job_service",
                    method="list_jobs",
                    args={
                        "aliases": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "status": "{status}",
                        "limit": "{limit}",
                    },
                ),
            ),
        ],
        notes=["Uses the Queue API (queue.keboola.com), not Storage API."],
    )
)

# ── job detail ─────────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="job.detail",
        description="Show detailed job information",
        steps=[
            HintStep(
                comment="Get job detail from Queue API",
                client=ClientCall(
                    method="get_job_detail",
                    args={"job_id": "{job_id}"},
                    result_var="job",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="JobService",
                    service_module="job_service",
                    method="get_job_detail",
                    args={"alias": "{project}", "job_id": "{job_id}"},
                ),
            ),
        ],
        notes=["Uses the Queue API (queue.keboola.com), not Storage API."],
    )
)

# ── job run ────────────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="job.run",
        description="Run a component configuration as a job",
        steps=[
            HintStep(
                comment="Create and submit job to Queue API",
                client=ClientCall(
                    method="create_job",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "config_row_ids": "{row_id}",
                    },
                    result_var="job",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="JobService",
                    service_module="job_service",
                    method="run_job",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "config_row_ids": "{row_id}",
                        "wait": "{wait}",
                        "timeout": "{timeout}",
                    },
                ),
            ),
            HintStep(
                comment="Poll until job completes (when --wait is used)",
                client=ClientCall(
                    method="get_job_detail",
                    args={"job_id": 'str(job["id"])'},
                    result_var="job",
                ),
                kind="poll_loop",
                poll_interval=5.0,
                poll_condition='not job.get("isFinished")',
            ),
        ],
        notes=[
            "Uses the Queue API (queue.keboola.com), not Storage API.",
            "Without --wait, returns immediately after job creation.",
            "Service layer handles both create + optional poll in one call.",
        ],
    )
)
