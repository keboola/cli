"""CLI commands for Keboola documentation Q&A.

Thin CLI layer: parses arguments, calls DocsService, formats output.
No business logic belongs here.
"""

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import (
    check_cli_permission,
    get_formatter,
    get_service,
    map_error_to_exit_code,
)

docs_app = typer.Typer(help="Ask the Keboola documentation natural-language questions")


@docs_app.callback(invoke_without_command=True)
def _docs_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "docs")


def _format_docs_answer(console: Console, data: dict) -> None:
    """Render a documentation answer as a Rich panel with a sources list.

    Args:
        console: Rich Console instance.
        data: Dict with "query", "text", and "source_urls" from DocsService.
    """
    text = data.get("text", "")
    source_urls = data.get("source_urls", [])

    # Answer text is remote Markdown -- escape it so stray brackets are not
    # interpreted as Rich markup (precedent: config.py / storage.py).
    lines = [escape(text.strip()) if text.strip() else "[dim](no answer text returned)[/dim]"]
    if source_urls:
        lines.append("")
        lines.append("[bold]Sources:[/bold]")
        lines.extend(f"  - {escape(url)}" for url in source_urls)

    panel = Panel("\n".join(lines), title="Keboola Docs", expand=False)
    console.print(panel)


@docs_app.command("query")
def docs_query(
    ctx: typer.Context,
    question: str = typer.Argument(
        ...,
        help="Natural language question about the Keboola platform",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project alias (uses first available if not set)",
    ),
) -> None:
    """Ask the Keboola documentation a natural language question."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "docs_service")

    try:
        result = service.ask_docs(alias=project, query=question)
        formatter.output(result, _format_docs_answer)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            project=project or "",
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None
