"""Typer sub-app for ``kbagent semantic-layer scope`` (PSGO-140).

Extracted from :mod:`commands.semantic_layer` for the same LOC-ceiling reason
as ``_semantic_layer_crud.py``. Thin per the 3-layer architecture: all scope
resolution and metastore calls live in
:meth:`SemanticLayerService.scope_*` / :mod:`services._semantic_layer_scope`.

Verbs follow the CLI spec (#791): ``get`` reads one attribute, ``add`` /
``remove`` attach / detach a target project, ``set`` writes the scope,
``request-create`` / ``request-delete`` / ``request-list`` manage the
elevation request.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from ..errors import ErrorCode
from ..services._semantic_layer_scope import DEFAULT_REQUEST_LIST_LIMIT
from ._helpers import check_cli_operation, check_cli_permission, get_formatter, get_service
from ._semantic_layer_helpers import ElevationScope, ItemType, _handle_service_call

scope_app = typer.Typer(
    name="scope",
    help=(
        "Manage an item's visibility scope (project/organization/targeted), "
        "target-project grants, and organization-scope elevation requests."
    ),
    no_args_is_help=True,
)

_PROJECT = typer.Option(..., "--project", help="Owning project alias")
_TYPE = typer.Option(..., "--type", help="Semantic item type")
_CONTEXT_ID = typer.Option(..., "--context-id", help="Item UUID")
_TARGET_PROJECT = typer.Option(
    [],
    "--target-project",
    help="Project alias or ID (repeatable, or comma-separated).",
)


@scope_app.callback(invoke_without_command=True)
def _scope_permission_check(ctx: typer.Context) -> None:
    """Permission check for the ``scope`` sub-app.

    Composes ``semantic-layer.scope.{subcommand}``: ``get`` / ``request-list``
    are ``read``, the rest are ``write``. ``set --scope organization`` is
    escalated to ``destructive`` in the command body (irreversible, widens
    visibility org-wide) -- see ``permissions.FLAG_ESCALATIONS``.
    """
    check_cli_permission(ctx, "semantic-layer.scope")


def _print_scope_status(console: Console, data: dict) -> None:
    if data.get("dry_run"):
        console.print("[yellow]Dry run -- nothing was changed.[/yellow]")
    console.print(f"[bold]scope:[/bold] {data.get('scope', 'project')}")
    targets = data.get("target_project_ids")
    if targets is not None:
        console.print(f"[bold]target_project_ids:[/bold] {targets}")
    if data.get("would_set_scope"):
        console.print(f"[bold]would set scope:[/bold] {data['would_set_scope']}")
    if data.get("would_set_target_project_ids") is not None:
        console.print(
            f"[bold]would set target_project_ids:[/bold] {data['would_set_target_project_ids']}"
        )
    pending = data.get("scope_elevation_requested_at")
    if pending:
        console.print(f"[bold]scope_elevation_requested_at:[/bold] {pending}")


def _print_request_table(console: Console, data: dict) -> None:
    items = data.get("items", [])
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
    if data.get("has_more"):
        console.print(
            f"[dim]More results: re-run with --offset {data['offset'] + data['limit']}.[/dim]"
        )


@scope_app.command("get")
def scope_get(
    ctx: typer.Context,
    project: str = _PROJECT,
    type_: ItemType = _TYPE,
    context_id: str = _CONTEXT_ID,
) -> None:
    """Show an item's current scope, target-project grants, and pending elevation."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx, service.scope_get, alias=project, kind=type_, context_id=context_id
    )
    formatter.output(result, _print_scope_status)


def _update_targets(ctx: typer.Context, project: str, type_: str, context_id: str, **delta) -> None:  # type: ignore[no-untyped-def]
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx,
        service.scope_update_targets,
        alias=project,
        kind=type_,
        context_id=context_id,
        **delta,
    )
    formatter.output(result, _print_scope_status)


def _require_targets(ctx: typer.Context, target_project: list[str]) -> None:
    if not target_project:
        get_formatter(ctx).error(
            message="--target-project is required.", error_code=ErrorCode.INVALID_ARGUMENT
        )
        raise typer.Exit(code=2)


@scope_app.command("add")
def scope_add(
    ctx: typer.Context,
    project: str = _PROJECT,
    type_: ItemType = _TYPE,
    context_id: str = _CONTEXT_ID,
    target_project: list[str] = _TARGET_PROJECT,
) -> None:
    """Add target projects to a targeted-scope item (merges with the current grants)."""
    _require_targets(ctx, target_project)
    _update_targets(ctx, project, type_, context_id, add=target_project)


@scope_app.command("remove")
def scope_remove(
    ctx: typer.Context,
    project: str = _PROJECT,
    type_: ItemType = _TYPE,
    context_id: str = _CONTEXT_ID,
    target_project: list[str] = _TARGET_PROJECT,
) -> None:
    """Remove target projects from a targeted-scope item (merges with the current grants)."""
    _require_targets(ctx, target_project)
    _update_targets(ctx, project, type_, context_id, remove=target_project)


@scope_app.command("set")
def scope_set(
    ctx: typer.Context,
    project: str = _PROJECT,
    type_: ItemType = _TYPE,
    context_id: str = _CONTEXT_ID,
    scope: ElevationScope | None = typer.Option(
        None,
        "--scope",
        help="Step the item up to organization scope. Requires org-admin; ONE-WAY, no downgrade.",
    ),
    target_project: list[str] = _TARGET_PROJECT,
    clear: bool = typer.Option(False, "--clear", help="Clear every target project."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the change without applying it."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the elevation confirmation."),
) -> None:
    """Write an item's scope: elevate to organization, or replace/clear its target projects.

    Pass exactly one of --scope organization, --target-project (replaces the
    whole list) or --clear. Elevating gives every project in the organization
    read access to the item and its full revision history, and cannot be undone.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    if scope == "organization":
        if not dry_run:
            check_cli_operation(ctx, "semantic-layer.scope.set --scope organization")
        if not (yes or dry_run or formatter.json_mode) and not typer.confirm(
            f"Elevate {type_} {context_id!r} to organization scope? This is IRREVERSIBLE "
            "and makes it visible to every project in the organization. Continue?"
        ):
            formatter.console.print("Aborted.")
            raise typer.Exit(code=0)
    result = _handle_service_call(
        ctx,
        service.scope_set,
        alias=project,
        kind=type_,
        context_id=context_id,
        scope=scope,
        target_projects=target_project,
        clear=clear,
        dry_run=dry_run,
    )
    formatter.output(result, _print_scope_status)


@scope_app.command("request-create")
def scope_request_create(
    ctx: typer.Context,
    project: str = _PROJECT,
    type_: ItemType = _TYPE,
    context_id: str = _CONTEXT_ID,
) -> None:
    """Flag a project-scoped item as awaiting an org-admin's step-up decision."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx, service.scope_request_create, alias=project, kind=type_, context_id=context_id
    )
    formatter.output(result, _print_scope_status)


@scope_app.command("request-delete")
def scope_request_delete(
    ctx: typer.Context,
    project: str = _PROJECT,
    type_: ItemType = _TYPE,
    context_id: str = _CONTEXT_ID,
) -> None:
    """Withdraw a pending scope-elevation request. Idempotent no-op if none is pending."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx, service.scope_request_delete, alias=project, kind=type_, context_id=context_id
    )
    formatter.output(result, _print_scope_status)


@scope_app.command("request-list")
def scope_request_list(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias (org-admin token)"),
    type_: ItemType = _TYPE,
    limit: int = typer.Option(DEFAULT_REQUEST_LIST_LIMIT, "--limit", min=1, help="Max results"),
    offset: int = typer.Option(0, "--offset", min=0, help="Skip this many results"),
) -> None:
    """List items of --type awaiting an org-admin's elevation decision, across the org."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx, service.scope_request_list, alias=project, kind=type_, limit=limit, offset=offset
    )
    formatter.output(result, _print_request_table)
