"""Shared command-layer helpers to eliminate duplication across command files.

Provides common patterns used by all CLI commands:
- Context extraction (formatter, services)
- Exit code mapping for API errors
- Warning emission for multi-project operations
- Branch resolution for --branch flag
- Hint mode detection and code generation
"""

import os
import sys
from typing import Any

import typer

from ..config_store import ConfigStore
from ..constants import ENV_KBC_MANAGE_API_TOKEN, EXIT_PERMISSION_DENIED
from ..errors import KeboolaApiError, PermissionDeniedError
from ..output import OutputFormatter


def resolve_manage_token() -> str:
    """Resolve the manage token from env var or interactive prompt.

    Token resolution order:
    1. KBC_MANAGE_API_TOKEN env var (for CI/CD)
    2. Interactive prompt with hidden input (if TTY)
    3. Error if neither available

    Returns:
        The manage API token.

    Raises:
        typer.Exit: If no token can be resolved.
    """
    env_token = os.environ.get(ENV_KBC_MANAGE_API_TOKEN)
    if env_token:
        return env_token

    is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
    if is_tty:
        return typer.prompt("Manage API token", hide_input=True)

    typer.echo(
        f"Error: No manage token available. Set {ENV_KBC_MANAGE_API_TOKEN} env var "
        "or run interactively.",
        err=True,
    )
    raise typer.Exit(code=2)


def get_formatter(ctx: typer.Context) -> OutputFormatter:
    """Retrieve the OutputFormatter from the Typer context."""
    return ctx.obj["formatter"]


def get_service(ctx: typer.Context, key: str) -> Any:
    """Retrieve a service from the Typer context."""
    return ctx.obj[key]


def map_error_to_exit_code(exc: KeboolaApiError) -> int:
    """Map a KeboolaApiError to a CLI exit code.

    Unified 3-case logic:
    - INVALID_TOKEN -> 3 (authentication error)
    - TIMEOUT / CONNECTION_ERROR / RETRY_EXHAUSTED -> 4 (network error)
    - Everything else -> 1 (general error)
    """
    if exc.error_code == "INVALID_TOKEN":
        return 3
    if exc.error_code in ("TIMEOUT", "CONNECTION_ERROR", "RETRY_EXHAUSTED"):
        return 4
    return 1


def emit_project_warnings(formatter: OutputFormatter, result: dict) -> None:
    """Emit warnings from multi-project operation results.

    Iterates the 'errors' list in the result dict (if present) and prints
    each entry as a warning via the formatter.
    """
    for err in result.get("errors", []):
        alias = err.get("project_alias", "unknown")
        message = err.get("message", "Unknown error")
        formatter.warning(f"Project '{alias}': {message}")


def _is_help_request(ctx: typer.Context) -> bool:
    """Check if the current invocation is a --help request.

    The group callback fires before Click parses subcommand arguments,
    so --help for a subcommand (e.g. 'branch delete --help') is still
    in sys.argv at this point. We allow help through even for blocked commands.

    Also respects Click's resilient_parsing mode (tab completions).
    """
    import sys

    if "--help" in sys.argv or "-h" in sys.argv:
        return True
    return bool(ctx.resilient_parsing)


def check_cli_permission(ctx: typer.Context, group_name: str) -> None:
    """Check CLI command permissions using the active policy.

    Called from sub-app callbacks. Constructs operation name as
    '{group_name}.{subcommand}' and checks against the permission engine.
    Always allows --help and --hint through (no API calls made).

    Args:
        ctx: Typer context (must have permission_engine in obj).
        group_name: The sub-app name (e.g., 'branch', 'config').
    """
    if _is_help_request(ctx):
        return

    # Hint mode: skip permission check + catch commands without hint definitions
    if should_hint(ctx):
        subcommand = ctx.invoked_subcommand
        if subcommand:
            cli_command = f"{group_name}.{subcommand}"
            from ..hints import HintRegistry
            from ..hints import definitions as _defs  # noqa: F401

            if HintRegistry.get(cli_command) is not None:
                return  # Hint exists — let the command function handle it
            # No hint registered for this command
            typer.echo(
                f"No --hint available for '{group_name} {subcommand}'.",
                err=True,
            )
            raise typer.Exit(0)
        return

    engine = ctx.obj.get("permission_engine")
    if engine is None or not engine.active:
        return

    subcommand = ctx.invoked_subcommand
    if subcommand is None:
        return

    operation = f"{group_name}.{subcommand}"

    try:
        engine.check_or_raise(operation)
    except PermissionDeniedError as exc:
        formatter = get_formatter(ctx)
        formatter.error(message=exc.message, error_code="PERMISSION_DENIED")
        raise typer.Exit(code=EXIT_PERMISSION_DENIED) from None


def validate_branch_requires_project(
    formatter: OutputFormatter,
    branch: int | None,
    project: str | None,
) -> None:
    """Validate that --branch is always accompanied by --project.

    Raises:
        typer.Exit: With code 2 if branch is set but project is not.
    """
    if branch is not None and not project:
        formatter.error(
            message="--branch requires --project (branch ID is per-project)",
            error_code="INVALID_ARGUMENT",
        )
        raise typer.Exit(code=2) from None


def resolve_branch(
    config_store: ConfigStore,
    formatter: OutputFormatter,
    project: str | None,
    branch: int | None,
) -> tuple[str | None, int | None]:
    """Resolve the effective branch and project.

    Resolution order:
    1. Explicit --branch always wins (no change)
    2. If no --branch, check active_branch_id from config for the resolved project
    3. If active branch found, use it and print info message in human mode

    When an active branch is resolved from config, --project is also set
    to the project alias (branch is per-project).

    Args:
        config_store: Config store for looking up project configs.
        formatter: Output formatter for info messages.
        project: Explicit --project alias or None.
        branch: Explicit --branch integer or None.

    Returns:
        Tuple of (effective_project, effective_branch_id).
    """
    if branch is not None:
        return project, branch

    if project is not None:
        proj_config = config_store.get_project(project)
        if proj_config and proj_config.active_branch_id is not None:
            if not formatter.json_mode:
                formatter.err_console.print(
                    f"[bold blue]Info:[/bold blue] Using active branch "
                    f"(ID: {proj_config.active_branch_id}) for project '{project}'"
                )
            return project, proj_config.active_branch_id
    else:
        config = config_store.load()
        active_projects = [
            (alias, proj)
            for alias, proj in config.projects.items()
            if proj.active_branch_id is not None
        ]
        if len(active_projects) == 1:
            alias, proj = active_projects[0]
            if not formatter.json_mode:
                formatter.err_console.print(
                    f"[bold blue]Info:[/bold blue] Using active branch "
                    f"(ID: {proj.active_branch_id}) for project '{alias}'"
                )
            return alias, proj.active_branch_id

    return project, None


# ── Hint mode helpers ──────────────────────────────────────────────


def should_hint(ctx: typer.Context) -> bool:
    """Check if --hint mode is active."""
    return ctx.obj.get("hint_mode") is not None


def emit_hint(ctx: typer.Context, cli_command: str, **params: Any) -> None:
    """Render Python hint code, print to stdout, and exit.

    Resolves the project alias to a stack_url and config_dir, then
    delegates to the hint renderer.

    Args:
        ctx: Typer context (must have hint_mode and config_store).
        cli_command: Dot-separated command key, e.g. 'config.list'.
        **params: CLI parameters passed to the command.
    """
    from ..hints import render_hint

    hint_mode = ctx.obj["hint_mode"]
    config_store: ConfigStore = ctx.obj["config_store"]

    # Resolve project alias -> stack_url
    stack_url = _resolve_hint_stack_url(config_store, params.get("project"))

    # Actual config_dir path (for service layer hints)
    config_dir = config_store.config_path.parent

    # Branch ID from params
    branch_id = params.get("branch")

    output = render_hint(cli_command, hint_mode, params, stack_url, config_dir, branch_id)
    sys.stdout.write(output + "\n")
    raise typer.Exit(0)


def _resolve_hint_stack_url(
    config_store: ConfigStore,
    project: str | list[str] | None,
) -> str | None:
    """Resolve a project alias to its stack_url for hint rendering.

    Returns None if the project cannot be resolved (hint will use a placeholder).
    """
    if project is None:
        return None

    alias = project[0] if isinstance(project, list) else project

    try:
        proj = config_store.get_project(alias)
        if proj:
            return proj.stack_url
    except Exception:
        pass

    return None
