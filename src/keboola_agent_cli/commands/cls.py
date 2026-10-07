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

import typer

from ._helpers import check_cli_permission
from .rls import (
    DIALECT_HELP,
    SCOPE_HELP,
    TABLE_HELP,
    TARGET_HELP,
    Dialect,
    PolicyGroup,
    PolicyScope,
    create_policy,
    delete_policy,
    list_policies,
    print_schema,
    show_policy,
    update_policy,
)

cls_app = typer.Typer(
    help=(
        "Manage column-level security policies (metastore-backed, per-principal column "
        "allowlist). 'list'/'detail'/'schema' are read-only; 'create'/'update'/'delete' "
        "are gated as admin -- see CONTRIBUTING.md."
    )
)

CLS = PolicyGroup(
    name="cls",
    label="CLS",
    rules_shape="{principal|principals, visible_columns}",
    rule_text=lambda rule: ", ".join(rule.get("visible_columns") or []),
    preview_text=lambda entry: f"SELECT {', '.join(entry.get('visible_columns') or [])}",
)


@cls_app.callback(invoke_without_command=True)
def _cls_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "cls")


@cls_app.command("list")
def cls_list(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
) -> None:
    """List column-level security policies visible to a project."""
    list_policies(ctx, CLS, project)


@cls_app.command("detail")
def cls_detail(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    policy_id: str = typer.Option(..., "--policy-id", help="CLS policy ID"),
) -> None:
    """Show one CLS policy's full rule set."""
    show_policy(ctx, CLS, project, policy_id)


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
    print_schema(ctx, CLS, project)


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
            f"JSON|@file|- array of {CLS.rules_shape} objects; visible_columns is the "
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
    create_policy(
        ctx,
        CLS,
        alias=project,
        table=table_id,
        dialect=dialect,
        rules=rules,
        scope=scope,
        target_project=target_project,
        dry_run=dry_run,
        yes=yes,
    )


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
    update_policy(
        ctx,
        CLS,
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
        CLS,
        "Its table's columns become unrestricted.",
        alias=project,
        policy_id=policy_id,
        dry_run=dry_run,
        yes=yes,
    )
