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
    emit_project_warnings,
    get_formatter,
    get_service,
    map_error_to_exit_code,
    resolve_branch,
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


@config_app.command("list", rich_help_panel="Browse")
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


@config_app.command("detail", rich_help_panel="Browse")
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
            "Single-config mode: state is read from the same detail response "
            "(no extra HTTP call). Bulk mode: state is fetched inline via "
            "include=state (no N+1). WARNING: --with-state output may contain "
            "OAuth tokens, refresh tokens, and other credential-bearing runtime "
            "data. Do not pipe into logs, scratch files, or shared workspaces "
            "without redaction."
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

    # Typer ``...`` enforces presence but not non-emptiness; guard here so
    # ``--component-id ""`` fails fast with a clear message instead of
    # constructing a malformed /components//configs URL downstream.
    if not component_id.strip():
        formatter.error(
            message="--component-id must not be empty.",
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
            # Single-config mode: shape unchanged for backward compat,
            # plus the opt-in sandbox annotation (since v0.42.1, issue #312):
            # the service layer now owns the keboola.sandboxes
            # configurationId->workspace.id resolution so HTTP and REST
            # callers get the same enrichment, not only the CLI.
            result = service.get_config_detail(
                alias=project[0],
                component_id=component_id,
                config_id=config_id,
                branch_id=effective_branch,
                with_state=with_state,
                include_sandbox_annotation=True,
            )
        else:
            # Bulk mode: one call per project, filtered by component_id.
            # Annotation flag stays off here -- bulk mode would N+1 the
            # workspace listing endpoint (one extra round-trip per config),
            # and the field that triggers the annotation in single-config
            # mode (parameters.id) is rarely consumed in bulk anyway.
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

    # ``component_id`` is user-supplied and Rich interprets ``[tag]`` syntax.
    # Escape before embedding in Rich-rendered strings so an ID like
    # ``keboola.[red]evil[/red]`` cannot paint the terminal.
    safe_component_id = escape(component_id)

    configs = data.get("configs", [])
    if not configs:
        console.print(
            f"[dim]No configurations found for [bold]{safe_component_id}[/bold] "
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
        table = Table(title=f"{safe_component_id} configs -- {alias} ({len(grouped[alias])})")
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


@config_app.command("search", rich_help_panel="Browse")
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


@config_app.command("update", rich_help_panel="Lifecycle")
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
    change_description: str | None = typer.Option(
        None,
        "--change-description",
        help="Version changeDescription for the audit trail (default: auto-generated)",
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
    allow_plaintext: bool = typer.Option(
        False,
        "--allow-plaintext-on-encrypt-failure",
        help="Allow write even if secret encryption fails (DANGEROUS: secrets stored as plaintext)",
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

      # Set a meaningful version changeDescription for the audit trail
      kbagent config update --project P --component-id C --config-id ID \\
        --set 'parameters.db.host=new-host' --change-description "AI-1234: point at new DB host"
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
            change_description=change_description,
            branch_id=branch,
            allow_plaintext_fallback=allow_plaintext,
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
    normalizations = result.get("normalizations") or []

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
            change_desc = result.get("change_description")
            if change_desc:
                formatter.console.print(f"[dim]changeDescription:[/dim] {change_desc}")
            _emit_normalizations_warning(formatter, normalizations)
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
        _emit_normalizations_warning(formatter, normalizations)
        _emit_plaintext_written_warning(formatter, result)


def _emit_normalizations_warning(formatter: Any, normalizations: list[dict[str, Any]]) -> None:
    """Surface ``parameters.blocks[].codes[].script`` normalizations in human mode.

    Storage API silently accepts a string for ``script``; the runtime
    validator rejects it later. When kbagent auto-fixes the shape, the
    operator must see what was changed -- otherwise the fix is invisible
    and a downstream consumer might rely on the original (broken) input
    shape having been written verbatim. JSON mode already exposes the
    same data via the ``normalizations`` field on the envelope.
    """
    if not normalizations:
        return
    formatter.console.print(
        f"[yellow]Auto-normalized {len(normalizations)} script field(s) "
        f"to array (string -> list). See --json for details.[/yellow]"
    )
    for entry in normalizations:
        formatter.console.print(
            f"  [dim]{entry['path']}: {entry['action']} -> {entry['after_length']} element(s)[/dim]"
        )


def _emit_plaintext_written_warning(formatter: Any, result: dict[str, Any]) -> None:
    """Surface a plaintext-on-encrypt-failure fallback in human mode.

    When ``--allow-plaintext-on-encrypt-failure`` lets a write proceed despite a
    failed encryption, the service result carries ``plaintext_written`` -- the
    secret key-paths now stored in PLAINTEXT (key-paths only, never the values).
    Name them and the remediation so the leak is visible and actionable. JSON
    mode already exposes the same list on the envelope, so emit only here.
    """
    leaked = result.get("plaintext_written")
    if not leaked:
        return
    formatter.warning(
        f"{len(leaked)} secret(s) were written in PLAINTEXT (encryption failed and "
        f"--allow-plaintext-on-encrypt-failure was set): {', '.join(leaked)}. "
        f"Rotate these credentials and re-encrypt once the Encryption API is reachable "
        f"-- config version history retains the plaintext copy."
    )


@config_app.command("set-default-bucket", rich_help_panel="Storage")
def config_set_default_bucket(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    component_id: str = typer.Option(
        ...,
        "--component-id",
        help="Component ID (e.g. keboola.ex-db-snowflake)",
    ),
    config_id: str = typer.Option(
        ...,
        "--config-id",
        help="Configuration ID",
    ),
    bucket: str | None = typer.Option(
        None,
        "--bucket",
        help="Bucket ID to set as default output (e.g. in.c-preferred-name)",
    ),
    clear: bool = typer.Option(
        False,
        "--clear",
        help="Remove the default_bucket key. Mutually exclusive with --bucket.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would change without applying",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Apply in a specific dev branch ID (defaults to active branch)",
    ),
) -> None:
    """Set or clear ``storage.output.default_bucket`` on a configuration.

    \b
    The Keboola Storage API honors ``configuration.storage.output.default_bucket``
    to override the auto-derived bucket name (``in.<component>-<config-id>``)
    for any output table that does not pin its own ``destination``.

    \b
    Examples:
      # Set
      kbagent config set-default-bucket --project P --component-id keboola.ex-db-snowflake \\
        --config-id 12345 --bucket in.c-preferred-name

      # Clear
      kbagent config set-default-bucket --project P --component-id keboola.ex-db-snowflake \\
        --config-id 12345 --clear

      # Preview
      kbagent config set-default-bucket --project P --component-id keboola.ex-db-snowflake \\
        --config-id 12345 --bucket in.c-preferred-name --dry-run
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "config_service")

    if bucket is not None and clear:
        formatter.error(
            message="Pass exactly one of --bucket or --clear, not both.",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
        raise typer.Exit(code=2) from None
    if bucket is None and not clear:
        formatter.error(
            message="Pass --bucket BUCKET_ID or --clear.",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
        raise typer.Exit(code=2) from None

    try:
        result = service.set_default_bucket(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            bucket=bucket,
            clear=clear,
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
        return

    target = f"{component_id}/{config_id}"
    if result.get("changed") is False:
        existing = result.get("default_bucket")
        if clear:
            formatter.success(f"No change: default_bucket was already absent on {target}.")
        else:
            formatter.success(f"No change: default_bucket on {target} was already '{existing}'.")
        return

    if clear:
        formatter.success(f"Cleared default_bucket on {target}.")
    else:
        formatter.success(f"Set default_bucket on {target} to '{bucket}'.")


@config_app.command("rename", rich_help_panel="Lifecycle")
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


@config_app.command("delete", rich_help_panel="Lifecycle")
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


@config_app.command("new", rich_help_panel="Lifecycle")
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
        help="Configuration name (default: auto-generated from component; required with --push)",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project alias (for AI Service auth; required with --push)",
    ),
    output_dir: str | None = typer.Option(
        None,
        "--output-dir",
        help="Write scaffold files to disk instead of printing",
    ),
    push: bool = typer.Option(
        False,
        "--push",
        help="Also create the configuration remotely via the Storage API (one-shot; requires --project and --name)",
    ),
    no_files: bool = typer.Option(
        False,
        "--no-files",
        help="With --push: skip writing/printing scaffold; only POST to API (FIIA-style one-shot)",
    ),
    description: str = typer.Option(
        "",
        "--description",
        help="Configuration description (used with --push)",
    ),
    configuration: str | None = typer.Option(
        None,
        "--configuration",
        help="Override the configuration body to POST (used with --push): JSON inline, @file, or - for stdin",
    ),
    configuration_file: Path | None = typer.Option(
        None,
        "--configuration-file",
        help="Override the configuration body from a JSON file (used with --push)",
        exists=True,
        readable=True,
    ),
    no_validate: bool = typer.Option(
        False,
        "--no-validate",
        help="Skip schema validation against the component's AI Service spec (used with --push)",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Create in a specific dev branch (used with --push; defaults to active branch)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="With --push: show planned POST + validation result without creating",
    ),
    allow_plaintext: bool = typer.Option(
        False,
        "--allow-plaintext-on-encrypt-failure",
        help="With --push: allow create even if secret encryption fails (DANGEROUS: secrets stored as plaintext)",
    ),
) -> None:
    """Generate boilerplate configuration files for a Keboola component, optionally creating the config remotely in one shot.

    \b
    Two modes:
      * Default (no --push): scaffold only -- generates ready-to-edit files
        (config YAML, code blocks, description). Writes to --output-dir or
        prints to stdout. **Zero API calls.**
      * With --push: scaffold step + POST to Storage API. Requires --project
        and a non-empty --name. Use --no-files for FIIA-style one-shot create
        with no filesystem step.

    \b
    Examples:
      # Scaffold-only (today's behavior, unchanged)
      kbagent config new --component-id keboola.ex-http --output-dir ./scratch

      # Scaffold AND remote create:
      kbagent config new --component-id keboola.ex-http --name "API ingest" \\
        --project prod --output-dir ./scratch --push

      # FIIA-style: one-shot remote create with no filesystem step:
      kbagent config new --component-id keboola.ex-http --name "API ingest" \\
        --project prod --push --no-files

      # Override the POSTed body with a pre-made config:
      kbagent config new --component-id keboola.python-transformation-v2 \\
        --name "T1" --project prod --push --no-files \\
        --configuration-file ./body.json --branch 42

      # Preview the planned POST without creating:
      kbagent config new --component-id keboola.ex-http --name "smoke" \\
        --project prod --push --no-files --dry-run
    """
    formatter = get_formatter(ctx)

    # ── Flag-combination validation ──────────────────────────────────────────
    #
    # All --push-gated flags must be set only when --push is on; --push itself
    # requires --project and a non-empty --name; --configuration vs
    # --configuration-file and --output-dir vs --no-files are mutually
    # exclusive.
    push_gated: list[tuple[str, bool]] = [
        ("--no-files", no_files),
        ("--description", bool(description)),
        ("--configuration", configuration is not None),
        ("--configuration-file", configuration_file is not None),
        ("--no-validate", no_validate),
        ("--branch", branch is not None),
        ("--dry-run", dry_run),
    ]
    if not push:
        for flag_name, flag_set in push_gated:
            if flag_set:
                formatter.error(
                    message=f"{flag_name} requires --push",
                    error_code=ErrorCode.VALIDATION_ERROR,
                )
                raise typer.Exit(code=2)
    else:
        if not project:
            formatter.error(
                message="--push requires --project",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
            raise typer.Exit(code=2)
        if not name:
            formatter.error(
                message="--push requires a non-empty --name",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
            raise typer.Exit(code=2)
        if configuration is not None and configuration_file is not None:
            formatter.error(
                message="--configuration and --configuration-file are mutually exclusive",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
            raise typer.Exit(code=2)
        if no_files and output_dir:
            formatter.error(
                message="--no-files and --output-dir are mutually exclusive",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
            raise typer.Exit(code=2)

    # ── Parse the optional body override (used only with --push) ─────────────
    config_body: dict[str, Any] | None = None
    if push:
        if configuration is not None:
            try:
                config_body = _parse_json_input(configuration)
            except (json.JSONDecodeError, FileNotFoundError) as exc:
                formatter.error(
                    message=f"Invalid --configuration input: {exc}",
                    error_code=ErrorCode.VALIDATION_ERROR,
                )
                raise typer.Exit(code=2) from None
        elif configuration_file is not None:
            try:
                config_body = json.loads(configuration_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                formatter.error(
                    message=f"Invalid JSON in --configuration-file {configuration_file}: {exc}",
                    error_code=ErrorCode.VALIDATION_ERROR,
                )
                raise typer.Exit(code=2) from None

    # ── Scaffold step (runs in scaffold-only mode and in push+files modes) ───
    scaffold: dict[str, Any] | None = None
    skip_scaffold = push and no_files
    if not skip_scaffold:
        component_service = get_service(ctx, "component_service")
        try:
            scaffold = component_service.generate_scaffold(
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

    # ── Push path: also create remotely via Storage API ──────────────────────
    if push:
        config_service = get_service(ctx, "config_service")
        try:
            push_result = config_service.create_config(
                alias=project,
                component_id=component_id,
                name=name,
                description=description,
                configuration=config_body,
                branch_id=branch,
                dry_run=dry_run,
                validate=not no_validate,
                allow_plaintext_fallback=allow_plaintext,
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

        # When --push is set AND --output-dir is given AND we're not in
        # dry-run mode, ALSO write the scaffold to disk in addition to the
        # POST (the "scaffold + push" combo). Dry-run must NOT touch the
        # filesystem -- the user expects a preview, not a side effect.
        #
        # ``silent=True``: the push-result envelope below is the authoritative
        # output for this path. In JSON mode emitting the scaffold envelope
        # too would produce two concatenated JSON documents on stdout (breaks
        # ``jq``); in human mode the dim banner adds noise above the "Created
        # config ..." line that already lists the same path. ``json_mode`` is
        # passed as a sentinel ``False`` because ``silent=True`` short-circuits
        # before it is read.
        if output_dir and scaffold is not None and not dry_run:
            _write_scaffold_to_disk(formatter, scaffold, output_dir, json_mode=False, silent=True)

        if formatter.json_mode:
            formatter.output(push_result)
        else:
            _render_push_result_human(formatter, push_result, component_id, name)
        return

    # ── Scaffold-only path: today's behavior, byte-for-byte unchanged ────────
    assert scaffold is not None  # narrowing for type-checker; skip_scaffold is False here.
    if output_dir:
        _write_scaffold_to_disk(formatter, scaffold, output_dir, json_mode=formatter.json_mode)
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


def _write_scaffold_to_disk(
    formatter: Any,
    scaffold: dict[str, Any],
    output_dir: str,
    json_mode: bool,
    silent: bool = False,
) -> None:
    """Shared helper: write the generated scaffold files under ``output_dir``.

    Detects an enclosing ``main/`` branch prefix the same way the pre-push
    path does, so the layout matches what ``kbagent sync push`` would expect.

    Output rules:
    - ``silent=True``: write files only; emit no banner and no JSON envelope.
      Used by the ``--push --output-dir`` path, where the push-result envelope
      (single JSON in JSON mode; the "Created config ..." line in human mode)
      is the authoritative output and the scaffold-banner side-channel would
      either duplicate JSON (breaking ``jq`` pipes) or distract in human mode.
    - ``silent=False`` (default; scaffold-only mode): emit a JSON envelope
      ``{directory, files_written}`` when ``json_mode`` is set, otherwise a
      dim-formatted "Scaffold written to ..." banner.
    """
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

    if silent:
        return

    if json_mode:
        formatter.output(
            {
                "directory": str(base_path),
                "files_written": [f["path"] for f in scaffold["files"]],
            }
        )
    else:
        formatter.console.print(
            f"[dim]Scaffold written to {base_path} ({len(scaffold['files'])} file(s))[/dim]"
        )


def _render_push_result_human(
    formatter: Any,
    result: dict[str, Any],
    component_id: str,
    name: str,
) -> None:
    """Render the human-mode banner for ``config new --push`` (dry-run + success)."""
    validation_status = result.get("validation_status", "skipped")
    validation_errors = result.get("validation_errors", []) or []
    branch_info = f" (branch {result['branch_id']})" if result.get("branch_id") else ""

    if result.get("dry_run"):
        formatter.console.print(
            f"\n[bold]Dry-run -- would POST {escape(component_id)} '{escape(name)}'{branch_info}[/bold]\n"
        )
        formatter.console.print("[dim]Planned configuration body:[/dim]")
        formatter.console.print(json.dumps(result.get("configuration", {}), indent=2))
        if validation_status == "ok":
            formatter.console.print("\n[green]✓ Schema validation passed[/green]")
        elif validation_status == "skipped":
            formatter.console.print(
                "\n[yellow]⚠ Schema validation skipped[/yellow] "
                "[dim](empty shell, no schema available, or --no-validate)[/dim]"
            )
        else:  # failed
            formatter.console.print("\n[red]✗ Schema validation failed:[/red]")
            for err in validation_errors:
                formatter.console.print(f"  [red]• {escape(err)}[/red]")
        return

    config_id = result.get("id", "")
    formatter.success(
        f"Created config '{escape(name)}' [{config_id}] in {escape(component_id)}{branch_info}"
    )
    if validation_status == "skipped":
        formatter.console.print(
            "[dim]Note: schema validation was skipped "
            "(empty shell, no schema available, or --no-validate).[/dim]"
        )
    _emit_plaintext_written_warning(formatter, result)


# ── Config metadata commands ───────────────────────────────────────────


@config_app.command("metadata-list", rich_help_panel="Metadata")
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


@config_app.command("get-metadata", rich_help_panel="Metadata")
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


@config_app.command("set-metadata", rich_help_panel="Metadata")
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


@config_app.command("delete-metadata", rich_help_panel="Metadata")
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


@config_app.command("set-folder", rich_help_panel="Metadata")
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


@config_app.command("variables-set", rich_help_panel="Variables")
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
        _emit_plaintext_written_warning(formatter, result)


@config_app.command("variables-get", rich_help_panel="Variables")
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


@config_app.command("variables-clear", rich_help_panel="Variables")
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


# ── config row-create ──────────────────────────────────────────────────────────


@config_app.command("row-create", rich_help_panel="Rows")
def config_row_create(
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
        help="Configuration ID to add the row to",
    ),
    name: str = typer.Option(
        ...,
        "--name",
        help="Row name",
    ),
    description: str = typer.Option(
        "",
        "--description",
        help="Row description",
    ),
    configuration: str | None = typer.Option(
        None,
        "--configuration",
        help="Row configuration JSON: inline, @file.json, or - for stdin",
    ),
    is_disabled: bool = typer.Option(
        False,
        "--is-disabled",
        help="Create the row in disabled state (excluded from job runs)",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Create in a specific dev branch ID (defaults to active branch)",
    ),
    allow_plaintext: bool = typer.Option(
        False,
        "--allow-plaintext-on-encrypt-failure",
        help="Allow write even if secret encryption fails (DANGEROUS: secrets stored as plaintext)",
    ),
) -> None:
    """Create a new configuration row.

    \b
    Examples:
      # Create a row with a name only (empty configuration)
      kbagent config row-create --project P --component-id C --config-id ID --name "Row 1"

      # Create a row with configuration content
      kbagent config row-create --project P --component-id C --config-id ID \\
        --name "Row 1" --configuration '{"parameters": {"table": "orders"}}'

      # Create from a JSON file
      kbagent config row-create --project P --component-id C --config-id ID \\
        --name "Row 1" --configuration @row.json

      # Create a disabled row
      kbagent config row-create --project P --component-id C --config-id ID \\
        --name "Row 1" --is-disabled
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "config_service")

    config_dict: dict | None = None
    if configuration:
        try:
            config_dict = _parse_json_input(configuration)
        except (json.JSONDecodeError, FileNotFoundError) as exc:
            formatter.error(
                message=f"Invalid --configuration input: {exc}",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
            raise typer.Exit(code=2) from None

    try:
        result = service.create_config_row(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            name=name,
            description=description,
            configuration=config_dict,
            is_disabled=is_disabled,
            branch_id=branch,
            allow_plaintext_fallback=allow_plaintext,
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
        row_name = result.get("name", name)
        row_id = result.get("id", "")
        branch_info = ""
        if result.get("branch_id"):
            branch_info = f" (branch {result['branch_id']})"
        formatter.success(
            f"Created row '{escape(row_name)}' [{row_id}] "
            f"in {escape(component_id)}/{escape(config_id)}{branch_info}"
        )
        _emit_plaintext_written_warning(formatter, result)


# ── config row-update ──────────────────────────────────────────────────────────


@config_app.command("row-update", rich_help_panel="Rows")
def config_row_update(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    component_id: str = typer.Option(
        ...,
        "--component-id",
        help="Component ID",
    ),
    config_id: str = typer.Option(
        ...,
        "--config-id",
        help="Configuration ID",
    ),
    row_id: str = typer.Option(
        ...,
        "--row-id",
        help="Row ID to update",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help="New row name",
    ),
    description: str | None = typer.Option(
        None,
        "--description",
        help="New row description",
    ),
    configuration: str | None = typer.Option(
        None,
        "--configuration",
        help="Row configuration JSON: inline, @file.json, or - for stdin",
    ),
    set_values: list[str] | None = typer.Option(
        None,
        "--set",
        help="Set a nested value: PATH=VALUE (e.g. --set 'parameters.table=orders')",
    ),
    merge: bool = typer.Option(
        False,
        "--merge",
        help="Deep-merge into existing row config instead of replacing",
    ),
    change_description: str | None = typer.Option(
        None,
        "--change-description",
        help="Version changeDescription for the audit trail (default: auto-generated)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would change without applying",
    ),
    is_disabled: bool = typer.Option(
        False,
        "--is-disabled",
        help="Disable the row (mutually exclusive with --is-enabled)",
    ),
    is_enabled: bool = typer.Option(
        False,
        "--is-enabled",
        help="Enable the row (mutually exclusive with --is-disabled)",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Update in a specific dev branch ID (defaults to active branch)",
    ),
    allow_plaintext: bool = typer.Option(
        False,
        "--allow-plaintext-on-encrypt-failure",
        help="Allow write even if secret encryption fails (DANGEROUS: secrets stored as plaintext)",
    ),
) -> None:
    """Update an existing configuration row.

    \b
    Content options modify the row configuration JSON:
      --configuration : provide a full JSON blob (inline, @file, or -)
      --set PATH=VALUE : set a single nested key (repeatable)
      --merge : deep-merge into existing row config (preserves sibling keys)
      --dry-run : preview changes without applying

    \b
    Examples:
      # Update just the name
      kbagent config row-update --project P --component-id C --config-id ID --row-id R --name "New name"

      # Replace row configuration from a file
      kbagent config row-update --project P --component-id C --config-id ID --row-id R \\
        --configuration @row.json

      # Set a single nested value (merge implied)
      kbagent config row-update --project P --component-id C --config-id ID --row-id R \\
        --set 'parameters.table=new_table'

      # Preview changes without applying
      kbagent config row-update --project P --component-id C --config-id ID --row-id R \\
        --set 'parameters.table=new_table' --dry-run

      # Disable a row (excludes it from job runs)
      kbagent config row-update --project P --component-id C --config-id ID --row-id R --is-disabled

      # Set a meaningful version changeDescription for the audit trail
      kbagent config row-update --project P --component-id C --config-id ID --row-id R \\
        --set 'parameters.table=new_table' --change-description "AI-1234: repoint row"
    """
    if is_disabled and is_enabled:
        formatter = get_formatter(ctx)
        formatter.error(
            message="--is-disabled and --is-enabled are mutually exclusive.",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
        raise typer.Exit(code=2) from None

    is_disabled_value: bool | None = None
    if is_disabled:
        is_disabled_value = True
    elif is_enabled:
        is_disabled_value = False

    formatter = get_formatter(ctx)
    service = get_service(ctx, "config_service")

    config_dict: dict | None = None
    if configuration:
        try:
            config_dict = _parse_json_input(configuration)
        except (json.JSONDecodeError, FileNotFoundError) as exc:
            formatter.error(
                message=f"Invalid --configuration input: {exc}",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
            raise typer.Exit(code=2) from None

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

    effective_merge = merge or bool(parsed_sets)

    try:
        result = service.update_config_row(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            row_id=row_id,
            name=name,
            description=description,
            configuration=config_dict,
            set_paths=parsed_sets,
            merge=effective_merge,
            dry_run=dry_run,
            change_description=change_description,
            is_disabled=is_disabled_value,
            branch_id=branch,
            allow_plaintext_fallback=allow_plaintext,
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
            change_desc = result.get("change_description")
            if change_desc:
                formatter.console.print(f"[dim]changeDescription:[/dim] {change_desc}")
        return

    if formatter.json_mode:
        formatter.output(result)
    else:
        updated_name = result.get("name", row_id)
        branch_info = ""
        if result.get("branch_id"):
            branch_info = f" (branch {result['branch_id']})"
        formatter.success(
            f"Updated row '{escape(updated_name)}' [{row_id}] "
            f"in {escape(component_id)}/{escape(config_id)}{branch_info}"
        )
        _emit_plaintext_written_warning(formatter, result)


# ── config row-delete ──────────────────────────────────────────────────────────


@config_app.command("row-delete", rich_help_panel="Rows")
def config_row_delete(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    component_id: str = typer.Option(..., "--component-id", help="Component ID"),
    config_id: str = typer.Option(..., "--config-id", help="Configuration ID"),
    row_id: str = typer.Option(..., "--row-id", help="Row ID to delete"),
    branch: int | None = typer.Option(
        None, "--branch", help="Delete from a specific dev branch ID (defaults to active branch)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Delete a configuration row.

    \b
    Examples:
      kbagent config row-delete --project P --component-id C --config-id ID --row-id ROW
      kbagent config row-delete --project P --component-id C --config-id ID --row-id ROW --yes
    """
    formatter = get_formatter(ctx)

    if (
        not yes
        and not formatter.json_mode
        and not typer.confirm(f"Delete row [{row_id}] from {component_id}/{config_id}?")
    ):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)

    service = get_service(ctx, "config_service")

    try:
        result = service.delete_config_row(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            row_id=row_id,
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
            f"Deleted row [{row_id}] from {escape(component_id)}/{escape(config_id)}{branch_info}"
        )


# ── config oauth-url ───────────────────────────────────────────────────────────


@config_app.command(
    "oauth-url",
    rich_help_panel="OAuth",
    help=(
        "Requires master token. Generate an OAuth authorization URL for a component configuration."
    ),
)
def config_oauth_url(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    component_id: str = typer.Option(
        ...,
        "--component-id",
        help="Component ID (e.g. keboola.ex-google-drive)",
    ),
    config_id: str = typer.Option(
        ...,
        "--config-id",
        help="Configuration ID to authorize",
    ),
    redirect_url: str | None = typer.Option(
        None,
        "--redirect-url",
        help="Optional URL to return to after the OAuth flow completes (sets returnUrl query param)",
    ),
) -> None:
    """Generate an OAuth authorization URL for a component configuration.

    Opens a short-lived, component-scoped authorization link.
    The user must open this URL in a browser and grant access.

    \b
    Examples:
      kbagent config oauth-url --project P --component-id keboola.ex-google-drive --config-id ID

      # Redirect back to a custom URL after the OAuth flow completes
      kbagent config oauth-url --project P --component-id keboola.ex-google-drive --config-id ID \\
        --redirect-url https://example.com/oauth-done
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "config_service")

    try:
        result = service.get_oauth_url(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            redirect_url=redirect_url,
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
        formatter.console.print(
            f"[bold]OAuth URL for[/bold] [cyan]{escape(component_id)}[/cyan]/"
            f"[cyan]{escape(config_id)}[/cyan]:\n"
        )
        formatter.console.print(f"  [link]{result['url']}[/link]")
        formatter.console.print("\n[dim]Open this URL in a browser and grant access.[/dim]")
