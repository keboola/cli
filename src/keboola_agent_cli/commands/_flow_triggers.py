"""The ``kbagent flow triggers`` command (issue #714).

A flow is started automatically by more than one mechanism, and kbagent used
to see exactly one of them. ``schedule list`` / ``search`` only ever looked at
``keboola.scheduler`` configs, so a flow driven by a **table trigger** -- a
separate Storage API resource, not a component config -- came back as
"nothing found". Twice in one investigation that was read as "this flow has no
trigger", when it was in fact already live.

This command answers the whole question in one call, and is explicit about the
part it does not cover (cross-project trigger-queue configs living in another
project). The distinction between "checked, found none" and "not covered" is
the entire point: for a human or an agent deciding whether something is safe to
leave unscheduled, they lead to opposite conclusions.

Lives in a private module because ``commands/flow.py`` is exactly at the
800-code-line commands soft ceiling (CONTRIBUTING.md file-size budgets), so
adding material there would tip it over. Mounted flat onto ``flow_app`` via
:func:`register`, so the permission key stays ``flow.triggers`` and
``kbagent flow --help`` lists it beside the other flow commands.
"""

from __future__ import annotations

from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import (
    get_formatter,
    get_service,
    map_error_to_exit_code,
    resolve_branch,
)

NOT_COVERED_NOTE = (
    "Cross-project triggers were NOT checked -- a trigger-queue app config in "
    "another project can start this flow and would not appear above. Treat an "
    "empty result as 'no trigger that kbagent checked', not 'no trigger'."
)


def _format_triggers(formatter: Any, result: dict[str, Any]) -> None:
    """Human-mode rendering: both trigger kinds, then what was not checked."""
    formatter.console.print(
        f"\n[bold]Triggers for flow {escape(str(result.get('config_id', '')))}[/bold]"
        f"  [dim]({escape(result.get('project_alias', ''))})[/dim]"
    )

    crons = result.get("cron_schedules") or []
    if crons:
        table = Table(title=f"Cron schedules ({len(crons)})")
        table.add_column("Schedule ID")
        table.add_column("Cron")
        table.add_column("Timezone")
        table.add_column("State")
        for cron in crons:
            table.add_row(
                escape(str(cron.get("schedule_id", ""))),
                escape(str(cron.get("cron_tab", ""))),
                escape(str(cron.get("timezone", ""))),
                escape(str(cron.get("state", ""))),
            )
        formatter.console.print(table)
    else:
        formatter.console.print("\n[dim]Cron schedules: none.[/dim]")

    triggers = result.get("table_triggers") or []
    if triggers:
        table = Table(title=f"Table triggers ({len(triggers)})")
        table.add_column("Trigger ID")
        table.add_column("Component")
        table.add_column("Watched tables")
        table.add_column("Cooldown (min)")
        table.add_column("Last run")
        for trigger in triggers:
            last_run = trigger.get("last_run")
            table.add_row(
                escape(str(trigger.get("trigger_id", ""))),
                escape(str(trigger.get("component_id", ""))),
                escape(", ".join(trigger.get("tables") or [])),
                escape(str(trigger.get("cool_down_period_minutes", ""))),
                # None means the trigger exists but has never fired -- which is
                # NOT the same as "not scheduled", so say so rather than
                # rendering a bare blank cell.
                escape(str(last_run)) if last_run else "[dim]never[/dim]",
            )
        formatter.console.print(table)
    else:
        formatter.console.print("[dim]Table triggers: none (production only).[/dim]")

    if not result.get("cross_project_triggers_checked", False):
        formatter.console.print(f"\n[yellow]{escape(NOT_COVERED_NOTE)}[/yellow]")


def register(app: typer.Typer) -> None:
    """Mount the ``triggers`` command onto ``app`` (the ``flow`` Typer group)."""

    @app.command("triggers")
    def flow_triggers(
        ctx: typer.Context,
        project: str = typer.Option(..., "--project", help="Project alias"),
        flow_id: str = typer.Option(..., "--flow-id", help="Flow configuration ID"),
        branch: int | None = typer.Option(
            None,
            "--branch",
            help="Dev branch ID. Narrows the CRON half only -- the Storage "
            "triggers route is production-only and has no branch-scoped variant.",
        ),
    ) -> None:
        """Show every trigger kbagent can see for a flow (cron + table triggers).

        Reports cross-project triggers as NOT CHECKED rather than as absent --
        an empty result here means "no trigger that kbagent checked".
        """
        formatter = get_formatter(ctx)
        service = get_service(ctx, "flow_service")
        config_store = ctx.obj["config_store"]
        _, effective_branch = resolve_branch(config_store, formatter, project, branch)

        try:
            result = service.get_flow_triggers(
                alias=project,
                config_id=flow_id,
                branch_id=effective_branch,
            )
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
            raise typer.Exit(code=5) from None
        except KeboolaApiError as exc:
            formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
            raise typer.Exit(code=map_error_to_exit_code(exc)) from None

        if formatter.json_mode:
            formatter.output(result)
        else:
            _format_triggers(formatter, result)
