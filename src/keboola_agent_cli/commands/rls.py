"""Row-level security commands (CLI-17).

Thin CLI layer over :class:`RlsService`. Seven subcommands:

- ``rls list`` -- policies visible to a project.
- ``rls detail`` -- one policy's full rules.
- ``rls schema`` -- the live ``rls-policy`` JSON Schema from the metastore.
- ``rls create`` -- author a policy for one table (write).
- ``rls update`` -- partial (PATCH) update of an existing policy (write).
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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import typer
from rich.syntax import Syntax
from rich.table import Table

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..services._rls_condition import COMPARISON_SQL, Dialect
from ..services.rls_service import PolicyScope
from ._checkbox_select import CheckboxItem, CheckboxUnavailable, _stdio_is_tty, checkbox_select
from ._helpers import (
    check_cli_operation,
    check_cli_permission,
    exit_code_for,
    get_formatter,
    get_service,
    handle_service_call,
    parse_json_arg,
)

rls_app = typer.Typer(
    help=(
        "Manage row-level security policies (metastore-backed). "
        "'list'/'detail'/'schema' are read-only; 'create'/'update'/'setup'/'delete' "
        "are gated as admin -- see CONTRIBUTING.md."
    )
)


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


@dataclass(frozen=True)
class PolicyGroup:
    """What differs between the ``rls`` and ``cls`` command groups; everything else is shared."""

    name: str  # "rls" | "cls": the permission prefix, `<name>_service`, `<name>-policy`
    rules_shape: str  # shown in the --rules parse error
    rule_text: Callable[[dict[str, Any]], str]  # one stored rule's restriction
    preview_text: Callable[[dict[str, Any]], str]  # one --dry-run preview entry's restriction

    @property
    def service(self) -> str:
        return f"{self.name}_service"

    @property
    def label(self) -> str:
        return self.name.upper()


RLS = PolicyGroup(
    name="rls",
    rules_shape="{principal|principals, condition}",
    rule_text=lambda rule: str(rule.get("condition")),
    preview_text=lambda entry: f"WHERE {entry.get('condition')}",
)


@rls_app.callback(invoke_without_command=True)
def _rls_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "rls")


# ---------------------------------------------------------------------------
# Shared helpers (also used by `cls`)
# ---------------------------------------------------------------------------


def _confirm_or_exit(formatter: Any, question: str) -> None:
    if not typer.confirm(question):
        formatter.console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(code=0)


def gate_scope(
    ctx: typer.Context, group: PolicyGroup, scope: PolicyScope, target_project: list[str] | None
) -> None:
    """Validate ``--scope`` before any API call or prompt.

    ``--scope organization`` governs the table in every project: a destructive-class flag. It has
    no grants, so ``--target-project`` with it is a usage error (exit 2).
    """
    if scope != PolicyScope.ORGANIZATION:
        return
    check_cli_operation(ctx, f"{group.name}.create --scope organization")
    if target_project:
        get_formatter(ctx).error(
            message="--target-project requires --scope targeted (organization scope has no grants)",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)


def _principal_text(item: dict[str, Any]) -> str:
    principal = item.get("principal") or item.get("principals") or []
    return principal if isinstance(principal, str) else ", ".join(principal)


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


def _print_policy(formatter: Any, group: PolicyGroup, row: dict[str, Any]) -> None:
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
        formatter.console.print(f"    - {_principal_text(rule)}: {group.rule_text(rule)}")


def _print_preview(formatter: Any, group: PolicyGroup, result: dict[str, Any]) -> None:
    formatter.console.print(
        f"\n[bold]Preview[/bold] -- {result.get('table', '')} "
        f"[dim]({result.get('dialect', '')}, scope={result.get('scope', '')})[/dim]"
    )
    for entry in result.get("preview", []):
        formatter.console.print(f"  {_principal_text(entry)}: {group.preview_text(entry)}")


def _print_warnings(formatter: Any, result: dict[str, Any]) -> None:
    """Human mode only -- in `--json` mode the warnings are already part of the payload."""
    for warning in result.get("warnings", []):
        formatter.warning(warning)


def _parse_rules_arg(formatter: Any, raw: str, shape: str) -> list[dict[str, Any]]:
    """Parse ``--rules``; ``shape`` is only the hint in the error."""
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


def list_policies(ctx: typer.Context, group: PolicyGroup, project: str) -> None:
    formatter = get_formatter(ctx)
    service = get_service(ctx, group.service)
    result = handle_service_call(ctx, service.list_policies, alias=project)

    if formatter.json_mode:
        formatter.output(result)
    elif not result.get("policies"):
        formatter.console.print(f"[dim]No {group.label} policies found.[/dim]")
    else:
        _format_policy_table(formatter, result["policies"])


def show_policy(ctx: typer.Context, group: PolicyGroup, project: str, policy_id: str) -> None:
    formatter = get_formatter(ctx)
    service = get_service(ctx, group.service)
    result = handle_service_call(ctx, service.get_policy, alias=project, policy_id=policy_id)

    if formatter.json_mode:
        formatter.output(result)
    else:
        _print_policy(formatter, group, result)


def print_schema(ctx: typer.Context, group: PolicyGroup, project: str) -> None:
    """The live schema, no offline bundled snapshot."""
    formatter = get_formatter(ctx)
    # An auth/permission failure is raised (not "schema unavailable") and reported by the handler.
    service = get_service(ctx, group.service)
    fetch = handle_service_call(ctx, service.fetch_schema, alias=project)
    if fetch.schema is None:
        formatter.error(
            message=f"Could not fetch the {group.name}-policy schema: {fetch.reason}",
            error_code=ErrorCode.NOT_FOUND,
        )
        raise typer.Exit(code=4)

    if formatter.json_mode:
        formatter.output({"format": "json-schema", "source": "live", "schema": fetch.schema})
        return
    formatter.console.print(
        Syntax(json.dumps(fetch.schema, indent=2), "json", theme="monokai", line_numbers=False)
    )


def create_policy(
    ctx: typer.Context,
    group: PolicyGroup,
    *,
    rules: str,
    scope: PolicyScope,
    target_project: list[str] | None,
    dry_run: bool,
    yes: bool,
    **kwargs: Any,
) -> None:
    """``rls create`` / ``cls create``; ``kwargs`` are ``alias``/``table``/``dialect``."""
    gate_scope(ctx, group, scope, target_project)
    formatter = get_formatter(ctx)
    service = get_service(ctx, group.service)
    kwargs |= {
        "rules": _parse_rules_arg(formatter, rules, group.rules_shape),
        "scope": scope.value,
        "target_projects": target_project,
    }

    if not dry_run and not yes and not formatter.json_mode:
        # A validation/network error here is reported properly by the real call below.
        with contextlib.suppress(ConfigError, KeboolaApiError):
            _print_preview(formatter, group, service.create_policy(**kwargs, dry_run=True))
        _confirm_or_exit(formatter, f"Create {group.label} policy on {kwargs['table']}?")

    result = handle_service_call(ctx, service.create_policy, **kwargs, dry_run=dry_run)

    if formatter.json_mode:
        formatter.output(result)
        return
    _print_warnings(formatter, result)
    if not dry_run:
        formatter.success(
            f"Created {group.label} policy {result.get('id', '')} on {kwargs['table']}"
        )
    _print_preview(formatter, group, result)


def update_policy(
    ctx: typer.Context,
    group: PolicyGroup,
    *,
    rules: str | None,
    target_project: list[str] | None,
    clear_target_projects: bool,
    dry_run: bool,
    yes: bool,
    **kwargs: Any,
) -> None:
    """``rls update`` / ``cls update``; ``kwargs`` are ``alias``/``policy_id``/``table``/``dialect``."""
    formatter = get_formatter(ctx)
    parsed_rules = (
        _parse_rules_arg(formatter, rules, group.rules_shape) if rules is not None else None
    )
    if clear_target_projects and target_project:
        formatter.error(
            message="--clear-target-projects cannot be combined with --target-project",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)
    service = get_service(ctx, group.service)
    policy_id = kwargs["policy_id"]

    if not dry_run and not yes and not formatter.json_mode:
        _confirm_or_exit(formatter, f"Update {group.label} policy {policy_id}?")

    result = handle_service_call(
        ctx,
        service.update_policy,
        **kwargs,
        rules=parsed_rules,
        target_projects=[] if clear_target_projects else target_project,
        dry_run=dry_run,
    )

    if formatter.json_mode:
        formatter.output(result)
        return
    _print_warnings(formatter, result)
    if not dry_run:
        formatter.success(f"Updated {group.label} policy {policy_id}")
    _print_preview(formatter, group, result)


def delete_policy(
    ctx: typer.Context,
    group: PolicyGroup,
    effect: str,
    *,
    alias: str,
    policy_id: str,
    dry_run: bool,
    yes: bool,
) -> None:
    """``rls delete`` / ``cls delete``; ``--dry-run`` shows the policy that would be deleted."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, group.service)

    if not dry_run and not yes and not formatter.json_mode:
        _confirm_or_exit(formatter, f"Delete {group.label} policy {policy_id}? {effect}")

    result = handle_service_call(
        ctx, service.delete_policy, alias=alias, policy_id=policy_id, dry_run=dry_run
    )

    if formatter.json_mode:
        formatter.output(result)
    elif dry_run:
        formatter.console.print(f"[bold]Would delete[/bold] {group.label} policy {policy_id}:")
        formatter.console.print_json(data=result["policy"])
    else:
        formatter.success(f"Deleted {group.label} policy {policy_id}")


# ---------------------------------------------------------------------------
# rls commands
# ---------------------------------------------------------------------------


@rls_app.command("list")
def rls_list(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
) -> None:
    """List row-level security policies visible to a project."""
    list_policies(ctx, RLS, project)


@rls_app.command("detail")
def rls_detail(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    policy_id: str = typer.Option(..., "--policy-id", help="RLS policy ID"),
) -> None:
    """Show one RLS policy's full rule set."""
    show_policy(ctx, RLS, project, policy_id)


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
    print_schema(ctx, RLS, project)


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
    create_policy(
        ctx,
        RLS,
        alias=project,
        table=table_id,
        dialect=dialect,
        rules=rules,
        scope=scope,
        target_project=target_project,
        dry_run=dry_run,
        yes=yes,
    )


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
    update_policy(
        ctx,
        RLS,
        alias=project,
        policy_id=policy_id,
        table=table_id,
        dialect=dialect,
        rules=rules,
        target_project=target_project,
        clear_target_projects=clear_target_projects,
        dry_run=dry_run,
        yes=yes,
    )


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
        RLS,
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

_COMPARISON_OPS = tuple(COMPARISON_SQL)  # eq, ne, gt, gte, lt, lte
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
    gate_scope(ctx, RLS, scope, target_project)
    formatter = get_formatter(ctx)
    if formatter.json_mode or not _stdio_is_tty():
        formatter.error(message=_SETUP_HINT, error_code=ErrorCode.INVALID_ARGUMENT)
        raise typer.Exit(code=2)

    storage_service = get_service(ctx, "storage_service")
    tables = handle_service_call(ctx, storage_service.list_tables, aliases=[project]).get(
        "tables", []
    )
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

    if not indices:
        formatter.console.print("No tables selected.")
        raise typer.Exit(code=0)

    parsed_rules = (
        _parse_rules_arg(formatter, rules, RLS.rules_shape)
        if rules is not None
        else _build_rules_interactively()
    )
    service = get_service(ctx, RLS.service)
    selected_tables = [str(tables[i]["id"]) for i in indices]
    policies = f"{len(selected_tables)} RLS polic{'y' if len(selected_tables) == 1 else 'ies'}"
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
        _print_preview(formatter, RLS, preview)

    if not yes:
        _confirm_or_exit(formatter, f"Create {policies}?")

    failed: list[str] = []
    exit_codes: set[int] = set()
    error_codes: set[str] = set()
    for table_id in selected_tables:
        try:
            result = service.create_policy(table=table_id, **kwargs)
        except (ConfigError, KeboolaApiError) as exc:
            formatter.warning(f"{table_id}: {exc}")
            failed.append(table_id)
            exit_codes.add(exit_code_for(exc))
            error_codes.add(
                ErrorCode.CONFIG_ERROR if isinstance(exc, ConfigError) else exc.error_code
            )
            continue
        _print_warnings(formatter, result)
        formatter.success(f"Created RLS policy {result.get('id', '')} on {table_id}")
    if failed:
        # Automation must not read a partly (or wholly) failed setup as success.
        formatter.error(
            message=f"{len(failed)} of {policies} could not be created: {', '.join(failed)}",
            error_code=error_codes.pop() if len(error_codes) == 1 else ErrorCode.API_ERROR,
        )
        # Same exit-code contract as `rls create`: when every failure maps to one code (auth -> 3,
        # config -> 5, ...) use it; a mix is a general failure.
        raise typer.Exit(code=exit_codes.pop() if len(exit_codes) == 1 else 1)
