"""Human renderers for the active project (the ``default_project`` pin).

Lives beside `commands/project.py` rather than inside it: that module is
already over its soft size budget. ``print_active_project_change`` is shared
by every command that sets or clears the active project (`project add`,
`project remove`, `init`, `org setup`, the `auth` registration commands), so
each of them prints the same line for the ``active_project_set`` /
``active_project_cleared`` keys of its result.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.markup import escape

from ._project_ref import env_override_warning


def print_active_project_change(
    console: Console, active_project_set: str | None, active_project_cleared: str | None
) -> None:
    """Print one line when a command set or cleared the active project."""
    if active_project_set:
        console.print(f"[dim]Active project set to '{escape(active_project_set)}'.[/dim]")
    if active_project_cleared:
        console.print(
            f"[yellow]Note:[/yellow] '{escape(active_project_cleared)}' was the active "
            "project; there is no active project now. Run 'kbagent project use <alias>' "
            "to choose one."
        )


def format_use_result(c: Console, d: dict[str, Any]) -> None:
    """Render `project use`: the new pin, the previous one, and an env override."""
    previous = d.get("previous")
    if previous and previous != d["alias"]:
        c.print(
            f"[bold green]Pinned:[/bold green] default project is now "
            f"[bold]{d['alias']}[/bold] (was [dim]{previous}[/dim])"
        )
    else:
        c.print(f"[bold green]Pinned:[/bold green] default project is [bold]{d['alias']}[/bold]")
    env_override = d.get("env_override")
    if env_override and env_override != d["alias"]:
        c.print(
            f"[yellow]Note:[/yellow] KBAGENT_PROJECT='{env_override}' is set "
            "and overrides this pin for the current shell."
        )


def format_current_result(c: Console, d: dict[str, Any]) -> None:
    """Render `project current`: the effective default project and its source."""
    alias = d.get("alias")
    source = d.get("source")
    if alias is None:
        c.print(
            "[dim](no default project set)[/dim] -- pass --project, set "
            "KBAGENT_PROJECT, or run 'kbagent project use <alias>'"
        )
        # The pin is empty, but a sole registered project still answers a
        # command without --project; say so instead of implying a failure.
        if d.get("resolved_source") == "sole":
            c.print(
                f"[dim]  (commands without --project use the only registered project: "
                f"{escape(d['resolved_alias'])})[/dim]"
            )
        return
    if source == "env":
        c.print(f"[bold cyan]{alias}[/bold cyan]  [dim](source: KBAGENT_PROJECT env var)[/dim]")
        warning = env_override_warning(d)
        if warning:
            c.print(f"[yellow]Warning:[/yellow] {escape(warning)}")
        pinned = d.get("pinned")
        if pinned:
            c.print(f"[dim]  (pinned in config: {pinned}, overridden)[/dim]")
    else:
        c.print(f"[bold cyan]{alias}[/bold cyan]  [dim](source: pinned default)[/dim]")
