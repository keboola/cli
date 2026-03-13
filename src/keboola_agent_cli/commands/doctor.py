"""Doctor command - thin CLI wrapper over DoctorService.

Delegates all health check logic to DoctorService (3-layer architecture).
"""

import typer
from rich.console import Console

from ..output import format_doctor_panel
from ._helpers import get_formatter, get_service


def doctor_command(
    ctx: typer.Context,
    fix: bool = typer.Option(
        False,
        "--fix",
        help="Auto-fix issues: install MCP server binary for faster startup.",
    ),
    stop_daemon: bool = typer.Option(
        False,
        "--stop-daemon",
        help="Stop the persistent MCP server daemon.",
    ),
    daemon_status: bool = typer.Option(
        False,
        "--daemon-status",
        help="Show status of the persistent MCP server daemon.",
    ),
) -> None:
    """Run health checks on CLI configuration and project connectivity."""
    formatter = get_formatter(ctx)
    doctor_service = get_service(ctx, "doctor_service")

    # Handle daemon control flags (mutually exclusive with normal checks)
    if stop_daemon:
        console = Console()
        try:
            from ..services.mcp_transport import get_server_manager
            manager = get_server_manager()
            status = manager.get_status()
            if status["running"]:
                manager.stop()
                console.print(f"[green]Stopped[/green] MCP daemon (pid={status['pid']})")
            else:
                console.print("[dim]MCP daemon is not running[/dim]")
        except Exception as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1)
        return

    if daemon_status:
        console = Console()
        try:
            from ..services.mcp_transport import get_server_manager
            manager = get_server_manager()
            status = manager.get_status()
            if status["running"]:
                idle = status.get("idle_seconds")
                idle_str = f", idle {idle}s / 1800s" if idle is not None else ""
                console.print(
                    f"[green]Running[/green] MCP daemon "
                    f"pid={status['pid']} port={status['port']}"
                    f"{idle_str}"
                )
            else:
                console.print("[dim]MCP daemon is not running[/dim]")
        except Exception as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1)
        return

    # If --fix, run warmup before checks
    if fix:
        console = Console()
        console.print("[bold]Running auto-fix...[/bold]")
        warmup_result = doctor_service.warmup()
        if warmup_result["installed"]:
            console.print(f"[green]OK[/green] {warmup_result['message']}")
        else:
            console.print(f"[dim]{warmup_result['message']}[/dim]")
        console.print()

    result = doctor_service.run_checks()
    formatter.output(result, format_doctor_panel)
