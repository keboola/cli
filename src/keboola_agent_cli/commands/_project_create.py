"""Human renderer for `kbagent project create` (agent provisioning).

Lives beside `commands/project.py` rather than inside it: that module is
already over its soft size budget, and terminal rendering is the part of this
command with no other caller. The command itself stays in `project.py`, where
the Typer group is.
"""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from ..services.auth_service import ProvisionProjectResult


def format_provision_result(console: Console, result: ProvisionProjectResult) -> None:
    """Render a provisioned project: what exists now, and the confirm link.

    The confirm URL is printed on its own line, unwrapped and unstyled, so it
    survives a copy-paste out of any terminal: it is single-use, it expires,
    and it is the only path to ever owning the project that was just created.

    It is printed with ``markup=False`` rather than ``escape()``: the value
    comes from the stack, so Rich markup in it must not be interpreted, but
    escaping would insert backslashes into the very string the user has to
    copy-paste verbatim. ``markup=False`` prints it byte-for-byte and
    interprets nothing.
    """
    alias = result.registered_projects[0].alias if result.registered_projects else ""
    console.print(
        Panel(
            "\n".join(
                [
                    (
                        f"[bold]Project:[/bold] {escape(result.project_name)} "
                        f"(id {result.project_id})"
                    ),
                    f"[bold]Stack:[/bold] {escape(result.stack_url)}",
                    f"[bold]Backend:[/bold] {escape(result.backend or 'stack default')}",
                    f"[bold]Local alias:[/bold] {escape(alias)}",
                    (
                        f"[bold]Session:[/bold] {escape(result.session_id)} "
                        f"(access token expires {result.access_expires_at})"
                    ),
                ]
            ),
            title="Keboola project created",
            expand=False,
        )
    )

    console.print(
        "\n[bold yellow]Nobody owns this project yet.[/bold yellow] "
        "Open this link in a browser and sign in to claim it:\n"
    )
    console.print(result.confirm_url, markup=False, highlight=False, soft_wrap=True)

    console.print("\n[bold]Next steps[/bold]")
    for index, step in enumerate(result.next_steps, start=1):
        console.print(f"  {index}. {escape(step)}")

    for warning in result.warnings:
        console.print(f"[bold yellow]Warning:[/bold yellow] {escape(warning)}")
