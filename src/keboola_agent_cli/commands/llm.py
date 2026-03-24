"""LLM commands - AI-optimized project export via kbc CLI.

Thin CLI layer: parses arguments, calls KbcService, formats output.
No business logic belongs here.
"""

import typer

from ..errors import ConfigError
from ._helpers import get_formatter, get_service

llm_app = typer.Typer(
    help="[deprecated] AI-optimized project export -- use 'sync pull' instead",
    deprecated=True,
)


@llm_app.command("export")
def llm_export(
    ctx: typer.Context,
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project alias to export (required if multiple projects configured)",
    ),
    with_samples: bool = typer.Option(
        False,
        "--with-samples",
        help="Include data samples (CSV) from tables",
    ),
    sample_limit: int | None = typer.Option(
        None,
        "--sample-limit",
        help="Max rows per table sample (requires --with-samples)",
    ),
    max_samples: int | None = typer.Option(
        None,
        "--max-samples",
        help="Max number of tables to sample (requires --with-samples)",
    ),
) -> None:
    """Export project to Twin Format for AI consumption.

    Creates an AI-optimized directory of JSON files containing table schemas,
    transformation SQL code, internal lineage graph, job statistics, and
    component configurations. Requires the kbc CLI (brew install keboola-cli).
    """
    formatter = get_formatter(ctx)

    # Validate sample options require --with-samples
    if (sample_limit is not None or max_samples is not None) and not with_samples:
        formatter.error(
            message="--sample-limit and --max-samples require --with-samples",
            error_code="USAGE_ERROR",
        )
        raise typer.Exit(code=2)

    kbc_service = get_service(ctx, "kbc_service")

    try:
        exit_code = kbc_service.run_llm_export(
            alias=project,
            with_samples=with_samples,
            sample_limit=sample_limit,
            max_samples=max_samples,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None

    if exit_code != 0:
        formatter.error(
            message=f"kbc llm export failed with exit code {exit_code}",
            error_code="KBC_ERROR",
        )
        raise typer.Exit(code=1) from None

    # Success output
    if formatter.json_mode:
        projects = kbc_service.resolve_projects([project] if project else None)
        resolved_alias = next(iter(projects.keys()))
        formatter.output(
            {
                "message": f"LLM export completed for project '{resolved_alias}'",
                "output_dir": str(resolved_alias),
            }
        )
    else:
        formatter.console.print("[bold green]LLM export completed successfully.[/bold green]")
