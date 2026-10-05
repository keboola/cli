"""Column-level security commands.

Thin CLI layer over :class:`ClsService`, the column-level sibling of
``kbagent rls``. Six subcommands (no interactive ``setup`` wizard -- a CLS
rule is just a principal plus a column allowlist, so ``create`` covers it):

- ``cls list`` -- policies visible to a project.
- ``cls detail`` -- one policy's full rules.
- ``cls schema`` -- the live ``cls-policy`` JSON Schema from the metastore.
- ``cls create`` -- author a policy for one table (write).
- ``cls update`` -- fetch-then-merge update of an existing policy (write).
- ``cls delete`` -- remove a policy (destructive).

Each rule is ``{principal|principals, visible_columns: [...]}``: the listed
columns are the only ones that principal can read (allowlist projection;
masking is not supported). As with ``rls``, every write is authored at
``organization`` or ``targeted`` scope, never ``project`` -- there is no
``--scope`` flag. Enforcement happens in ``keboola-mcp-server``'s
``query_data``; kbagent only authors policies.

``list``/``detail``/``schema`` are read-only and safe under ``--deny-writes``;
``create``/``update``/``delete`` are gated as ``admin`` (``cls.*`` in
``OPERATION_REGISTRY``).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from typing import Any

import typer
from rich.syntax import Syntax

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..services.rls_service import RLS_DIALECTS
from ._helpers import check_cli_permission, get_formatter, get_service, map_error_to_exit_code
from .rls import _format_policy_table, _parse_rules_arg, _print_warnings

cls_app = typer.Typer(
    help=(
        "Manage column-level security policies (metastore-backed, per-principal column "
        "allowlist). 'list'/'detail'/'schema' are read-only; 'create'/'update'/'delete' "
        "are gated as admin -- see CONTRIBUTING.md."
    )
)

_RULES_SHAPE = "{principal|principals, visible_columns}"


@cls_app.callback(invoke_without_command=True)
def _cls_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "cls")


def _call(formatter: Any, fn: Callable[..., dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    """Run a service call, turning its errors into a formatted message + exit code."""
    try:
        return fn(**kwargs)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None


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
        formatter.console.print(
            f"    - {principal}: {', '.join(rule.get('visible_columns') or [])}"
        )


def _print_preview(formatter: Any, result: dict[str, Any]) -> None:
    formatter.console.print(
        f"\n[bold]Preview[/bold] -- {result.get('table', '')} "
        f"[dim]({result.get('dialect', '')}, scope={result.get('scope', '')})[/dim]"
    )
    for entry in result.get("preview", []):
        formatter.console.print(
            f"  {entry.get('principal')}: SELECT {', '.join(entry.get('visible_columns') or [])}"
        )


def _check_dialect(formatter: Any, dialect: str | None) -> None:
    if dialect is not None and dialect not in RLS_DIALECTS:
        formatter.error(
            message=f"--dialect must be one of {RLS_DIALECTS}, got {dialect!r}",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None


def _confirm_or_exit(formatter: Any, question: str) -> None:
    if not typer.confirm(question):
        formatter.console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(code=0)


@cls_app.command("list")
def cls_list(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
) -> None:
    """List column-level security policies visible to a project."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "cls_service")
    result = _call(formatter, service.list_policies, alias=project)

    if formatter.json_mode:
        formatter.output(result)
    elif not result.get("policies"):
        formatter.console.print("[dim]No CLS policies found.[/dim]")
    else:
        _format_policy_table(formatter, result["policies"])


@cls_app.command("detail")
def cls_detail(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    policy_id: str = typer.Option(..., "--policy-id", help="CLS policy ID"),
) -> None:
    """Show one CLS policy's full rule set."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "cls_service")
    result = _call(formatter, service.get_policy, alias=project, policy_id=policy_id)

    if formatter.json_mode:
        formatter.output(result)
    else:
        _print_policy(formatter, result)


@cls_app.command("schema")
def cls_schema(
    ctx: typer.Context,
    project: str = typer.Option(
        ..., "--project", help="Project alias -- fetch the live schema from this stack"
    ),
) -> None:
    """Print the live ``cls-policy`` JSON Schema fetched from the metastore.

    Live-only (no bundled snapshot): if the metastore of that stack does not
    have the ``cls-policy`` object type registered, this fails with a clean,
    classified ``NOT_FOUND`` error.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "cls_service")
    try:
        fetch = service.fetch_schema(project)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:  # an auth/permission failure is not "schema unavailable"
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if fetch.schema is None:
        formatter.error(
            message=f"Could not fetch the cls-policy schema: {fetch.reason}",
            error_code=ErrorCode.NOT_FOUND,
        )
        raise typer.Exit(code=4)

    if formatter.json_mode:
        formatter.output({"format": "json-schema", "source": "live", "schema": fetch.schema})
        return
    formatter.console.print(
        Syntax(json.dumps(fetch.schema, indent=2), "json", theme="monokai", line_numbers=False)
    )


@cls_app.command("create")
def cls_create(
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
            f"JSON|@file|- array of {_RULES_SHAPE} objects; visible_columns is the "
            "allowlist of columns that principal can read"
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
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the projection without writing"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Create one CLS policy for one table.

    Always authored at ``organization`` scope, or ``targeted`` scope when
    ``--target-project`` is given -- never plain ``project`` scope.
    """
    formatter = get_formatter(ctx)
    parsed_rules = _parse_rules_arg(formatter, rules, _RULES_SHAPE)
    _check_dialect(formatter, dialect)
    service = get_service(ctx, "cls_service")
    kwargs = {
        "alias": project,
        "table": table,
        "dialect": dialect,
        "rules": parsed_rules,
        "target_project_ids": target_project,
    }

    if not dry_run and not yes and not formatter.json_mode:
        # A validation/network error here is reported properly by the real call below.
        with contextlib.suppress(ConfigError, KeboolaApiError):
            _print_preview(formatter, service.create_policy(**kwargs, dry_run=True))
        _confirm_or_exit(formatter, f"Create CLS policy on {table}?")

    result = _call(formatter, service.create_policy, **kwargs, dry_run=dry_run)

    if formatter.json_mode:
        formatter.output(result)
        return
    _print_warnings(formatter, result)
    if not dry_run:
        formatter.success(f"Created CLS policy {result.get('id', '')} on {table}")
    _print_preview(formatter, result)


@cls_app.command("update")
def cls_update(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    policy_id: str = typer.Option(..., "--policy-id", help="CLS policy ID"),
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
    clear_target_projects: bool = typer.Option(
        False,
        "--clear-target-projects",
        help="Revoke every project the policy is shared with (cannot be combined with --target-project)",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the projection without writing"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Update an existing CLS policy.

    Fetch-then-merge: only the flags you pass are changed -- an omitted flag
    keeps the policy's current value, it is never silently blanked.
    """
    formatter = get_formatter(ctx)
    parsed_rules = _parse_rules_arg(formatter, rules, _RULES_SHAPE) if rules is not None else None
    if clear_target_projects and target_project:
        formatter.error(
            message="--clear-target-projects cannot be combined with --target-project",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None
    _check_dialect(formatter, dialect)
    service = get_service(ctx, "cls_service")

    if not dry_run and not yes and not formatter.json_mode:
        _confirm_or_exit(formatter, f"Update CLS policy {policy_id}?")

    result = _call(
        formatter,
        service.update_policy,
        alias=project,
        policy_id=policy_id,
        table=table,
        dialect=dialect,
        rules=parsed_rules,
        target_project_ids=[] if clear_target_projects else target_project,
        dry_run=dry_run,
    )

    if formatter.json_mode:
        formatter.output(result)
        return
    _print_warnings(formatter, result)
    if not dry_run:
        formatter.success(f"Updated CLS policy {policy_id}")
    _print_preview(formatter, result)


@cls_app.command("delete")
def cls_delete(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    policy_id: str = typer.Option(..., "--policy-id", help="CLS policy ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Delete a CLS policy."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "cls_service")

    if not yes and not formatter.json_mode:
        _confirm_or_exit(
            formatter, f"Delete CLS policy {policy_id}? Its table's columns become unrestricted."
        )

    result = _call(formatter, service.delete_policy, alias=project, policy_id=policy_id)

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.success(f"Deleted CLS policy {policy_id}")
