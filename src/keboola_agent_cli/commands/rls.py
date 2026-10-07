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

**Every policy this group writes is authored at ``targeted`` scope (the
default: the owning project plus the ``--target-project`` grants) or, only
when asked for with ``--scope organization``, at ``organization`` scope**
(every project in the organization). ``--scope`` offers no ``project``: the
metastore schema does not support it for policies (see the RFC in
``keboola-mcp-server``'s ``feature_spec/rls_query_tool/RFC.md``).

On a stack whose metastore predates the ``rls-policy`` schema every command here
answers with a clean, classified error -- see ``gotchas.md``'s RLS/CLS entry.

``list``/``detail``/``schema`` are read-only and safe under ``--deny-writes``.
``create``/``update``/``setup`` are gated as ``admin`` (organization-level,
not merely ``write``); ``delete`` and ``--scope organization`` are
``destructive`` -- see ``rls.*`` in ``OPERATION_REGISTRY`` / ``FLAG_ESCALATIONS``
(``permissions.py``).
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Callable
from enum import StrEnum
from typing import Any

import typer
from rich.syntax import Syntax
from rich.table import Table

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._checkbox_select import CheckboxItem, CheckboxUnavailable, _stdio_is_tty, checkbox_select
from ._helpers import (
    check_cli_operation,
    check_cli_permission,
    get_formatter,
    get_service,
    map_error_to_exit_code,
    parse_json_arg,
)

logger = logging.getLogger(__name__)

rls_app = typer.Typer(
    help=(
        "Manage row-level security policies (metastore-backed). "
        "'list'/'detail'/'schema' are read-only; 'create'/'update'/'setup'/'delete' "
        "are gated as admin -- see CONTRIBUTING.md."
    )
)


class Dialect(StrEnum):
    """``--dialect`` values (the policy schema's enum); omitted = the project backend."""

    SNOWFLAKE = "snowflake"
    BIGQUERY = "bigquery"


class PolicyScope(StrEnum):
    """``--scope`` values: the schema supports no ``project`` scope for policies."""

    TARGETED = "targeted"
    ORGANIZATION = "organization"


DIALECT_HELP = "Workspace SQL dialect; must match the project backend (default: the backend)"
SCOPE_HELP = (
    "targeted (default): the owning project plus --target-project grants. organization: "
    "EVERY project of the organization that has the row-level-security feature -- an explicit, "
    "organization-admin-only choice, gated as destructive."
)
TARGET_HELP = (
    "Project alias or ID the policy also applies to (repeatable or comma-separated; "
    "--scope targeted only; granting needs the organization-admin role)"
)
TABLE_HELP = "Storage table ID, e.g. in.c-crm.invoices"


def _call[T](formatter: Any, fn: Callable[..., T], **kwargs: Any) -> T:
    """Run a service call, turning its errors into a formatted message + exit code.

    A bad option value (``INVALID_ARGUMENT``) is a usage error, exit 2.
    """
    try:
        return fn(**kwargs)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        code = 2 if exc.error_code == ErrorCode.INVALID_ARGUMENT else map_error_to_exit_code(exc)
        raise typer.Exit(code=code) from None


def _confirm_or_exit(formatter: Any, question: str) -> None:
    if not typer.confirm(question):
        formatter.console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(code=0)


def gate_scope(
    ctx: typer.Context,
    group: str,
    operation: str,
    scope: PolicyScope,
    target_project: list[str] | None,
) -> None:
    """Validate ``--scope`` before any API call or prompt.

    ``--scope organization`` governs the table in every project: a destructive-class flag. It has
    no grants, so ``--target-project`` with it is a usage error (exit 2).
    """
    if scope != PolicyScope.ORGANIZATION:
        return
    check_cli_operation(ctx, f"{group}.{operation} --scope organization")
    if target_project:
        get_formatter(ctx).error(
            message="--target-project requires --scope targeted (organization scope has no grants)",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)


def reject_target_conflict(
    formatter: Any, target_project: list[str] | None, clear_target_projects: bool
) -> None:
    if clear_target_projects and target_project:
        formatter.error(
            message="--clear-target-projects cannot be combined with --target-project",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)


@rls_app.callback(invoke_without_command=True)
def _rls_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "rls")


def _is_interactive() -> bool:
    """True when BOTH stdin and stdout are terminals -- the same rule the checkbox picker applies.

    Checked up front so a non-interactive `rls setup` refuses before it makes any API call, rather
    than listing the project's tables and only then failing in the picker.
    """
    return _stdio_is_tty()


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
        "Owner Project",
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
            str(policy.get("owner_project_id") or "(organization)"),
            ", ".join(str(p) for p in policy.get("target_project_ids", [])),
        )
    formatter.console.print(tbl)


def _print_policy(formatter: Any, row: dict[str, Any]) -> None:
    formatter.console.print(
        f"\n[bold]{row.get('table', '')}[/bold] [dim](id {row.get('id', '')})[/dim]"
    )
    formatter.console.print(f"  Dialect:          {row.get('dialect', '')}")
    formatter.console.print(f"  Scope:            {row.get('scope', '')}")
    formatter.console.print(
        f"  Owner project:    {row.get('owner_project_id') or '(organization)'}"
    )
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


def _print_warnings(formatter: Any, result: dict[str, Any]) -> None:
    """Human mode only -- in `--json` mode the warnings are already part of the payload."""
    for warning in result.get("warnings", []):
        formatter.warning(warning)


def _parse_rules_arg(
    formatter: Any, raw: str, shape: str = "{principal|principals, condition}"
) -> list[dict[str, Any]]:
    """Parse ``--rules``; ``shape`` is only the hint in the error (``cls`` passes its own)."""
    try:
        parsed = parse_json_arg(raw, label="--rules")
    except ValueError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.INVALID_ARGUMENT)
        raise typer.Exit(code=2) from None
    if not isinstance(parsed, list):
        formatter.error(
            message=f"--rules must be a JSON array of {shape} objects",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None
    return parsed


# ---------------------------------------------------------------------------
# rls list / detail / schema
# ---------------------------------------------------------------------------


@rls_app.command("list")
def rls_list(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
) -> None:
    """List row-level security policies visible to a project."""
    formatter = get_formatter(ctx)
    result = _call(formatter, get_service(ctx, "rls_service").list_policies, alias=project)

    if formatter.json_mode:
        formatter.output(result)
    elif not result.get("policies"):
        formatter.console.print("[dim]No RLS policies found.[/dim]")
    else:
        _format_policy_table(formatter, result["policies"])


@rls_app.command("detail")
def rls_detail(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    policy_id: str = typer.Option(..., "--policy-id", help="RLS policy ID"),
) -> None:
    """Show one RLS policy's full rule set."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "rls_service")
    result = _call(formatter, service.get_policy, alias=project, policy_id=policy_id)

    if formatter.json_mode:
        formatter.output(result)
    else:
        _print_policy(formatter, result)


def print_schema(ctx: typer.Context, service_name: str, item_type: str, project: str) -> None:
    """``rls schema`` / ``cls schema``: the live schema, no offline bundled snapshot."""
    formatter = get_formatter(ctx)
    # An auth/permission failure is raised (not "schema unavailable") and reported by `_call`.
    fetch = _call(formatter, get_service(ctx, service_name).fetch_schema, alias=project)
    if fetch.schema is None:
        formatter.error(
            message=f"Could not fetch the {item_type} schema: {fetch.reason}",
            error_code=ErrorCode.NOT_FOUND,
        )
        raise typer.Exit(code=4)

    if formatter.json_mode:
        formatter.output({"format": "json-schema", "source": "live", "schema": fetch.schema})
        return
    formatter.console.print(
        Syntax(json.dumps(fetch.schema, indent=2), "json", theme="monokai", line_numbers=False)
    )


@rls_app.command("schema")
def rls_schema(
    ctx: typer.Context,
    project: str = typer.Option(
        ..., "--project", help="Project alias -- fetch the live schema from this stack"
    ),
) -> None:
    """Print the live ``rls-policy`` JSON Schema fetched from the metastore.

    Unlike ``flow schema``, there is no offline bundled snapshot -- the
    schema is authoritative only from the live metastore, and on a stack whose
    metastore predates it, fetching fails with a clean, classified error.
    """
    print_schema(ctx, "rls_service", "rls-policy", project)


# ---------------------------------------------------------------------------
# rls create / update / delete
# ---------------------------------------------------------------------------


@rls_app.command("create")
def rls_create(
    ctx: typer.Context,
    project: str = typer.Option(
        ..., "--project", help="Project alias -- the project that owns the policy"
    ),
    table_id: str = typer.Option(..., "--table-id", help=TABLE_HELP),
    rules: str = typer.Option(
        ...,
        "--rules",
        help=(
            "JSON|@file|- array of {principal|principals, condition} objects "
            "(see `rls schema` / rls-workflow.md for the condition shape)"
        ),
    ),
    dialect: Dialect | None = typer.Option(None, "--dialect", help=DIALECT_HELP),
    scope: PolicyScope = typer.Option(PolicyScope.TARGETED, "--scope", help=SCOPE_HELP),
    target_project: list[str] | None = typer.Option(None, "--target-project", help=TARGET_HELP),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview the compiled condition without writing"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Create one RLS policy for one table (``targeted`` scope unless ``--scope organization``)."""
    gate_scope(ctx, "rls", "create", scope, target_project)
    formatter = get_formatter(ctx)
    service = get_service(ctx, "rls_service")
    kwargs = {
        "alias": project,
        "table": table_id,
        "dialect": dialect,
        "rules": _parse_rules_arg(formatter, rules),
        "scope": scope.value,
        "target_projects": target_project,
    }

    if not dry_run and not yes and not formatter.json_mode:
        # A validation/network error here is reported properly by the real call below.
        with contextlib.suppress(ConfigError, KeboolaApiError):
            _print_preview(formatter, service.create_policy(**kwargs, dry_run=True))
        _confirm_or_exit(formatter, f"Create RLS policy on {table_id}?")

    result = _call(formatter, service.create_policy, **kwargs, dry_run=dry_run)

    if formatter.json_mode:
        formatter.output(result)
        return
    _print_warnings(formatter, result)
    if not dry_run:
        formatter.success(f"Created RLS policy {result.get('id', '')} on {table_id}")
    _print_preview(formatter, result)


@rls_app.command("update")
def rls_update(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    policy_id: str = typer.Option(..., "--policy-id", help="RLS policy ID"),
    table_id: str | None = typer.Option(None, "--table-id", help="New table (unset = unchanged)"),
    dialect: Dialect | None = typer.Option(
        None, "--dialect", help="New dialect, must match the project backend (unset = unchanged)"
    ),
    rules: str | None = typer.Option(
        None, "--rules", help="New JSON|@file|- rules array (unset = unchanged)"
    ),
    target_project: list[str] | None = typer.Option(
        None, "--target-project", help=f"{TARGET_HELP}; replaces the list (unset = unchanged)"
    ),
    clear_target_projects: bool = typer.Option(
        False,
        "--clear-target-projects",
        help="Revoke every project the policy is shared with (cannot be combined with --target-project)",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview the compiled condition without writing"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Update an existing RLS policy.

    Only the flags you pass change -- an omitted flag keeps the policy's
    current value. The write is a partial update (PATCH) of just those keys.
    """
    formatter = get_formatter(ctx)
    parsed_rules = _parse_rules_arg(formatter, rules) if rules is not None else None
    reject_target_conflict(formatter, target_project, clear_target_projects)
    service = get_service(ctx, "rls_service")

    if not dry_run and not yes and not formatter.json_mode:
        _confirm_or_exit(formatter, f"Update RLS policy {policy_id}?")

    result = _call(
        formatter,
        service.update_policy,
        alias=project,
        policy_id=policy_id,
        table=table_id,
        dialect=dialect,
        rules=parsed_rules,
        target_projects=[] if clear_target_projects else target_project,
        dry_run=dry_run,
    )

    if formatter.json_mode:
        formatter.output(result)
        return
    _print_warnings(formatter, result)
    if not dry_run:
        formatter.success(f"Updated RLS policy {policy_id}")
    _print_preview(formatter, result)


def delete_policy(
    ctx: typer.Context, service_name: str, label: str, effect: str, **kwargs: Any
) -> None:
    """``rls delete`` / ``cls delete``; ``--dry-run`` shows the policy that would be deleted."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, service_name)
    policy_id, dry_run, yes = kwargs["policy_id"], kwargs["dry_run"], kwargs.pop("yes")

    if not dry_run and not yes and not formatter.json_mode:
        _confirm_or_exit(formatter, f"Delete {label} policy {policy_id}? {effect}")

    result = _call(formatter, service.delete_policy, **kwargs)

    if formatter.json_mode:
        formatter.output(result)
    elif dry_run:
        formatter.console.print(f"[bold]Would delete[/bold] {label} policy {policy_id}:")
        formatter.console.print_json(data=result["policy"])
    else:
        formatter.success(f"Deleted {label} policy {policy_id}")


@rls_app.command("delete")
def rls_delete(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    policy_id: str = typer.Option(..., "--policy-id", help="RLS policy ID"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the policy without deleting it"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Delete an RLS policy -- its table's rows are no longer filtered."""
    delete_policy(
        ctx,
        "rls_service",
        "RLS",
        "This un-protects its table.",
        alias=project,
        policy_id=policy_id,
        dry_run=dry_run,
        yes=yes,
    )


# ---------------------------------------------------------------------------
# rls setup (guided, interactive-terminal-only -- no REST route, see
# CONTRIBUTING.md's "genuinely terminal-only" carve-out)
# ---------------------------------------------------------------------------

_SETUP_HINT = (
    "rls setup is an interactive picker and needs a real terminal. "
    "Use `rls create --project P --table-id T --rules '[...]'` "
    "directly instead (see rls-workflow.md)."
)

_CONDITION_MENU = (
    "  1) column comparison (=, !=, >, >=, <, <=)\n"
    "  2) column IN / NOT IN a list of values\n"
    "  3) column IS NULL / IS NOT NULL\n"
    "  4) always true (no filtering for this principal)"
)

_COMPARISON_OPS = ("eq", "ne", "gt", "gte", "lt", "lte")
_VALUE_HINT = 'parsed as JSON when it is one (42, 4.5, true); quote it ("42") to force a string'


def _prompt_choice(text: str, choices: tuple[str, ...], default: str) -> str:
    while True:
        value = typer.prompt(text, default=default)
        if value in choices:
            return value
        typer.echo(f"Enter one of: {', '.join(choices)}")


def _typed_value(raw: str) -> Any:
    """A prompted value as the JSON scalar it spells (``42``, ``true``), else the text itself.

    Without this every value would be stored as a string, and the filter would compare a numeric or
    boolean column to a string literal. ``null`` is not a value here: it never matches (use option 3).
    """
    try:
        value = json.loads(raw)
    except ValueError:
        return raw
    return value if isinstance(value, str | int | float | bool) else raw


def _prompt_condition() -> dict[str, Any]:
    typer.echo(_CONDITION_MENU)
    choice = _prompt_choice("Choice", ("1", "2", "3", "4"), default="1")
    if choice == "4":
        return {"true": True}
    column = typer.prompt("Column name")
    if choice == "1":
        op = _prompt_choice("Operator", _COMPARISON_OPS, default="eq")
        value = _typed_value(typer.prompt(f"Value ({_VALUE_HINT})"))
        return {"column": column, "op": op, "value": value}
    if choice == "2":
        op = _prompt_choice("Operator", ("in", "not_in"), default="in")
        raw_values = typer.prompt(f"Values (comma-separated; each {_VALUE_HINT})")
        values = [_typed_value(v.strip()) for v in raw_values.split(",") if v.strip()]
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
    dialect: Dialect | None = typer.Option(None, "--dialect", help=DIALECT_HELP),
    rules: str | None = typer.Option(
        None,
        "--rules",
        help="JSON|@file|- rules array -- skips the interactive condition builder",
    ),
    scope: PolicyScope = typer.Option(PolicyScope.TARGETED, "--scope", help=SCOPE_HELP),
    target_project: list[str] | None = typer.Option(None, "--target-project", help=TARGET_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the final confirmation"),
) -> None:
    """Guided RLS setup: pick tables, build a condition, preview, then write.

    Interactive-terminal-only (with ``--json`` or without a terminal it exits 2
    with a usage error -- use ``rls create`` there). Creates one RLS policy per
    selected table, all sharing the same rules, via the exact same write path
    ``rls create`` uses.
    """
    # `setup` performs `create`'s writes, so an exact `rls.create` denial must cover it too.
    check_cli_operation(ctx, "rls.create")
    gate_scope(ctx, "rls", "create", scope, target_project)
    formatter = get_formatter(ctx)
    if formatter.json_mode or not _is_interactive():
        formatter.error(message=_SETUP_HINT, error_code=ErrorCode.INVALID_ARGUMENT)
        raise typer.Exit(code=2)

    tables = _call(
        formatter, get_service(ctx, "storage_service").list_tables, aliases=[project]
    ).get("tables", [])
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
        formatter.error(message=_SETUP_HINT, error_code=ErrorCode.INVALID_ARGUMENT)
        raise typer.Exit(code=2) from None

    selected = indices or []
    if not selected:
        formatter.console.print("No tables selected.")
        raise typer.Exit(code=0)

    parsed_rules = (
        _parse_rules_arg(formatter, rules) if rules is not None else _build_rules_interactively()
    )
    service = get_service(ctx, "rls_service")
    selected_tables = [str(tables[i]["id"]) for i in selected]
    kwargs = {
        "alias": project,
        "dialect": dialect,  # None = the project backend, resolved by the service
        "rules": parsed_rules,
        "scope": scope.value,
        "target_projects": target_project,
    }

    for table_id in selected_tables:
        try:
            preview = service.create_policy(table=table_id, **kwargs, dry_run=True)
        except (ConfigError, KeboolaApiError) as exc:
            formatter.console.print(f"[yellow]{table_id}: preview failed ({exc}).[/yellow]")
            continue
        _print_preview(formatter, preview)

    if not yes:
        _confirm_or_exit(
            formatter,
            f"Create {len(selected_tables)} RLS polic{'y' if len(selected_tables) == 1 else 'ies'}?",
        )

    failed: list[str] = []
    exit_codes: set[int] = set()
    error_codes: set[str] = set()
    for table_id in selected_tables:
        try:
            result = service.create_policy(table=table_id, **kwargs)
        except (ConfigError, KeboolaApiError) as exc:
            formatter.warning(f"{table_id}: {exc}")
            failed.append(table_id)
            exit_codes.add(5 if isinstance(exc, ConfigError) else map_error_to_exit_code(exc))
            error_codes.add(
                ErrorCode.CONFIG_ERROR if isinstance(exc, ConfigError) else exc.error_code
            )
            continue
        _print_warnings(formatter, result)
        formatter.success(f"Created RLS policy {result.get('id', '')} on {table_id}")
    if failed:
        # Automation must not read a partly (or wholly) failed setup as success.
        formatter.error(
            message=f"{len(failed)} of {len(selected_tables)} polic{'y' if len(selected_tables) == 1 else 'ies'}"
            f" could not be created: {', '.join(failed)}",
            error_code=error_codes.pop() if len(error_codes) == 1 else ErrorCode.API_ERROR,
        )
        # Same exit-code contract as `rls create`: when every failure maps to one code (auth -> 3,
        # config -> 5, ...) use it; a mix is a general failure.
        raise typer.Exit(code=exit_codes.pop() if len(exit_codes) == 1 else 1)
