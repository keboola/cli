"""Shared command-layer helpers to eliminate duplication across command files.

Provides common patterns used by all CLI commands:
- Context extraction (formatter, services)
- Exit code mapping for API errors
- Warning emission for multi-project operations
- Branch resolution for --branch flag
"""

from typing import Any

import typer

from ..config_store import ConfigStore
from ..constants import EXIT_PERMISSION_DENIED
from ..errors import KeboolaApiError, PermissionDeniedError
from ..output import OutputFormatter


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
    Always allows --help through so users can read docs for blocked commands.

    Args:
        ctx: Typer context (must have permission_engine in obj).
        group_name: The sub-app name (e.g., 'branch', 'config').
    """
    if _is_help_request(ctx):
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
