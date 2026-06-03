"""CLI commands for Kai (Keboola AI Assistant) integration.

Bridges Claude Code (local) to Kai (cloud) via kbagent CLI.
Kai has MCP access to project data and can answer Keboola-specific questions.
"""

import typer

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import (
    check_cli_permission,
    get_formatter,
    get_service,
    map_error_to_exit_code,
)

kai_app = typer.Typer(
    help=(
        "(BETA) Keboola AI Assistant (Kai) — ask questions about your project.\n\n"
        "Requires a master Storage API token (the auto-generated 'owner' token, "
        "not a custom one) with the 'AI Agent Chat' feature flag enabled on the "
        "project. Custom Storage API tokens cannot be used with Kai."
    )
)


@kai_app.callback(invoke_without_command=True)
def _kai_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "kai")


@kai_app.command("ping")
def kai_ping(
    ctx: typer.Context,
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project alias (uses default if omitted).",
    ),
) -> None:
    """Check Kai server health and MCP connection status."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "kai_service")

    try:
        alias = service.resolve_alias(project)
        result = service.ping(alias)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    def _human(console, data):
        console.print(f"[bold green]Kai is alive[/bold green] ({data['project_alias']})")
        console.print(f"  Timestamp:      {data['timestamp']}")
        console.print(f"  App:            {data['app_name']} {data['app_version']}")
        console.print(f"  Server:         {data['server_version']}")
        console.print(f"  MCP connection: {data['mcp_status']}")

    formatter.output(result, _human)


@kai_app.command("ask")
def kai_ask(
    ctx: typer.Context,
    message: str = typer.Option(
        ...,
        "--message",
        "-m",
        help="Question to ask Kai about your project.",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project alias (uses default if omitted).",
    ),
) -> None:
    """Ask Kai a one-shot question and get the full response.

    Kai has access to your project's data, configurations, and lineage
    via MCP tools. Use this for Keboola-specific questions that require
    project context.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "kai_service")

    try:
        alias = service.resolve_alias(project)
        result = service.ask(alias, message)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    def _human(console, data):
        console.print(data["response"])

    formatter.output(result, _human)


@kai_app.command("chat")
def kai_chat(
    ctx: typer.Context,
    message: str = typer.Option(
        ...,
        "--message",
        "-m",
        help="Message to send to Kai.",
    ),
    chat_id: str | None = typer.Option(
        None,
        "--chat-id",
        help="Continue an existing chat session.",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project alias (uses default if omitted).",
    ),
) -> None:
    """Send a message to Kai in a chat session.

    Use --chat-id to continue a previous conversation.
    Without --chat-id, starts a new chat.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "kai_service")

    try:
        alias = service.resolve_alias(project)
        result = service.chat_message(alias, message, chat_id=chat_id)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    def _human(console, data):
        console.print(data["response"])
        console.print(f"\n[dim]Chat ID: {data['chat_id']}[/dim]")

    formatter.output(result, _human)


@kai_app.command("preflight")
def kai_preflight(
    ctx: typer.Context,
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project alias (uses default if omitted).",
    ),
) -> None:
    """Check whether the configured token can use Kai (master token + AI Agent Chat).

    Inspects /v2/storage/tokens/verify and returns a structured readiness
    payload WITHOUT raising on failure — unlike ``ping``/``ask``/``chat``
    which fail-fast with KAI_NOT_ENABLED. Useful for UIs and pre-flight
    automation that need to render an informative warning instead of an
    error cascade.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "kai_service")

    try:
        alias = service.resolve_alias(project)
        result = service.preflight(alias)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    def _human(console, data):
        if data["ok"]:
            console.print(f"[bold green]Kai is ready[/bold green] ({data['project_alias']})")
        else:
            console.print(f"[bold red]Kai is NOT ready[/bold red] ({data['project_alias']})")
        console.print(f"  Token:           {data['token_description'] or '—'}")
        console.print(f"  Master token:    {'yes' if data['is_master_token'] else 'no'}")
        console.print(
            f"  AI Agent Chat:   {'enabled' if data['has_agent_chat_feature'] else 'disabled'}"
        )
        if data["project_name"]:
            console.print(f"  Project:         {data['project_name']} ({data['project_id']})")
        if data["error"]:
            console.print(f"  [red]Reason:[/red] {data['error']}")

    formatter.output(result, _human)


@kai_app.command("chat-detail")
def kai_chat_detail(
    ctx: typer.Context,
    chat_id: str = typer.Option(
        ...,
        "--chat-id",
        help="Kai chat ID (UUID, from `kai history` or `kai chat`).",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project alias (uses default if omitted).",
    ),
) -> None:
    """Fetch the full message history of a single Kai chat.

    Use this to restore a previous conversation when continuing it with
    `kai chat --chat-id ID`, or to export a transcript for offline review.
    Returns a flat list of ``{role, content, created_at}`` records — tool
    calls and other non-text parts are skipped (they are Kai's internal
    streaming protocol, not user-facing content).
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "kai_service")

    try:
        alias = service.resolve_alias(project)
        result = service.get_chat_detail(alias, chat_id)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    def _human(console, data):
        title = data.get("title") or "(untitled)"
        console.print(f"[bold]{title}[/bold] [dim]({data['chat_id']})[/dim]")
        if data.get("created_at"):
            console.print(f"[dim]Created: {data['created_at']}[/dim]")
        messages = data.get("messages") or []
        if not messages:
            console.print("[dim](no messages)[/dim]")
            return
        for msg in messages:
            role = msg["role"]
            style = "cyan" if role == "user" else "green"
            console.print(f"\n[bold {style}]{role}:[/bold {style}]")
            console.print(msg["content"])

    formatter.output(result, _human)


@kai_app.command("history")
def kai_history(
    ctx: typer.Context,
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project alias (uses default if omitted).",
    ),
    limit: int = typer.Option(
        10,
        "--limit",
        "-n",
        help="Maximum number of chats to return.",
    ),
) -> None:
    """List recent Kai chat sessions."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "kai_service")

    try:
        alias = service.resolve_alias(project)
        result = service.get_history(alias, limit=limit)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    def _human(console, data):
        chats = data["chats"]
        if not chats:
            console.print("[dim]No chat history.[/dim]")
            return
        from rich.table import Table

        table = Table(title=f"Kai Chat History ({data['project_alias']})")
        table.add_column("Chat ID", style="cyan", no_wrap=True)
        table.add_column("Title")
        table.add_column("Created", style="dim")
        for chat in chats:
            table.add_row(
                chat["id"][:12] + "...",
                chat["title"],
                chat["created_at"] or "",
            )
        console.print(table)
        if data["has_more"]:
            console.print("[dim]More chats available. Use --limit to see more.[/dim]")

    formatter.output(result, _human)
