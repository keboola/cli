"""Branch commands - list, create, use, reset, delete, and merge development branches.

Thin CLI layer: parses arguments, calls BranchService, formats output.
No business logic belongs here.
"""

import typer

from ..errors import ConfigError, KeboolaApiError
from ..output import format_branches_table
from ._helpers import (
    check_cli_permission,
    emit_hint,
    emit_project_warnings,
    get_formatter,
    get_service,
    map_error_to_exit_code,
    should_hint,
)

branch_app = typer.Typer(help="Manage development branches")


@branch_app.callback(invoke_without_command=True)
def _branch_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "branch")


@branch_app.command("list")
def branch_list(
    ctx: typer.Context,
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias to query (can be repeated for multiple projects)",
    ),
) -> None:
    """List development branches from connected projects."""
    if should_hint(ctx):
        emit_hint(ctx, "branch.list", project=project)
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "branch_service")

    try:
        result = service.list_branches(aliases=project)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        format_branches_table(formatter.console, result)
        emit_project_warnings(formatter, result)


@branch_app.command("create")
def branch_create(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias to create the branch in",
    ),
    name: str = typer.Option(
        ...,
        "--name",
        help="Name for the new development branch",
    ),
    description: str = typer.Option(
        "",
        "--description",
        help="Optional description for the branch",
    ),
) -> None:
    """Create a new development branch and auto-activate it.

    The created branch becomes the active branch for the project,
    so subsequent tool calls will automatically use it.
    """
    if should_hint(ctx):
        emit_hint(ctx, "branch.create", project=project, name=name, description=description)
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "branch_service")

    try:
        result = service.create_branch(alias=project, name=name, description=description)
        formatter.output(
            result,
            lambda c, d: c.print(f"[bold green]Success:[/bold green] {d['message']}"),
        )
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None


@branch_app.command("use")
def branch_use(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias to set the active branch for",
    ),
    branch: int = typer.Option(
        ...,
        "--branch",
        help="Branch ID to activate",
    ),
) -> None:
    """Set an existing development branch as active.

    Validates the branch exists via the API before activating it.
    Subsequent tool calls will automatically use this branch.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "branch_service")

    try:
        result = service.set_active_branch(alias=project, branch_id=branch)
        formatter.output(
            result,
            lambda c, d: c.print(f"[bold green]Success:[/bold green] {d['message']}"),
        )
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None


@branch_app.command("reset")
def branch_reset(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias to reset the active branch for",
    ),
) -> None:
    """Reset the active branch back to main/production.

    Clears the active development branch so subsequent tool calls
    operate on the main branch.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "branch_service")

    try:
        result = service.reset_branch(alias=project)
        formatter.output(
            result,
            lambda c, d: c.print(f"[bold green]Success:[/bold green] {d['message']}"),
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None


@branch_app.command("delete")
def branch_delete(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias to delete the branch from",
    ),
    branch: int = typer.Option(
        ...,
        "--branch",
        help="Branch ID to delete",
    ),
) -> None:
    """Delete a development branch.

    If the deleted branch was the active branch, it is automatically
    reset to main/production.
    """
    if should_hint(ctx):
        emit_hint(ctx, "branch.delete", project=project, branch=branch)
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "branch_service")

    try:
        result = service.delete_branch(alias=project, branch_id=branch)
        formatter.output(
            result,
            lambda c, d: c.print(f"[bold green]Success:[/bold green] {d['message']}"),
        )
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None


@branch_app.command("merge")
def branch_merge(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Branch ID to merge (uses active branch if not set)",
    ),
) -> None:
    """Get the KBC UI merge URL for a development branch.

    Does NOT perform the merge via API. Instead, generates the URL
    to the Keboola UI where you can review and merge safely.
    After displaying the URL, resets the active branch to main.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "branch_service")

    try:
        result = service.get_merge_url(alias=project, branch_id=branch)
        formatter.output(
            result,
            lambda c, d: (
                c.print(f"\n[bold]Merge URL:[/bold] {d['url']}"),
                c.print(f"\n{d['message']}"),
            ),
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
