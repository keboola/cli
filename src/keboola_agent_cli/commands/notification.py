"""Flow Notification subscription commands (issue #600).

Thin CLI layer over :class:`NotificationService`. One subcommand:

- ``notification list`` -- who gets notified when a job fails, succeeds, or
  runs long, across one or more projects, sourced from ``GET
  /project-subscriptions`` on the ``notification.<stack-suffix>`` host.

This is the Flow Builder **Notifications tab** (the bell icon: Success /
Error / Processing-delay cards). It is a different mechanism from the in-flow
``type: "notification"`` task, which lives inside the flow's own
configuration and is already visible through ``kbagent flow detail``.

Read-only: safe under ``--deny-writes``. The service's create/delete
endpoints -- which change who gets paged when production breaks -- are
deliberately not exposed.
"""

from __future__ import annotations

from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from ..errors import ConfigError, ErrorCode
from ._helpers import check_cli_permission, get_formatter, get_service

notification_app = typer.Typer(
    help="Audit Flow Notification subscriptions across projects (issue #600). "
    "Read-only -- the Notifications tab recipients, which flow detail cannot show."
)


@notification_app.callback(invoke_without_command=True)
def _notification_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "notification")


def _format_subscription_table(formatter: Any, subscriptions: list[dict[str, Any]]) -> None:
    tbl = Table(
        "Project",
        "Event",
        "Flow / scope",
        "Component",
        "Branch",
        "Channel",
        "Recipient",
        "Expires",
        show_header=True,
        header_style="bold cyan",
    )
    for row in subscriptions:
        config_id = row.get("config_id", "")
        if config_id:
            # A subscription pointing at a deleted config resolves to no name;
            # show the bare id rather than an empty cell, since a dangling
            # subscription is exactly what an audit is hunting for.
            target = escape(row.get("config_name") or config_id)
        else:
            target = "[dim]project-wide[/dim]"
        tbl.add_row(
            escape(row.get("project_alias", "")),
            escape(row.get("event", "")),
            target,
            escape(row.get("component_id", "") or "-"),
            escape(row.get("branch_id", "") or "production"),
            escape(row.get("channel", "")),
            escape(row.get("address", "")),
            escape(row.get("expires_at", "") or "-"),
        )
    formatter.console.print(tbl)


def _emit_errors(formatter: Any, errors: list[dict[str, Any]]) -> None:
    for err in errors:
        formatter.warning(
            f"Project '{escape(str(err.get('project_alias', '?')))}': "
            f"{escape(str(err.get('message', 'error')))}"
        )


@notification_app.command("list")
def notification_list(
    ctx: typer.Context,
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias (repeatable; omit for all registered projects)",
    ),
    event: str | None = typer.Option(
        None,
        "--event",
        help="Event name, e.g. job-failed, job-succeeded, "
        "job-succeeded-with-warning, job-processing-long (also phase-job-*)",
    ),
    component_id: str | None = typer.Option(
        None, "--component-id", help="Only subscriptions filtered to this component"
    ),
    config_id: str | None = typer.Option(
        None, "--config-id", help="Only subscriptions filtered to this configuration"
    ),
    branch: int | None = typer.Option(
        None, "--branch", help="Only subscriptions carrying this branch.id filter"
    ),
) -> None:
    """List Flow Notification subscriptions (the Notifications tab) across projects.

    Answers "who gets paged when this flow breaks" for a whole fleet at once.
    These recipients live in the notification service, not in the flow's
    configuration, so `flow detail` / `config detail` cannot show them --
    they are the notification surface that used to require opening each flow
    in the UI by hand.

    Each row shows: project alias, event, the flow the subscription is
    filtered to (or `project-wide` when it carries no config filter -- the
    catch-all "notify me on any job failure"), component, branch, channel
    (email or webhook), the recipient address or URL, and expiry.

    `--event` is passed to the API; `--component-id`, `--config-id` and
    `--branch` match client-side against the subscription's own filter
    fields. NOTE the endpoint is not branch-scoped: it answers with every
    branch's subscriptions, so without `--branch` the output includes
    dev-branch ones alongside production. Unlike branch-scoped commands,
    `--branch` here is NEVER inferred from the project's active branch --
    doing so would silently hide the production recipients this audit
    exists to check.

    Read-only. The in-flow `type: "notification"` task is a different
    mechanism -- see `kbagent flow detail` for those.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "notification_service")

    # A branch id is only meaningful inside one project, and it is a filter
    # here rather than a scope -- so it is never inferred from the project's
    # active branch the way branch-scoped commands do it (see the docstring).
    if branch is not None and (not project or len(project) != 1):
        formatter.error(
            message="--branch requires exactly one --project",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None

    try:
        result = service.list_subscriptions(
            aliases=project,
            event=event,
            component_id=component_id,
            config_id=config_id,
            branch_id=branch,
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
    _emit_errors(formatter, result.get("errors", []))
