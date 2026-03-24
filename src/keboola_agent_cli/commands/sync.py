"""Sync commands - init, pull, and status for local filesystem sync.

Thin CLI layer: parses arguments, calls SyncService, formats output.
No business logic belongs here.
"""

from pathlib import Path

import typer

from ..errors import ConfigError, KeboolaApiError
from ._helpers import get_formatter, get_service, map_error_to_exit_code

sync_app = typer.Typer(help="Sync project configurations with local filesystem")


@sync_app.command("init")
def sync_init(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias to initialize sync for",
    ),
    directory: Path = typer.Option(
        Path("."),
        "--directory",
        "-d",
        help="Target directory for the project files",
    ),
    git_branching: bool = typer.Option(
        False,
        "--git-branching",
        help="Enable git-branching mode (maps git branches to Keboola branches)",
    ),
) -> None:
    """Initialize a sync working directory for a Keboola project.

    Creates the .keboola/ directory with manifest.json containing
    project metadata and naming conventions. Optionally enables
    git-branching mode for branch-to-branch mapping.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "sync_service")
    project_root = directory.resolve()

    try:
        result = service.init_sync(
            alias=project,
            project_root=project_root,
            git_branching=git_branching,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except FileExistsError as exc:
        formatter.error(message=str(exc), error_code="ALREADY_EXISTS")
        raise typer.Exit(code=1) from None
    except KeboolaApiError as exc:
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
        )
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.success(
            f"Initialized sync for project '{result['project_alias']}' "
            f"(ID: {result['project_id']})"
        )
        formatter.console.print(f"  API host: {result['api_host']}")
        if result["git_branching"]:
            formatter.console.print(
                f"  Git-branching: enabled (default branch: {result['default_branch']})"
            )
        for f in result["files_created"]:
            formatter.console.print(f"  Created: {f}")


@sync_app.command("pull")
def sync_pull(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias to pull configurations from",
    ),
    directory: Path = typer.Option(
        Path("."),
        "--directory",
        "-d",
        help="Project root directory (must contain .keboola/)",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite local files without checking for modifications",
    ),
) -> None:
    """Download all configurations from a Keboola project to local files.

    Reads the manifest from .keboola/manifest.json, fetches all
    configurations from the API, and writes them as _config.yml files
    in the dev-friendly directory structure.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "sync_service")
    project_root = directory.resolve()

    try:
        result = service.pull(
            alias=project,
            project_root=project_root,
            force=force,
        )
    except FileNotFoundError as exc:
        formatter.error(message=str(exc), error_code="NOT_INITIALIZED")
        raise typer.Exit(code=1) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
        )
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.success(
            f"Pulled {result['configs_pulled']} configurations "
            f"({result['rows_pulled']} rows) "
            f"into {result['branch_dir']}/"
        )
        formatter.console.print(f"  Files written: {result['files_written']}")


@sync_app.command("status")
def sync_status(
    ctx: typer.Context,
    directory: Path = typer.Option(
        Path("."),
        "--directory",
        "-d",
        help="Project root directory (must contain .keboola/)",
    ),
) -> None:
    """Show which local configurations have been modified, added, or deleted.

    Compares the local filesystem state against the manifest to detect
    changes since the last pull.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "sync_service")
    project_root = directory.resolve()

    try:
        result = service.status(project_root=project_root)
    except FileNotFoundError as exc:
        formatter.error(message=str(exc), error_code="NOT_INITIALIZED")
        raise typer.Exit(code=1) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        modified = result["modified"]
        added = result["added"]
        deleted = result["deleted"]
        unchanged = result["unchanged"]

        if not modified and not added and not deleted:
            formatter.console.print(
                f"[green]No changes detected.[/green] "
                f"({unchanged} configurations tracked)"
            )
            return

        if modified:
            formatter.console.print(f"\n[yellow]Modified ({len(modified)}):[/yellow]")
            for m in modified:
                formatter.console.print(f"  M {m['path']}")

        if added:
            formatter.console.print(f"\n[green]Added ({len(added)}):[/green]")
            for a in added:
                formatter.console.print(f"  A {a['path']}")

        if deleted:
            formatter.console.print(f"\n[red]Deleted ({len(deleted)}):[/red]")
            for d in deleted:
                formatter.console.print(f"  D {d['path']}")

        formatter.console.print(
            f"\n{len(modified)} modified, {len(added)} added, "
            f"{len(deleted)} deleted, {unchanged} unchanged"
        )
