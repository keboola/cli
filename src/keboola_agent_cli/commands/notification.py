"""Notification subscription audit commands (issue #600).

Thin CLI layer over :class:`NotificationService`. Two subcommands:

- ``notification list`` — fleet-wide listing of notification subscriptions.
- ``notification detail`` — one subscription including its raw filters.

These are the recipients behind the Flow Builder's *Notifications* tab (the
bell icon), which live in a separate platform service and are therefore
invisible to ``flow detail`` / ``config detail``. The in-flow
``type: "notification"`` task is a different mechanism and stays visible there.

Both subcommands are read-only and safe under ``--deny-writes``.
"""

from __future__ import annotations

import logging
from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..services.notification_service import KNOWN_EVENTS, SCOPE_PROJECT_WIDE
from ._helpers import (
    check_cli_permission,
    get_formatter,
    get_service,
    map_error_to_exit_code,
)

logger = logging.getLogger(__name__)

notification_app = typer.Typer(
    help="Audit notification subscriptions across projects (Flow Notifications tab)"
)

_EVENT_HELP = (
    "Event name filter, e.g. 'job-failed'. Known events: "
    + ", ".join(KNOWN_EVENTS)
    + ". Not validated against that list -- the service may add more."
)


@notification_app.callback(invoke_without_command=True)
def _notification_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "notification")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _config_cell(row: dict[str, Any]) -> str:
    """Render the config column: name, falling back to ID, then to the scope.

    A subscription with no ``job.configuration.id`` filter fires for every job
    in the project, so it gets the literal scope word rather than a blank --
    a blank would read as "lookup failed" instead of "deliberately global".
    """
    if row.get("scope") == SCOPE_PROJECT_WIDE:
        return f"[dim]{SCOPE_PROJECT_WIDE}[/dim]"
    return escape(row.get("config_name") or row.get("config_id") or "")


def _format_subscription_table(formatter: Any, subscriptions: list[dict[str, Any]]) -> None:
    tbl = Table(
        "Project",
        "Event",
        "Config",
        "Component",
        "Channel",
        "Recipient",
        "Branch",
        "Expires",
        show_header=True,
        header_style="bold cyan",
    )
    for sub in subscriptions:
        tbl.add_row(
            escape(sub.get("project_alias", "")),
            escape(sub.get("event", "")),
            _config_cell(sub),
            escape(sub.get("component_id", "") or "[dim]any[/dim]"),
            escape(sub.get("channel", "")),
            escape(sub.get("address", "")),
            escape(sub.get("branch_id", "") or ""),
            escape(sub.get("expires_at", "") or ""),
        )
    formatter.console.print(tbl)


def _emit_errors(formatter: Any, errors: list[dict[str, Any]]) -> None:
    for err in errors:
        formatter.warning(
            f"Project '{err.get('project_alias', '?')}': {err.get('message', 'error')}"
        )


# ---------------------------------------------------------------------------
# notification list
# ---------------------------------------------------------------------------


@notification_app.command("list")
def notification_list(
    ctx: typer.Context,
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias (repeatable; omit for all registered projects)",
    ),
    event: str | None = typer.Option(None, "--event", help=_EVENT_HELP),
    component_id: str | None = typer.Option(
        None,
        "--component-id",
        help="Only subscriptions filtering on this component (e.g. keboola.flow)",
    ),
    config_id: str | None = typer.Option(
        None,
        "--config-id",
        help="Only subscriptions filtering on this configuration ID",
    ),
) -> None:
    """List notification subscriptions (Flow Notifications tab) across projects.

    Each row shows the project, event, the configuration the subscription is
    scoped to, and the recipient that gets paged.

    Subscriptions are project-level, not branch-scoped: a branch-specific one
    carries a ``branch.id`` filter, shown in the Branch column. Configuration
    names are resolved against each project's active branch.

    Companion to ``flow list`` / ``flow detail``, which only expose in-flow
    notification *tasks* -- a different mechanism from these subscriptions.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "notification_service")

    try:
        result = service.list_subscriptions(
            aliases=project,
            event=event,
            component_id=component_id,
            config_id=config_id,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
        return

    subscriptions = result.get("subscriptions", [])
    if not subscriptions:
        formatter.console.print("[dim]No notification subscriptions found.[/dim]")
    else:
        _format_subscription_table(formatter, subscriptions)

    excluded = result.get("project_wide_excluded", 0)
    if excluded:
        formatter.warning(
            f"{excluded} project-wide subscription(s) hidden by --component-id/--config-id. "
            "They have no configuration filter, so they also fire for this one -- "
            "re-run without the filter to see who else gets paged."
        )

    _emit_errors(formatter, result.get("errors", []))


# ---------------------------------------------------------------------------
# notification detail
# ---------------------------------------------------------------------------


@notification_app.command("detail")
def notification_detail(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    subscription_id: str = typer.Option(
        ..., "--subscription-id", help="Notification subscription ID"
    ),
) -> None:
    """Show one notification subscription, including its raw filter list.

    The table view has columns only for the filters with an obvious meaning
    (component, configuration, branch, phase); this view prints every filter
    verbatim, so threshold filters like ``durationOvertimePercentage`` are
    visible too.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "notification_service")

    try:
        result = service.get_subscription_detail(
            alias=project,
            subscription_id=subscription_id,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
        return

    formatter.console.print(
        f"\n[bold]{escape(result.get('event', ''))}[/bold] "
        f"[dim](subscription {escape(result.get('subscription_id', ''))})[/dim]"
    )
    formatter.console.print(f"  Project:     {escape(result.get('project_alias', ''))}")
    formatter.console.print(f"  Scope:       {escape(result.get('scope', ''))}")
    formatter.console.print(
        f"  Config:      {escape(result.get('config_name', '') or '(unknown)')} "
        f"[dim]{escape(result.get('component_id', '') or 'any')}/"
        f"{escape(result.get('config_id', '') or 'any')}[/dim]"
    )
    if result.get("branch_id"):
        formatter.console.print(f"  Branch:      {escape(result['branch_id'])}")
    if result.get("phase_id"):
        formatter.console.print(f"  Phase:       {escape(result['phase_id'])}")
    formatter.console.print(
        f"  Recipient:   {escape(result.get('address', ''))} "
        f"[dim]({escape(result.get('channel', ''))})[/dim]"
    )
    if result.get("expires_at"):
        formatter.console.print(f"  Expires:     {escape(result['expires_at'])}")

    filters = result.get("filters") or []
    if filters:
        formatter.console.print("  Filters:")
        for item in filters:
            operator = item.get("operator", "==")
            formatter.console.print(
                f"    {escape(str(item.get('field', '')))} "
                f"{escape(str(operator))} {escape(str(item.get('value', '')))}"
            )
