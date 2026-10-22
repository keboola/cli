"""Policy-binding commands of the ``rls`` group: ``bind``, ``unbind``, ``bindings``, ``bindings-sweep``.

A binding restricts one governed workspace to some of the project's rls/cls policies; a workspace
without one gets every policy of the project. Mounted onto ``rls_app`` by ``rls.py`` (that module
is near the commands-file size ceiling), so the commands share the ``rls.*`` permission namespace.
Business logic: :class:`~keboola_agent_cli.services.policy_binding_service.PolicyBindingService`.
"""

from __future__ import annotations

from typing import Any

import typer
from rich.table import Table

from ..constants import ENV_STACK_ID, STACK_ID_EXAMPLE
from ..errors import ErrorCode
from ._helpers import (
    _confirm_or_abort,
    check_cli_operation,
    finish_items,
    get_formatter,
    get_service,
    handle_service_call,
    split_multi,
)

SERVICE = "policy_binding_service"
STACK_ID_HELP = (
    "Stack segment of the workspace KID -- the value Query Service is configured with, "
    f"for example {STACK_ID_EXAMPLE} (no API exposes it)"
)
_ALL_POLICIES = "The workspace now gets every policy of the project again."


def _print_warnings(formatter: Any, result: dict[str, Any]) -> None:
    for warning in result.get("warnings", []):
        formatter.warning(warning)


def _ids(values: list[str]) -> str:
    return ", ".join(values) or "-"


def _bindings_table(formatter: Any, rows: list[dict[str, Any]], *, sweep: bool) -> None:
    table = Table(title="Policy bindings" if not sweep else "Policy bindings sweep")
    for column in ("KID", "RLS policies", "CLS policies", "Updated"):
        table.add_column(column)
    table.add_column("Status" if sweep else "Workspace exists")
    if sweep:
        table.add_column("Reason")
    for row in rows:
        cells = [row["kid"], _ids(row["rls_policy_ids"]), _ids(row["cls_policy_ids"])]
        cells.append(str(row.get("updated") or "-"))
        if sweep:
            cells += [row["status"], row.get("reason", "")]
        else:
            exists = row.get("workspace_exists")
            cells.append("-" if exists is None else ("yes" if exists else "NO"))
        table.add_row(*cells)
    formatter.console.print(table)


def register(app: typer.Typer) -> None:
    """Mount the binding commands onto ``app`` (the ``rls`` Typer group)."""

    @app.command("bind")
    def rls_bind(
        ctx: typer.Context,
        project: str = typer.Option(..., "--project", "-p", help="Project alias"),
        workspace_id: int = typer.Option(..., "--workspace-id", help="Governed workspace ID"),
        rls_policy: list[str] | None = typer.Option(
            None,
            "--rls-policy",
            help="RLS policy ID to bind (repeatable or comma-separated)",
        ),
        cls_policy: list[str] | None = typer.Option(
            None,
            "--cls-policy",
            help="CLS policy ID to bind (repeatable or comma-separated)",
        ),
        stack_id: str | None = typer.Option(
            None, "--stack-id", envvar=ENV_STACK_ID, help=STACK_ID_HELP
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Validate and show the binding without writing"
        ),
    ) -> None:
        """Bind policies to a governed workspace: it then gets only these policies.

        Creates the binding, or replaces both of its lists when the workspace is already bound.
        Without a binding the workspace gets every policy of the project.
        """
        formatter = get_formatter(ctx)
        result = handle_service_call(
            ctx,
            get_service(ctx, SERVICE).bind,
            project,
            workspace_id,
            stack_id=stack_id,
            rls_policies=split_multi(rls_policy),
            cls_policies=split_multi(cls_policy),
            dry_run=dry_run,
        )
        if formatter.json_mode:
            formatter.output(result)
            return
        _print_warnings(formatter, result)
        lists = f"RLS: {_ids(result['rls_policy_ids'])}; CLS: {_ids(result['cls_policy_ids'])}"
        if dry_run:
            formatter.console.print(
                f"Would {result['action']} the binding {result['kid']} ({lists})."
            )
        else:
            formatter.success(f"Binding {result['action']}: {result['kid']} ({lists})")

    @app.command("unbind")
    def rls_unbind(
        ctx: typer.Context,
        project: str = typer.Option(..., "--project", "-p", help="Project alias"),
        workspace_id: int = typer.Option(..., "--workspace-id", help="Workspace ID"),
        stack_id: str | None = typer.Option(
            None, "--stack-id", envvar=ENV_STACK_ID, help=STACK_ID_HELP
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Show the binding without deleting it"
        ),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    ) -> None:
        """Delete the policy binding of a workspace: it then gets every policy of the project."""
        formatter = get_formatter(ctx)
        service = get_service(ctx, SERVICE)
        found = handle_service_call(
            ctx, service.unbind, project, workspace_id, stack_id=stack_id, dry_run=True
        )
        if not found["bound"]:
            result = {**found, "dry_run": dry_run}
            if formatter.json_mode:
                formatter.output(result)
            else:
                formatter.console.print(
                    f"No binding for {found['kid']}: the workspace already gets every policy."
                )
            return
        if dry_run:
            if formatter.json_mode:
                formatter.output(found)
            else:
                formatter.console.print(f"Would delete the binding {found['kid']}. {_ALL_POLICIES}")
            return
        _confirm_or_abort(
            formatter, yes, f"Delete the policy binding {found['kid']}? {_ALL_POLICIES}"
        )
        result = handle_service_call(ctx, service.unbind, project, workspace_id, stack_id=stack_id)
        if formatter.json_mode:
            formatter.output(result)
        else:
            formatter.success(f"Deleted the binding {result['kid']}. {_ALL_POLICIES}")

    @app.command("bindings")
    def rls_bindings(
        ctx: typer.Context,
        project: str = typer.Option(..., "--project", "-p", help="Project alias"),
        workspace_id: int | None = typer.Option(
            None, "--workspace-id", help="Only the binding of this workspace"
        ),
    ) -> None:
        """List the policy bindings of a project and whether each workspace still exists."""
        formatter = get_formatter(ctx)
        result = handle_service_call(
            ctx, get_service(ctx, SERVICE).list_bindings, project, workspace_id
        )
        if formatter.json_mode:
            formatter.output(result)
        elif not result["bindings"]:
            formatter.console.print(
                "[dim]No policy bindings: every workspace gets every policy of the project.[/dim]"
            )
        else:
            _bindings_table(formatter, result["bindings"], sweep=False)

    @app.command("bindings-sweep")
    def rls_bindings_sweep(
        ctx: typer.Context,
        project: str = typer.Option(..., "--project", "-p", help="Project alias"),
        older_than: int = typer.Option(
            30,
            "--older-than",
            min=0,
            help="Only bindings last updated more than this many days ago",
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Only report the orphaned bindings (the default)"
        ),
        yes: bool = typer.Option(False, "--yes", "-y", help="Delete the orphaned bindings"),
    ) -> None:
        """Find the bindings whose workspace no longer exists; with --yes, delete them.

        A binding is orphaned when its workspace is gone (404 in production and every dev branch),
        it was last updated more than --older-than days ago, and the KID is a workspace KID of this
        project. App KIDs are never swept. Without --yes nothing is deleted.
        """
        formatter = get_formatter(ctx)
        if dry_run and yes:
            formatter.error(
                message="--dry-run and --yes cannot be combined: --yes deletes, --dry-run reports.",
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
            raise typer.Exit(code=2)
        if yes:
            check_cli_operation(ctx, "rls.bindings-sweep --yes")
        result = handle_service_call(
            ctx,
            get_service(ctx, SERVICE).sweep,
            project,
            older_than_days=older_than,
            delete=yes,
        )
        counts = result["summary"]
        rows = result["bindings"]
        if yes:
            summary = (
                f"{len(rows)} binding(s) checked: {counts['deleted']} deleted, "
                f"{counts['kept']} kept."
            )
        else:
            summary = (
                f"{len(rows)} binding(s) checked: {counts['orphaned']} would be deleted, "
                f"{counts['kept']} kept."
            )

        def render() -> None:
            if rows:
                _bindings_table(formatter, rows, sweep=True)
            formatter.console.print(f"[dim]{result['references_note']}[/dim]")

        finish_items(
            formatter,
            result,
            failed=counts["failed"],
            total=len(rows),
            noun="bindings",
            summary=summary,
            render=render,
        )
