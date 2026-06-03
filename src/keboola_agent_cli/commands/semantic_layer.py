"""Typer wrappers for the ``kbagent semantic-layer`` command group.

Thin layer per the 3-layer architecture: parse arguments, call
:class:`SemanticLayerService`, format output. All business logic lives in the
service layer.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ..errors import ErrorCode
from ._helpers import (
    check_cli_permission,
    emit_hint,
    get_formatter,
    get_service,
    should_hint,
)
from ._semantic_layer_crud import add_app, edit_app, remove_app
from ._semantic_layer_helpers import _handle_service_call
from ._semantic_layer_reference_data import reference_data_app

semantic_layer_app = typer.Typer(
    name="semantic-layer",
    help=(
        "Manage Keboola semantic layer (metastore) models -- datasets, metrics, "
        "relationships, constraints, and glossary terms."
    ),
    no_args_is_help=True,
)


@semantic_layer_app.callback(invoke_without_command=True)
def _semantic_layer_permission_check(ctx: typer.Context) -> None:
    """Enforce permission policy for every semantic-layer subcommand."""
    check_cli_permission(ctx, "semantic-layer")


# ---------------------------------------------------------------------------
# semantic-layer model -- model lifecycle (list)
# ---------------------------------------------------------------------------

model_app = typer.Typer(
    name="model",
    help="Manage semantic-layer models (list models in a project).",
    no_args_is_help=True,
)
semantic_layer_app.add_typer(model_app, name="model")


@model_app.callback(invoke_without_command=True)
def _model_permission_check(ctx: typer.Context) -> None:
    """Per-subcommand permission check for the ``model`` sub-app.

    Uses the standard ``check_cli_permission`` helper which composes the
    operation key as ``"{group}.{subcommand}"`` — so we get
    ``semantic-layer.model.list`` (read), ``semantic-layer.model.create``
    (write), and ``semantic-layer.model.delete`` (destructive). A collapsed
    single ``semantic-layer.model`` key would deny `model list` under
    ``--deny-writes`` even though it's read-only.
    """
    check_cli_permission(ctx, "semantic-layer.model")


def _print_models_table(console: Console, data: dict) -> None:
    """Pretty-print the list of models for a project."""
    project = data.get("project", "")
    models = data.get("models", [])
    if not models:
        console.print(f"[dim]No semantic-layer models in project '{project}'.[/dim]")
        return
    table = Table(title=f"Semantic-layer models in '{project}'")
    table.add_column("Name", style="bold cyan")
    table.add_column("UUID", style="dim")
    table.add_column("SQL Dialect")
    table.add_column("Description", max_width=60)
    for m in models:
        table.add_row(
            m.get("name", ""),
            m.get("id", ""),
            m.get("sql_dialect", ""),
            m.get("description", ""),
        )
    console.print(table)


@model_app.command("list")
def model_list(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
) -> None:
    """List all semantic-layer models in a project."""
    if should_hint(ctx):
        emit_hint(ctx, "semantic-layer.model.list", project=project)
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(ctx, service.list_models, alias=project)
    formatter.output(result, _print_models_table)


@model_app.command("create")
def model_create(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    name: str = typer.Option(..., "--name", help="Model name (unique within project)"),
    description: str = typer.Option("", "--description", help="Optional description"),
    sql_dialect: str = typer.Option(
        "Snowflake", "--sql-dialect", help="SQL dialect (default: Snowflake)"
    ),
) -> None:
    """Create a new semantic-layer model."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.model.create",
            project=project,
            name=name,
            description=description,
            sql_dialect=sql_dialect,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx,
        service.create_model,
        alias=project,
        name=name,
        description=description,
        sql_dialect=sql_dialect,
    )
    formatter.output(
        result,
        lambda c, d: c.print(
            f"[bold green]Created model[/bold green] [cyan]{d['model']['attributes']['name']}[/cyan] "
            f"([dim]{d['model']['id']}[/dim])"
        ),
    )


# ---------------------------------------------------------------------------
# semantic-layer add | edit | remove -- mounted from a sibling module to keep
# this commands file under the CONTRIBUTING.md hard ceiling. See
# :mod:`commands._semantic_layer_crud` for the sub-app implementations.
# ---------------------------------------------------------------------------

semantic_layer_app.add_typer(add_app, name="add")
semantic_layer_app.add_typer(edit_app, name="edit")
semantic_layer_app.add_typer(remove_app, name="remove")
semantic_layer_app.add_typer(reference_data_app, name="reference-data")


@model_app.command("delete")
def model_delete(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str = typer.Option(..., "--model", help="Model name or UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Delete a semantic-layer model and cascade-delete its children."""
    if should_hint(ctx):
        emit_hint(ctx, "semantic-layer.model.delete", project=project, model=model)
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")

    if (
        not yes
        and not formatter.json_mode
        and not typer.confirm(
            f"Delete model '{model}' in project '{project}'? "
            "This cascade-deletes ALL child entities (datasets, metrics, "
            "relationships, constraints, glossary terms) belonging to the model. "
            "This is irreversible."
        )
    ):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)

    result = _handle_service_call(
        ctx,
        service.delete_model,
        alias=project,
        model_name_or_uuid=model,
    )

    def _render(console: Console, data: dict) -> None:
        cascaded = sum(data.get("cascade", {}).get("deleted", {}).values())
        suffix = f" + cascaded {cascaded} child(ren)" if cascaded else ""
        console.print(
            f"[bold green]Deleted model[/bold green] "
            f"[cyan]{data['deleted']['name']}[/cyan] "
            f"([dim]{data['deleted']['id']}[/dim]){suffix}"
        )

    formatter.output(result, _render)


# ---------------------------------------------------------------------------
# semantic-layer show
# ---------------------------------------------------------------------------


def _print_show_summary(console: Console, data: dict) -> None:
    """Render the show payload (no --type) as a compact count table."""
    project = data.get("project", "")
    model = data.get("model", {})
    console.print(
        f"\n[bold]Model[/bold] [cyan]{model.get('name', '?')}[/cyan] "
        f"([dim]{model.get('id', '')}[/dim]) in project [magenta]{project}[/magenta]:"
    )
    table = Table()
    table.add_column("Entity", style="bold cyan")
    table.add_column("Count", justify="right")
    for label, key in (
        ("datasets", "datasets"),
        ("metrics", "metrics"),
        ("relationships", "relationships"),
        ("constraints", "constraints"),
        ("glossary", "glossary"),
    ):
        if key in data:
            table.add_row(label, str(len(data.get(key, []))))
    console.print(table)


def _print_show_detail(console: Console, data: dict) -> None:
    """Render the show payload with --type as a per-item table."""
    for type_key in ("datasets", "metrics", "relationships", "constraints", "glossary"):
        if type_key not in data:
            continue
        items = data[type_key]
        if not items:
            console.print(f"[dim]No {type_key} in this model.[/dim]")
            continue
        table = Table(title=type_key.capitalize())
        # Column layout differs per type, but a generic name/id/key-attrs
        # rendering keeps the table compact and is enough for `show`.
        keys = list(items[0].keys())
        # Drop modelUUID -- noisy, present on every item
        keys = [k for k in keys if k != "modelUUID"]
        for col in keys:
            table.add_column(col)
        for item in items:
            table.add_row(*(str(item.get(c, "")) for c in keys))
        console.print(table)


# ---------------------------------------------------------------------------
# semantic-layer import -- replay a snapshot
# ---------------------------------------------------------------------------


def _print_import_result(console: Console, data: dict) -> None:
    console.print(
        f"\n[bold]Import[/bold] into [magenta]{data.get('target_project')}[/magenta]"
        f" (model {data.get('target_model')}, "
        f"dry_run={data.get('dry_run')}, overwrite={data.get('overwrite')}):"
    )
    table = Table()
    table.add_column("Type", style="bold cyan")
    table.add_column("Created", justify="right", style="green")
    table.add_column("Skipped", justify="right", style="yellow")
    table.add_column("Overwritten", justify="right", style="magenta")
    table.add_column("Failed", justify="right", style="red")
    for plural in ("datasets", "metrics", "relationships", "glossary", "constraints"):
        per = (data.get("imported") or {}).get(plural)
        if per is None:
            continue
        table.add_row(
            plural,
            str(per.get("created", 0)),
            str(per.get("skipped", 0)),
            str(per.get("overwritten", 0)),
            str(len(per.get("failed", []))),
        )
    console.print(table)
    for plural, per in (data.get("imported") or {}).items():
        for f in per.get("failed", []):
            console.print(f"  [red]✗ {plural}.{f.get('name')}: {f.get('reason')}[/red]")


# ---------------------------------------------------------------------------
# semantic-layer promote -- cross-project copy
# ---------------------------------------------------------------------------


def _print_promote_result(console: Console, data: dict) -> None:
    console.print(
        f"\n[bold]Promote[/bold] [magenta]{data.get('from_project')}[/magenta] "
        f"-> [magenta]{data.get('to_project')}[/magenta] "
        f"(dry_run={data.get('dry_run')}):"
    )
    table = Table()
    table.add_column("Type", style="bold cyan")
    table.add_column("New", justify="right", style="green")
    table.add_column("Overwritten", justify="right", style="magenta")
    table.add_column("Identical", justify="right", style="dim")
    table.add_column("Failed", justify="right", style="red")
    for plural in ("datasets", "metrics", "relationships", "glossary", "constraints"):
        per = data.get(plural)
        if per is None:
            continue
        table.add_row(
            plural,
            str(per.get("new", 0)),
            str(per.get("overwritten", 0)),
            str(per.get("identical", 0)),
            str(len(per.get("failed", []))),
        )
    console.print(table)
    for plural in ("datasets", "metrics", "relationships", "glossary", "constraints"):
        per = data.get(plural) or {}
        for c in per.get("changes", []):
            key = c.get("name", c.get("term", "?"))
            keys = ", ".join(c.get("diff_keys", []))
            console.print(f"  [yellow]~ {plural}.{key}[/yellow] ([dim]{keys}[/dim])")
        for f in per.get("failed", []):
            console.print(f"  [red]✗ {plural}.{f.get('name')}: {f.get('reason')}[/red]")


# ---------------------------------------------------------------------------
# semantic-layer build -- non-interactive greenfield
# ---------------------------------------------------------------------------


def _print_build_result(console: Console, data: dict) -> None:
    fallback = data.get("fallback_used")
    valid = data.get("validated", False)
    console.print(
        f"\n[bold]Build[/bold] (mode=[cyan]{fallback}[/cyan], dry_run={data.get('dry_run')})"
    )
    val = data.get("validation") or {}
    errs = val.get("errors", [])
    warns = val.get("warnings", [])
    console.print(f"  Datasets:      {len(data.get('generated', {}).get('datasets', []))}")
    console.print(f"  Metrics:       {len(data.get('generated', {}).get('metrics', []))}")
    console.print(f"  Relationships: {len(data.get('generated', {}).get('relationships', []))}")
    console.print(f"  Constraints:   {len(data.get('generated', {}).get('constraints', []))}")
    console.print(f"  Glossary:      {len(data.get('generated', {}).get('glossary', []))}")
    if data.get("output_path"):
        console.print(f"\nWritten to: [cyan]{data['output_path']}[/cyan]")
    if errs:
        console.print(f"\n[bold red]Validation: {len(errs)} error(s)[/bold red]")
        for e in errs:
            console.print(f"  [red]✗[/red] {e['type']} {e['item']} — {e['detail']}")
    if warns:
        console.print(f"\n[bold yellow]Validation: {len(warns)} warning(s)[/bold yellow]")
    if data.get("created"):
        console.print(f"\nCreated: {data['created']}")
    elif data.get("dry_run"):
        console.print("\n[dim]--dry-run: no API calls were made.[/dim]")
    elif valid:
        console.print("\n[bold green]Pushed.[/bold green]")


# ---------------------------------------------------------------------------
# semantic-layer token --encrypt
# ---------------------------------------------------------------------------


@semantic_layer_app.command("token")
def semantic_layer_token(
    ctx: typer.Context,
    encrypt: bool = typer.Option(
        False, "--encrypt", help="Encrypt the project token for `user_properties` (required)"
    ),
    project: str = typer.Option(..., "--project", help="Project alias"),
    component_id: str = typer.Option(
        ...,
        "--component-id",
        help="Keboola component id the encrypted token will be used in",
    ),
) -> None:
    """Encrypt the project's storage token for transformation `user_properties`.

    Builds the ``{"#metastore_token": <token>}`` payload using the
    project's already-stored Storage API token (no config-file digging),
    then delegates to the existing EncryptService. Output (human) is the
    raw envelope ready to paste into `user_properties`; JSON mode emits
    the full `{encrypted, component_id, project}` response.
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.token",
            project=project,
            component_id=component_id,
        )
        return
    formatter = get_formatter(ctx)
    if not encrypt:
        formatter.error(
            message=(
                "Currently only --encrypt mode is supported. "
                "Pass --encrypt to encrypt the project token for user_properties."
            ),
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2)

    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx,
        service.encrypt_token,
        alias=project,
        component_id=component_id,
    )

    def _print_encrypted_token(console: Console, data: dict) -> None:
        console.print(
            "\n[bold green]Encrypted token[/bold green] "
            f"for component [cyan]{data['component_id']}[/cyan] "
            f"in project [magenta]{data['project']}[/magenta]:"
        )
        console.print_json(json.dumps(data["encrypted"], indent=2))
        console.print(
            "[dim]Paste the JSON above into the transformation's `user_properties` block.[/dim]"
        )

    formatter.output(result, _print_encrypted_token)


@semantic_layer_app.command("build")
def semantic_layer_build(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Target project alias"),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Update this existing model (name or UUID). If omitted, a new model is created.",
    ),
    tables: str | None = typer.Option(
        None,
        "--tables",
        help="Comma-separated tableIds to base the model on (required).",
    ),
    name: str | None = typer.Option(
        None, "--name", help="Model name when creating (default: kbagent_build_model)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the generated JSON + validation, no POST."
    ),
    keep_on_failure: bool = typer.Option(
        False,
        "--keep-on-failure",
        help=(
            "Keep partially-pushed model + children on failure (forensics). "
            "Default: rollback in reverse PUSH_ORDER + delete the model if "
            "we created it."
        ),
    ),
    output: Path | None = typer.Option(
        None, "--output", help="Also write the generated JSON to this file."
    ),
) -> None:
    """Build a semantic-layer model from a list of storage tables (non-interactive).

    NOTE: The AI Service client currently has no JSON-generation endpoint, so
    this command falls back to a DETERMINISTIC HEURISTIC builder (one dataset
    + one COUNT(*) metric + one glossary entry per table; no relationships;
    no constraints). The intent is "best starting point" — iterate with
    `add` / `edit`. The fallback is logged in the response as
    `fallback_used: "heuristic"` so callers can detect it.
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.build",
            project=project,
            model=model,
            tables=tables or "",
            name=name,
            dry_run=dry_run,
            keep_on_failure=keep_on_failure,
            output=str(output) if output else "",
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")

    if not tables:
        formatter.error(
            message="--tables is required (comma-separated tableIds).",
            error_code=ErrorCode.MISSING_PARAMETER,
        )
        raise typer.Exit(code=2)
    table_ids = [t.strip() for t in tables.split(",") if t.strip()]
    if not table_ids:
        formatter.error(
            message="--tables contained no tableIds.",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
        raise typer.Exit(code=2)

    result = _handle_service_call(
        ctx,
        service.build_model,
        alias=project,
        table_ids=table_ids,
        model_name=name,
        model_name_or_uuid=model,
        dry_run=dry_run,
        keep_on_failure=keep_on_failure,
        output_path=output,
    )
    formatter.output(result, _print_build_result)


@semantic_layer_app.command("promote")
def semantic_layer_promote(
    ctx: typer.Context,
    from_project: str = typer.Option(..., "--from-project", help="Source project alias"),
    to_project: str = typer.Option(..., "--to-project", help="Target project alias"),
    from_model: str | None = typer.Option(
        None, "--from-model", help="Source model name or UUID (defaults to sole)"
    ),
    to_model: str | None = typer.Option(
        None, "--to-model", help="Target model name or UUID (defaults to sole)"
    ),
    types: str | None = typer.Option(
        None,
        "--types",
        help="Comma-separated subset (datasets,metrics,relationships,glossary,constraints)",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Classify NEW/IDENTICAL/CHANGED without writing"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the cross-project confirmation prompt"
    ),
) -> None:
    """Promote a model from one project to another (NEW + overwrite CHANGED; never deletes).

    Default behaviour: NEW items are POSTed, CHANGED items are
    DELETE+POSTed, IDENTICAL items are skipped. Items only present in
    the target are never touched (additive-only).
    """
    if should_hint(ctx):
        # Use `from_project` to resolve the hint stack URL.
        emit_hint(
            ctx,
            "semantic-layer.promote",
            project=from_project,
            from_project=from_project,
            to_project=to_project,
            from_model=from_model,
            to_model=to_model,
            types=types,
            dry_run=dry_run,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    type_list = [t.strip() for t in types.split(",") if t.strip()] if types else None

    if (
        not yes
        and not dry_run
        and not formatter.json_mode
        and not typer.confirm(
            f"Promote model from '{from_project}' to '{to_project}'? "
            "This will overwrite CHANGED items in the target."
        )
    ):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)

    result = _handle_service_call(
        ctx,
        service.promote_model,
        from_project=from_project,
        to_project=to_project,
        from_model=from_model,
        to_model=to_model,
        types=type_list,
        dry_run=dry_run,
    )
    formatter.output(result, _print_promote_result)


@semantic_layer_app.command("import")
def semantic_layer_import(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Target project alias"),
    file: Path = typer.Option(
        ..., "--file", help="Snapshot JSON file (output of `semantic-layer export`)"
    ),
    model: str | None = typer.Option(
        None, "--model", help="Target model name or UUID (defaults to the sole model)"
    ),
    types: str | None = typer.Option(
        None,
        "--types",
        help=(
            "Comma-separated subset to import: datasets,metrics,relationships,glossary,constraints"
        ),
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Plan the import without calling any write API"
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="DELETE+POST conflicting items (default: skip)"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip confirmation (alias for default SKIP behavior)"
    ),
) -> None:
    """Replay a snapshot into a project. Default: skip on conflict (no surprise overwrites)."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.import",
            project=project,
            file=str(file),
            model=model,
            types=types,
            dry_run=dry_run,
            overwrite=overwrite,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    # --yes is the alias for the default skip-on-conflict mode; users can
    # still opt into destructive overwrite via --overwrite.
    _ = yes  # explicit (no behavioural effect when --overwrite is False)
    type_list = [t.strip() for t in types.split(",") if t.strip()] if types else None
    result = _handle_service_call(
        ctx,
        service.import_snapshot,
        alias=project,
        file=file,
        model_name_or_uuid=model,
        types=type_list,
        dry_run=dry_run,
        overwrite=overwrite,
    )
    formatter.output(result, _print_import_result)


@semantic_layer_app.command("show")
def semantic_layer_show(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Model name or UUID. Optional when the project has a single model.",
    ),
    type_filter: str | None = typer.Option(
        None,
        "--type",
        help=(
            "Filter to one entity type: dataset | metric | relationship | constraint | glossary."
        ),
    ),
) -> None:
    """Show the entities in a semantic-layer model."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.show",
            project=project,
            model=model,
            type=type_filter,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx,
        service.show_model,
        alias=project,
        model_name_or_uuid=model,
        type_filter=type_filter,
    )
    if type_filter is None:
        formatter.output(result, _print_show_summary)
    else:
        formatter.output(result, _print_show_detail)


# ---------------------------------------------------------------------------
# semantic-layer validate [--deep]
# ---------------------------------------------------------------------------


def _print_validate(console: Console, data: dict) -> None:
    project = data.get("project", "")
    model = data.get("model", {})
    deep = data.get("deep", False)
    valid = data.get("valid", False)
    console.print(
        f"\n[bold]Validation[/bold] for model [cyan]{model.get('name', '?')}[/cyan] "
        f"in [magenta]{project}[/magenta]"
        f" ({'deep' if deep else 'basic'}):"
    )
    errs = data.get("errors", [])
    warns = data.get("warnings", [])
    if errs:
        console.print(f"\n[bold red]Errors ({len(errs)}):[/bold red]")
        for e in errs:
            console.print(f"  [red]✗[/red] [bold]{e['type']}[/bold] {e['item']} — {e['detail']}")
    if warns:
        console.print(f"\n[bold yellow]Warnings ({len(warns)}):[/bold yellow]")
        for w in warns:
            console.print(
                f"  [yellow]![/yellow] [bold]{w['type']}[/bold] {w['item']} — {w['detail']}"
            )
    if valid and not warns:
        console.print("\n[bold green]Model is clean.[/bold green]")
    elif valid:
        console.print(f"\n[bold green]Model is valid[/bold green] (with {len(warns)} warning(s)).")
    else:
        console.print(f"\n[bold red]Model has {len(errs)} error(s).[/bold red]")


# ---------------------------------------------------------------------------
# semantic-layer export
# ---------------------------------------------------------------------------


def _print_export(console: Console, data: dict) -> None:
    counts = data.get("counts", {})
    console.print(f"\n[bold green]Exported model[/bold green] to: [cyan]{data['path']}[/cyan]")
    table = Table()
    table.add_column("Entity", style="bold cyan")
    table.add_column("Count", justify="right")
    for key in ("datasets", "metrics", "relationships", "constraints", "glossary"):
        table.add_row(key, str(counts.get(key, 0)))
    console.print(table)


@semantic_layer_app.command("export")
def semantic_layer_export(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str | None = typer.Option(
        None, "--model", help="Model name or UUID (optional if project has one model)."
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        help=("Output JSON path. Defaults to ./sl_export_{model_name}_{YYYYMMDD_HHMMSS}.json."),
    ),
) -> None:
    """Snapshot a semantic-layer model to a self-describing JSON file."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.export",
            project=project,
            model=model,
            output=str(output) if output else "",
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx,
        service.export_model,
        alias=project,
        model_name_or_uuid=model,
        output_path=output,
    )
    formatter.output(result, _print_export)


# ---------------------------------------------------------------------------
# semantic-layer diff
# ---------------------------------------------------------------------------


def _print_diff(console: Console, data: dict) -> None:
    left = data.get("left", {})
    right = data.get("right", {})
    console.print(
        f"\n[bold]Diff[/bold] left=[cyan]{left.get('source')}[/cyan]:[magenta]"
        f"{left.get('ref')}[/magenta] "
        f"right=[cyan]{right.get('source')}[/cyan]:[magenta]{right.get('ref')}[/magenta]"
    )
    for type_key in ("datasets", "metrics", "relationships", "constraints", "glossary"):
        per = data.get(type_key, {})
        added = per.get("added", [])
        removed = per.get("removed", [])
        changed = per.get("changed", [])
        if not (added or removed or changed):
            continue
        console.print(f"\n[bold cyan]{type_key}[/bold cyan]:")
        for name in added:
            console.print(f"  [green]+ {name}[/green]")
        for name in removed:
            console.print(f"  [red]- {name}[/red]")
        for c in changed:
            key = c.get("name", c.get("term", "?"))
            keys = ", ".join(c.get("diff_keys", []))
            console.print(f"  [yellow]~ {key}[/yellow] ([dim]{keys}[/dim])")


@semantic_layer_app.command("diff")
def semantic_layer_diff(
    ctx: typer.Context,
    project_a: str | None = typer.Option(None, "--project-a", help="Left side: project alias"),
    project_b: str | None = typer.Option(None, "--project-b", help="Right side: project alias"),
    model_a: str | None = typer.Option(
        None, "--model-a", help="Left side: model name/UUID (when --project-a is set)"
    ),
    model_b: str | None = typer.Option(
        None, "--model-b", help="Right side: model name/UUID (when --project-b is set)"
    ),
    file_a: Path | None = typer.Option(
        None,
        "--file-a",
        help="Left side: snapshot JSON path (mutually exclusive with --project-a)",
    ),
    file_b: Path | None = typer.Option(
        None,
        "--file-b",
        help="Right side: snapshot JSON path (mutually exclusive with --project-b)",
    ),
) -> None:
    """Diff two semantic-layer snapshots (project↔project, project↔file, file↔file).

    Pass exactly one of ``--project-a`` / ``--file-a`` and one of
    ``--project-b`` / ``--file-b``. Output groups changes per entity type:
    ``added``, ``removed``, ``changed`` (with ``diff_keys`` listing the
    attribute fields that differ).
    """
    if should_hint(ctx):
        # `project` resolves the hint stack URL; prefer A, fall back to B.
        hint_project = project_a or project_b
        emit_hint(
            ctx,
            "semantic-layer.diff",
            project=hint_project,
            project_a=project_a,
            project_b=project_b,
            model_a=model_a,
            model_b=model_b,
            file_a=str(file_a) if file_a else "",
            file_b=str(file_b) if file_b else "",
        )
        return
    formatter = get_formatter(ctx)

    # Mutual exclusion: exactly one source per side.
    if (project_a is None) == (file_a is None):
        formatter.error(
            message="Specify exactly one of --project-a or --file-a.",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2)
    if (project_b is None) == (file_b is None):
        formatter.error(
            message="Specify exactly one of --project-b or --file-b.",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2)

    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx,
        service.diff,
        project_a=project_a,
        project_b=project_b,
        model_a=model_a,
        model_b=model_b,
        file_a=file_a,
        file_b=file_b,
    )
    formatter.output(result, _print_diff)


@semantic_layer_app.command("validate")
def semantic_layer_validate(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str | None = typer.Option(
        None, "--model", help="Model name or UUID (optional if project has one model)."
    ),
    deep: bool = typer.Option(
        False,
        "--deep",
        help=(
            "Fetch every dataset's storage schema in parallel and add "
            "phantom-field, metric-phantom, and agg-on-STRING checks."
        ),
    ),
) -> None:
    """Validate a semantic-layer model.

    Basic checks: duplicates, dangling rel/metric, sum-on-pct, constraint
    orphan, severity-suffix warning. With ``--deep``: also probe the actual
    Snowflake schema for phantom fields, phantom column refs, and AGG-on-STRING.
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.validate",
            project=project,
            model=model,
            deep=deep,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx,
        service.validate_model,
        alias=project,
        model_name_or_uuid=model,
        deep=deep,
    )
    formatter.output(result, _print_validate)


# ---------------------------------------------------------------------------
# semantic-layer search-context / get-context
#
# Project-wide read surface that mirrors the upstream
# ``keboola-mcp-server`` semantic-context tools. Lets downstream callers
# (FIIA, scheduled agents) drop the MCP dependency for the common
# "is the model populated?" + "what's at this id?" lookups.
# ---------------------------------------------------------------------------


def _print_search_context(console: Console, data: dict) -> None:
    project = data.get("project", "")
    total = data.get("total_count", 0)
    console.print(
        f"\n[bold]Semantic contexts[/bold] in [magenta]{project}[/magenta]: "
        f"{total} match{'es' if total != 1 else ''}"
    )
    contexts = data.get("contexts", []) or []
    if not contexts:
        console.print("[dim](no matches)[/dim]")
        return
    table = Table()
    table.add_column("Type", style="bold cyan")
    table.add_column("Name", style="bold")
    table.add_column("ID")
    table.add_column("Description")
    for c in contexts:
        table.add_row(
            str(c.get("type", "")),
            str(c.get("name", "")),
            str(c.get("id", "")),
            str(c.get("description", ""))[:60],
        )
    console.print(table)


def _print_get_context(console: Console, data: dict) -> None:
    console.print(
        f"\n[bold]{data.get('type', '?')}[/bold] "
        f"[cyan]{data.get('name', '')}[/cyan] "
        f"([dim]{data.get('id', '')}[/dim]) in "
        f"[magenta]{data.get('project', '')}[/magenta]"
    )
    desc = data.get("description", "")
    if desc:
        console.print(f"\n{desc}\n")
    attrs = data.get("attributes") or {}
    if attrs:
        console.print("[bold]Attributes:[/bold]")
        console.print(json.dumps(attrs, indent=2, sort_keys=True, default=str))


@semantic_layer_app.command("search-context")
def semantic_layer_search_context(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    pattern: list[str] = typer.Option(
        ["*"],
        "--pattern",
        help=(
            "Glob pattern matched against entity name (case-sensitive "
            "fnmatch). Repeatable; matches the union. Default: '*'."
        ),
    ),
    type_filter: str = typer.Option(
        "all",
        "--type",
        help=(
            "Restrict to one type: model | dataset | metric | relationship | "
            "constraint | glossary | all. Default: all (every child type)."
        ),
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        help="Maximum number of results to return. Default: no cap.",
    ),
) -> None:
    """Search semantic-layer entities across a project by name pattern.

    Project-wide (not model-scoped). Equivalent to the upstream
    ``keboola-mcp-server`` ``search_semantic_context`` tool. Use this as a
    pre-flight check ("is the semantic model populated?") before kicking
    off a downstream pipeline that depends on it.
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.search-context",
            project=project,
            pattern=pattern,
            type_filter=type_filter,
            limit=limit,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx,
        service.search_context,
        alias=project,
        patterns=pattern,
        type_filter=type_filter,
        limit=limit,
    )
    formatter.output(result, _print_search_context)


@semantic_layer_app.command("get-context")
def semantic_layer_get_context(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    context_id: str = typer.Option(
        ...,
        "--context-id",
        help="UUID of the entity to fetch (model, dataset, metric, ...).",
    ),
) -> None:
    """Fetch a single semantic-layer entity by id, irrespective of its type.

    Probes every type (model + datasets / metrics / relationships /
    constraints / glossary) until it finds the entity, then returns the
    full attribute dict. Exits 1 if no type matches.
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.get-context",
            project=project,
            context_id=context_id,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx,
        service.get_context,
        alias=project,
        context_id=context_id,
    )
    formatter.output(result, _print_get_context)
