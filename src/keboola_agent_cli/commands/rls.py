"""Row-level security commands (CLI-17).

Thin CLI layer over :class:`RlsService`. Seven subcommands:

- ``rls list`` -- policies visible to a project.
- ``rls detail`` -- one policy's full rules.
- ``rls schema`` -- the live ``rls-policy`` JSON Schema from the metastore.
- ``rls create`` -- author a policy for one table (write).
- ``rls update`` -- fetch-then-merge update of an existing policy (write).
- ``rls delete`` -- remove a policy (destructive).
- ``rls setup`` -- guided, interactive-terminal-only wizard: pick tables via
  a checkbox picker, build conditions, preview, then create one policy per
  table via the same ``create_policy`` path ``create`` uses.

**Every policy this group writes is authored at ``organization`` or
``targeted`` scope, never ``project``** -- there is deliberately no
``--scope`` flag that could select ``project``, matching the metastore
backend's own restriction (an RLS policy is centrally governed, never
authored by a project's own admin for its own tables -- see the RFC in
``keboola-mcp-server``'s ``feature_spec/rls_query_tool/RFC.md``).

The ``rls-policy`` metastore object type does not exist on any deployed
backend yet -- see ``gotchas.md``'s "RLS backend not yet available" entry.
Every read/write command here will answer with a clean, classified error
against a real project until that backend work (tracked separately) lands.

``list``/``detail``/``schema`` are read-only and safe under ``--deny-writes``.
``create``/``update``/``setup`` are gated as ``admin`` (organization-level,
not merely ``write``); ``delete`` is gated as ``admin`` too -- see
``rls.*`` in ``OPERATION_REGISTRY`` (``permissions.py``).
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

import typer
from rich.syntax import Syntax
from rich.table import Table

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..services.rls_service import RLS_DIALECTS
from ._checkbox_select import CheckboxItem, CheckboxUnavailable, checkbox_select
from ._helpers import (
    check_cli_permission,
    get_formatter,
    get_service,
    map_error_to_exit_code,
    parse_json_arg,
)

logger = logging.getLogger(__name__)

rls_app = typer.Typer(
    help=(
        "Manage row-level security policies (metastore-backed, org-admin-only). "
        "'list'/'detail'/'schema' are read-only; 'create'/'update'/'setup'/'delete' "
        "are gated as admin -- see CONTRIBUTING.md."
    )
)


@rls_app.callback(invoke_without_command=True)
def _rls_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "rls")


def _is_stdout_tty() -> bool:
    """True when stdout is an interactive terminal (picker eligibility check)."""
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _format_policy_table(formatter: Any, policies: list[dict[str, Any]]) -> None:
    tbl = Table(
        "ID",
        "Table",
        "Dialect",
        "Rules",
        "Scope",
        "Source Project",
        "Target Projects",
        show_header=True,
        header_style="bold cyan",
    )
    for policy in policies:
        tbl.add_row(
            str(policy.get("id", "")),
            str(policy.get("table", "")),
            str(policy.get("dialect", "")),
            str(policy.get("rule_count", 0)),
            str(policy.get("scope", "")),
            str(policy.get("source_project_id") or ""),
            ", ".join(str(p) for p in policy.get("target_project_ids", [])),
        )
    formatter.console.print(tbl)


def _print_policy(formatter: Any, row: dict[str, Any]) -> None:
    formatter.console.print(
        f"\n[bold]{row.get('table', '')}[/bold] [dim](id {row.get('id', '')})[/dim]"
    )
    formatter.console.print(f"  Dialect:          {row.get('dialect', '')}")
    formatter.console.print(f"  Scope:            {row.get('scope', '')}")
    formatter.console.print(f"  Source project:   {row.get('source_project_id') or '(none)'}")
    targets = row.get("target_project_ids") or []
    if targets:
        formatter.console.print(f"  Target projects:  {', '.join(str(t) for t in targets)}")
    rules = row.get("rules") or []
    formatter.console.print(f"  Rules ({len(rules)}):")
    for rule in rules:
        principal = rule.get("principal") or ", ".join(rule.get("principals") or [])
        formatter.console.print(f"    - {principal}: {rule.get('condition')}")


def _print_preview(formatter: Any, result: dict[str, Any]) -> None:
    formatter.console.print(
        f"\n[bold]Preview[/bold] -- {result.get('table', '')} "
        f"[dim]({result.get('dialect', '')}, scope={result.get('scope', '')})[/dim]"
    )
    for entry in result.get("preview", []):
        formatter.console.print(f"  {entry.get('principal')}: WHERE {entry.get('condition')}")


def _parse_rules_arg(formatter: Any, raw: str) -> list[dict[str, Any]]:
    try:
        parsed = parse_json_arg(raw, label="--rules")
    except ValueError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.INVALID_ARGUMENT)
        raise typer.Exit(code=2) from None
    if not isinstance(parsed, list):
        formatter.error(
            message="--rules must be a JSON array of {principal|principals, condition} objects",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None
    return parsed


# ---------------------------------------------------------------------------
# rls list
# ---------------------------------------------------------------------------


@rls_app.command("list")
def rls_list(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
) -> None:
    """List row-level security policies visible to a project."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "rls_service")

    try:
        result = service.list_policies(alias=project)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
        return

    policies = result.get("policies", [])
    if not policies:
        formatter.console.print("[dim]No RLS policies found.[/dim]")
    else:
        _format_policy_table(formatter, policies)


# ---------------------------------------------------------------------------
# rls detail
# ---------------------------------------------------------------------------


@rls_app.command("detail")
def rls_detail(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    policy_id: str = typer.Option(..., "--policy-id", help="RLS policy ID"),
) -> None:
    """Show one RLS policy's full rule set."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "rls_service")

    try:
        result = service.get_policy(alias=project, policy_id=policy_id)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
        return

    _print_policy(formatter, result)


# ---------------------------------------------------------------------------
# rls schema
# ---------------------------------------------------------------------------


@rls_app.command("schema")
def rls_schema(
    ctx: typer.Context,
    project: str = typer.Option(
        ..., "--project", help="Project alias -- fetch the live schema from this stack"
    ),
) -> None:
    """Print the live ``rls-policy`` JSON Schema fetched from the metastore.

    Unlike ``flow schema``, there is no offline bundled snapshot -- the
    schema is authoritative only from the live metastore, and (until the
    go-monorepo backend work lands) fetching it will fail with a clean,
    classified error rather than returning a schema.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "rls_service")

    try:
        fetch = service.fetch_schema(project)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    if fetch.schema is None:
        formatter.error(
            message=f"Could not fetch the rls-policy schema: {fetch.reason}",
            error_code=ErrorCode.NOT_FOUND,
        )
        raise typer.Exit(code=4)

    if formatter.json_mode:
        formatter.output({"format": "json-schema", "source": "live", "schema": fetch.schema})
        return
    formatter.console.print(
        Syntax(json.dumps(fetch.schema, indent=2), "json", theme="monokai", line_numbers=False)
    )


# ---------------------------------------------------------------------------
# rls create
# ---------------------------------------------------------------------------


@rls_app.command("create")
def rls_create(
    ctx: typer.Context,
    project: str = typer.Option(
        ..., "--project", help="Project alias -- the project this policy protects"
    ),
    table: str = typer.Option(..., "--table", help="Table key, e.g. in.c-crm.invoices"),
    dialect: str = typer.Option(
        ..., "--dialect", help=f"Workspace SQL dialect: {' | '.join(RLS_DIALECTS)}"
    ),
    rules: str = typer.Option(
        ...,
        "--rules",
        help=(
            "JSON|@file|- array of {principal|principals, condition} objects "
            "(see `rls schema` / rls-workflow.md for the condition shape)"
        ),
    ),
    target_project: list[str] | None = typer.Option(
        None,
        "--target-project",
        help=(
            "Project ID this policy also applies to (repeatable). Omit for plain "
            "organization scope -- still only applies where source_project_id / "
            "target_project_ids match at read time, never by table-name text alone."
        ),
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview the compiled condition without writing"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Create one RLS policy for one table.

    Always authored at ``organization`` scope, or ``targeted`` scope when
    ``--target-project`` is given -- never plain ``project`` scope.
    """
    formatter = get_formatter(ctx)
    parsed_rules = _parse_rules_arg(formatter, rules)

    if dialect not in RLS_DIALECTS:
        formatter.error(
            message=f"--dialect must be one of {RLS_DIALECTS}, got {dialect!r}",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None

    service = get_service(ctx, "rls_service")

    if not dry_run and not yes and not formatter.json_mode:
        try:
            preview = service.create_policy(
                alias=project,
                table=table,
                dialect=dialect,
                rules=parsed_rules,
                target_project_ids=target_project,
                dry_run=True,
            )
        except (ConfigError, KeboolaApiError):
            pass  # validation/network error -- let the real call below report it properly
        else:
            _print_preview(formatter, preview)
        confirmed = typer.confirm(f"Create RLS policy on {table}?")
        if not confirmed:
            formatter.console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=0)

    try:
        result = service.create_policy(
            alias=project,
            table=table,
            dialect=dialect,
            rules=parsed_rules,
            target_project_ids=target_project,
            dry_run=dry_run,
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

    if dry_run:
        _print_preview(formatter, result)
        return
    formatter.success(f"Created RLS policy {result.get('id', '')} on {table}")
    _print_preview(formatter, result)


# ---------------------------------------------------------------------------
# rls update
# ---------------------------------------------------------------------------


@rls_app.command("update")
def rls_update(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    policy_id: str = typer.Option(..., "--policy-id", help="RLS policy ID"),
    table: str | None = typer.Option(None, "--table", help="New table key (unset = unchanged)"),
    dialect: str | None = typer.Option(
        None, "--dialect", help=f"New dialect: {' | '.join(RLS_DIALECTS)} (unset = unchanged)"
    ),
    rules: str | None = typer.Option(
        None, "--rules", help="New JSON|@file|- rules array (unset = unchanged)"
    ),
    target_project: list[str] | None = typer.Option(
        None, "--target-project", help="New target-project list (repeatable; unset = unchanged)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview the compiled condition without writing"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Update an existing RLS policy.

    Fetch-then-merge: only the flags you pass are changed -- an omitted flag
    keeps the policy's current value, it is never silently blanked.
    """
    formatter = get_formatter(ctx)
    parsed_rules = _parse_rules_arg(formatter, rules) if rules is not None else None

    if dialect is not None and dialect not in RLS_DIALECTS:
        formatter.error(
            message=f"--dialect must be one of {RLS_DIALECTS}, got {dialect!r}",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None

    service = get_service(ctx, "rls_service")

    if not dry_run and not yes and not formatter.json_mode:
        confirmed = typer.confirm(f"Update RLS policy {policy_id}?")
        if not confirmed:
            formatter.console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=0)

    try:
        result = service.update_policy(
            alias=project,
            policy_id=policy_id,
            table=table,
            dialect=dialect,
            rules=parsed_rules,
            target_project_ids=target_project,
            dry_run=dry_run,
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

    if dry_run:
        _print_preview(formatter, result)
        return
    formatter.success(f"Updated RLS policy {policy_id}")
    _print_preview(formatter, result)


# ---------------------------------------------------------------------------
# rls delete
# ---------------------------------------------------------------------------


@rls_app.command("delete")
def rls_delete(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    policy_id: str = typer.Option(..., "--policy-id", help="RLS policy ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Delete an RLS policy."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "rls_service")

    if not yes and not formatter.json_mode:
        confirmed = typer.confirm(f"Delete RLS policy {policy_id}? This un-protects its table.")
        if not confirmed:
            formatter.console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=0)

    try:
        result = service.delete_policy(alias=project, policy_id=policy_id)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.success(f"Deleted RLS policy {policy_id}")


# ---------------------------------------------------------------------------
# rls setup (guided, interactive-terminal-only -- no REST route, see
# CONTRIBUTING.md's "genuinely terminal-only" carve-out)
# ---------------------------------------------------------------------------

_SETUP_HINT = (
    "rls setup is an interactive picker and needs a real terminal. "
    "Use `rls create --project P --table T --dialect D --rules '[...]'` "
    "directly instead (see rls-workflow.md)."
)

_CONDITION_MENU = (
    "  1) column comparison (=, !=, >, >=, <, <=)\n"
    "  2) column IN / NOT IN a list of values\n"
    "  3) column IS NULL / IS NOT NULL\n"
    "  4) always true (no filtering for this principal)"
)

_COMPARISON_OPS = ("eq", "ne", "gt", "gte", "lt", "lte")


def _prompt_choice(text: str, choices: tuple[str, ...], default: str) -> str:
    while True:
        value = typer.prompt(text, default=default)
        if value in choices:
            return value
        typer.echo(f"Enter one of: {', '.join(choices)}")


def _prompt_condition() -> dict[str, Any]:
    typer.echo(_CONDITION_MENU)
    choice = _prompt_choice("Choice", ("1", "2", "3", "4"), default="1")
    if choice == "4":
        return {"true": True}
    column = typer.prompt("Column name")
    if choice == "1":
        op = _prompt_choice("Operator", _COMPARISON_OPS, default="eq")
        value = typer.prompt("Value")
        return {"column": column, "op": op, "value": value}
    if choice == "2":
        op = _prompt_choice("Operator", ("in", "not_in"), default="in")
        raw_values = typer.prompt("Values (comma-separated)")
        values = [v.strip() for v in raw_values.split(",") if v.strip()]
        return {"column": column, "op": op, "values": values}
    op = _prompt_choice("Operator", ("is_null", "is_not_null"), default="is_null")
    return {"column": column, "op": op}


def _build_rules_interactively() -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    while True:
        principal = typer.prompt("Principal (user email, or comma-separated list for a group)")
        principals = [p.strip() for p in principal.split(",") if p.strip()]
        condition = _prompt_condition()
        rule: dict[str, Any] = {"condition": condition}
        if len(principals) == 1:
            rule["principal"] = principals[0]
        else:
            rule["principals"] = principals
        rules.append(rule)
        if not typer.confirm("Add another rule to this policy?", default=False):
            return rules


@rls_app.command("setup")
def rls_setup(
    ctx: typer.Context,
    project: str = typer.Option(
        ..., "--project", help="Project alias whose tables you're protecting"
    ),
    dialect: str | None = typer.Option(
        None, "--dialect", help=f"Workspace SQL dialect: {' | '.join(RLS_DIALECTS)}"
    ),
    rules: str | None = typer.Option(
        None,
        "--rules",
        help="JSON|@file|- rules array -- skips the interactive condition builder",
    ),
    target_project: list[str] | None = typer.Option(
        None, "--target-project", help="Project ID to also share the policy with (repeatable)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the final confirmation"),
) -> None:
    """Guided RLS setup: pick tables, build a condition, preview, then write.

    Interactive-terminal-only (no ``--json`` / non-TTY path -- use ``rls
    create`` directly there). Creates one RLS policy per selected table,
    all sharing the same rules, via the exact same write path ``rls
    create`` uses.
    """
    formatter = get_formatter(ctx)
    if formatter.json_mode or not _is_stdout_tty():
        target_console = formatter.err_console if formatter.json_mode else formatter.console
        target_console.print(_SETUP_HINT)
        raise typer.Exit(code=2)

    storage_service = get_service(ctx, "storage_service")
    try:
        tables_result = storage_service.list_tables(aliases=[project])
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    tables = tables_result.get("tables", [])
    if not tables:
        formatter.console.print(f"[dim]No tables found in project '{project}'.[/dim]")
        raise typer.Exit(code=0)

    items = [
        CheckboxItem(label=str(t.get("id", "")), hint=f"{t.get('rows_count', 0)} rows")
        for t in tables
    ]
    try:
        indices = checkbox_select(items, title=f"Select tables to protect with RLS in '{project}'")
    except CheckboxUnavailable:
        formatter.console.print(f"[yellow]{_SETUP_HINT}[/yellow]")
        raise typer.Exit(code=2) from None

    selected = indices or []
    if not selected:
        formatter.console.print("No tables selected.")
        raise typer.Exit(code=0)

    resolved_dialect = dialect or _prompt_choice("Dialect", RLS_DIALECTS, default="snowflake")

    if rules is not None:
        parsed_rules = _parse_rules_arg(formatter, rules)
    else:
        parsed_rules = _build_rules_interactively()

    service = get_service(ctx, "rls_service")
    selected_tables = [str(tables[i]["id"]) for i in selected]

    for table_id in selected_tables:
        try:
            preview = service.create_policy(
                alias=project,
                table=table_id,
                dialect=resolved_dialect,
                rules=parsed_rules,
                target_project_ids=target_project,
                dry_run=True,
            )
        except (ConfigError, KeboolaApiError) as exc:
            formatter.console.print(f"[yellow]{table_id}: preview failed ({exc}).[/yellow]")
            continue
        _print_preview(formatter, preview)

    if not yes:
        confirmed = typer.confirm(
            f"Create {len(selected_tables)} RLS polic{'y' if len(selected_tables) == 1 else 'ies'}?"
        )
        if not confirmed:
            formatter.console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=0)

    for table_id in selected_tables:
        try:
            result = service.create_policy(
                alias=project,
                table=table_id,
                dialect=resolved_dialect,
                rules=parsed_rules,
                target_project_ids=target_project,
            )
        except (ConfigError, KeboolaApiError) as exc:
            formatter.warning(f"{table_id}: {exc}")
            continue
        formatter.success(f"Created RLS policy {result.get('id', '')} on {table_id}")
