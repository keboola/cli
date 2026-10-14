"""Human output for ``config variables-set`` / ``variables-get`` / ``variables-clear``.

Split out of ``commands/config.py``, which is over its size ceiling
(``make loc-check``). ``#``-prefixed keys always print as ``<encrypted>``.
"""

from __future__ import annotations

from typing import Any

from rich.markup import escape


def format_variables_get(formatter: Any, result: dict) -> None:
    if not result.get("linked"):
        formatter.console.print(
            "[yellow]No variables linked[/yellow] to "
            f"[cyan]{escape(result['parent_component_id'])}[/cyan]/"
            f"[cyan]{escape(result['parent_config_id'])}[/cyan]."
        )
        return

    formatter.console.print(
        f"[bold]Variables on[/bold] "
        f"[cyan]{escape(result['parent_component_id'])}[/cyan]/"
        f"[cyan]{escape(result['parent_config_id'])}[/cyan] "
        f"[dim](variables_id={escape(result['variables_id'] or '')}, "
        f"values_id={escape(result['values_id'] or '')})[/dim]"
    )
    if not result["values"]:
        formatter.console.print("  [dim](no values set)[/dim]")
        return
    for k in sorted(result["values"]):
        v = result["values"][k]
        display_v = "<encrypted>" if k.startswith("#") else escape(str(v))
        formatter.console.print(f"  [green]{escape(k)}[/green] = {display_v}")


def format_variables_set(formatter: Any, result: dict) -> None:
    action_label = (
        "[green]created[/green]" if result["action"] == "created" else "[yellow]updated[/yellow]"
    )
    formatter.console.print(
        f"Variables {action_label} on "
        f"[cyan]{escape(result['parent_component_id'])}[/cyan]/"
        f"[cyan]{escape(result['parent_config_id'])}[/cyan]"
    )
    formatter.console.print(f"  variables_id: [cyan]{escape(result['variables_id'])}[/cyan]")
    formatter.console.print(f"  values_id:    [cyan]{escape(result['values_id'])}[/cyan]")
    if result.get("encrypted_keys"):
        joined = ", ".join(escape(k) for k in result["encrypted_keys"])
        formatter.console.print(f"  [dim]encrypted: {joined}[/dim]")
    formatter.console.print("[bold]Final values:[/bold]")
    for k in sorted(result["values"]):
        v = result["values"][k]
        display_v = "<encrypted>" if k.startswith("#") else escape(str(v))
        formatter.console.print(f"  [green]{escape(k)}[/green] = {display_v}")


def format_variables_clear(formatter: Any, result: dict) -> None:
    if not result["was_linked"]:
        formatter.console.print(
            f"[yellow]Nothing to clear[/yellow]: "
            f"[cyan]{escape(result['parent_component_id'])}[/cyan]/"
            f"[cyan]{escape(result['parent_config_id'])}[/cyan] had no variables linked."
        )
        return
    formatter.console.print(
        f"[green]Unlinked[/green] variables from "
        f"[cyan]{escape(result['parent_component_id'])}[/cyan]/"
        f"[cyan]{escape(result['parent_config_id'])}[/cyan] "
        f"[dim](was variables_id={escape(result['unlinked_variables_id'] or '')}, "
        f"values_id={escape(result['unlinked_values_id'] or '')})[/dim]"
    )
    formatter.console.print(
        "[dim]The underlying keboola.variables config was NOT deleted. "
        "Use 'kbagent config delete' to remove it if no other config references it.[/dim]"
    )


def format_variables_dry_run(formatter: Any, result: dict) -> None:
    verb = "create" if result["action"] == "would_create" else "update"
    formatter.console.print(
        f"[yellow]DRY RUN[/yellow]: would {verb} variables on "
        f"[cyan]{escape(result['parent_component_id'])}[/cyan]/"
        f"[cyan]{escape(result['parent_config_id'])}[/cyan]"
    )
    if result["was_linked"]:
        formatter.console.print(
            f"  current variables_id: [cyan]{escape(result['current_variables_id'] or '')}[/cyan]"
        )
    else:
        formatter.console.print("  [dim]no variables currently linked[/dim]")

    current_keys = set(result["current_values"])
    proposed_keys = set(result["would_write"])
    for k in sorted(current_keys | proposed_keys):
        current_v = result["current_values"].get(k)
        proposed_v = result["would_write"].get(k)
        if k not in proposed_keys:
            display = "<encrypted>" if k.startswith("#") else escape(str(current_v))
            formatter.console.print(f"  [red]- {escape(k)}[/red] = {display} [dim](dropped)[/dim]")
        elif k not in current_keys:
            display = "<encrypted>" if k.startswith("#") else escape(str(proposed_v))
            formatter.console.print(f"  [green]+ {escape(k)}[/green] = {display}")
        elif current_v != proposed_v:
            display_cur = "<encrypted>" if k.startswith("#") else escape(str(current_v))
            display_new = "<encrypted>" if k.startswith("#") else escape(str(proposed_v))
            formatter.console.print(
                f"  [yellow]~ {escape(k)}[/yellow] = {display_cur} -> {display_new}"
            )
        else:
            display = "<encrypted>" if k.startswith("#") else escape(str(current_v))
            formatter.console.print(f"  [dim]= {escape(k)} = {display}[/dim]")
