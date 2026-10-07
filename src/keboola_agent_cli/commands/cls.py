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
``targeted`` scope unless ``--scope organization`` is passed -- never
``project``. Enforcement happens in ``keboola-mcp-server``'s
``query_data``; kbagent only authors policies.

``list``/``detail``/``schema`` are read-only and safe under ``--deny-writes``;
``create``/``update`` are gated as ``admin``, ``delete`` and ``--scope
organization`` as ``destructive`` (``cls.*`` in ``OPERATION_REGISTRY`` /
``FLAG_ESCALATIONS``).
"""

from __future__ import annotations

import contextlib
from typing import Any

import typer

from ..errors import ConfigError, KeboolaApiError
from ._helpers import check_cli_permission, get_formatter, get_service
from .rls import (
    DIALECT_HELP,
    SCOPE_HELP,
    TABLE_HELP,
    TARGET_HELP,
    Dialect,
    PolicyScope,
    _call,
    _confirm_or_exit,
    _format_policy_table,
    _parse_rules_arg,
    _print_warnings,
    delete_policy,
    gate_scope,
    print_schema,
    reject_target_conflict,
)

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


@cls_app.command("list")
def cls_list(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
) -> None:
    """List column-level security policies visible to a project."""
    formatter = get_formatter(ctx)
    result = _call(formatter, get_service(ctx, "cls_service").list_policies, alias=project)

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
    print_schema(ctx, "cls_service", "cls-policy", project)


@cls_app.command("create")
def cls_create(
    ctx: typer.Context,
    project: str = typer.Option(
        ..., "--project", help="Project alias -- the project that owns the policy"
    ),
    table_id: str = typer.Option(..., "--table-id", help=TABLE_HELP),
    rules: str = typer.Option(
        ...,
        "--rules",
        help=(
            f"JSON|@file|- array of {_RULES_SHAPE} objects; visible_columns is the "
            "allowlist of columns that principal can read"
        ),
    ),
    dialect: Dialect | None = typer.Option(None, "--dialect", help=DIALECT_HELP),
    scope: PolicyScope = typer.Option(PolicyScope.TARGETED, "--scope", help=SCOPE_HELP),
    target_project: list[str] | None = typer.Option(None, "--target-project", help=TARGET_HELP),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the projection without writing"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Create one CLS policy for one table (``targeted`` scope unless ``--scope organization``)."""
    gate_scope(ctx, "cls", "create", scope, target_project)
    formatter = get_formatter(ctx)
    service = get_service(ctx, "cls_service")
    kwargs = {
        "alias": project,
        "table": table_id,
        "dialect": dialect,
        "rules": _parse_rules_arg(formatter, rules, _RULES_SHAPE),
        "scope": scope.value,
        "target_projects": target_project,
    }

    if not dry_run and not yes and not formatter.json_mode:
        # A validation/network error here is reported properly by the real call below.
        with contextlib.suppress(ConfigError, KeboolaApiError):
            _print_preview(formatter, service.create_policy(**kwargs, dry_run=True))
        _confirm_or_exit(formatter, f"Create CLS policy on {table_id}?")

    result = _call(formatter, service.create_policy, **kwargs, dry_run=dry_run)

    if formatter.json_mode:
        formatter.output(result)
        return
    _print_warnings(formatter, result)
    if not dry_run:
        formatter.success(f"Created CLS policy {result.get('id', '')} on {table_id}")
    _print_preview(formatter, result)


@cls_app.command("update")
def cls_update(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    policy_id: str = typer.Option(..., "--policy-id", help="CLS policy ID"),
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
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the projection without writing"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Update an existing CLS policy.

    Only the flags you pass change -- an omitted flag keeps the policy's
    current value. The write is a partial update (PATCH) of just those keys.
    """
    formatter = get_formatter(ctx)
    parsed_rules = _parse_rules_arg(formatter, rules, _RULES_SHAPE) if rules is not None else None
    reject_target_conflict(formatter, target_project, clear_target_projects)
    service = get_service(ctx, "cls_service")

    if not dry_run and not yes and not formatter.json_mode:
        _confirm_or_exit(formatter, f"Update CLS policy {policy_id}?")

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
        formatter.success(f"Updated CLS policy {policy_id}")
    _print_preview(formatter, result)


@cls_app.command("delete")
def cls_delete(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    policy_id: str = typer.Option(..., "--policy-id", help="CLS policy ID"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the policy without deleting it"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Delete a CLS policy -- its table's columns become unrestricted."""
    delete_policy(
        ctx,
        "cls_service",
        "CLS",
        "Its table's columns become unrestricted.",
        alias=project,
        policy_id=policy_id,
        dry_run=dry_run,
        yes=yes,
    )
