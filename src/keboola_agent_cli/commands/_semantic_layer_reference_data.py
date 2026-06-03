"""Typer sub-app for ``kbagent semantic-layer reference-data``.

Reference data = dimension-member records in the metastore
(``semantic-reference-data``): one record per dimension, holding the full
member list in a ``members[]`` array. The driving use case is a Chart of
Accounts (the account list + all attributes) held in the semantic layer
instead of a hardcoded Storage table.

Deliberately self-contained: reference-data is NOT AI-generated and is kept
out of ``build`` / ``export`` / ``diff`` / cascade. The four leaves here
(``list`` / ``get`` / ``set`` / ``delete``) compose the generic metastore
verbs in :class:`SemanticLayerService`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from ..errors import ErrorCode
from ._helpers import (
    check_cli_permission,
    get_formatter,
    get_service,
)
from ._semantic_layer_helpers import _handle_service_call, _is_stdin_tty

reference_data_app = typer.Typer(
    name="reference-data",
    help=(
        "Manage reference / dimension-member records (e.g. a Chart of "
        "Accounts) held in the metastore: list / get / set / delete."
    ),
    no_args_is_help=True,
)


@reference_data_app.callback(invoke_without_command=True)
def _reference_data_permission_check(ctx: typer.Context) -> None:
    """Per-leaf permission check for the ``reference-data`` sub-app.

    ``check_cli_permission`` composes ``semantic-layer.reference-data.{leaf}``
    so ``list`` / ``get`` stay ``read`` while ``set`` is ``write`` and
    ``delete`` is ``destructive`` (see permissions.OPERATION_REGISTRY).
    """
    check_cli_permission(ctx, "semantic-layer.reference-data")


def _print_reference_data_table(console: Console, data: dict) -> None:
    project = data.get("project", "")
    records = data.get("reference_data", [])
    if not records:
        console.print(f"[dim]No reference-data records in project '{project}'.[/dim]")
        return
    table = Table(title=f"Reference data in '{project}'")
    table.add_column("Dimension", style="bold cyan")
    table.add_column("UUID", style="dim")
    table.add_column("Members", justify="right")
    table.add_column("Dataset", max_width=40)
    for r in records:
        table.add_row(
            r.get("dimension_name", ""),
            r.get("id", ""),
            str(r.get("member_count", 0)),
            r.get("dataset_id") or "",
        )
    console.print(table)


def _print_reference_data_detail(console: Console, data: dict) -> None:
    console.print(
        f"[bold]{data.get('dimension_name', '')}[/bold] "
        f"([dim]{data.get('id', '')}[/dim]) — "
        f"{data.get('member_count', 0)} members, rev {data.get('revision')}"
    )
    if data.get("dataset_id"):
        console.print(f"  dataset: {data['dataset_id']}")
    members = data.get("members") or []
    preview = members[:10]
    for m in preview:
        key = m.get("account_code") or m.get("code") or "?"
        name = m.get("account_name") or m.get("name") or ""
        console.print(f"  · [cyan]{key}[/cyan] {name}")
    if len(members) > len(preview):
        console.print(f"  [dim]… and {len(members) - len(preview)} more[/dim]")


def _print_reference_data_set_result(console: Console, data: dict) -> None:
    action = str(data.get("action", "set")).capitalize()
    console.print(
        f"[bold green]{action}[/bold green] reference data "
        f"[cyan]{data.get('dimension_name', '')}[/cyan] "
        f"({data.get('member_count', 0)} members, [dim]{data.get('id', '')}[/dim])"
    )


def _print_reference_data_delete_result(console: Console, data: dict) -> None:
    removed = data.get("removed", {})
    console.print(
        f"[bold green]Removed reference data[/bold green] "
        f"[cyan]{removed.get('dimension_name', '')}[/cyan] ([dim]{removed.get('id', '')}[/dim])"
    )


def _load_members(formatter: Any, members_file: str) -> list[dict]:
    """Read a JSON array of member objects from a file or ``-`` (stdin)."""
    try:
        raw = sys.stdin.read() if members_file == "-" else Path(members_file).read_text()
    except OSError as exc:
        formatter.error(
            message=f"Could not read members file {members_file!r}: {exc}",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
        raise typer.Exit(code=2) from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        formatter.error(
            message=f"Members file is not valid JSON: {exc}",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
        raise typer.Exit(code=2) from exc
    if not isinstance(parsed, list):
        formatter.error(
            message="Members file must contain a JSON array of member objects.",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
        raise typer.Exit(code=2)
    return parsed


@reference_data_app.command("list")
def reference_data_list(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str | None = typer.Option(None, "--model", help="Filter to one model (name or UUID)"),
) -> None:
    """List reference-data records (dimension summaries; use ``get`` for members)."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx, service.list_reference_data, alias=project, model_name_or_uuid=model
    )
    formatter.output(result, _print_reference_data_table)


@reference_data_app.command("get")
def reference_data_get(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    id_: str | None = typer.Option(None, "--id", help="Record UUID"),
    dimension: str | None = typer.Option(
        None, "--dimension", help="Dimension name (project-unique; instead of --id)"
    ),
) -> None:
    """Fetch one record (all members) by ``--id`` or by ``--dimension``."""
    formatter = get_formatter(ctx)
    if id_ is not None and dimension is not None:
        formatter.error(
            message="Pass --id or --dimension, not both.",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
        raise typer.Exit(code=2)
    if id_ is None and dimension is None:
        formatter.error(
            message="Provide --id or --dimension.",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
        raise typer.Exit(code=2)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx,
        service.get_reference_data,
        alias=project,
        record_id=id_,
        dimension=dimension,
    )
    formatter.output(result, _print_reference_data_detail)


@reference_data_app.command("set")
def reference_data_set(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str | None = typer.Option(None, "--model", help="Model name or UUID"),
    dimension: str = typer.Option(
        ..., "--dimension", help="Dimension name, e.g. 'chart_of_accounts'"
    ),
    members_file: str = typer.Option(
        ...,
        "--members-file",
        help="Path to a JSON array of member objects ('-' reads stdin).",
    ),
    dataset_id: str | None = typer.Option(
        None, "--dataset-id", help="Optional tableId of the descriptive dataset (e.g. DIM_COA)"
    ),
    description: str | None = typer.Option(None, "--description", help="Optional description"),
) -> None:
    """Create or replace a reference-data record (keyed by dimension).

    Idempotent: an existing record for the same dimension (project-unique) is
    replaced in place (revision increments); otherwise a new record is created.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    members = _load_members(formatter, members_file)
    result = _handle_service_call(
        ctx,
        service.set_reference_data,
        alias=project,
        model_name_or_uuid=model,
        dimension=dimension,
        members=members,
        dataset_id=dataset_id,
        description=description,
    )
    formatter.output(result, _print_reference_data_set_result)


@reference_data_app.command("delete")
def reference_data_delete(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    id_: str = typer.Option(..., "--id", help="Record UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirm prompt"),
) -> None:
    """Delete a reference-data record by UUID (server-side soft-delete)."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    if not yes:
        if not _is_stdin_tty():
            formatter.error(
                message=f"Refusing to delete reference-data {id_!r} non-interactively without --yes.",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
            raise typer.Exit(code=2)
        if not formatter.json_mode and not typer.confirm(f"Delete reference-data record '{id_}'?"):
            formatter.console.print("Aborted.")
            raise typer.Exit(code=0)
    result = _handle_service_call(ctx, service.delete_reference_data, alias=project, record_id=id_)
    formatter.output(result, _print_reference_data_delete_result)
