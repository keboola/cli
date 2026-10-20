"""``kbagent storage delete-bucket`` + ``storage file-delete``.

Lives in a private module because ``commands/storage.py`` is past the
commands-file ceiling (CONTRIBUTING.md). Mounted onto ``storage_app`` via
:func:`register`, so the permission keys stay ``storage.delete-bucket`` /
``storage.file-delete`` and the commands keep their help panels.
"""

import typer

from ..config_store import ConfigStore
from ..effective_branch import resolve_branch
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import (
    _confirm_or_abort,
    emit_api_error,
    finish_bulk,
    get_formatter,
    get_service,
    map_error_to_exit_code,
)


def register(app: typer.Typer) -> None:
    """Mount delete-bucket + file-delete onto ``app`` (the ``storage`` Typer group)."""

    @app.command("delete-bucket", rich_help_panel="Buckets")
    def storage_delete_bucket(
        ctx: typer.Context,
        project: str = typer.Option(
            ...,
            "--project",
            help="Project alias",
        ),
        bucket_id: list[str] = typer.Option(
            ...,
            "--bucket-id",
            help="Bucket ID to delete (e.g. 'in.c-my-bucket'). Can be repeated.",
        ),
        force: bool = typer.Option(
            False,
            "--force",
            help="Force delete even if bucket contains tables (cascade). Never deletes a shared bucket.",
        ),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Show what would be deleted without executing",
        ),
        yes: bool = typer.Option(
            False,
            "--yes",
            "-y",
            help="Skip confirmation prompt",
        ),
        branch: int | None = typer.Option(
            None,
            "--branch",
            help="Dev branch ID (defaults to active branch if set via 'branch use')",
        ),
    ) -> None:
        """Delete one or more storage buckets.

        Without --force, fails if a bucket contains tables.
        With --force, cascade-deletes all tables in the bucket.
        Linked and shared buckets are protected, also with --force
        (use sharing unlink/unshare first).
        """
        formatter = get_formatter(ctx)
        service = get_service(ctx, "storage_service")
        config_store: ConfigStore = ctx.obj["config_store"]
        effective_branch = resolve_branch(config_store, project, branch)

        if not dry_run:
            question = (
                f"Delete {len(bucket_id)} bucket(s) from project '{project}'?"
                " Shared and linked buckets are skipped."
            )
            if force:
                question = (
                    f"FORCE-delete {len(bucket_id)} bucket(s) from project '{project}'?"
                    " Shared and linked buckets are skipped."
                    " All tables in the deleted buckets are deleted too."
                )
            _confirm_or_abort(formatter, yes, question)

        try:
            result = service.delete_buckets(
                alias=project,
                bucket_ids=bucket_id,
                force=force,
                dry_run=dry_run,
                branch_id=effective_branch,
            )
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
            raise typer.Exit(code=5) from None

        def render() -> None:
            if dry_run:
                for bid in result.get("would_delete", []):
                    force_hint = " [force]" if force else ""
                    formatter.console.print(
                        f"[bold blue]Would delete:[/bold blue] {bid}{force_hint}"
                    )
            else:
                for bid in result["deleted"]:
                    formatter.console.print(f"[bold green]Deleted:[/bold green] {bid}")
            for f_item in result["failed"]:
                formatter.console.print(
                    f"[bold red]Failed:[/bold red] {f_item['id']}: {f_item['error']}"
                )

        done = result.get("would_delete", []) if dry_run else result["deleted"]
        verb = "Would delete" if dry_run else "Deleted"
        finish_bulk(formatter, result, done, "bucket", verb, render)

    @app.command("file-delete", rich_help_panel="Files")
    def storage_file_delete(
        ctx: typer.Context,
        project: str = typer.Option(
            ...,
            "--project",
            help="Project alias",
        ),
        file_id: list[int] = typer.Option(
            ...,
            "--file-id",
            help="Storage file ID to delete (repeat for multiple)",
        ),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Show what would be deleted without executing",
        ),
        yes: bool = typer.Option(
            False,
            "--yes",
            "-y",
            help="Skip confirmation prompt",
        ),
    ) -> None:
        """Delete one or more Storage Files."""
        formatter = get_formatter(ctx)
        service = get_service(ctx, "storage_service")

        if not dry_run:
            _confirm_or_abort(
                formatter, yes, f"Delete {len(file_id)} file(s) from project '{project}'?"
            )

        try:
            result = service.delete_files(
                alias=project,
                file_ids=file_id,
                dry_run=dry_run,
            )
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
            raise typer.Exit(code=5) from None
        except KeboolaApiError as exc:
            emit_api_error(formatter, exc)
            raise typer.Exit(code=map_error_to_exit_code(exc)) from None

        def render() -> None:
            if dry_run:
                for fid in result.get("would_delete", []):
                    formatter.console.print(f"[bold blue]Would delete:[/bold blue] file ID {fid}")
            else:
                for fid in result["deleted"]:
                    formatter.console.print(f"[bold green]Deleted:[/bold green] file ID {fid}")
            for f_err in result["failed"]:
                formatter.console.print(
                    f"[bold red]Failed:[/bold red] file ID {f_err['id']}: {f_err['error']}"
                )

        done = result.get("would_delete", []) if dry_run else result["deleted"]
        verb = "Would delete" if dry_run else "Deleted"
        finish_bulk(formatter, result, done, "file", verb, render)
