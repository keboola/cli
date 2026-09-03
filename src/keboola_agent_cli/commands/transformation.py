"""Transformation commands -- create / show / edit SQL transformations.

Thin CLI layer over
:class:`keboola_agent_cli.services.transformation_service.TransformationService`
(issue #396: native port of the MCP server's create_sql_transformation /
update_sql_transformation tools). No business logic belongs here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape

from ..config_store import ConfigStore
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..services.transformation_service import TransformationService
from ._helpers import (
    check_cli_permission,
    get_formatter,
    map_error_to_exit_code,
    parse_json_arg,
    resolve_branch,
    resolve_project_alias,
)

# Max characters of SQL shown per code in human mode (MCP structure_summary parity).
SQL_SNIPPET_MAX_CHARS = 150

transformation_app = typer.Typer(
    help="SQL transformations - create, inspect, and edit blocks/codes"
)


@transformation_app.callback(invoke_without_command=True)
def _transformation_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "transformation")


def _get_transformation_service(ctx: typer.Context) -> TransformationService:
    """Fetch the TransformationService from ctx.obj, constructing lazily.

    Falls back to building the service from the shared ConfigStore so the
    command group works even before cli.py registers a dedicated
    ``transformation_service`` entry.
    """
    service = ctx.obj.get("transformation_service")
    if service is None:
        service = TransformationService(config_store=ctx.obj["config_store"])
        ctx.obj["transformation_service"] = service
    return service


def _render_blocks_human(console: Console, data: dict[str, Any]) -> None:
    """Render the block/code tree with synthetic IDs and SQL snippets."""
    name = data.get("name") or ""
    header = f"[bold]{escape(name)}[/bold]" if name else "[bold](unnamed)[/bold]"
    console.print(f"{header}  {data.get('component_id', '')} / {data.get('config_id', '')}")

    blocks = data.get("blocks") or []
    if not blocks:
        console.print("[dim]No blocks in this transformation.[/dim]")
    for block in blocks:
        console.print(f"[cyan]{block['id']}[/cyan]  block: {escape(block.get('name', ''))}")
        codes = block.get("codes") or []
        if not codes:
            console.print("  [dim]no codes[/dim]")
        for code in codes:
            statement_count = len(code.get("script") or [])
            plural = "s" if statement_count != 1 else ""
            console.print(
                f"  [green]{code['id']}[/green]  code: {escape(code.get('name', ''))} "
                f"({statement_count} statement{plural})"
            )
            snippet = (code.get("script_text") or "").strip()
            if snippet:
                if len(snippet) > SQL_SNIPPET_MAX_CHARS:
                    truncated = len(snippet) - SQL_SNIPPET_MAX_CHARS
                    snippet = snippet[:SQL_SNIPPET_MAX_CHARS] + f"... ({truncated} chars truncated)"
                console.print(f"    [dim]{escape(snippet)}[/dim]")

    storage = data.get("storage") or {}
    input_tables = (storage.get("input") or {}).get("tables") or []
    output_tables = (storage.get("output") or {}).get("tables") or []
    if input_tables or output_tables:
        console.print(
            f"[dim]storage: {len(input_tables)} input table(s), "
            f"{len(output_tables)} output table(s)[/dim]"
        )


@transformation_app.command("create")
def transformation_create(
    ctx: typer.Context,
    project: str | None = typer.Option(None, "--project", help="Project alias"),
    name: str = typer.Option(..., "--name", help="Transformation name"),
    sql: str | None = typer.Option(
        None,
        "--sql",
        help="SQL text (semicolon-separated statements). Mutually exclusive with --sql-file.",
    ),
    sql_file: Path | None = typer.Option(
        None,
        "--sql-file",
        help="Read SQL from a file. Mutually exclusive with --sql.",
    ),
    created_table: list[str] | None = typer.Option(
        None,
        "--created-table",
        help=(
            "Table name created by the SQL (repeatable). Each is mapped to "
            "out.c-<derived-bucket>.<table> in the output mapping."
        ),
    ),
    component_id: str | None = typer.Option(
        None,
        "--component-id",
        help=(
            "SQL transformation component ID (keboola.snowflake-transformation or "
            "keboola.google-bigquery-transformation). Default: derived from the "
            "project's default backend."
        ),
    ),
    description: str = typer.Option("", "--description", help="Configuration description"),
    branch: int | None = typer.Option(
        None, "--branch", help="Create in a specific dev branch ID (defaults to active branch)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the would-be configuration payload without creating"
    ),
) -> None:
    """Create a SQL transformation from a SQL script.

    The SQL is split into one statement per script element (Keboola runtime
    requirement) and stored as a single block "Blocks" with one code "Code".
    Each --created-table T is mapped to out.c-<bucket>.<T>, where <bucket>
    is derived from the transformation name (diacritics stripped, spaces
    to dashes -- same rule as the Keboola UI and MCP server).

    \b
    Examples:
      kbagent transformation create --project prod --name "Orders Report" \\
          --sql 'CREATE TABLE "report" AS SELECT * FROM "orders";' --created-table report
      kbagent transformation create --project prod --name Cleanup --sql-file ./cleanup.sql --dry-run
    """
    formatter = get_formatter(ctx)
    service = _get_transformation_service(ctx)
    config_store: ConfigStore = ctx.obj["config_store"]

    if (sql is None) == (sql_file is None):
        formatter.error(
            message="Provide exactly one of --sql or --sql-file.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None

    if sql_file is not None:
        if not sql_file.is_file():
            formatter.error(
                message=f"SQL file not found: {sql_file}",
                error_code=ErrorCode.FILE_NOT_FOUND,
            )
            raise typer.Exit(code=2) from None
        sql = sql_file.read_text(encoding="utf-8")

    alias = resolve_project_alias(ctx, formatter, project)
    _, branch_id = resolve_branch(config_store, formatter, alias, branch)

    try:
        result = service.create(
            alias,
            name=name,
            sql=sql or "",
            created_tables=created_table,
            component_id=component_id,
            description=description,
            branch_id=branch_id,
            dry_run=dry_run,
        )
    except ValueError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.VALIDATION_ERROR)
        raise typer.Exit(code=1) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    def _human(console: Console, data: dict[str, Any]) -> None:
        if data.get("dry_run"):
            console.print(
                "[bold yellow]Dry run[/bold yellow] - configuration that would be created "
                f"under [cyan]{data['component_id']}[/cyan]:"
            )
            console.print_json(json.dumps(data["configuration"]))
            return
        console.print(
            f"Created transformation [bold]{escape(data['name'])}[/bold] "
            f"(config id [cyan]{data['config_id']}[/cyan], "
            f"component {data['component_id']}, version {data.get('version')})"
        )
        output_tables = data["configuration"]["storage"]["output"]["tables"]
        for table in output_tables:
            console.print(f"  output: {table['source']} -> {table['destination']}")

    formatter.output(result, _human)


@transformation_app.command("show")
def transformation_show(
    ctx: typer.Context,
    project: str | None = typer.Option(None, "--project", help="Project alias"),
    config_id: str = typer.Option(..., "--config-id", help="Configuration ID"),
    component_id: str | None = typer.Option(
        None,
        "--component-id",
        help=(
            "Component ID. When omitted, the known SQL transformation components "
            "are tried until the configuration is found."
        ),
    ),
    branch: int | None = typer.Option(
        None, "--branch", help="Read from a specific dev branch ID (defaults to active branch)"
    ),
) -> None:
    """Show a SQL transformation's block/code tree with positional IDs.

    Blocks get synthetic IDs b0, b1, ...; codes get b0.c0, b0.c1, ...
    (derived from position, matching the MCP server). Use these IDs with
    'kbagent transformation edit --op'.
    """
    formatter = get_formatter(ctx)
    service = _get_transformation_service(ctx)
    config_store: ConfigStore = ctx.obj["config_store"]

    alias = resolve_project_alias(ctx, formatter, project)
    _, branch_id = resolve_branch(config_store, formatter, alias, branch)

    try:
        result = service.show(
            alias,
            config_id=config_id,
            component_id=component_id,
            branch_id=branch_id,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    formatter.output(result, _render_blocks_human)


@transformation_app.command("edit")
def transformation_edit(
    ctx: typer.Context,
    project: str | None = typer.Option(None, "--project", help="Project alias"),
    config_id: str = typer.Option(..., "--config-id", help="Configuration ID"),
    component_id: str | None = typer.Option(
        None,
        "--component-id",
        help=(
            "Component ID. When omitted, the known SQL transformation components "
            "are tried until the configuration is found."
        ),
    ),
    branch: int | None = typer.Option(
        None, "--branch", help="Edit in a specific dev branch ID (defaults to active branch)"
    ),
    change_description: str = typer.Option(
        ...,
        "--change-description",
        help="Human-readable summary of this change (stored in config version history)",
    ),
    op: list[str] | None = typer.Option(
        None,
        "--op",
        help=(
            "Operation as inline JSON (repeatable, applied in order). Ops: "
            "add_block, remove_block, rename_block, add_code, remove_code, "
            "rename_code, set_code, add_script, str_replace. Example: "
            '\'{"op": "set_code", "block_id": "b0", "code_id": "b0.c0", '
            '"script": "SELECT 1;"}\'. IDs come from `transformation show`. '
            "Mutually exclusive with --op-file."
        ),
    ),
    op_file: Path | None = typer.Option(
        None,
        "--op-file",
        help="Read operations from a JSON file containing an array of op objects.",
    ),
    storage: str | None = typer.Option(
        None,
        "--storage",
        help=(
            "FULL REPLACEMENT of configuration.storage (inline JSON, @file, or - "
            "for stdin). Include every input/output mapping you want to keep -- "
            "the existing storage block is overwritten wholesale."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Apply ops locally and print the resulting tree without writing",
    ),
) -> None:
    """Edit a SQL transformation's blocks/codes with positional operations.

    Operations apply sequentially against the structure as fetched: IDs
    (b0, b0.c0, ...) refer to positions at the start of the batch, and
    elements added within the batch are not addressable until the next
    invocation. Run 'kbagent transformation show' first to get IDs.

    \b
    Examples:
      kbagent transformation edit --project prod --config-id 123 \\
          --change-description "Filter active" \\
          --op '{"op": "str_replace", "search_for": "orders", "replace_with": "orders_active"}'
      kbagent transformation edit --project prod --config-id 123 \\
          --change-description "Restructure" --op-file ops.json --dry-run
    """
    formatter = get_formatter(ctx)
    service = _get_transformation_service(ctx)
    config_store: ConfigStore = ctx.obj["config_store"]

    if op and op_file is not None:
        formatter.error(
            message="Use either --op or --op-file, not both.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None
    if not op and op_file is None and storage is None:
        formatter.error(
            message="Nothing to do: provide --op/--op-file and/or --storage.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None

    try:
        raw_ops = _collect_ops(op, op_file)
        storage_payload = (
            parse_json_arg(storage, label="--storage") if storage is not None else None
        )
    except ValueError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.INPUT_ERROR)
        raise typer.Exit(code=2) from None

    if storage_payload is not None and not isinstance(storage_payload, dict):
        formatter.error(
            message="--storage must be a JSON object.",
            error_code=ErrorCode.INPUT_ERROR,
        )
        raise typer.Exit(code=2) from None

    alias = resolve_project_alias(ctx, formatter, project)
    _, branch_id = resolve_branch(config_store, formatter, alias, branch)

    try:
        result = service.edit(
            alias,
            config_id=config_id,
            ops=raw_ops,
            change_description=change_description,
            component_id=component_id,
            storage=storage_payload,
            branch_id=branch_id,
            dry_run=dry_run,
        )
    except ValueError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.VALIDATION_ERROR)
        raise typer.Exit(code=1) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    def _human(console: Console, data: dict[str, Any]) -> None:
        if data.get("dry_run"):
            console.print("[bold yellow]Dry run[/bold yellow] - no changes written.")
        for message in data.get("operations_applied") or []:
            console.print(f"  - {escape(message)}")
        if data.get("storage_replaced"):
            console.print("  - Replaced configuration.storage wholesale")
        _render_blocks_human(console, data)
        if not data.get("dry_run"):
            console.print(
                f"Updated config [cyan]{data['config_id']}[/cyan] to version {data.get('version')}"
            )

    formatter.output(result, _human)


def _collect_ops(op: list[str] | None, op_file: Path | None) -> list[dict[str, Any]]:
    """Collect raw op dicts from repeated --op JSON strings or --op-file.

    Raises:
        ValueError: On malformed JSON, missing file, or non-object entries.
    """
    raw_ops: list[dict[str, Any]] = []
    if op_file is not None:
        parsed = parse_json_arg(f"@{op_file}", label="--op-file")
        if not isinstance(parsed, list):
            raise ValueError("--op-file must contain a JSON array of operation objects")
        entries = parsed
    else:
        entries = [parse_json_arg(item, label="--op") for item in op or []]

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"Operation #{index} is not a JSON object: {entry!r}")
        raw_ops.append(entry)
    return raw_ops
