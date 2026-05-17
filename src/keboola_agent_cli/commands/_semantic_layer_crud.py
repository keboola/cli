"""Typer sub-apps for ``kbagent semantic-layer add|edit|remove``.

Extracted from :mod:`commands.semantic_layer` so the parent commands file
stays under the 1,200-LOC commands-file ceiling defined in CONTRIBUTING.md.
The three sub-apps are mounted onto ``semantic_layer_app`` via
``add_typer(...)`` in the parent module; they share error handling and the
stdin-TTY probe via :mod:`commands._semantic_layer_helpers`.
"""

from __future__ import annotations

import typer
from rich.console import Console

from ..errors import ErrorCode
from ._helpers import (
    check_cli_permission,
    emit_hint,
    get_formatter,
    get_service,
    should_hint,
)
from ._semantic_layer_helpers import _handle_service_call, _is_stdin_tty

# ---------------------------------------------------------------------------
# semantic-layer add -- one sub-subcommand per entity type
# ---------------------------------------------------------------------------


add_app = typer.Typer(
    name="add",
    help="Add an entity (metric, dataset, relationship, constraint, glossary).",
    no_args_is_help=True,
)


@add_app.callback(invoke_without_command=True)
def _add_permission_check(ctx: typer.Context) -> None:
    """Permission check for the ``add`` sub-app.

    Uses the standard ``check_cli_permission`` helper which composes the
    operation key as ``"semantic-layer.add.{subcommand}"`` (one per leaf).
    Every leaf within ``add`` is classified ``write`` in
    :mod:`permissions.OPERATION_REGISTRY`, so the gate is uniform.
    """
    check_cli_permission(ctx, "semantic-layer.add")


def _print_item_added(label: str):  # type: ignore[no-untyped-def]
    """Build a human-mode lambda that confirms one item was added."""

    def _render(c: Console, d: dict) -> None:
        attrs = d.get("attributes") or {}
        name = attrs.get("name") or attrs.get("term", "?")
        c.print(
            f"[bold green]Added {label}[/bold green] [cyan]{name}[/cyan] "
            f"([dim]{d.get('id', '')}[/dim])"
        )

    return _render


@add_app.command("metric")
def add_metric(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str | None = typer.Option(None, "--model", help="Model name or UUID"),
    name: str = typer.Option(..., "--name", help="Metric name"),
    sql: str = typer.Option(..., "--sql", help="SQL expression for the metric"),
    dataset: str = typer.Option(..., "--dataset", help="Dataset tableId this metric belongs to"),
    description: str = typer.Option("", "--description", help="Optional description"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the dataset-mismatch warning"),
) -> None:
    """Add a metric to a semantic-layer model."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.add.metric",
            project=project,
            model=model,
            name=name,
            sql=sql,
            dataset=dataset,
            description=description,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx,
        service.add_metric,
        alias=project,
        model_name_or_uuid=model,
        name=name,
        sql=sql,
        dataset=dataset,
        description=description,
        assume_yes=yes,
        is_tty=_is_stdin_tty(),
        confirm_cb=typer.confirm,
    )
    formatter.output(result, _print_item_added("metric"))


@add_app.command("dataset")
def add_dataset(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str | None = typer.Option(None, "--model", help="Model name or UUID"),
    name: str = typer.Option(..., "--name", help="Dataset name"),
    table_id: str = typer.Option(
        ..., "--table-id", help="Storage tableId, e.g. out.c-bucket.table"
    ),
    description: str = typer.Option("", "--description"),
    grain: str = typer.Option("", "--grain", help="Grain description"),
    primary_key: list[str] | None = typer.Option(
        None, "--primary-key", help="Repeat for multi-col PK"
    ),
    deep_fields: bool = typer.Option(
        False,
        "--deep-fields",
        help="Fetch storage schema and synthesise fields[] with role heuristics.",
    ),
) -> None:
    """Add a dataset (FQN derived from tableId)."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.add.dataset",
            project=project,
            model=model,
            name=name,
            table_id=table_id,
            description=description,
            grain=grain,
            primary_key=primary_key,
            deep_fields=deep_fields,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx,
        service.add_dataset,
        alias=project,
        model_name_or_uuid=model,
        name=name,
        table_id=table_id,
        description=description,
        grain=grain,
        primary_key=primary_key,
        deep_fields=deep_fields,
    )
    formatter.output(result, _print_item_added("dataset"))


@add_app.command("relationship")
def add_relationship(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str | None = typer.Option(None, "--model", help="Model name or UUID"),
    name: str = typer.Option(..., "--name", help="Relationship name"),
    from_: str = typer.Option(..., "--from", help="Source dataset tableId"),
    to: str = typer.Option(..., "--to", help="Target dataset tableId"),
    on: str = typer.Option(..., "--on", help="Join condition"),
    type_: str = typer.Option("left", "--type", help="Join type: 'left' or 'inner'."),
) -> None:
    """Add a relationship between two datasets."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.add.relationship",
            project=project,
            model=model,
            name=name,
            from_=from_,
            to=to,
            on=on,
            type_=type_,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx,
        service.add_relationship,
        alias=project,
        model_name_or_uuid=model,
        name=name,
        from_=from_,
        to=to,
        on=on,
        type_=type_,
    )
    formatter.output(result, _print_item_added("relationship"))


@add_app.command("constraint")
def add_constraint(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str | None = typer.Option(None, "--model", help="Model name or UUID"),
    name: str = typer.Option(
        ...,
        "--name",
        help=(
            "Constraint name (regex ^[a-z][a-z0-9_]*$). For the 4-band "
            "health convention end with _critical / _warning / _healthy / _review."
        ),
    ),
    constraint_type: str = typer.Option(
        ...,
        "--constraint-type",
        help=("One of: inequality|equality|range|composition|exclusion|temporal|conditional."),
    ),
    rule: str = typer.Option(
        ...,
        "--rule",
        help='Rule expression STRING (e.g. "value >= 0"). NOT an object.',
    ),
    metrics: str = typer.Option(
        ...,
        "--metrics",
        help="Comma-separated list of metric names this constraint applies to.",
    ),
    severity: str = typer.Option(
        "warning", "--severity", help="One of: error|warning|info (the 3-level API enum)."
    ),
) -> None:
    """Add a constraint."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.add.constraint",
            project=project,
            model=model,
            name=name,
            constraint_type=constraint_type,
            rule=rule,
            metrics=metrics,
            severity=severity,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    metrics_list = [m.strip() for m in metrics.split(",") if m.strip()]
    if not metrics_list:
        formatter.error(
            message="--metrics must contain at least one metric name.",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
        raise typer.Exit(code=2)
    result = _handle_service_call(
        ctx,
        service.add_constraint,
        alias=project,
        model_name_or_uuid=model,
        name=name,
        constraint_type=constraint_type,
        rule=rule,
        metrics=metrics_list,
        severity=severity,
    )
    formatter.output(result, _print_item_added("constraint"))


@add_app.command("glossary")
def add_glossary(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str | None = typer.Option(None, "--model", help="Model name or UUID"),
    term: str = typer.Option(..., "--term", help="Glossary term"),
    definition: str = typer.Option("", "--definition", help="Optional definition"),
) -> None:
    """Add a glossary term."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.add.glossary",
            project=project,
            model=model,
            term=term,
            definition=definition,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx,
        service.add_glossary,
        alias=project,
        model_name_or_uuid=model,
        term=term,
        definition=definition,
    )
    formatter.output(result, _print_item_added("glossary"))


# ---------------------------------------------------------------------------
# semantic-layer edit -- DELETE+POST with rollback + rename cascade
# ---------------------------------------------------------------------------


edit_app = typer.Typer(
    name="edit",
    help=(
        "Edit a metric, dataset, constraint, relationship, or glossary term "
        "(DELETE+POST with rollback)."
    ),
    no_args_is_help=True,
)


@edit_app.callback(invoke_without_command=True)
def _edit_permission_check(ctx: typer.Context) -> None:
    """Permission check for the ``edit`` sub-app.

    Every ``edit`` leaf is classified ``write`` in OPERATION_REGISTRY.
    """
    check_cli_permission(ctx, "semantic-layer.edit")


def _print_edit_result(label: str):  # type: ignore[no-untyped-def]
    """Build a human-mode renderer for edit responses."""

    def _render(c: Console, d: dict) -> None:
        updated = d.get("updated") or {}
        attrs = updated.get("attributes") or {}
        name = attrs.get("name") or attrs.get("term", "?")
        if d.get("partial_state"):
            c.print(
                f"[bold red]PARTIAL STATE[/bold red] -- {label} edit "
                f"succeeded but one or more cascade entries failed. The "
                f"model is internally inconsistent until you re-run the "
                f"failed cascades."
            )
        c.print(
            f"[bold green]Updated {label}[/bold green] [cyan]{name}[/cyan] "
            f"([dim]{updated.get('id', '')}[/dim])"
        )
        cascaded = d.get("cascaded_constraints") or []
        for entry in cascaded:
            status = entry.get("status", "?")
            colour = "green" if status == "updated" else "red"
            c.print(
                f"  [{colour}]cascade {status}[/{colour}] "
                f"constraint [cyan]{entry.get('constraint', '?')}[/cyan]"
            )
        if d.get("rollback"):
            c.print(f"[bold red]Rollback applied:[/bold red] {d['rollback']}")
        if d.get("recovery_hint"):
            c.print(f"[bold yellow]Recovery:[/bold yellow] {d['recovery_hint']}")

    return _render


@edit_app.command("metric")
def edit_metric(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str | None = typer.Option(None, "--model", help="Model name or UUID"),
    name: str = typer.Option(..., "--name", help="Current metric name"),
    new_name: str | None = typer.Option(
        None, "--new-name", help="Rename to this name (triggers constraint cascade)"
    ),
    new_sql: str | None = typer.Option(None, "--new-sql", help="Replace SQL"),
    new_dataset: str | None = typer.Option(None, "--new-dataset", help="Replace dataset tableId"),
    new_description: str | None = typer.Option(
        None, "--new-description", help="Replace description"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the rename-cascade prompt"),
) -> None:
    """Edit a metric. Rename cascades to any constraint that references it."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.edit.metric",
            project=project,
            model=model,
            name=name,
            new_name=new_name,
            new_sql=new_sql,
            new_dataset=new_dataset,
            new_description=new_description,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx,
        service.edit_metric,
        alias=project,
        model_name_or_uuid=model,
        current_name=name,
        new_name=new_name,
        new_sql=new_sql,
        new_dataset=new_dataset,
        new_description=new_description,
        assume_yes=yes,
        is_tty=_is_stdin_tty(),
        confirm_cb=typer.confirm,
    )
    formatter.output(result, _print_edit_result("metric"))


@edit_app.command("dataset")
def edit_dataset(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str | None = typer.Option(None, "--model", help="Model name or UUID"),
    name: str = typer.Option(..., "--name", help="Current dataset name"),
    new_name: str | None = typer.Option(None, "--new-name", help="Rename to this name"),
    new_description: str | None = typer.Option(
        None, "--new-description", help="Replace description"
    ),
    new_grain: str | None = typer.Option(None, "--new-grain", help="Replace grain"),
) -> None:
    """Edit a dataset (no cascade — metric.dataset uses tableId, not name)."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.edit.dataset",
            project=project,
            model=model,
            name=name,
            new_name=new_name,
            new_description=new_description,
            new_grain=new_grain,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx,
        service.edit_dataset,
        alias=project,
        model_name_or_uuid=model,
        current_name=name,
        new_name=new_name,
        new_description=new_description,
        new_grain=new_grain,
    )
    formatter.output(result, _print_edit_result("dataset"))


@edit_app.command("constraint")
def edit_constraint(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str | None = typer.Option(None, "--model", help="Model name or UUID"),
    name: str = typer.Option(..., "--name", help="Current constraint name"),
    new_name: str | None = typer.Option(
        None, "--new-name", help=f"Rename to this name (regex {'^[a-z][a-z0-9_]*$'!r})"
    ),
    new_rule: str | None = typer.Option(None, "--new-rule", help="Replace rule (STRING)"),
    new_constraint_type: str | None = typer.Option(
        None, "--new-constraint-type", help="Replace constraintType (closed enum)"
    ),
    new_severity: str | None = typer.Option(
        None, "--new-severity", help="Replace severity (error|warning|info)"
    ),
    new_metrics: str | None = typer.Option(
        None, "--new-metrics", help="Comma-separated list of metric names"
    ),
) -> None:
    """Edit a constraint (DELETE+POST, with local validators)."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.edit.constraint",
            project=project,
            model=model,
            name=name,
            new_name=new_name,
            new_rule=new_rule,
            new_constraint_type=new_constraint_type,
            new_severity=new_severity,
            new_metrics=new_metrics,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    metrics_list = (
        [m.strip() for m in new_metrics.split(",") if m.strip()]
        if new_metrics is not None
        else None
    )
    result = _handle_service_call(
        ctx,
        service.edit_constraint,
        alias=project,
        model_name_or_uuid=model,
        current_name=name,
        new_name=new_name,
        new_rule=new_rule,
        new_constraint_type=new_constraint_type,
        new_severity=new_severity,
        new_metrics=metrics_list,
    )
    formatter.output(result, _print_edit_result("constraint"))


@edit_app.command("relationship")
def edit_relationship(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str | None = typer.Option(None, "--model", help="Model name or UUID"),
    name: str = typer.Option(..., "--name", help="Current relationship name"),
    new_name: str | None = typer.Option(None, "--new-name", help="Rename to this name"),
    new_from: str | None = typer.Option(None, "--new-from", help="Replace source dataset tableId"),
    new_to: str | None = typer.Option(None, "--new-to", help="Replace target dataset tableId"),
    new_on: str | None = typer.Option(None, "--new-on", help="Replace join condition"),
    new_type: str | None = typer.Option(
        None, "--new-type", help="Replace join type (left | inner)"
    ),
) -> None:
    """Edit a relationship (DELETE+POST). Validates ``--new-type`` locally."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.edit.relationship",
            project=project,
            model=model,
            name=name,
            new_name=new_name,
            new_from=new_from,
            new_to=new_to,
            new_on=new_on,
            new_type=new_type,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")
    result = _handle_service_call(
        ctx,
        service.edit_relationship,
        alias=project,
        model_name_or_uuid=model,
        current_name=name,
        new_name=new_name,
        new_from=new_from,
        new_to=new_to,
        new_on=new_on,
        new_type=new_type,
    )
    formatter.output(result, _print_edit_result("relationship"))


@edit_app.command("glossary")
def edit_glossary(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str | None = typer.Option(None, "--model", help="Model name or UUID"),
    term: str = typer.Option(..., "--term", help="Current glossary term"),
    new_term: str | None = typer.Option(
        None,
        "--new-term",
        help=(
            "Rename the term (DESTRUCTIVE cascade: downstream consumers joining on the "
            "term string will break -- pass --yes to confirm)."
        ),
    ),
    new_definition: str | None = typer.Option(
        None, "--new-definition", help="Replace the definition"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the rename-cascade prompt (required for --new-term)"
    ),
) -> None:
    """Edit a glossary term. ``--new-term`` is destructive for downstream joins."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.edit.glossary",
            project=project,
            model=model,
            term=term,
            new_term=new_term,
            new_definition=new_definition,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")

    if new_term is not None and new_term != term and not yes:
        if not _is_stdin_tty():
            formatter.error(
                message=(
                    f"Refusing to rename glossary term {term!r} -> {new_term!r} "
                    "non-interactively without --yes (downstream consumers joining "
                    "on the term string will break)."
                ),
                error_code=ErrorCode.VALIDATION_ERROR,
            )
            raise typer.Exit(code=2)
        if not formatter.json_mode and not typer.confirm(
            f"Rename glossary term '{term}' to '{new_term}'? "
            "Downstream consumers joining on the term will break."
        ):
            formatter.console.print("Aborted.")
            raise typer.Exit(code=0)

    result = _handle_service_call(
        ctx,
        service.edit_glossary,
        alias=project,
        model_name_or_uuid=model,
        current_term=term,
        new_term=new_term,
        new_definition=new_definition,
    )
    formatter.output(result, _print_edit_result("glossary"))


# ---------------------------------------------------------------------------
# semantic-layer remove -- destructive, orphan-warning before delete
# ---------------------------------------------------------------------------


remove_app = typer.Typer(
    name="remove",
    help=("Remove a metric / dataset / constraint / relationship / glossary term (destructive)."),
    no_args_is_help=True,
)


@remove_app.callback(invoke_without_command=True)
def _remove_permission_check(ctx: typer.Context) -> None:
    """Permission check for the ``remove`` sub-app.

    Every ``remove`` leaf is classified ``destructive`` in OPERATION_REGISTRY.
    """
    check_cli_permission(ctx, "semantic-layer.remove")


def _run_remove(
    ctx: typer.Context,
    *,
    kind: str,
    project: str,
    model: str | None,
    name: str,
    yes: bool,
) -> None:
    formatter = get_formatter(ctx)
    service = get_service(ctx, "semantic_layer_service")

    # Always echo the orphan warning (--yes only skips the prompt).
    preview = _handle_service_call(
        ctx,
        service.preview_remove,
        alias=project,
        model_name_or_uuid=model,
        kind=kind,
        name=name,
    )

    orphans = preview.get("orphaned_constraints") or []
    is_tty = _is_stdin_tty()

    if orphans and not formatter.json_mode:
        formatter.err_console.print(
            f"\n[bold yellow]Removing {kind} '{name}' will orphan "
            f"{len(orphans)} constraint(s):[/bold yellow]"
        )
        for orph in orphans:
            metrics = orph.get("metrics", [])
            formatter.err_console.print(f"  · {orph['name']} (metrics: {metrics})")
        formatter.err_console.print(
            "These constraints will have a dangling reference in DIM_METRIC_THRESHOLD.\n"
            "To avoid this, remove or update the constraints first."
        )

    # Non-TTY without --yes: refuse with exit 2 (warning above already shown).
    if not yes:
        if not is_tty:
            formatter.error(
                message=(f"Refusing to remove {kind} {name!r} non-interactively without --yes."),
                error_code=ErrorCode.VALIDATION_ERROR,
            )
            raise typer.Exit(code=2)
        if not formatter.json_mode and not typer.confirm(f"Delete {kind} '{name}' anyway?"):
            formatter.console.print("Aborted.")
            raise typer.Exit(code=0)

    result = _handle_service_call(
        ctx,
        service.remove_item,
        alias=project,
        model_name_or_uuid=model,
        kind=kind,
        name=name,
    )
    formatter.output(
        result,
        lambda c, d: c.print(
            f"[bold green]Removed {kind}[/bold green] [cyan]{d['removed']['name']}[/cyan] "
            f"([dim]{d['removed']['id']}[/dim])"
        ),
    )


@remove_app.command("metric")
def remove_metric(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str | None = typer.Option(None, "--model", help="Model name or UUID"),
    name: str = typer.Option(..., "--name", help="Metric name"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirm prompt"),
) -> None:
    """Remove a metric. Prints an orphan-warning when constraints reference it."""
    if should_hint(ctx):
        emit_hint(ctx, "semantic-layer.remove.metric", project=project, model=model, name=name)
        return
    _run_remove(ctx, kind="metric", project=project, model=model, name=name, yes=yes)


@remove_app.command("dataset")
def remove_dataset(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str | None = typer.Option(None, "--model", help="Model name or UUID"),
    name: str = typer.Option(..., "--name", help="Dataset name"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirm prompt"),
) -> None:
    """Remove a dataset."""
    if should_hint(ctx):
        emit_hint(ctx, "semantic-layer.remove.dataset", project=project, model=model, name=name)
        return
    _run_remove(ctx, kind="dataset", project=project, model=model, name=name, yes=yes)


@remove_app.command("constraint")
def remove_constraint(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str | None = typer.Option(None, "--model", help="Model name or UUID"),
    name: str = typer.Option(..., "--name", help="Constraint name"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirm prompt"),
) -> None:
    """Remove a constraint."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.remove.constraint",
            project=project,
            model=model,
            name=name,
        )
        return
    _run_remove(ctx, kind="constraint", project=project, model=model, name=name, yes=yes)


@remove_app.command("relationship")
def remove_relationship(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str | None = typer.Option(None, "--model", help="Model name or UUID"),
    name: str = typer.Option(..., "--name", help="Relationship name"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirm prompt"),
) -> None:
    """Remove a relationship. No orphan-check (relationships are leaf entities)."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.remove.relationship",
            project=project,
            model=model,
            name=name,
        )
        return
    _run_remove(ctx, kind="relationship", project=project, model=model, name=name, yes=yes)


@remove_app.command("glossary")
def remove_glossary(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    model: str | None = typer.Option(None, "--model", help="Model name or UUID"),
    term: str = typer.Option(..., "--term", help="Glossary term"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirm prompt"),
) -> None:
    """Remove a glossary term. No orphan-check (glossary is a leaf entity)."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "semantic-layer.remove.glossary",
            project=project,
            model=model,
            term=term,
        )
        return
    _run_remove(ctx, kind="glossary", project=project, model=model, name=term, yes=yes)
