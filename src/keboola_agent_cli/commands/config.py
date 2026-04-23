"""Configuration commands - list, detail, search, update, delete, and scaffold.

Thin CLI layer: parses arguments, calls ConfigService, formats output.
No business logic belongs here.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape
from rich.syntax import Syntax

from ..config_store import ConfigStore
from ..constants import KEBOOLA_DIR_NAME, MANIFEST_FILENAME, VALID_COMPONENT_TYPES
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..output import format_config_detail, format_configs_table, format_search_results
from ._helpers import (
    check_cli_permission,
    emit_hint,
    emit_project_warnings,
    get_formatter,
    get_service,
    map_error_to_exit_code,
    resolve_branch,
    should_hint,
)

logger = logging.getLogger(__name__)


def _detect_branch_prefix(output_dir: Path) -> str | None:
    """Detect kbc project branch path from .keboola/manifest.json.

    When output_dir is inside a kbc project (has .keboola/manifest.json),
    returns the default branch path (e.g. "main") so scaffold files
    land in the correct location (main/extractor/... instead of extractor/...).

    Returns None if not a kbc project or manifest is unreadable.
    """
    manifest_path = output_dir / KEBOOLA_DIR_NAME / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        branches = manifest.get("branches", [])
        if branches:
            # Use the first (default) branch path
            branch_path = branches[0].get("path", "")
            if branch_path:
                logger.debug("Detected kbc branch prefix: %s", branch_path)
                return branch_path
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Could not read manifest: %s", exc)

    return None


config_app = typer.Typer(help="Browse and inspect configurations")


@config_app.callback(invoke_without_command=True)
def _config_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "config")


@config_app.command("list")
def config_list(
    ctx: typer.Context,
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias to query (can be repeated for multiple projects)",
    ),
    component_type: str | None = typer.Option(
        None,
        "--component-type",
        help="Filter by component type: extractor, writer, transformation, application",
    ),
    component_id: str | None = typer.Option(
        None,
        "--component-id",
        help="Filter by specific component ID (e.g. keboola.ex-db-snowflake)",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="List configs from a specific dev branch ID (defaults to active branch)",
    ),
    include_rows: bool = typer.Option(
        False,
        "--include-rows",
        help=(
            "Include full configuration + rows body per config (noticeably larger "
            "payload). Without this flag the response is summary-level only "
            "(name/description/component/last_modified/folder)."
        ),
    ),
) -> None:
    """List configurations from connected projects.

    If a dev branch is active (via 'branch use'), configs from that branch
    are listed. Use --branch to override.
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "config.list",
            project=project,
            component_type=component_type,
            component_id=component_id,
            branch=branch,
            include_rows=include_rows,
        )

    formatter = get_formatter(ctx)
    service = get_service(ctx, "config_service")
    config_store: ConfigStore = ctx.obj["config_store"]

    # --branch requires --project (branch ID is per-project)
    # For list with multiple projects, only validate if explicit --branch given
    if branch is not None and (not project or len(project) != 1):
        formatter.error(
            message="--branch requires exactly one --project (branch ID is per-project)",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    # Resolve active branch (only for single-project queries)
    effective_branch: int | None = branch
    effective_project = project
    if branch is None and project and len(project) == 1:
        _, effective_branch = resolve_branch(config_store, formatter, project[0], None)

    # Validate component_type if provided
    if component_type and component_type not in VALID_COMPONENT_TYPES:
        formatter.error(
            message=f"Invalid component type '{component_type}'. "
            f"Valid types: {', '.join(VALID_COMPONENT_TYPES)}",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    try:
        result = service.list_configs(
            aliases=effective_project,
            component_type=component_type,
            component_id=component_id,
            branch_id=effective_branch,
            include_rows=include_rows,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    # In JSON mode, include both configs and errors in the response
    if formatter.json_mode:
        formatter.output(result)
    else:
        # In human mode, show per-project errors as warnings and configs as table
        format_configs_table(formatter.console, result)
        emit_project_warnings(formatter, result)


@config_app.command("detail")
def config_detail(
    ctx: typer.Context,
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help=(
            "Project alias to query. Repeat for multiple projects (only valid in "
            "bulk mode, i.e. when --config-id is omitted)."
        ),
    ),
    component_id: str = typer.Option(..., "--component-id", help="Component ID"),
    config_id: str | None = typer.Option(
        None,
        "--config-id",
        help=(
            "Configuration ID. When omitted, the command switches to BULK mode "
            "and returns every configuration under --component-id as a JSON "
            "array ({'configs': [...], 'errors': [...]})."
        ),
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Get detail from a specific dev branch ID (defaults to active branch)",
    ),
    with_state: bool = typer.Option(
        False,
        "--with-state",
        help=(
            "Attach the runtime state dict to each config under 'state'. "
            "Single-config mode: one extra call via get_config_state. "
            "Bulk mode: state is fetched inline via include=state (no N+1)."
        ),
    ),
) -> None:
    """Show detailed information about one or many configurations.

    \b
    Two modes, switched by --config-id:
      - Single-config mode (default when --config-id given): returns the
        full configuration detail dict, shape identical to historical output
        (callers depending on this shape are unaffected).
      - Bulk mode (--config-id omitted): returns every config under
        --component-id as {"configs": [...], "errors": [...]}. Works across
        multiple projects when --project is repeated (per-project errors
        surface in the errors list without aborting other projects).

    If a dev branch is active (via 'branch use') the detail is fetched
    from that branch. --branch overrides; bulk mode with multiple projects
    rejects --branch (branch IDs are per-project).

    \b
    Examples:
      # Single config (unchanged shape)
      kbagent config detail --project prod --component-id keboola.ex-db-snowflake --config-id 101
      # Bulk: every Snowflake writer in one project
      kbagent --json config detail --project prod --component-id keboola.wr-db-snowflake
      # Bulk: every Snowflake writer across many projects (with runtime state)
      kbagent --json config detail --project prod --project stage --component-id keboola.wr-db-snowflake --with-state
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "config.detail",
            project=project,
            component_id=component_id,
            config_id=config_id,
            branch=branch,
            with_state=with_state,
        )

    formatter = get_formatter(ctx)
    service = get_service(ctx, "config_service")
    config_store: ConfigStore = ctx.obj["config_store"]

    # --project is required (zero, one, or many)
    if not project:
        formatter.error(
            message="--project is required (repeat for multiple projects in bulk mode).",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    # --config-id + multiple --project is ambiguous: a single config lives in
    # exactly one project. Reject with a clear message instead of picking one.
    if config_id is not None and len(project) > 1:
        formatter.error(
            message=(
                "--config-id is only valid with exactly one --project. "
                "Omit --config-id to fan out across multiple projects."
            ),
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    # --branch requires exactly one --project (branch IDs are per-project).
    if branch is not None and len(project) != 1:
        formatter.error(
            message="--branch requires exactly one --project (branch ID is per-project).",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    # Resolve active branch when only one --project was passed
    effective_branch: int | None = branch
    if branch is None and len(project) == 1:
        _, effective_branch = resolve_branch(config_store, formatter, project[0], None)

    try:
        if config_id is not None:
            # Single-config mode: shape unchanged for backward compat
            result = service.get_config_detail(
                alias=project[0],
                component_id=component_id,
                config_id=config_id,
                branch_id=effective_branch,
                with_state=with_state,
            )
        else:
            # Bulk mode: one call per project, filtered by component_id
            result = service.get_config_detail(
                alias=project[0],
                component_id=component_id,
                config_id=None,
                branch_id=effective_branch,
                with_state=with_state,
                aliases=project,
            )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            project=project[0],
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None

    if config_id is not None:
        # Single-config mode: emit unchanged shape
        formatter.output(result, format_config_detail)
    else:
        # Bulk mode: emit {configs: [...], errors: [...]}
        if formatter.json_mode:
            formatter.output(result)
        else:
            _format_config_detail_bulk(formatter.console, result, component_id, with_state)
            emit_project_warnings(formatter, result)


def _format_config_detail_bulk(
    console: Any,
    data: dict,
    component_id: str,
    with_state: bool,
) -> None:
    """Render bulk config detail results as a Rich table grouped by project.

    Args:
        console: Rich Console instance.
        data: ``{"configs": [...], "errors": [...]}`` from the service layer.
        component_id: The component ID the bulk query was scoped to.
        with_state: Whether ``state`` was requested (adds a State column).
    """
    from rich.table import Table

    configs = data.get("configs", [])
    if not configs:
        console.print(
            f"[dim]No configurations found for [bold]{component_id}[/bold] "
            "in the selected project(s).[/dim]"
        )
        return

    # Group by project_alias for readability
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for cfg in configs:
        alias = cfg.get("project_alias", "unknown")
        if alias not in grouped:
            order.append(alias)
            grouped[alias] = []
        grouped[alias].append(cfg)

    for alias in order:
        table = Table(title=f"{component_id} configs -- {alias} ({len(grouped[alias])})")
        table.add_column("Config ID", style="bold cyan", justify="right")
        table.add_column("Name")
        table.add_column("Rows", justify="right", style="dim")
        table.add_column("Version", justify="right", style="dim")
        table.add_column("Disabled", style="dim")
        if with_state:
            table.add_column("State Keys", style="dim")

        for cfg in grouped[alias]:
            row = [
                str(cfg.get("config_id", "")),
                str(cfg.get("name", "")),
                str(len(cfg.get("rows", []) or [])),
                str(cfg.get("version", "") or ""),
                "yes" if cfg.get("isDisabled") else "",
            ]
            if with_state:
                state = cfg.get("state") or {}
                row.append(str(len(state) if isinstance(state, dict) else 0))
            table.add_row(*row)

        console.print(table)
        console.print()


@config_app.command("search")
def config_search(
    ctx: typer.Context,
    query: str = typer.Option(..., "--query", "-q", help="Search string or regex pattern"),
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias to search (can be repeated for multiple projects)",
    ),
    component_type: str | None = typer.Option(
        None,
        "--component-type",
        help="Filter by component type: extractor, writer, transformation, application",
    ),
    component_id: str | None = typer.Option(
        None,
        "--component-id",
        help="Filter by specific component ID (e.g. keboola.ex-db-snowflake)",
    ),
    ignore_case: bool = typer.Option(
        False,
        "--ignore-case",
        "-i",
        help="Case-insensitive matching",
    ),
    use_regex: bool = typer.Option(
        False,
        "--regex",
        "-r",
        help="Interpret query as a regular expression",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Search configs in a specific dev branch ID (defaults to active branch)",
    ),
) -> None:
    """Search through configuration bodies for a string or pattern.

    Searches config names, descriptions, parameters, and row definitions.
    Reports which configurations match and where in the JSON tree.

    If a dev branch is active (via 'branch use'), configs from that branch
    are searched. Use --branch to override.
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "config.search",
            query=query,
            project=project,
            component_type=component_type,
            component_id=component_id,
            ignore_case=ignore_case,
            regex=use_regex,
            branch=branch,
        )

    formatter = get_formatter(ctx)
    service = get_service(ctx, "config_service")
    config_store: ConfigStore = ctx.obj["config_store"]

    # --branch requires exactly one --project
    if branch is not None and (not project or len(project) != 1):
        formatter.error(
            message="--branch requires exactly one --project (branch ID is per-project)",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    # Resolve active branch (only for single-project queries)
    effective_branch: int | None = branch
    if branch is None and project and len(project) == 1:
        _, effective_branch = resolve_branch(config_store, formatter, project[0], None)

    # Validate component_type
    if component_type and component_type not in VALID_COMPONENT_TYPES:
        formatter.error(
            message=f"Invalid component type '{component_type}'. "
            f"Valid types: {', '.join(VALID_COMPONENT_TYPES)}",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    # Validate regex if provided
    if use_regex:
        try:
            re.compile(query)
        except re.error as exc:
            formatter.error(
                message=f"Invalid regex pattern: {exc}",
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
            raise typer.Exit(code=2) from None

    try:
        result = service.search_configs(
            query=query,
            aliases=project,
            component_type=component_type,
            component_id=component_id,
            ignore_case=ignore_case,
            use_regex=use_regex,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        format_search_results(formatter.console, result)
        emit_project_warnings(formatter, result)


def _parse_json_input(raw: str) -> dict:
    """Parse JSON from inline string, ``@file``, or ``-`` (stdin)."""
    import sys

    if raw == "-":
        return json.loads(sys.stdin.read())
    if raw.startswith("@"):
        file_path = Path(raw[1:])
        if not file_path.is_file():
            raise FileNotFoundError(f"Input file not found: {file_path}")
        return json.loads(file_path.read_text(encoding="utf-8"))
    return json.loads(raw)


def _parse_set_value(raw: str) -> object:
    """Try to parse *raw* as JSON; fall back to plain string."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


@config_app.command("update")
def config_update(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    component_id: str = typer.Option(
        ...,
        "--component-id",
        help="Component ID (e.g. keboola.python-transformation-v2)",
    ),
    config_id: str = typer.Option(
        ...,
        "--config-id",
        help="Configuration ID to update",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help="New configuration name",
    ),
    description: str | None = typer.Option(
        None,
        "--description",
        help="New configuration description",
    ),
    configuration: str | None = typer.Option(
        None,
        "--configuration",
        help="Configuration JSON: inline, @file.json, or - for stdin",
    ),
    configuration_file: Path | None = typer.Option(
        None,
        "--configuration-file",
        help="Path to a JSON file with configuration content",
        exists=True,
        readable=True,
    ),
    set_values: list[str] | None = typer.Option(
        None,
        "--set",
        help="Set a nested value: PATH VALUE (e.g. --set 'parameters.db.host=new-host')",
    ),
    merge: bool = typer.Option(
        False,
        "--merge",
        help="Deep-merge into existing config instead of replacing",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would change without applying",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Update in a specific dev branch ID (defaults to active branch)",
    ),
) -> None:
    """Update a configuration's metadata and/or content.

    \b
    Metadata options (--name, --description) update display info.
    Content options modify the configuration JSON itself:
      --configuration / --configuration-file : provide a full JSON blob
      --set PATH=VALUE : set a single nested key (repeatable)
      --merge : deep-merge into existing config (preserves sibling keys)
      --dry-run : preview changes without applying

    \b
    Examples:
      # Update just the name
      kbagent config update --project P --component-id C --config-id ID --name "New name"

      # Replace entire configuration from a file
      kbagent config update --project P --component-id C --config-id ID --configuration-file config.json

      # Deep-merge a partial update (preserves sibling keys!)
      kbagent config update --project P --component-id C --config-id ID \\
        --configuration '{"parameters": {"tables": {"new": "data"}}}' --merge

      # Set a single nested value
      kbagent config update --project P --component-id C --config-id ID \\
        --set 'parameters.db.host=new-host.example.com'

      # Preview changes without applying
      kbagent config update --project P --component-id C --config-id ID \\
        --set 'parameters.db.host=new-host' --dry-run
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "config_service")

    # --- Parse configuration content ------------------------------------------
    config_dict: dict | None = None
    if configuration and configuration_file:
        formatter.error(
            message="Cannot use both --configuration and --configuration-file.",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
        raise typer.Exit(code=2) from None

    if configuration:
        try:
            config_dict = _parse_json_input(configuration)
        except (json.JSONDecodeError, FileNotFoundError) as exc:
            formatter.error(
                message=f"Invalid --configuration input: {exc}",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
            raise typer.Exit(code=2) from None

    if configuration_file:
        try:
            config_dict = json.loads(configuration_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            formatter.error(
                message=f"Invalid JSON in {configuration_file}: {exc}",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
            raise typer.Exit(code=2) from None

    # --- Parse --set values ---------------------------------------------------
    parsed_sets: list[tuple[str, object]] | None = None
    if set_values:
        parsed_sets = []
        for item in set_values:
            if "=" not in item:
                formatter.error(
                    message=f"Invalid --set format: '{item}'. Expected PATH=VALUE.",
                    error_code=ErrorCode.VALIDATION_ERROR,
                )
                raise typer.Exit(code=2) from None
            path, _, raw_value = item.partition("=")
            parsed_sets.append((path.strip(), _parse_set_value(raw_value.strip())))

    # --set implies merge
    effective_merge = merge or bool(parsed_sets)

    try:
        result = service.update_config(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            name=name,
            description=description,
            configuration=config_dict,
            set_paths=parsed_sets,
            merge=effective_merge,
            dry_run=dry_run,
            branch_id=branch,
        )
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

    # --- Output ---------------------------------------------------------------
    if result.get("dry_run"):
        changes = result.get("changes", [])
        if formatter.json_mode:
            formatter.output(result)
        else:
            if not changes:
                formatter.success("No changes detected.")
            else:
                formatter.console.print(f"\n[bold]Dry-run: {len(changes)} change(s)[/bold]\n")
                for change in changes:
                    formatter.console.print(f"  {change}")
                formatter.console.print()
        return

    if formatter.json_mode:
        formatter.output(result)
    else:
        updated_name = result.get("name", "")
        branch_info = ""
        if result.get("branch_id"):
            branch_info = f" (branch {result['branch_id']})"
        formatter.success(
            f"Updated config '{updated_name}' "
            f"({result.get('component_id', component_id)}/{config_id})"
            f"{branch_info}"
        )


@config_app.command("rename")
def config_rename(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    component_id: str = typer.Option(
        ...,
        "--component-id",
        help="Component ID (e.g. keboola.python-transformation-v2)",
    ),
    config_id: str = typer.Option(
        ...,
        "--config-id",
        help="Configuration ID to rename",
    ),
    name: str = typer.Option(
        ...,
        "--name",
        help="New name for the configuration",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Rename in a specific dev branch ID (defaults to active branch)",
    ),
    directory: Path | None = typer.Option(
        None,
        "--directory",
        "-d",
        help="Sync working directory (auto-detects .keboola/manifest.json in CWD if omitted)",
    ),
) -> None:
    """Rename a configuration (update name via API + rename local sync directory).

    Updates the configuration name in the Keboola project. If a local sync
    directory is detected (either via --directory or the current working
    directory), the local folder is renamed and the manifest is updated
    to match.

    \b
    Examples:
      # Simple rename
      kbagent config rename --project prod --component-id kds-team.app-custom-python \\
        --config-id abc123 --name "Stripe Extractor"

      # Rename with explicit sync directory
      kbagent config rename --project prod --component-id kds-team.app-custom-python \\
        --config-id abc123 --name "Stripe Extractor" --directory ./my-project
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "config.rename",
            project=project,
            component_id=component_id,
            config_id=config_id,
            name=name,
            branch=branch,
        )

    formatter = get_formatter(ctx)
    service = get_service(ctx, "config_service")

    # Auto-detect sync directory from CWD if not specified
    effective_directory = directory
    if effective_directory is None:
        cwd = Path.cwd()
        if (cwd / KEBOOLA_DIR_NAME / MANIFEST_FILENAME).exists():
            effective_directory = cwd

    try:
        result = service.rename_config(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            name=name,
            branch_id=branch,
            directory=effective_directory,
        )
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
        branch_info = ""
        if result.get("branch_id"):
            branch_info = f" (branch {result['branch_id']})"
        formatter.success(
            f'Renamed "{result["old_name"]}" -> "{result["new_name"]}"'
            f" ({component_id}/{config_id}){branch_info}"
        )
        sync_info = result.get("sync")
        if sync_info:
            formatter.console.print(
                f"  Sync: {sync_info['old_path']}/ -> {sync_info['new_path']}/"
                f" ({sync_info['method']})"
            )


@config_app.command("delete")
def config_delete(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    component_id: str = typer.Option(
        ...,
        "--component-id",
        help="Component ID (e.g. keboola.python-transformation-v2)",
    ),
    config_id: str = typer.Option(
        ...,
        "--config-id",
        help="Configuration ID to delete",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Delete from a specific dev branch ID (defaults to active branch)",
    ),
) -> None:
    """Delete a configuration from a project.

    If a dev branch is active (via 'branch use'), the deletion targets
    that branch. Use --branch to override. Deleting in a branch marks
    the config as removed without affecting Main.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "config_service")

    try:
        result = service.delete_config(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            branch_id=branch,
        )
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
        branch_info = ""
        if result.get("branch_id"):
            branch_info = f" (branch {result['branch_id']})"
        formatter.success(
            f"Deleted config {result['component_id']}/{result['config_id']} "
            f"from project '{result['project_alias']}'{branch_info}"
        )


# --- File extension to Rich Syntax lexer mapping ---
_EXT_TO_LEXER: dict[str, str] = {
    ".yml": "yaml",
    ".yaml": "yaml",
    ".json": "json",
    ".sql": "sql",
    ".py": "python",
    ".toml": "toml",
    ".md": "markdown",
    ".txt": "text",
    ".sh": "bash",
    ".r": "r",
    ".js": "javascript",
    ".ts": "typescript",
}


@config_app.command("new")
def config_new(
    ctx: typer.Context,
    component_id: str = typer.Option(
        ...,
        "--component-id",
        help="Component ID (e.g. keboola.ex-http)",
    ),
    name: str = typer.Option(
        "",
        "--name",
        help="Configuration name (default: auto-generated from component)",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project alias (for AI Service auth)",
    ),
    output_dir: str | None = typer.Option(
        None,
        "--output-dir",
        help="Write scaffold files to disk instead of printing",
    ),
) -> None:
    """Generate boilerplate configuration files for a Keboola component.

    Produces a ready-to-edit scaffold (config YAML, SQL/Python code blocks,
    description) that can be written to disk with --output-dir or printed
    to stdout for inspection.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "component_service")

    try:
        scaffold = service.generate_scaffold(
            alias=project,
            component_id=component_id,
            name=name or None,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            project=project or "",
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None

    if output_dir:
        # Detect kbc project branch prefix (e.g. "main/")
        branch_prefix = _detect_branch_prefix(Path(output_dir))
        if branch_prefix:
            scaffold_dir = branch_prefix + "/" + scaffold["directory"]
        else:
            scaffold_dir = scaffold["directory"]

        base_path = Path(output_dir) / scaffold_dir
        base_path.mkdir(parents=True, exist_ok=True)

        for file_entry in scaffold["files"]:
            file_path = base_path / file_entry["path"]
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(file_entry["content"], encoding="utf-8")

        if formatter.json_mode:
            formatter.output(
                {
                    "directory": str(base_path),
                    "files_written": [f["path"] for f in scaffold["files"]],
                }
            )
        else:
            formatter.success(f"Scaffold written to {base_path} ({len(scaffold['files'])} file(s))")
    else:
        # Print scaffold content
        if formatter.json_mode:
            formatter.output(scaffold)
        else:
            formatter.console.print(f"\n[bold]Scaffold for [cyan]{component_id}[/cyan][/bold]")
            formatter.console.print(f"[dim]Directory: {scaffold['directory']}[/dim]\n")

            for file_entry in scaffold["files"]:
                file_name = file_entry["path"]
                content = file_entry["content"]

                # Determine lexer from file extension
                suffix = Path(file_name).suffix.lower()
                lexer = _EXT_TO_LEXER.get(suffix, "text")

                formatter.console.rule(f"[bold]{file_name}[/bold]")
                syntax = Syntax(
                    content,
                    lexer,
                    theme="monokai",
                    line_numbers=True,
                )
                formatter.console.print(syntax)
                formatter.console.print()


# ── Config metadata commands ───────────────────────────────────────────


@config_app.command("metadata-list")
def config_metadata_list(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    component_id: str = typer.Option(..., "--component-id", help="Component ID"),
    config_id: str = typer.Option(..., "--config-id", help="Configuration ID"),
    branch: int | None = typer.Option(
        None, "--branch", help="Dev branch ID (defaults to active branch)"
    ),
) -> None:
    """List all metadata entries on a configuration."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "config.metadata-list",
            project=project,
            component_id=component_id,
            config_id=config_id,
            branch=branch,
        )
        return
    formatter = get_formatter(ctx)
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)
    service = get_service(ctx, "config_service")
    try:
        result = service.list_config_metadata(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        entries = result.get("metadata", [])
        if not entries:
            formatter.console.print("[dim]No metadata entries.[/dim]")
        else:
            for e in entries:
                formatter.console.print(
                    f"  [dim]{escape(str(e.get('id', '')))}[/dim]  [green]{escape(e.get('key', ''))}[/green] = {escape(str(e.get('value', '')))}  [dim]{escape(e.get('provider', 'user'))}[/dim]"
                )


@config_app.command("get-metadata")
def config_get_metadata(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    component_id: str = typer.Option(..., "--component-id", help="Component ID"),
    config_id: str = typer.Option(..., "--config-id", help="Configuration ID"),
    key: str = typer.Option(..., "--key", help="Metadata key to read"),
    branch: int | None = typer.Option(
        None, "--branch", help="Dev branch ID (defaults to active branch)"
    ),
) -> None:
    """Read a single metadata value by key.

    Exits with code 1 (NOT_FOUND) if the key is not present.
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "config.get-metadata",
            project=project,
            component_id=component_id,
            config_id=config_id,
            key=key,
            branch=branch,
        )
        return
    formatter = get_formatter(ctx)
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)
    service = get_service(ctx, "config_service")
    try:
        result = service.get_config_metadata_value(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            key=key,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output(result, lambda c, d: c.print(d["value"]))


@config_app.command("set-metadata")
def config_set_metadata(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    component_id: str = typer.Option(..., "--component-id", help="Component ID"),
    config_id: str = typer.Option(..., "--config-id", help="Configuration ID"),
    key: str = typer.Option(..., "--key", help="Metadata key to set"),
    value: str = typer.Option(..., "--value", help="Metadata value (string)"),
    branch: int | None = typer.Option(
        None, "--branch", help="Dev branch ID (defaults to active branch)"
    ),
) -> None:
    """Set a metadata key/value on a configuration (upsert)."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "config.set-metadata",
            project=project,
            component_id=component_id,
            config_id=config_id,
            key=key,
            value=value,
            branch=branch,
        )
        return
    formatter = get_formatter(ctx)
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)
    service = get_service(ctx, "config_service")
    try:
        result = service.set_config_metadata(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            key=key,
            value=value,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output(
        result, lambda c, d: c.print(f"[bold green]Success:[/bold green] {d['message']}")
    )


@config_app.command("delete-metadata")
def config_delete_metadata(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    component_id: str = typer.Option(..., "--component-id", help="Component ID"),
    config_id: str = typer.Option(..., "--config-id", help="Configuration ID"),
    metadata_id: int = typer.Option(..., "--metadata-id", help="Numeric ID from metadata-list"),
    branch: int | None = typer.Option(
        None, "--branch", help="Dev branch ID (defaults to active branch)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a configuration metadata entry by its numeric ID."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "config.delete-metadata",
            project=project,
            component_id=component_id,
            config_id=config_id,
            metadata_id=metadata_id,
            branch=branch,
        )
        return
    formatter = get_formatter(ctx)
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    if (
        not yes
        and not formatter.json_mode
        and not typer.confirm(f"Delete metadata ID {metadata_id} from {component_id}/{config_id}?")
    ):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)

    service = get_service(ctx, "config_service")
    try:
        result = service.delete_config_metadata(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            metadata_id=metadata_id,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output(
        result, lambda c, d: c.print(f"[bold green]Success:[/bold green] {d['message']}")
    )


@config_app.command("set-folder")
def config_set_folder(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    component_id: str = typer.Option(..., "--component-id", help="Component ID"),
    config_id: str = typer.Option(..., "--config-id", help="Configuration ID"),
    name: str = typer.Option(..., "--name", help="Folder name (empty string to clear)"),
    branch: int | None = typer.Option(
        None, "--branch", help="Dev branch ID (defaults to active branch)"
    ),
) -> None:
    """Set the folder (KBC.configuration.folderName) on a configuration.

    Organises configs into named groups in the Keboola UI.
    Pass an empty string to remove the folder assignment.
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "config.set-folder",
            project=project,
            component_id=component_id,
            config_id=config_id,
            name=name,
            branch=branch,
        )
        return
    formatter = get_formatter(ctx)
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)
    service = get_service(ctx, "config_service")
    try:
        result = service.set_config_folder(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            folder_name=name,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output(
        result, lambda c, d: c.print(f"[bold green]Success:[/bold green] {d['message']}")
    )


def _parse_kv_var(raw: str) -> tuple[str, str]:
    """Split a ``KEY=VALUE`` token into ``(key, value)``; ``#``-prefix preserved."""
    if "=" not in raw:
        raise typer.BadParameter(
            f"Invalid --var: '{raw}'. Expected KEY=VALUE (use # prefix for secrets)."
        )
    key, _, value = raw.partition("=")
    key = key.strip()
    if not key:
        raise typer.BadParameter(f"Invalid --var: '{raw}'. Empty key.")
    return key, value


@config_app.command("variables-set")
def config_variables_set(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    component_id: str = typer.Option(
        ..., "--component-id", help="Component ID of the config to attach variables to"
    ),
    config_id: str = typer.Option(
        ..., "--config-id", help="Configuration ID to attach variables to"
    ),
    variable: list[str] | None = typer.Option(
        None,
        "--var",
        help="Variable as KEY=VALUE (repeatable). Prefix key with # to mark as a secret (auto-encrypted).",
    ),
    replace: bool = typer.Option(
        False,
        "--replace",
        help="Replace ALL variable values instead of merging (drops any keys not in --var).",
    ),
    variables_id: str | None = typer.Option(
        None,
        "--variables-id",
        help="Attach parent to an existing keboola.variables config (skips auto-create).",
    ),
    values_id: str | None = typer.Option(
        None,
        "--values-id",
        help="Attach to a specific values row (defaults to the first row).",
    ),
    branch: int | None = typer.Option(None, "--branch", help="Development branch ID (per-project)"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview the change without writing to Keboola."
    ),
    allow_plaintext: bool = typer.Option(
        False,
        "--allow-plaintext-on-encrypt-failure",
        help="Fall back to plaintext if encryption fails (NOT recommended).",
    ),
) -> None:
    """Assign variables to a config (auto-creates backing keboola.variables on first call).

    Variables are presented as a flat KEY=VALUE dict. The implementation detail
    that Keboola stores them as a separate keboola.variables configuration with
    rows is hidden: first call creates the sibling config named
    <parent-name>-vars + default row; subsequent calls update the same row.
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "config.variables-set",
            project=project,
            component_id=component_id,
            config_id=config_id,
            variable=variable,
            replace=replace,
            variables_id=variables_id,
            values_id=values_id,
            branch=branch,
        )
        return

    formatter = get_formatter(ctx)
    config_store: ConfigStore = ctx.obj["config_store"]

    raw_vars = variable or []
    if not raw_vars:
        formatter.error(
            message="At least one --var KEY=VALUE is required.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    variables_dict: dict[str, str] = {}
    for raw in raw_vars:
        try:
            key, value = _parse_kv_var(raw)
        except typer.BadParameter as exc:
            formatter.error(message=str(exc), error_code=ErrorCode.INVALID_ARGUMENT)
            raise typer.Exit(code=2) from None
        variables_dict[key] = value

    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    service = get_service(ctx, "variables_service")

    if dry_run:
        try:
            current = service.get_variables(
                alias=project,
                component_id=component_id,
                config_id=config_id,
                branch_id=effective_branch,
            )
        except KeboolaApiError as exc:
            formatter.error(
                message=exc.message,
                error_code=exc.error_code,
                retryable=exc.retryable,
            )
            raise typer.Exit(code=map_error_to_exit_code(exc)) from None
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
            raise typer.Exit(code=5) from None

        preview_values = (
            dict(variables_dict) if replace else {**current["values"], **variables_dict}
        )
        result = {
            "dry_run": True,
            "project_alias": project,
            "parent_component_id": component_id,
            "parent_config_id": config_id,
            "was_linked": current["linked"],
            "current_variables_id": current["variables_id"],
            "current_values": current["values"],
            "would_write": preview_values,
            "action": "would_create" if not current["linked"] else "would_update",
        }
        if formatter.json_mode:
            formatter.output(result)
        else:
            _format_variables_dry_run(formatter, result)
        return

    try:
        result = service.set_variables(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            variables=variables_dict,
            replace=replace,
            variables_id=variables_id,
            values_id=values_id,
            branch_id=effective_branch,
            allow_plaintext_fallback=allow_plaintext,
        )
    except KeboolaApiError as exc:
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        _format_variables_set(formatter, result)


@config_app.command("variables-get")
def config_variables_get(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    component_id: str = typer.Option(
        ..., "--component-id", help="Component ID of the config whose variables to read"
    ),
    config_id: str = typer.Option(
        ..., "--config-id", help="Configuration ID whose variables to read"
    ),
    branch: int | None = typer.Option(None, "--branch", help="Development branch ID (per-project)"),
) -> None:
    """Read the current variable values attached to a config."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "config.variables-get",
            project=project,
            component_id=component_id,
            config_id=config_id,
            branch=branch,
        )
        return

    formatter = get_formatter(ctx)
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    service = get_service(ctx, "variables_service")
    try:
        result = service.get_variables(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            branch_id=effective_branch,
        )
    except KeboolaApiError as exc:
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        _format_variables_get(formatter, result)


@config_app.command("variables-clear")
def config_variables_clear(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    component_id: str = typer.Option(
        ..., "--component-id", help="Component ID of the config to unlink"
    ),
    config_id: str = typer.Option(
        ..., "--config-id", help="Configuration ID to unlink variables from"
    ),
    branch: int | None = typer.Option(None, "--branch", help="Development branch ID (per-project)"),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt.",
    ),
) -> None:
    """Unlink variables from a config (does NOT delete the underlying keboola.variables)."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "config.variables-clear",
            project=project,
            component_id=component_id,
            config_id=config_id,
            branch=branch,
        )
        return

    formatter = get_formatter(ctx)
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    if not yes and not formatter.json_mode:
        confirmed = typer.confirm(
            f"Unlink variables from {component_id}/{config_id}? "
            "(The underlying variables config will NOT be deleted.)"
        )
        if not confirmed:
            formatter.console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=0)

    service = get_service(ctx, "variables_service")
    try:
        result = service.clear_variables(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            branch_id=effective_branch,
        )
    except KeboolaApiError as exc:
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        _format_variables_clear(formatter, result)


def _format_variables_get(formatter: Any, result: dict) -> None:
    if not result.get("linked"):
        formatter.console.print(
            "[yellow]No variables linked[/yellow] to "
            f"[cyan]{escape(result['parent_component_id'])}[/cyan]/"
            f"[cyan]{escape(result['parent_config_id'])}[/cyan]."
        )
        return

    formatter.console.print(
        f"[bold]Variables on[/bold] "
        f"[cyan]{escape(result['parent_component_id'])}[/cyan]/"
        f"[cyan]{escape(result['parent_config_id'])}[/cyan] "
        f"[dim](variables_id={escape(result['variables_id'] or '')}, "
        f"values_id={escape(result['values_id'] or '')})[/dim]"
    )
    if not result["values"]:
        formatter.console.print("  [dim](no values set)[/dim]")
        return
    for k in sorted(result["values"]):
        v = result["values"][k]
        display_v = "<encrypted>" if k.startswith("#") else escape(str(v))
        formatter.console.print(f"  [green]{escape(k)}[/green] = {display_v}")


def _format_variables_set(formatter: Any, result: dict) -> None:
    action_label = (
        "[green]created[/green]" if result["action"] == "created" else "[yellow]updated[/yellow]"
    )
    formatter.console.print(
        f"Variables {action_label} on "
        f"[cyan]{escape(result['parent_component_id'])}[/cyan]/"
        f"[cyan]{escape(result['parent_config_id'])}[/cyan]"
    )
    formatter.console.print(f"  variables_id: [cyan]{escape(result['variables_id'])}[/cyan]")
    formatter.console.print(f"  values_id:    [cyan]{escape(result['values_id'])}[/cyan]")
    if result.get("encrypted_keys"):
        joined = ", ".join(escape(k) for k in result["encrypted_keys"])
        formatter.console.print(f"  [dim]encrypted: {joined}[/dim]")
    formatter.console.print("[bold]Final values:[/bold]")
    for k in sorted(result["values"]):
        v = result["values"][k]
        display_v = "<encrypted>" if k.startswith("#") else escape(str(v))
        formatter.console.print(f"  [green]{escape(k)}[/green] = {display_v}")


def _format_variables_clear(formatter: Any, result: dict) -> None:
    if not result["was_linked"]:
        formatter.console.print(
            f"[yellow]Nothing to clear[/yellow]: "
            f"[cyan]{escape(result['parent_component_id'])}[/cyan]/"
            f"[cyan]{escape(result['parent_config_id'])}[/cyan] had no variables linked."
        )
        return
    formatter.console.print(
        f"[green]Unlinked[/green] variables from "
        f"[cyan]{escape(result['parent_component_id'])}[/cyan]/"
        f"[cyan]{escape(result['parent_config_id'])}[/cyan] "
        f"[dim](was variables_id={escape(result['unlinked_variables_id'] or '')}, "
        f"values_id={escape(result['unlinked_values_id'] or '')})[/dim]"
    )
    formatter.console.print(
        "[dim]The underlying keboola.variables config was NOT deleted. "
        "Use 'kbagent config delete' to remove it if no other config references it.[/dim]"
    )


def _format_variables_dry_run(formatter: Any, result: dict) -> None:
    verb = "create" if result["action"] == "would_create" else "update"
    formatter.console.print(
        f"[yellow]DRY RUN[/yellow]: would {verb} variables on "
        f"[cyan]{escape(result['parent_component_id'])}[/cyan]/"
        f"[cyan]{escape(result['parent_config_id'])}[/cyan]"
    )
    if result["was_linked"]:
        formatter.console.print(
            f"  current variables_id: [cyan]{escape(result['current_variables_id'] or '')}[/cyan]"
        )
    else:
        formatter.console.print("  [dim]no variables currently linked[/dim]")

    current_keys = set(result["current_values"])
    proposed_keys = set(result["would_write"])
    for k in sorted(current_keys | proposed_keys):
        current_v = result["current_values"].get(k)
        proposed_v = result["would_write"].get(k)
        if k not in proposed_keys:
            display = "<encrypted>" if k.startswith("#") else escape(str(current_v))
            formatter.console.print(f"  [red]- {escape(k)}[/red] = {display} [dim](dropped)[/dim]")
        elif k not in current_keys:
            display = "<encrypted>" if k.startswith("#") else escape(str(proposed_v))
            formatter.console.print(f"  [green]+ {escape(k)}[/green] = {display}")
        elif current_v != proposed_v:
            display_cur = "<encrypted>" if k.startswith("#") else escape(str(current_v))
            display_new = "<encrypted>" if k.startswith("#") else escape(str(proposed_v))
            formatter.console.print(
                f"  [yellow]~ {escape(k)}[/yellow] = {display_cur} -> {display_new}"
            )
        else:
            display = "<encrypted>" if k.startswith("#") else escape(str(current_v))
            formatter.console.print(f"  [dim]= {escape(k)} = {display}[/dim]")
