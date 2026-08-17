"""Pay-As-You-Go (PAYG) credit balance commands.

Thin CLI layer over :class:`BillingService`. One subcommand:

- ``billing credits`` -- current credit balance (consumed/remaining/purchased)
  across one or more projects, sourced from ``GET /credits`` on the
  ``billing.<stack-suffix>`` host.

This surfaces the BALANCE ONLY. Purchase history and Stripe invoice IDs live
on ``connection.<stack>`` `/pay-as-you-go/billing/*`, which does not accept a
plain project Storage token -- that surface is out of scope for issue #594
and is not exposed here.

Read-only: safe under ``--deny-writes``.
"""

from __future__ import annotations

from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from ..errors import ConfigError, ErrorCode
from ._helpers import check_cli_permission, get_formatter, get_service

billing_app = typer.Typer(
    help="PAYG credit balance across projects (issue #594). Balance only -- "
    "purchase history / Stripe invoice IDs are not reachable with a "
    "project token and are not exposed by this command."
)


@billing_app.callback(invoke_without_command=True)
def _billing_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "billing")


def _format_credits_table(formatter: Any, credits: list[dict[str, Any]]) -> None:
    tbl = Table(
        "Project",
        "Remaining",
        "Consumed",
        "Purchased",
        "Remaining (min)",
        show_header=True,
        header_style="bold cyan",
    )
    for row in credits:
        tbl.add_row(
            escape(row.get("project_alias", "")),
            f"{row.get('remaining', 0.0):.2f}",
            f"{row.get('consumed', 0.0):.2f}",
            f"{row.get('purchased', 0.0):.2f}",
            f"{row.get('remaining_minutes', 0.0):.0f}",
        )
    formatter.console.print(tbl)


def _emit_errors(formatter: Any, errors: list[dict[str, Any]]) -> None:
    for err in errors:
        formatter.warning(
            f"Project '{escape(str(err.get('project_alias', '?')))}': "
            f"{escape(str(err.get('message', 'error')))}"
        )


@billing_app.command("credits")
def billing_credits(
    ctx: typer.Context,
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias (repeatable; omit for all registered projects)",
    ),
) -> None:
    """Show the current PAYG credit balance for one or more projects.

    Balance only -- consumed, remaining, and purchased (derived as
    consumed + remaining) credits, plus the same figures expressed in
    minutes (the Keboola UI's unit: minutes = credits * 60). A project
    without the `pay-as-you-go` feature flag surfaces as a per-project
    warning (`PAYG_NOT_AVAILABLE`), not a hard failure -- one non-PAYG
    project in a multi-project run never blocks the others.

    Purchase history and Stripe invoice IDs are NOT available here: that
    data lives on `connection.<stack>` `/pay-as-you-go/billing/*`, which
    does not accept a plain project Storage API token (issue #594).
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "billing_service")

    try:
        result = service.get_credits(aliases=project)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
        return

    credits = result.get("credits", [])
    if not credits:
        formatter.console.print("[dim]No PAYG projects found.[/dim]")
    else:
        _format_credits_table(formatter, credits)
    _emit_errors(formatter, result.get("errors", []))
