"""Branch commands - list, create, use, reset, delete, and merge development branches.

Thin CLI layer: parses arguments, calls BranchService, formats output.
No business logic belongs here.
"""

from pathlib import Path

import typer

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..output import format_branch_metadata_table, format_branches_table
from ._helpers import (
    check_cli_permission,
    emit_project_warnings,
    get_formatter,
    get_service,
    map_error_to_exit_code,
)
from ._metadata_input import resolve_text_input

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
    formatter = get_formatter(ctx)
    service = get_service(ctx, "branch_service")

    try:
        result = service.list_branches(aliases=project)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None


# ── Branch metadata commands ──────────────────────────────────────────


@branch_app.command("metadata-list")
def branch_metadata_list(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias to query",
    ),
    branch: str = typer.Option(
        "default",
        "--branch",
        help='Branch ID or "default" for the main branch',
    ),
) -> None:
    """List all metadata entries on a branch.

    Metadata lives on a branch (not on the project) and is keyed by
    arbitrary strings like ``KBC.projectDescription``.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "branch_service")

    try:
        result = service.list_branch_metadata(alias=project, branch_id=branch)
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=exit_code) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        format_branch_metadata_table(formatter.console, result)


@branch_app.command("metadata-get")
def branch_metadata_get(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias to query"),
    key: str = typer.Option(..., "--key", help="Metadata key to read"),
    branch: str = typer.Option(
        "default",
        "--branch",
        help='Branch ID or "default" for the main branch',
    ),
) -> None:
    """Read a single metadata value by key.

    Exits with code 1 (NOT_FOUND) if the key is not present on the branch.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "branch_service")

    try:
        result = service.get_branch_metadata(alias=project, key=key, branch_id=branch)
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=exit_code) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    formatter.output(
        result,
        lambda c, d: c.print(d["value"]),
    )


@branch_app.command("metadata-set")
def branch_metadata_set(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    key: str = typer.Option(..., "--key", help="Metadata key to set"),
    text: str | None = typer.Option(None, "--text", help="Inline string value"),
    file: Path | None = typer.Option(
        None,
        "--file",
        help="Read value from a UTF-8 text file",
        exists=False,  # validated in helper with a clearer error message
    ),
    stdin: bool = typer.Option(
        False,
        "--stdin",
        help="Read value from standard input",
    ),
    branch: str = typer.Option(
        "default",
        "--branch",
        help='Branch ID or "default" for the main branch',
    ),
) -> None:
    """Set a metadata key/value on a branch.

    The value is taken from exactly one of --text, --file, or --stdin.
    """
    formatter = get_formatter(ctx)

    try:
        value = resolve_text_input(text=text, file=file, stdin=stdin)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.INVALID_ARGUMENT)
        raise typer.Exit(code=2) from None

    service = get_service(ctx, "branch_service")

    try:
        result = service.set_branch_metadata(alias=project, key=key, value=value, branch_id=branch)
        formatter.output(
            result,
            lambda c, d: c.print(f"[bold green]Success:[/bold green] {d['message']}"),
        )
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=exit_code) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None


@branch_app.command("metadata-delete")
def branch_metadata_delete(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    metadata_id: int = typer.Option(
        ...,
        "--metadata-id",
        help="Numeric ID of the metadata entry (from metadata-list)",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt",
    ),
    branch: str = typer.Option(
        "default",
        "--branch",
        help='Branch ID or "default" for the main branch',
    ),
) -> None:
    """Delete a branch metadata entry by its numeric ID."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "branch_service")

    if (
        not yes
        and not formatter.json_mode
        and not typer.confirm(f"Delete metadata entry {metadata_id} from project '{project}'?")
    ):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)

    try:
        result = service.delete_branch_metadata(
            alias=project, metadata_id=metadata_id, branch_id=branch
        )
        formatter.output(
            result,
            lambda c, d: c.print(f"[bold green]Success:[/bold green] {d['message']}"),
        )
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=exit_code) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
