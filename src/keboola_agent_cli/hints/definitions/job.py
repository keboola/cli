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
                comment=(
                    "Resolve linked variables values row (skip when "
                    "--no-variables; override via --variable-values-id)"
                ),
                client=ClientCall(
                    method="get_config_detail",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                    },
                    result_var="detail",
                    result_hint="dict",
                ),
            ),
            HintStep(
                comment="Look up first row of linked variables config if values_id absent",
                client=ClientCall(
                    method="list_config_rows",
                    args={
                        "component_id": '"keboola.variables"',
                        "config_id": 'detail["configuration"]["runtime"]["variables_id"]',
                    },
                    result_var="var_rows",
                    result_hint="list",
                ),
            ),
            HintStep(
                comment="Create and submit job to Queue API",
                client=ClientCall(
                    method="create_job",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "config_row_ids": "{row_id}",
                        "variable_values_id": 'var_rows[0]["id"] if var_rows else None',
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
                        "variable_values_id": "{variable_values_id}",
                        "no_variables": "{no_variables}",
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
            "Service layer handles both resolve + create + optional poll in one call.",
            (
                "Service auto-resolves variableValuesId from "
                "configuration.runtime.variables_id; the client hint shows "
                "the underlying two-request pattern."
            ),
            "Pass --no-variables to skip resolution entirely.",
        ],
    )
)

# ── job terminate ──────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="job.terminate",
        description="Request termination of one or more Queue API jobs",
        steps=[
            HintStep(
                comment="Kill a single job (async: sets desiredStatus=terminating)",
                client=ClientCall(
                    method="kill_job",
                    args={"job_id": "{job_id}"},
                    result_var="job",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="JobService",
                    service_module="job_service",
                    method="terminate_jobs",
                    args={
                        "alias": "{project}",
                        "job_ids": "{job_id}",
                        "dry_run": "{dry_run}",
                    },
                ),
            ),
        ],
        notes=[
            "Uses the Queue API (queue.keboola.com) - POST /jobs/{id}/kill.",
            "Killable states: created, waiting, processing. Others return HTTP 400.",
            "Transition is async - poll get_job_detail for isFinished=True.",
            "Service partitions results into killed / already_finished / not_found / failed.",
            "For bulk mode, resolve job IDs first via list_jobs, then call terminate_jobs.",
        ],
    )
)
