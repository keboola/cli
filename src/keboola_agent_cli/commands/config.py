"""Configuration commands - list, detail, search, update, delete, and scaffold.

Thin CLI layer: parses arguments, calls ConfigService, formats output.
No business logic belongs here.
"""

import json
import logging
import re
from pathlib import Path

import typer
from rich.syntax import Syntax

from ..config_store import ConfigStore
from ..constants import KEBOOLA_DIR_NAME, MANIFEST_FILENAME, VALID_COMPONENT_TYPES
from ..errors import ConfigError, KeboolaApiError
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
        )

    formatter = get_formatter(ctx)
    service = get_service(ctx, "config_service")
    config_store: ConfigStore = ctx.obj["config_store"]

    # --branch requires --project (branch ID is per-project)
    # For list with multiple projects, only validate if explicit --branch given
    if branch is not None and (not project or len(project) != 1):
        formatter.error(
            message="--branch requires exactly one --project (branch ID is per-project)",
            error_code="INVALID_ARGUMENT",
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
            error_code="INVALID_ARGUMENT",
        )
        raise typer.Exit(code=2)

    try:
        result = service.list_configs(
            aliases=effective_project,
            component_type=component_type,
            component_id=component_id,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
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
    project: str = typer.Option(..., "--project", help="Project alias"),
    component_id: str = typer.Option(..., "--component-id", help="Component ID"),
    config_id: str = typer.Option(..., "--config-id", help="Configuration ID"),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Get detail from a specific dev branch ID (defaults to active branch)",
    ),
) -> None:
    """Show detailed information about a specific configuration.

    If a dev branch is active (via 'branch use'), the detail is fetched
    from that branch. Use --branch to override.
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "config.detail",
            project=project,
            component_id=component_id,
            config_id=config_id,
            branch=branch,
        )

    formatter = get_formatter(ctx)
    service = get_service(ctx, "config_service")
    config_store: ConfigStore = ctx.obj["config_store"]

    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    try:
        result = service.get_config_detail(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            branch_id=effective_branch,
        )
        formatter.output(result, format_config_detail)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            project=project,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None


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
            error_code="INVALID_ARGUMENT",
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
            error_code="INVALID_ARGUMENT",
        )
        raise typer.Exit(code=2)

    # Validate regex if provided
    if use_regex:
        try:
            re.compile(query)
        except re.error as exc:
            formatter.error(
                message=f"Invalid regex pattern: {exc}",
                error_code="INVALID_ARGUMENT",
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
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
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
            error_code="VALIDATION_ERROR",
        )
        raise typer.Exit(code=2) from None

    if configuration:
        try:
            config_dict = _parse_json_input(configuration)
        except (json.JSONDecodeError, FileNotFoundError) as exc:
            formatter.error(
                message=f"Invalid --configuration input: {exc}",
                error_code="VALIDATION_ERROR",
            )
            raise typer.Exit(code=2) from None

    if configuration_file:
        try:
            config_dict = json.loads(configuration_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            formatter.error(
                message=f"Invalid JSON in {configuration_file}: {exc}",
                error_code="VALIDATION_ERROR",
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
                    error_code="VALIDATION_ERROR",
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
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
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
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
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
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
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
