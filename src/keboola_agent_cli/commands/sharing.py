"""Sharing commands - cross-project bucket sharing and linking.

Enable data sharing between Keboola projects:
- share/unshare: control bucket visibility (requires master token)
- link/unlink: create/remove linked buckets in target projects
- list: browse available shared buckets
"""

import typer

from ..errors import ConfigError, KeboolaApiError
from ._helpers import (
    check_cli_permission,
    emit_project_warnings,
    get_formatter,
    get_service,
    map_error_to_exit_code,
)

sharing_app = typer.Typer(
    help=(
        "Cross-project bucket sharing and linking.\n\n"
        "share/unshare require a master token (org membership). "
        "Set via KBC_MASTER_TOKEN_{ALIAS} or KBC_MASTER_TOKEN env var.\n\n"
        "list/link/unlink work with the regular project token."
    )
)


@sharing_app.callback(invoke_without_command=True)
def _sharing_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "sharing")


@sharing_app.command("list")
def sharing_list(
    ctx: typer.Context,
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias (repeatable). Omit to query all projects.",
    ),
) -> None:
    """List shared buckets available for linking.

    Shows buckets shared within your organization that can be linked
    into your projects. Uses the regular project token.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "sharing_service")

    try:
        result = service.list_shared(aliases=project)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        from rich.table import Table

        buckets = result["shared_buckets"]
        if not buckets:
            formatter.console.print("[dim]No shared buckets found.[/dim]")
            return

        table = Table(title="Shared Buckets")
        table.add_column("Source Project", style="bold")
        table.add_column("Bucket ID", style="cyan")
        table.add_column("Sharing Type", style="dim")
        table.add_column("Backend", style="dim")
        table.add_column("Tables", justify="right")
        table.add_column("Rows", justify="right")

        for b in buckets:
            table.add_row(
                f"{b['source_project_name']} (#{b['source_project_id']})",
                b["source_bucket_id"],
                b["sharing"],
                b["backend"],
                str(len(b.get("tables", []))),
                str(b["rows_count"]),
            )

        formatter.console.print(table)
        emit_project_warnings(formatter, result)


@sharing_app.command("share")
def sharing_share(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias that owns the bucket to share.",
    ),
    bucket_id: str = typer.Option(
        ...,
        "--bucket-id",
        help="Bucket ID to share (e.g. out.c-data).",
    ),
    sharing_type: str = typer.Option(
        ...,
        "--type",
        help=(
            "Sharing type: 'organization' (all org members), "
            "'organization-project' (all project members in org), "
            "'selected-projects' (specific projects, use --target-project-ids), "
            "'selected-users' (specific users, use --target-users)."
        ),
    ),
    target_project_ids: str | None = typer.Option(
        None,
        "--target-project-ids",
        help="Comma-separated project IDs (required for --type selected-projects).",
    ),
    target_users: str | None = typer.Option(
        None,
        "--target-users",
        help="Comma-separated email addresses (required for --type selected-users).",
    ),
) -> None:
    """Enable sharing on a bucket.

    Requires a master token with organization membership. Set via env var:

      KBC_MASTER_TOKEN_{ALIAS}  (project-specific, e.g. KBC_MASTER_TOKEN_PROD)
      KBC_MASTER_TOKEN          (global fallback)

    Falls back to the project's configured token if no master token is set.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "sharing_service")

    # Parse target lists
    parsed_project_ids: list[int] | None = None
    parsed_users: list[str] | None = None

    if sharing_type == "selected-projects":
        if not target_project_ids:
            formatter.error(
                message="--target-project-ids is required for --type selected-projects",
                error_code="USAGE_ERROR",
            )
            raise typer.Exit(code=2)
        parsed_project_ids = [int(pid.strip()) for pid in target_project_ids.split(",")]

    if sharing_type == "selected-users":
        if not target_users:
            formatter.error(
                message="--target-users is required for --type selected-users",
                error_code="USAGE_ERROR",
            )
            raise typer.Exit(code=2)
        parsed_users = [u.strip() for u in target_users.split(",")]

    try:
        result = service.share(
            alias=project,
            bucket_id=bucket_id,
            sharing_type=sharing_type,
            target_project_ids=parsed_project_ids,
            target_users=parsed_users,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.success(result["message"])


@sharing_app.command("unshare")
def sharing_unshare(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias that owns the shared bucket.",
    ),
    bucket_id: str = typer.Option(
        ...,
        "--bucket-id",
        help="Bucket ID to stop sharing (e.g. out.c-data).",
    ),
) -> None:
    """Disable sharing on a bucket.

    Fails if other projects still have linked buckets pointing to it.

    Requires a master token (see 'sharing share --help' for env var details).
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "sharing_service")

    try:
        result = service.unshare(alias=project, bucket_id=bucket_id)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.success(result["message"])


@sharing_app.command("link")
def sharing_link(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Target project alias where the linked bucket will be created.",
    ),
    source_project_id: int = typer.Option(
        ...,
        "--source-project-id",
        help="ID of the project that owns the shared bucket.",
    ),
    bucket_id: str = typer.Option(
        ...,
        "--bucket-id",
        help="Source bucket ID to link (e.g. out.c-data).",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help="Display name for the linked bucket. Auto-generated if omitted.",
    ),
) -> None:
    """Link a shared bucket into a project.

    Creates a read-only linked bucket in the target project that mirrors
    the source bucket's tables. Uses the regular project token
    (no master token needed).

    Use 'sharing list' to discover available shared buckets.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "sharing_service")

    try:
        result = service.link(
            alias=project,
            source_project_id=source_project_id,
            source_bucket_id=bucket_id,
            name=name,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.success(result["message"])


@sharing_app.command("unlink")
def sharing_unlink(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias containing the linked bucket.",
    ),
    bucket_id: str = typer.Option(
        ...,
        "--bucket-id",
        help="Linked bucket ID to remove (e.g. in.c-shared-data).",
    ),
) -> None:
    """Remove a linked bucket from a project.

    Deletes the linked bucket. Does not affect the source bucket or
    other projects that have linked it. Uses the regular project token.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "sharing_service")

    try:
        result = service.unlink(alias=project, bucket_id=bucket_id)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.success(result["message"])
