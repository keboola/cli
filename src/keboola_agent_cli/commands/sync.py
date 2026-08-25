"""Sync commands - init, pull, push, diff, and status for local filesystem sync.

Thin CLI layer: parses arguments, calls SyncService, formats output.
No business logic belongs here.
"""

from pathlib import Path
from typing import Any

import typer

from ..constants import SYNC_ORPHAN_PREVIEW_LIMIT
from ..errors import ConfigError, ErrorCode, KeboolaApiError, SyncConflictError
from ._helpers import check_cli_permission, get_formatter, get_service, map_error_to_exit_code

sync_app = typer.Typer(help="Sync project configurations with local filesystem")


@sync_app.callback(invoke_without_command=True)
def _sync_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "sync")


KEBOOLA_DIR = ".keboola"
MANIFEST_FILE = "manifest.json"


def _safe_resolve_dir(directory: Path) -> Path:
    """Resolve a directory path safely (handles deleted CWD)."""
    try:
        return directory.resolve()
    except (OSError, ValueError):
        return Path(str(directory)).expanduser()


def _resolve_project_root(directory: Path, alias: str | None = None) -> Path:
    """Find the project root directory containing .keboola/manifest.json.

    Tries in order:
    1. directory itself (explicit --directory or current dir)
    2. directory/{alias}/ (auto-detect subdirectory from --all-projects layout)
    """
    root = _safe_resolve_dir(directory)
    if (root / KEBOOLA_DIR / MANIFEST_FILE).exists():
        return root
    if alias:
        sub = root / alias
        if (sub / KEBOOLA_DIR / MANIFEST_FILE).exists():
            return sub
    return root  # let caller handle the error


def _load_override_file(path: Path) -> dict[str, str]:
    """Load a clone override map (JSON or YAML) as a flat ``{str: str}`` dict.

    Values must be scalars (bucket ids, variable values, and path prefixes are
    all strings); a nested mapping or list raises instead of being silently
    stringified into a bogus ID.
    """
    from ..yaml_input import load_flat_scalar_mapping

    return load_flat_scalar_mapping(path, label="override file")


def _change_label(change: dict) -> str:
    """Build a human-readable label for a config change entry."""
    path = change.get("path", "")
    name = change.get("config_name", "")
    component = change["component_id"]
    if path:
        return f"{component}/{path}"
    if name:
        return f"{component}/{name}"
    config_id = change.get("config_id", "")
    return f"{component}/{config_id}" if config_id else component


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
    adopt_existing: bool = typer.Option(
        False,
        "--adopt-existing",
        help="Adopt an existing .keboola/manifest.json (e.g. written by kbc) "
        "instead of failing. Validates the manifest's project_id against the alias "
        "and normalises the file. Idempotent.",
    ),
) -> None:
    """Initialize a sync working directory for a Keboola project.

    Creates the .keboola/ directory with manifest.json containing
    project metadata and naming conventions. Optionally enables
    git-branching mode for branch-to-branch mapping.

    Use --adopt-existing to register a directory that was already initialised
    by the official kbc CLI without overwriting the manifest.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "sync_service")
    project_root = directory.resolve()

    try:
        result = service.init_sync(
            alias=project,
            project_root=project_root,
            git_branching=git_branching,
            adopt_existing=adopt_existing,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except FileExistsError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.ALREADY_EXISTS)
        raise typer.Exit(code=1) from None
    except KeboolaApiError as exc:
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        status = result.get("status", "initialized")
        if status == "adopted":
            formatter.success(
                f"Adopted manifest for project '{result['project_alias']}' "
                f"(ID: {result['project_id']})"
            )
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


def _format_pull_result(formatter: Any, result: dict) -> None:
    """Format a single-project pull result for human output."""
    is_dry = result.get("status") == "dry_run"
    details = result.get("details", [])
    new_cfgs = [d for d in details if d["action"] == "new"]
    updated_cfgs = [d for d in details if d["action"] == "updated"]
    removed_cfgs = [d for d in details if d["action"] == "removed"]
    renamed_cfgs = [d for d in details if d["action"] == "renamed"]
    skipped_cfgs = [d for d in details if d["action"] == "skipped"]

    has_changes = bool(new_cfgs or updated_cfgs or removed_cfgs or renamed_cfgs)

    storage = result.get("storage", {})
    jobs_written = result.get("jobs_written", 0)
    has_extra = bool(storage.get("buckets") or storage.get("tables") or jobs_written)
    # Row-only refetches (e.g. `pull --theirs` restoring an edited row file)
    # produce no config-level detail entries but DO write files -- do not
    # report "Already up to date" when anything was written.
    has_writes = bool(
        result.get("configs_pulled") or result.get("rows_pulled") or result.get("files_written")
    )

    if not has_changes and not skipped_cfgs and not has_extra and not has_writes:
        formatter.console.print("[green]Already up to date.[/green] No changes from remote.")
        return
    elif is_dry:
        formatter.console.print("[yellow]Dry run -- no files written:[/yellow]")
        formatter.console.print(
            f"  Would pull {result['configs_pulled']} configurations "
            f"({result['rows_pulled']} rows), "
            f"write {result['files_written']} files"
        )
    else:
        formatter.success(
            f"Pulled {result['configs_pulled']} configurations "
            f"({result['rows_pulled']} rows) "
            f"into {result['branch_dir']}/"
        )
        formatter.console.print(f"  Files written: {result['files_written']}")
        if storage.get("buckets") or storage.get("tables"):
            formatter.console.print(
                f"  Storage: {storage.get('buckets', 0)} buckets, {storage.get('tables', 0)} tables"
            )
        if storage.get("samples"):
            formatter.console.print(f"  Samples: {storage['samples']} tables")
        if jobs_written:
            formatter.console.print(f"  Jobs: {jobs_written} configs with job history")

    if renamed_cfgs:
        formatter.console.print(f"  [magenta]Renamed ({len(renamed_cfgs)}):[/magenta]")
        for d in renamed_cfgs:
            formatter.console.print(
                f"    > {d.get('old_path', '?')} -> {d['component_id']}/{d['config_name']}"
            )
    if new_cfgs:
        formatter.console.print(f"  [green]New ({len(new_cfgs)}):[/green]")
        for d in new_cfgs:
            formatter.console.print(f"    + {d['component_id']}/{d['config_name']}")
    if updated_cfgs:
        formatter.console.print(f"  [yellow]Updated ({len(updated_cfgs)}):[/yellow]")
        for d in updated_cfgs:
            formatter.console.print(f"    ~ {d['component_id']}/{d['config_name']}")
    if removed_cfgs:
        formatter.console.print(f"  [red]Removed from remote ({len(removed_cfgs)}):[/red]")
        for d in removed_cfgs:
            formatter.console.print(f"    - {d['path']}")
    if skipped_cfgs:
        formatter.console.print(
            f"  [cyan]Skipped ({len(skipped_cfgs)}) -- locally modified:[/cyan]"
        )
        for d in skipped_cfgs:
            formatter.console.print(f"    ! {d['component_id']}/{d['config_name']}")


def _format_never_fetched(formatter: Any, never_fetched: list[dict]) -> None:
    """Warn about manifest entries that were registered but never materialized."""
    if not never_fetched:
        return
    formatter.console.print(
        f"\n  [yellow]Warning: {len(never_fetched)} manifest entr"
        f"{'y was' if len(never_fetched) == 1 else 'ies were'} registered but "
        f"never fetched (no local files). They are excluded from push planning "
        f"-- run 'kbagent sync pull' to materialize them:[/yellow]"
    )
    for item in never_fetched:
        formatter.console.print(
            f"    ? {item.get('component_id')}/{item.get('config_id')}  "
            f"[dim]{item.get('path', '')}[/dim]"
        )


def _format_orphaned(formatter: Any, orphaned: list[dict]) -> None:
    """Warn about configs excluded because they belong to another branch's tree.

    ``sync pull --branch <dev>`` re-targets the whole manifest, so this list is
    project-sized in the case that matters (issue #649) -- print a preview and
    collapse the tail into a count.
    """
    if not orphaned:
        return
    formatter.console.print(
        f"\n  [yellow]Warning: {len(orphaned)} config(s) belong to a different branch "
        f"than the one being synced. They are excluded from the diff and from push "
        f"planning -- run 'kbagent sync pull' to re-target the manifest to this "
        f"branch, or remove the stale directory:[/yellow]"
    )
    for item in orphaned[:SYNC_ORPHAN_PREVIEW_LIMIT]:
        formatter.console.print(
            f"    ? {item.get('component_id')}/{item.get('config_id')}  "
            f"[dim]{item.get('path', '')} -- {item.get('hint', '')}[/dim]"
        )
    remaining = len(orphaned) - SYNC_ORPHAN_PREVIEW_LIMIT
    if remaining > 0:
        formatter.console.print(f"    [dim]... and {remaining} more[/dim]")


def _format_diff_result(formatter: Any, result: dict) -> None:
    """Format a single-project diff result for human output."""
    changes = result["changes"]
    summary = result["summary"]
    remote_only = result.get("remote_only", [])
    never_fetched = result.get("never_fetched", [])
    orphaned = result.get("orphaned", [])

    if not changes and not remote_only:
        if never_fetched or orphaned:
            _format_never_fetched(formatter, never_fetched)
            _format_orphaned(formatter, orphaned)
        else:
            formatter.console.print("[green]No differences.[/green]")
        return

    local_changes = [c for c in changes if c["change_type"] in ("added", "modified", "deleted")]
    remote_changes = [c for c in changes if c["change_type"] == "remote_modified"]
    conflict_changes = [c for c in changes if c["change_type"] == "conflict"]

    if local_changes:
        for change in local_changes:
            ct = change["change_type"]
            label = _change_label(change)
            prefix = {"added": "+", "modified": "~", "deleted": "-"}.get(ct, "?")
            formatter.console.print(f"  {prefix} {ct.upper()} {label}")
        formatter.console.print(
            f"  {summary['added']} to create, {summary['modified']} to update, "
            f"{summary['deleted']} to delete"
        )
    if remote_changes:
        for change in remote_changes:
            formatter.console.print(f"  ~ REMOTE MODIFIED {_change_label(change)}")
    if conflict_changes:
        for change in conflict_changes:
            formatter.console.print(f"  ! CONFLICT {_change_label(change)}")
    if remote_only:
        formatter.console.print(f"  {len(remote_only)} new remote-only config(s)")
    _format_never_fetched(formatter, never_fetched)
    _format_orphaned(formatter, orphaned)


def _format_conflict_list(formatter: Any, conflicts: list[dict[str, str]]) -> None:
    """Print the per-config force-pull conflict list (human mode only)."""
    if not conflicts:
        return
    n = len(conflicts)
    formatter.console.print(
        f"\n[bold red]Merge conflict:[/bold red] {n} config(s) changed BOTH "
        f"locally and on the remote since the last pull:"
    )
    for c in conflicts:
        label = "row" if c.get("scope") == "row" else "config"
        formatter.console.print(
            f"  [red]![/red] {c.get('component_id')}/{c.get('config_id')} "
            f"[dim]({label})[/dim]  {c.get('config_name', '')}"
        )


def _format_push_result(formatter: Any, result: dict) -> None:
    """Format a single-project push result for human output."""
    status = result.get("status", "")
    if status == "no_changes":
        formatter.console.print("  No changes to push.")
        _format_never_fetched(formatter, result.get("never_fetched", []))
        _format_orphaned(formatter, result.get("orphaned", []))
        return
    if status == "dry_run":
        summary = result.get("summary", {})
        formatter.console.print(
            f"  Would create {summary.get('added', 0)}, "
            f"update {summary.get('modified', 0)}, "
            f"delete {summary.get('deleted', 0)}"
        )
        _format_never_fetched(formatter, result.get("never_fetched", []))
        _format_orphaned(formatter, result.get("orphaned", []))
        return
    formatter.console.print(
        f"  {result.get('created', 0)} created, "
        f"{result.get('updated', 0)} updated, "
        f"{result.get('deleted', 0)} deleted"
    )
    _format_never_fetched(formatter, result.get("never_fetched", []))
    _format_orphaned(formatter, result.get("orphaned", []))
    # Show name drift warnings
    drift_warnings = result.get("name_drift_warnings", [])
    if drift_warnings:
        formatter.console.print(
            f"\n  [yellow]Warning: {len(drift_warnings)} config(s) have "
            f"local directory names that don't match their config name:[/yellow]"
        )
        for w in drift_warnings:
            formatter.console.print(
                f"    '{w['local_dirname']}' should be "
                f"'{w['expected_dirname']}' (config: {w['config_name']})"
            )
        formatter.console.print("  Run 'kbagent config rename' or 'kbagent sync pull' to fix.")


def _pull_one_liner(result: dict) -> str:
    """One-line summary of a single pull result."""
    details = result.get("details", [])
    new_n = sum(1 for d in details if d["action"] == "new")
    upd_n = sum(1 for d in details if d["action"] == "updated")
    rem_n = sum(1 for d in details if d["action"] == "removed")
    ren_n = sum(1 for d in details if d["action"] == "renamed")
    skip_n = sum(1 for d in details if d["action"] == "skipped")
    if not new_n and not upd_n and not rem_n and not ren_n and not skip_n:
        return "[green]up to date[/green]"
    parts = []
    if ren_n:
        parts.append(f"[magenta]>{ren_n} renamed[/magenta]")
    if new_n:
        parts.append(f"[green]+{new_n} new[/green]")
    if upd_n:
        parts.append(f"[yellow]~{upd_n} updated[/yellow]")
    if rem_n:
        parts.append(f"[red]-{rem_n} removed[/red]")
    if skip_n:
        parts.append(f"[cyan]!{skip_n} skipped[/cyan]")
    return ", ".join(parts)


def _diff_one_liner(result: dict) -> str:
    """One-line summary of a single diff result."""
    s = result.get("summary", {})
    mod = s.get("modified", 0)
    add = s.get("added", 0)
    dlt = s.get("deleted", 0)
    rmod = s.get("remote_modified", 0)
    conf = s.get("conflict", 0)
    ro = s.get("remote_only", 0)
    if not any([mod, add, dlt, rmod, conf, ro]):
        return "[green]in sync[/green]"
    parts = []
    if add:
        parts.append(f"[green]{add} to create[/green]")
    if mod:
        parts.append(f"[yellow]{mod} to push[/yellow]")
    if dlt:
        parts.append(f"[red]{dlt} to delete[/red]")
    if rmod:
        parts.append(f"[cyan]{rmod} to pull[/cyan]")
    if conf:
        parts.append(f"[red]{conf} conflicts[/red]")
    if ro:
        parts.append(f"[cyan]{ro} new remote[/cyan]")
    return ", ".join(parts)


def _push_one_liner(result: dict) -> str:
    """One-line summary of a single push result."""
    status = result.get("status", "")
    if status == "no_changes":
        return "[green]nothing to push[/green]"
    if status == "dry_run":
        s = result.get("summary", {})
        return f"would: +{s.get('added', 0)} ~{s.get('modified', 0)} -{s.get('deleted', 0)}"
    c = result.get("created", 0)
    u = result.get("updated", 0)
    d = result.get("deleted", 0)
    return f"+{c} created, ~{u} updated, -{d} deleted"


def _format_all_results(
    formatter: Any,
    data: dict,
    per_project_formatter: Any = None,
    one_liner: Any = None,
) -> None:
    """Format multi-project results for human output.

    In default mode: one line per project (compact summary).
    With --verbose: full detail per project.
    Errors always shown.
    """
    summary = data["summary"]
    projects = data["projects"]
    skipped = data.get("skipped", [])
    verbose = getattr(formatter, "verbose", False)

    for alias in sorted(projects):
        proj_result = projects[alias]
        if "error" in proj_result:
            formatter.console.print(f"  [red]x[/red] {alias}: [red]{proj_result['error']}[/red]")
        elif verbose and per_project_formatter:
            formatter.console.print(f"\n[bold]{alias}:[/bold]")
            per_project_formatter(formatter, proj_result)
        elif one_liner:
            formatter.console.print(f"  [green]OK[/green] {alias}: {one_liner(proj_result)}")
        else:
            formatter.console.print(f"  [green]OK[/green] {alias}")

    if skipped:
        formatter.console.print(f"\n[dim]Skipped (no manifest): {', '.join(skipped)}[/dim]")

    formatter.console.print(
        f"\n{summary['total']} projects: "
        f"[green]{summary['success']} OK[/green], "
        f"[red]{summary['failed']} failed[/red]"
        + (f", {summary.get('skipped', 0)} skipped" if summary.get("skipped") else "")
    )


@sync_app.command("pull")
def sync_pull(
    ctx: typer.Context,
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project alias to pull configurations from",
    ),
    all_projects: bool = typer.Option(
        False,
        "--all-projects",
        help="Pull all configured projects in parallel",
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
        help=(
            "Force re-pull. Locally-modified configs whose remote is unchanged "
            "are PRESERVED (kept as pending changes for `sync push`); a true "
            "merge conflict (local AND remote both changed since the last pull) "
            "aborts the pull so you can resolve it."
        ),
    ),
    theirs: bool = typer.Option(
        False,
        "--theirs",
        help=(
            "Discard local changes and make the tree match remote (the "
            "supported reconcile path for a drifted tree). Overwrites "
            "locally-modified configs, restores deleted/missing files, and "
            "resolves merge conflicts by taking the remote version instead "
            "of aborting."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be pulled without writing any files",
    ),
    job_limit: int = typer.Option(
        5,
        "--job-limit",
        help="Max recent jobs to pull per configuration (default 5)",
    ),
    no_storage: bool = typer.Option(
        False,
        "--no-storage",
        help="Skip downloading storage bucket/table metadata",
    ),
    no_jobs: bool = typer.Option(
        False,
        "--no-jobs",
        help="Skip downloading per-config job history",
    ),
    with_samples: bool = typer.Option(
        False,
        "--with-samples",
        help="Download table data samples (CSV previews)",
    ),
    sample_limit: int = typer.Option(
        100,
        "--sample-limit",
        help="Max rows per table sample (default 100)",
    ),
    max_samples: int = typer.Option(
        50,
        "--max-samples",
        help="Max number of tables to sample (default 50)",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help=(
            "Dev branch ID. Overrides the manifest / 'branch use' active "
            "branch for this single invocation. Requires exactly one --project."
        ),
    ),
) -> None:
    """Download configurations from a Keboola project to local files.

    Use --project for a single project or --all-projects for all configured
    projects in parallel (each in its own subdirectory).
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "sync_service")

    if all_projects and project:
        formatter.error(
            message="Cannot use --project with --all-projects",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2)
    if not all_projects and not project:
        formatter.error(
            message="Specify --project ALIAS or --all-projects",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2)
    if branch is not None and all_projects:
        formatter.error(
            message="--branch requires --project (branch id is per-project)",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2)

    if all_projects:
        base_dir = _safe_resolve_dir(directory)
        try:
            data = service.pull_all(
                base_dir,
                force=force,
                dry_run=dry_run,
                job_limit=job_limit,
                no_storage=no_storage,
                no_jobs=no_jobs,
                with_samples=with_samples,
                sample_limit=sample_limit,
                max_samples=max_samples,
                theirs=theirs,
            )
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
            raise typer.Exit(code=5) from None

        if formatter.json_mode:
            formatter.output(data)
        else:
            _format_all_results(formatter, data, _format_pull_result, _pull_one_liner)
        return

    project_root = _resolve_project_root(directory, project)

    # Auto-init if no manifest exists (same as --all-projects behavior)
    manifest_path = project_root / KEBOOLA_DIR / MANIFEST_FILE
    if not manifest_path.exists():
        try:
            service.init_sync(project, project_root)
        except Exception as exc:
            formatter.error(message=str(exc), error_code=ErrorCode.INIT_ERROR)
            raise typer.Exit(code=1) from None

    try:
        result = service.pull(
            alias=project,
            project_root=project_root,
            force=force,
            dry_run=dry_run,
            job_limit=job_limit,
            no_storage=no_storage,
            no_jobs=no_jobs,
            with_samples=with_samples,
            sample_limit=sample_limit,
            max_samples=max_samples,
            branch_override=branch,
            theirs=theirs,
        )
    except SyncConflictError as exc:
        if not formatter.json_mode:
            _format_conflict_list(formatter, exc.conflicts)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            project=project or "",
            details={"conflicts": exc.conflicts},
        )
        raise typer.Exit(code=1) from None
    except FileNotFoundError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.NOT_INITIALIZED)
        raise typer.Exit(code=1) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        _format_pull_result(formatter, result)


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
        formatter.error(message=str(exc), error_code=ErrorCode.NOT_INITIALIZED)
        raise typer.Exit(code=1) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        modified = result["modified"]
        added = result["added"]
        deleted = result["deleted"]
        unchanged = result["unchanged"]
        secret_warnings = result.get("plaintext_secret_warnings", [])

        if not modified and not added and not deleted:
            formatter.console.print(
                f"[green]No local changes detected.[/green] "
                f"({unchanged} configurations tracked) "
                f"[dim]Local check only -- run 'kbagent sync diff --project "
                f"<alias>' to compare against the remote.[/dim]"
            )
            _format_never_fetched(formatter, result.get("never_fetched", []))
        else:
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

            _format_never_fetched(formatter, result.get("never_fetched", []))

            formatter.console.print(
                f"\n{len(modified)} modified, {len(added)} added, "
                f"{len(deleted)} deleted, {unchanged} unchanged"
            )

        # Plaintext secret warning -- independent of local change state (issue #378).
        if secret_warnings:
            formatter.console.print(
                f"\n[bold red]PLAINTEXT SECRETS in {len(secret_warnings)} synced "
                f"config(s)[/bold red] (issue #378) -- in sync with the remote but NOT encrypted:"
            )
            for w in secret_warnings:
                keys = ", ".join(w["secret_keys"])
                formatter.console.print(f"  [red]![/red] {w['path']}  ({keys})")
            formatter.console.print(
                "[dim]  Re-push on kbagent >=0.54.0 to encrypt, then ROTATE the credential "
                "-- config version history keeps the old plaintext.[/dim]"
            )


@sync_app.command("diff")
def sync_diff(
    ctx: typer.Context,
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project alias to diff against",
    ),
    all_projects: bool = typer.Option(
        False,
        "--all-projects",
        help="Diff all configured projects in parallel",
    ),
    directory: Path = typer.Option(
        Path("."),
        "--directory",
        "-d",
        help="Project root directory (must contain .keboola/)",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help=(
            "Dev branch ID. Overrides the manifest / 'branch use' active "
            "branch for this single invocation. Requires exactly one --project."
        ),
    ),
) -> None:
    """Show detailed diff between local and remote configurations.

    Use --project for a single project or --all-projects for all configured
    projects in parallel.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "sync_service")

    if all_projects and project:
        formatter.error(
            message="Cannot use --project with --all-projects",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2)
    if not all_projects and not project:
        formatter.error(
            message="Specify --project ALIAS or --all-projects",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2)
    if branch is not None and all_projects:
        formatter.error(
            message="--branch requires --project (branch id is per-project)",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2)

    if all_projects:
        base_dir = _safe_resolve_dir(directory)
        try:
            data = service.diff_all(base_dir)
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
            raise typer.Exit(code=5) from None

        if formatter.json_mode:
            formatter.output(data)
        else:
            _format_all_results(formatter, data, _format_diff_result, _diff_one_liner)
        return

    project_root = _resolve_project_root(directory, project)

    try:
        result = service.diff(alias=project, project_root=project_root, branch_override=branch)
    except FileNotFoundError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.NOT_INITIALIZED)
        raise typer.Exit(code=1) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        changes = result["changes"]
        summary = result["summary"]
        remote_only = result.get("remote_only", [])

        if not changes and not remote_only:
            if result.get("never_fetched") or result.get("orphaned"):
                _format_never_fetched(formatter, result.get("never_fetched", []))
                _format_orphaned(formatter, result.get("orphaned", []))
            else:
                formatter.console.print(
                    "[green]No differences found.[/green] Local and remote are in sync."
                )
            return

        # Categorize changes by direction
        prefix_map = {
            "added": "[green]+ ",
            "modified": "[yellow]~ ",
            "remote_modified": "[cyan]~ ",
            "conflict": "[red]! ",
            "deleted": "[red]- ",
        }
        suffix_map = {
            "added": "[/green]",
            "modified": "[/yellow]",
            "remote_modified": "[/cyan]",
            "conflict": "[/red]",
            "deleted": "[/red]",
        }
        label_map = {
            "added": "ADDED",
            "modified": "MODIFIED",
            "remote_modified": "REMOTE MODIFIED",
            "conflict": "CONFLICT",
            "deleted": "DELETED",
        }

        local_changes = [c for c in changes if c["change_type"] in ("added", "modified", "deleted")]
        remote_changes = [c for c in changes if c["change_type"] == "remote_modified"]
        conflict_changes = [c for c in changes if c["change_type"] == "conflict"]

        # Local changes (what push would do)
        if local_changes:
            formatter.console.print("[bold]Local changes (push would apply):[/bold]")
            for change in local_changes:
                ct = change["change_type"]
                label = _change_label(change)
                formatter.console.print(
                    f"  {prefix_map[ct]}{label_map[ct]} {label}{suffix_map[ct]}"
                )
                for detail in change.get("details", []):
                    formatter.console.print(f"    {detail}")
            formatter.console.print(
                f"\n{summary['added']} to create, {summary['modified']} to update, "
                f"{summary['deleted']} to delete"
            )

        # Remote changes (need pull)
        if remote_changes:
            if local_changes:
                formatter.console.print()
            formatter.console.print("[bold]Remote changes (run 'sync pull' to fetch):[/bold]")
            for change in remote_changes:
                label = _change_label(change)
                formatter.console.print(f"  [cyan]~ REMOTE MODIFIED {label}[/cyan]")
                for detail in change.get("details", []):
                    formatter.console.print(f"    {detail}")

        # Conflicts (both sides changed)
        if conflict_changes:
            if local_changes or remote_changes:
                formatter.console.print()
            formatter.console.print(
                "[bold red]Conflicts (both local and remote changed):[/bold red]"
            )
            for change in conflict_changes:
                label = _change_label(change)
                formatter.console.print(f"  [red]! CONFLICT {label}[/red]")
                for detail in change.get("details", []):
                    formatter.console.print(f"    {detail}")

        # Remote-only configs (new on server, not yet pulled)
        if remote_only:
            if changes:
                formatter.console.print()
            formatter.console.print(
                f"[bold]Remote only ({len(remote_only)} new, run 'sync pull' to fetch):[/bold]"
            )
            for cfg in remote_only:
                name = cfg.get("config_name", cfg.get("config_id", ""))
                formatter.console.print(f"  [cyan]+ NEW {cfg['component_id']}/{name}[/cyan]")

        _format_never_fetched(formatter, result.get("never_fetched", []))
        _format_orphaned(formatter, result.get("orphaned", []))


@sync_app.command("push")
def sync_push(
    ctx: typer.Context,
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project alias to push changes to",
    ),
    all_projects: bool = typer.Option(
        False,
        "--all-projects",
        help="Push all configured projects in parallel",
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
    allow_plaintext: bool = typer.Option(
        False,
        "--allow-plaintext-on-encrypt-failure",
        help="Allow push even if secret encryption fails (DANGEROUS: secrets stored as plaintext)",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help=(
            "Dev branch ID. Overrides the manifest / 'branch use' active "
            "branch for this single invocation. Requires exactly one --project. "
            "When no '<branch_name>/' subtree exists on disk, the default tree "
            "(main/) is promoted to this branch."
        ),
    ),
    no_name_drift_warnings: bool = typer.Option(
        False,
        "--no-name-drift-warnings",
        help=(
            "Suppress the cosmetic name_drift_warnings array in the result "
            "envelope (the underlying detection still runs)."
        ),
    ),
) -> None:
    """Push local configuration changes to a Keboola project.

    Use --project for a single project or --all-projects for all configured
    projects in parallel.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "sync_service")

    if all_projects and project:
        formatter.error(
            message="Cannot use --project with --all-projects",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2)
    if not all_projects and not project:
        formatter.error(
            message="Specify --project ALIAS or --all-projects",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2)
    if branch is not None and all_projects:
        formatter.error(
            message="--branch requires --project (branch id is per-project)",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2)

    if all_projects:
        base_dir = _safe_resolve_dir(directory)
        try:
            data = service.push_all(
                base_dir,
                dry_run=dry_run,
                force=force,
                allow_plaintext_fallback=allow_plaintext,
            )
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
            raise typer.Exit(code=5) from None

        if formatter.json_mode:
            formatter.output(data)
        else:
            _format_all_results(formatter, data, _format_push_result, _push_one_liner)
        return

    project_root = _resolve_project_root(directory, project)

    try:
        result = service.push(
            alias=project,
            project_root=project_root,
            dry_run=dry_run,
            force=force,
            allow_plaintext_fallback=allow_plaintext,
            branch_override=branch,
            no_name_drift_warnings=no_name_drift_warnings,
        )
    except FileNotFoundError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.NOT_INITIALIZED)
        raise typer.Exit(code=1) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        status = result.get("status", "")

        if status == "no_changes":
            formatter.console.print("[green]No changes to push.[/green]")
            skipped_reason = result.get("skipped_reason")
            if skipped_reason:
                formatter.console.print(f"  [yellow]{skipped_reason}[/yellow]")
            _format_never_fetched(formatter, result.get("never_fetched", []))
            _format_orphaned(formatter, result.get("orphaned", []))
            return

        if status == "dry_run":
            formatter.console.print("[yellow]Dry run -- no changes applied:[/yellow]")
            for change in result.get("changes", []):
                label = _change_label(change)
                formatter.console.print(f"  {change['change_type'].upper()} {label}")
            summary = result["summary"]
            formatter.console.print(
                f"\nWould create {summary['added']}, update {summary['modified']}, "
                f"delete {summary['deleted']}"
            )
            _format_never_fetched(formatter, result.get("never_fetched", []))
            _format_orphaned(formatter, result.get("orphaned", []))
            return

        formatter.success(
            f"Pushed: {result['created']} created, "
            f"{result['updated']} updated, "
            f"{result['deleted']} deleted"
        )
        _format_never_fetched(formatter, result.get("never_fetched", []))
        _format_orphaned(formatter, result.get("orphaned", []))
        for change in result.get("pushed_details", []):
            label = _change_label(change)
            action = change["change_type"].upper()
            formatter.console.print(f"  {action} {label}")
        for err in result.get("errors", []):
            formatter.warning(
                f"  Error: {err['change_type']} {err['component_id']}/{err['config_id']}: "
                f"{err['message']}"
            )
        for warn in result.get("warnings", []):
            formatter.warning(f"  {warn['message']}")


@sync_app.command("clone")
def sync_clone(
    ctx: typer.Context,
    source: Path = typer.Option(
        ...,
        "--source",
        help="Reference synced project directory (must contain .keboola/manifest.json)",
    ),
    target: str = typer.Option(
        ...,
        "--target",
        help="Target project alias to clone INTO (a fresh/empty project on first clone)",
    ),
    target_dir: Path = typer.Option(
        ...,
        "--target-dir",
        help="Directory to materialise the clone into (must not exist on first clone)",
    ),
    bucket_map: Path | None = typer.Option(
        None,
        "--bucket-map",
        help="JSON/YAML file mapping {old_bucket_id: new_bucket_id} for input/output rewrites",
    ),
    variable_values: Path | None = typer.Option(
        None,
        "--variable-values",
        help="JSON/YAML file mapping {variable_name: value} to override keboola.variables rows",
    ),
    instance_rename: Path | None = typer.Option(
        None,
        "--instance-rename",
        help="JSON/YAML file mapping {old_path_prefix: new_path_prefix} to rename config dirs",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Apply overrides and show the would-be diff without pushing",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Target dev branch id (defaults to the target project's production branch)",
    ),
) -> None:
    """Clone a reference project into a fresh target, parameterised by overrides.

    Copies the reference tree, applies declarative overrides (bucket_map,
    variable_values, instance_rename), and pushes so every config is CREATEd
    fresh -- keboola.flow task configIds and transformation variable links are
    remapped reference->ULID automatically. Idempotent: re-running with an
    existing --target-dir just pushes and reports no_changes.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "sync_service")

    try:
        overrides: dict[str, Any] = {}
        if bucket_map is not None:
            overrides["bucket_map"] = _load_override_file(bucket_map)
        if variable_values is not None:
            overrides["variable_values"] = _load_override_file(variable_values)
        if instance_rename is not None:
            overrides["instance_rename"] = _load_override_file(instance_rename)

        result = service.clone_project(
            source=source,
            target_alias=target,
            target_dir=target_dir,
            overrides=overrides,
            dry_run=dry_run,
            branch_override=branch,
        )
    except FileNotFoundError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.NOT_INITIALIZED)
        raise typer.Exit(code=1) from None
    except FileExistsError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        _format_clone_result(formatter, result)


def _format_clone_result(formatter: Any, result: dict[str, Any]) -> None:
    """Human-mode rendering for ``sync clone``."""
    status = result.get("status", "")
    overrides = (
        f"buckets={result.get('bucket_rewrites', 0)}, "
        f"variables={result.get('variable_overrides', 0)}, "
        f"renamed={result.get('renamed_instances', 0)}"
    )
    if status == "dry_run":
        summary = result.get("summary", {})
        formatter.console.print("[yellow]Dry run -- nothing pushed.[/yellow]")
        formatter.console.print(f"  Overrides applied: {overrides}")
        formatter.console.print(
            f"  Would create {summary.get('added', 0)} config(s) in "
            f"[cyan]{result.get('target_alias')}[/cyan]."
        )
        return
    if status == "no_changes":
        formatter.console.print(
            f"[green]Already cloned[/green] -- no changes to push into "
            f"[cyan]{result.get('target_alias')}[/cyan]."
        )
        return
    formatter.success(
        f"Cloned into {result.get('target_alias')}: {result.get('created', 0)} created "
        f"({overrides}, flow_task_remaps={result.get('flow_task_remaps', 0)})"
    )
    for err in result.get("errors", []):
        formatter.warning(
            f"  Error: {err.get('change_type')} "
            f"{err.get('component_id')}/{err.get('config_id')}: {err.get('message')}"
        )


@sync_app.command("branch-link")
def sync_branch_link(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    directory: Path = typer.Option(Path("."), "--directory", "-d", help="Project root directory"),
    branch_id: int | None = typer.Option(
        None, "--branch-id", help="Link to existing Keboola branch by ID"
    ),
    branch_name: str | None = typer.Option(
        None, "--branch-name", help="Create/find branch with this name"
    ),
) -> None:
    """Link the current git branch to a Keboola development branch.

    Creates a new Keboola dev branch if one doesn't exist with the same name
    as the current git branch.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "sync_service")
    project_root = directory.resolve()

    try:
        result = service.branch_link(
            alias=project,
            project_root=project_root,
            branch_id=branch_id,
            branch_name=branch_name,
        )
    except FileNotFoundError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.NOT_INITIALIZED)
        raise typer.Exit(code=1) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        status = result["status"]
        if status == "already_linked":
            formatter.console.print(
                f"Already linked: {result['git_branch']} -> "
                f"Keboola branch {result['keboola_branch_id']} ({result['keboola_branch_name']})"
            )
        else:
            formatter.success(
                f"Linked {result['git_branch']} -> "
                f"Keboola branch {result['keboola_branch_id']} ({result['keboola_branch_name']})"
            )


@sync_app.command("branch-unlink")
def sync_branch_unlink(
    ctx: typer.Context,
    directory: Path = typer.Option(Path("."), "--directory", "-d", help="Project root directory"),
) -> None:
    """Remove the branch mapping for the current git branch.

    Does NOT delete the Keboola branch itself.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "sync_service")
    project_root = directory.resolve()

    try:
        result = service.branch_unlink(project_root=project_root)
    except FileNotFoundError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.NOT_INITIALIZED)
        raise typer.Exit(code=1) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        if result["status"] == "not_linked":
            formatter.console.print(f"Branch '{result['git_branch']}' is not linked.")
        else:
            formatter.success(
                f"Unlinked {result['git_branch']} from Keboola branch {result['keboola_branch_id']}"
            )


@sync_app.command("branch-status")
def sync_branch_status(
    ctx: typer.Context,
    directory: Path = typer.Option(Path("."), "--directory", "-d", help="Project root directory"),
) -> None:
    """Show the branch mapping status for the current git branch."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "sync_service")
    project_root = directory.resolve()

    try:
        result = service.branch_status(project_root=project_root)
    except FileNotFoundError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.NOT_INITIALIZED)
        raise typer.Exit(code=1) from None
    except ConfigError as exc:
        # Corrupted .keboola/branch-mapping.json -- surface as clean
        # exit-5 envelope so the user sees the descriptive message
        # ("Failed to parse ...: Invalid branch ID ...") instead of a
        # Python traceback. Mirrors the handler other sync commands
        # already have (issue #269 sec-20 follow-up).
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        if not result.get("git_branching"):
            formatter.console.print("Git-branching mode is not enabled.")
            return

        git_branch = result.get("git_branch", "unknown")
        if result.get("linked"):
            if result.get("is_production"):
                formatter.console.print(
                    f"Branch: {git_branch}\nKeboola: production\nStatus: [green]Linked[/green]"
                )
            else:
                formatter.console.print(
                    f"Branch: {git_branch}\n"
                    f"Keboola: {result['keboola_branch_id']} ({result['keboola_branch_name']})\n"
                    f"Status: [green]Linked[/green]"
                )
        else:
            formatter.console.print(
                f"Branch: {git_branch}\n"
                f"Keboola: (none)\n"
                f"Status: [red]Not linked[/red]\n\n"
                f"Run 'kbagent sync branch-link --project ALIAS' to link."
            )
