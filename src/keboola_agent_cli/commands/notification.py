"""Notification subscription commands (issue #600, #690).

Thin CLI layer over :class:`NotificationService`. Five subcommands:

- ``notification list`` — fleet-wide listing of notification subscriptions.
- ``notification detail`` — one subscription including its raw filters.
- ``notification create`` — add a subscription (write).
- ``notification delete`` — remove a subscription (destructive).
- ``notification replace-recipient`` — swap a subscription's recipient by
  creating a new subscription and deleting the old one (write).

These are the recipients behind the Flow Builder's *Notifications* tab (the
bell icon), which live in a separate platform service and are therefore
invisible to ``flow detail`` / ``config detail``. The in-flow
``type: "notification"`` task is a different mechanism and stays visible there.

``list`` and ``detail`` are read-only and safe under ``--deny-writes``.
``create`` / ``replace-recipient`` are gated as ``write``; ``delete`` is
gated as ``destructive`` -- see ``notification.*`` in
``CLI_OPERATIONS`` (``permissions.py``).
"""

from __future__ import annotations

import logging
from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..services.notification_service import KNOWN_EVENTS, SCOPE_PROJECT_WIDE, VALID_CHANNELS
from ._helpers import (
    check_cli_permission,
    get_formatter,
    get_service,
    map_error_to_exit_code,
)

logger = logging.getLogger(__name__)

notification_app = typer.Typer(
    help=(
        "Manage notification subscriptions across projects (Flow Notifications tab). "
        "'list'/'detail' are read-only; 'create'/'replace-recipient' are gated as "
        "write, 'delete' as destructive."
    )
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


def _component_cell(row: dict[str, Any]) -> str:
    """Render the component column, or a dim "any" when unfiltered.

    The markup has to sit OUTSIDE ``escape()``: escaping the fallback itself
    turns ``[dim]`` into ``\\[dim]``, which Rich then prints literally.
    """
    component_id = row.get("component_id", "")
    return escape(component_id) if component_id else "[dim]any[/dim]"


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
            _component_cell(sub),
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


def _print_subscription(formatter: Any, row: dict[str, Any]) -> None:
    """Print one subscription's audit row in human mode.

    Shared by ``detail``, ``create``, and ``replace-recipient`` -- all three
    show the same subscription shape, so the rendering lives in one place
    instead of being copy-pasted at each call site.

    The table view (``list``) has columns only for the filters with an
    obvious meaning (component, configuration, branch, phase); this view
    prints every filter verbatim, so threshold filters like
    ``durationOvertimePercentage`` are visible too.
    """
    formatter.console.print(
        f"\n[bold]{escape(row.get('event', ''))}[/bold] "
        f"[dim](subscription {escape(row.get('subscription_id', ''))})[/dim]"
    )
    formatter.console.print(f"  Project:     {escape(row.get('project_alias', ''))}")
    formatter.console.print(f"  Scope:       {escape(row.get('scope', ''))}")
    formatter.console.print(
        f"  Config:      {escape(row.get('config_name', '') or '(unknown)')} "
        f"[dim]{escape(row.get('component_id', '') or 'any')}/"
        f"{escape(row.get('config_id', '') or 'any')}[/dim]"
    )
    if row.get("branch_id"):
        formatter.console.print(f"  Branch:      {escape(row['branch_id'])}")
    if row.get("phase_id"):
        formatter.console.print(f"  Phase:       {escape(row['phase_id'])}")
    formatter.console.print(
        f"  Recipient:   {escape(row.get('address', ''))} "
        f"[dim]({escape(row.get('channel', ''))})[/dim]"
    )
    if row.get("expires_at"):
        formatter.console.print(f"  Expires:     {escape(row['expires_at'])}")

    filters = row.get("filters") or []
    if filters:
        formatter.console.print("  Filters:")
        for item in filters:
            operator = item.get("operator", "==")
            formatter.console.print(
                f"    {escape(str(item.get('field', '')))} "
                f"{escape(str(operator))} {escape(str(item.get('value', '')))}"
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

    _print_subscription(formatter, result)


# ---------------------------------------------------------------------------
# notification create
# ---------------------------------------------------------------------------


@notification_app.command("create")
def notification_create(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    event: str = typer.Option(..., "--event", help=_EVENT_HELP),
    channel: str = typer.Option(
        ..., "--channel", help=f"Recipient channel: {' | '.join(VALID_CHANNELS)}"
    ),
    address: str = typer.Option(
        ..., "--address", help="Email address (channel=email) or callback URL (channel=webhook)"
    ),
    component_id: str | None = typer.Option(
        None, "--component-id", help="Restrict to jobs of this component (e.g. keboola.flow)"
    ),
    config_id: str | None = typer.Option(
        None, "--config-id", help="Restrict to jobs of this configuration ID"
    ),
    branch: int | None = typer.Option(None, "--branch", help="Restrict to jobs on this branch"),
    expires_at: str | None = typer.Option(
        None, "--expires-at", help="Optional ISO-8601 expiry for the subscription"
    ),
) -> None:
    """Create a notification subscription.

    ``--address`` carries the recipient's destination -- an email address for
    ``--channel email``, or a callback URL for ``--channel webhook``.
    """
    formatter = get_formatter(ctx)

    if channel not in VALID_CHANNELS:
        formatter.error(
            message=f"Invalid --channel '{channel}'. Valid channels: {', '.join(VALID_CHANNELS)}.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None

    service = get_service(ctx, "notification_service")

    try:
        result = service.create_subscription(
            alias=project,
            event=event,
            channel=channel,
            address=address,
            component_id=component_id,
            config_id=config_id,
            branch_id=branch,
            expires_at=expires_at,
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

    formatter.success(
        f"Created notification subscription {escape(result.get('subscription_id', ''))}"
    )
    _print_subscription(formatter, result)


# ---------------------------------------------------------------------------
# notification delete
# ---------------------------------------------------------------------------


@notification_app.command("delete")
def notification_delete(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    subscription_id: str = typer.Option(
        ..., "--subscription-id", help="Notification subscription ID"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Delete a notification subscription."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "notification_service")

    if not yes and not formatter.json_mode:
        confirmed = typer.confirm(f"Delete notification subscription {subscription_id}?")
        if not confirmed:
            formatter.console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=0)

    try:
        result = service.delete_subscription(alias=project, subscription_id=subscription_id)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.success(f"Deleted notification subscription {escape(subscription_id)}")


# ---------------------------------------------------------------------------
# notification replace-recipient
# ---------------------------------------------------------------------------


@notification_app.command("replace-recipient")
def notification_replace_recipient(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    subscription_id: str = typer.Option(
        ..., "--subscription-id", help="Notification subscription ID to replace"
    ),
    address: str = typer.Option(
        ..., "--address", help="New email address or webhook URL for the recipient"
    ),
    channel: str | None = typer.Option(
        None,
        "--channel",
        help=(
            f"Override the recipient channel: {' | '.join(VALID_CHANNELS)}. "
            "Defaults to the old subscription's channel."
        ),
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Replace a subscription's recipient.

    Creates a new subscription with the new recipient (same event/filters/
    expiry as the old one), then deletes the old subscription. If the delete
    fails, the old subscription is left in place alongside the new one --
    reported as a warning, never silently swallowed.
    """
    formatter = get_formatter(ctx)

    if channel is not None and channel not in VALID_CHANNELS:
        formatter.error(
            message=f"Invalid --channel '{channel}'. Valid channels: {', '.join(VALID_CHANNELS)}.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None

    service = get_service(ctx, "notification_service")

    if not yes and not formatter.json_mode:
        confirmed = typer.confirm(
            f"Replace the recipient of notification subscription {subscription_id}?"
        )
        if not confirmed:
            formatter.console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=0)

    try:
        result = service.replace_subscription_recipient(
            alias=project,
            subscription_id=subscription_id,
            new_address=address,
            new_channel=channel,
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

    formatter.success(
        f"Replaced recipient: subscription {escape(result.get('old_subscription_id', ''))} "
        f"-> {escape(result.get('new_subscription_id', ''))}"
    )
    _print_subscription(formatter, result)
    for warning in result.get("warnings", []):
        formatter.warning(warning)
