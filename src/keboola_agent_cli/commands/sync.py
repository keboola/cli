"""Sync commands - init, pull, push, diff, and status for local filesystem sync.

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


@sync_app.command("diff")
def sync_diff(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias to diff against",
    ),
    directory: Path = typer.Option(
        Path("."),
        "--directory",
        "-d",
        help="Project root directory (must contain .keboola/)",
    ),
) -> None:
    """Show detailed diff between local and remote configurations.

    Fetches the current remote state and compares each local _config.yml
    against it, showing which configs would be created, updated, or deleted.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "sync_service")
    project_root = directory.resolve()

    try:
        result = service.diff(alias=project, project_root=project_root)
    except FileNotFoundError as exc:
        formatter.error(message=str(exc), error_code="NOT_INITIALIZED")
        raise typer.Exit(code=1) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        changes = result["changes"]
        summary = result["summary"]

        if not changes:
            formatter.console.print(
                "[green]No differences found.[/green] "
                "Local and remote are in sync."
            )
            return

        for change in changes:
            change_type = change["change_type"]
            path = change.get("path", change.get("config_name", ""))
            prefix = {"added": "[green]+ ", "modified": "[yellow]~ ", "deleted": "[red]- "}
            suffix = {"added": "[/green]", "modified": "[/yellow]", "deleted": "[/red]"}
            formatter.console.print(
                f"  {prefix.get(change_type, '')}"
                f"{change_type.upper()} {change['component_id']}/{path}"
                f"{suffix.get(change_type, '')}"
            )
            for detail in change.get("details", []):
                formatter.console.print(f"    {detail}")

        formatter.console.print(
            f"\n{summary['added']} to create, {summary['modified']} to update, "
            f"{summary['deleted']} to delete"
        )


@sync_app.command("push")
def sync_push(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias to push changes to",
    ),
    directory: Path = typer.Option(
        Path("."),
        "--directory",
        "-d",
        help="Project root directory (must contain .keboola/)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be pushed without actually pushing",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Allow deletion of remote configs that were removed locally",
    ),
) -> None:
    """Push local configuration changes to a Keboola project.

    Compares local files against remote state and creates, updates,
    or deletes configurations as needed. New configs get IDs assigned
    by the API. After push, runs a pull to sync the manifest.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "sync_service")
    project_root = directory.resolve()

    try:
        result = service.push(
            alias=project,
            project_root=project_root,
            dry_run=dry_run,
            force=force,
        )
    except FileNotFoundError as exc:
        formatter.error(message=str(exc), error_code="NOT_INITIALIZED")
        raise typer.Exit(code=1) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        status = result.get("status", "")

        if status == "no_changes":
            formatter.console.print("[green]No changes to push.[/green]")
            return

        if status == "dry_run":
            formatter.console.print("[yellow]Dry run -- no changes applied:[/yellow]")
            for change in result.get("changes", []):
                ct = change["change_type"]
                formatter.console.print(
                    f"  {ct.upper()} {change['component_id']}/{change.get('path', '')}"
                )
            summary = result["summary"]
            formatter.console.print(
                f"\nWould create {summary['added']}, update {summary['modified']}, "
                f"delete {summary['deleted']}"
            )
            return

        formatter.success(
            f"Pushed: {result['created']} created, "
            f"{result['updated']} updated, "
            f"{result['deleted']} deleted"
        )
        for err in result.get("errors", []):
            formatter.warning(
                f"  Error: {err['change_type']} {err['component_id']}/{err['config_id']}: "
                f"{err['message']}"
            )
