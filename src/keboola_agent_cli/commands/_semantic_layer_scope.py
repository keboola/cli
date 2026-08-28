"""Typer sub-app for ``kbagent semantic-layer scope`` (PSGO-140).

Extracted from :mod:`commands.semantic_layer` for the same LOC-ceiling reason
as ``_semantic_layer_crud.py``. Thin per the 3-layer architecture: all scope
resolution and metastore calls live in
:meth:`SemanticLayerService.scope_*` / :mod:`services._semantic_layer_scope`.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from ..errors import ErrorCode
from ._helpers import check_cli_permission, get_formatter, get_service
from ._semantic_layer_helpers import _handle_service_call

scope_app = typer.Typer(
    name="scope",
    help=(
        "Manage an item's visibility scope (project/organization/targeted), "
        "target-project grants, and organization-scope elevation requests."
    ),
    no_args_is_help=True,
)


@scope_app.callback(invoke_without_command=True)
def _scope_permission_check(ctx: typer.Context) -> None:
    """Permission check for the ``scope`` sub-app.

    Composes ``semantic-layer.scope.{subcommand}`` -- ``status``/``pending``
    are ``read``, ``grant``/``request-elevation``/``withdraw-elevation`` are
    ``write``, ``elevate`` is ``destructive`` (irreversible, widens
    visibility org-wide). See ``permissions.OPERATION_REGISTRY``.
    """
    check_cli_permission(ctx, "semantic-layer.scope")


def _print_scope_status(console: Console, data: dict) -> None:
    console.print(f"[bold]scope:[/bold] {data.get('scope', 'project')}")
    targets = data.get("target_project_ids")
    if targets:
        console.print(f"[bold]target_project_ids:[/bold] {targets}")
    pending = data.get("scope_elevation_requested_at")
    if pending:
        console.print(f"[bold]scope_elevation_requested_at:[/bold] {pending}")


def _print_pending_table(console: Console, data: dict) -> None:
    items = data if isinstance(data, list) else data.get("items", [])
    if not items:
        console.print("[dim]No items awaiting scope elevation.[/dim]")
        return
    table = Table(title="Pending scope-elevation requests")
    table.add_column("Name", style="bold cyan")
    table.add_column("ID", style="dim")
    table.add_column("Requested at")
    for item in items:
        table.add_row(
            item.get("name") or "",
            item.get("id") or "",
            str(item.get("scope_elevation_requested_at") or ""),
        )
    console.print(table)


@scope_app.command("status")
def scope_status(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    type_: str = typer.Option(
        ..., "--type", help="model|dataset|metric|relationship|constraint|glossary"
    ),
    item_id: str = typer.Option(..., "--id", help="Item UUID"),
) -> None:
    """Show an item's current scope, target-project grants, and pending elevation."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx, service.scope_status, alias=project, kind=type_, item_id=item_id
    )
    formatter.output(result, _print_scope_status)


@scope_app.command("grant")
def scope_grant(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Owning project alias"),
    type_: str = typer.Option(
        ..., "--type", help="model|dataset|metric|relationship|constraint|glossary"
    ),
    item_id: str = typer.Option(..., "--id", help="Item UUID (must have scope='targeted')"),
    target_project: list[str] = typer.Option(
        [], "--target-project", help="Project alias to add to the grant list (repeatable)."
    ),
    remove_target_project: list[str] = typer.Option(
        [], "--remove-target-project", help="Project alias to remove from the grant list."
    ),
    replace: bool = typer.Option(
        False,
        "--replace",
        help="Replace the whole grant list with exactly --target-project (matches the API's "
        "native semantics) instead of merging with the current set.",
    ),
    clear: bool = typer.Option(
        False, "--clear", help="Clear every grant (same as --replace with no --target-project)."
    ),
) -> None:
    """Grant, revoke, or replace the target-project list of a targeted-scope item.

    Default is an additive/subtractive merge against the current grants.
    Pass --replace to send exactly --target-project (the server's native
    replace-only semantics, one round trip); --clear is shorthand for
    --replace with no --target-project.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    if clear and (target_project or remove_target_project):
        formatter.error(
            message="--clear cannot be combined with --target-project/--remove-target-project.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)
    result = _handle_service_call(
        ctx,
        service.scope_grant,
        alias=project,
        kind=type_,
        item_id=item_id,
        add=target_project or None,
        remove=remove_target_project or None,
        replace=([] if clear else (target_project if replace else None)),
    )
    formatter.output(result, _print_scope_status)


@scope_app.command("request-elevation")
def scope_request_elevation(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Owning project alias"),
    type_: str = typer.Option(
        ..., "--type", help="model|dataset|metric|relationship|constraint|glossary"
    ),
    item_id: str = typer.Option(..., "--id", help="Item UUID"),
) -> None:
    """Flag a project-scoped item as awaiting an org-admin's step-up decision."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx, service.scope_request_elevation, alias=project, kind=type_, item_id=item_id
    )
    formatter.output(result, _print_scope_status)


@scope_app.command("withdraw-elevation")
def scope_withdraw_elevation(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Owning project alias"),
    type_: str = typer.Option(
        ..., "--type", help="model|dataset|metric|relationship|constraint|glossary"
    ),
    item_id: str = typer.Option(..., "--id", help="Item UUID"),
) -> None:
    """Withdraw a pending scope-elevation request. Idempotent no-op if none is pending."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx, service.scope_withdraw_elevation, alias=project, kind=type_, item_id=item_id
    )
    formatter.output(result, _print_scope_status)


@scope_app.command("elevate")
def scope_elevate(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Owning project alias"),
    type_: str = typer.Option(
        ..., "--type", help="model|dataset|metric|relationship|constraint|glossary"
    ),
    item_id: str = typer.Option(..., "--id", help="Item UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Step an item up to organization scope. Requires org-admin; ONE-WAY, no downgrade.

    Every project in the organization gains read access to this item and its
    full revision history once elevated -- there is no API to reverse it.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    if not yes and not formatter.json_mode:
        confirmed = typer.confirm(
            f"Elevate {type_} {item_id!r} to organization scope? This is IRREVERSIBLE "
            "and makes it visible to every project in the organization. Continue?"
        )
        if not confirmed:
            formatter.console.print("Aborted.")
            raise typer.Exit(code=0)
    result = _handle_service_call(
        ctx, service.scope_elevate, alias=project, kind=type_, item_id=item_id
    )
    formatter.output(result, _print_scope_status)


@scope_app.command("pending")
def scope_pending(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias (org-admin token)"),
    type_: str = typer.Option(
        ..., "--type", help="model|dataset|metric|relationship|constraint|glossary"
    ),
    limit: int | None = typer.Option(None, "--limit", help="Max results"),
    offset: int | None = typer.Option(None, "--offset", help="Skip this many results"),
) -> None:
    """List items of --type awaiting an org-admin's elevation decision, across the org."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx, service.scope_pending, alias=project, kind=type_, limit=limit, offset=offset
    )
    formatter.output(result, _print_pending_table)
