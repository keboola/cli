"""Shared command-layer helpers to eliminate duplication across command files.

Provides common patterns used by all CLI commands:
- Context extraction (formatter, services)
- Exit code mapping for API errors
- Warning emission for multi-project operations
- Branch resolution for --branch flag
"""

import os
import secrets
import sys
from typing import Any

import typer

from ..config_store import ConfigStore
from ..constants import (
    ENV_KBC_MANAGE_API_TOKEN,
    EXIT_JOB_TIMEOUT_TERMINATED,
    EXIT_PERMISSION_DENIED,
)
from ..errors import ErrorCode, KeboolaApiError, PermissionDeniedError
from ..output import OutputFormatter


def resolve_manage_token(*, allow_env: bool = False) -> str:
    """Resolve the manage token from a permitted source.

    Default-deny: KBC_MANAGE_API_TOKEN is ignored unless ``allow_env=True``
    (set by the top-level ``--allow-env-manage-token`` flag). The change
    closes the AI-exfiltration risk where any subprocess running as the
    same user can read the env var; the new default is "human at a TTY".

    Resolution order:
    1. ``KBC_MANAGE_API_TOKEN`` env var, IF ``allow_env`` is True. Otherwise
       a one-shot stderr warning is emitted and the env var is ignored.
    2. Interactive prompt with hidden input (if stdin is a TTY).
    3. Exit 2 with an actionable error naming the opt-in flag.

    Args:
        allow_env: When True, restores the legacy env-var-first behaviour
            for the current invocation. Plumbed from the top-level
            ``--allow-env-manage-token`` flag via ``ctx.obj``.

    Returns:
        The manage API token.

    Raises:
        typer.Exit: If no token can be resolved (exit code 2).
    """
    env_token = os.environ.get(ENV_KBC_MANAGE_API_TOKEN)
    if env_token:
        if allow_env:
            return env_token
        typer.echo(
            f"Warning: {ENV_KBC_MANAGE_API_TOKEN} found in environment "
            "but ignored. Pass --allow-env-manage-token to opt in.",
            err=True,
        )

    is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
    if is_tty:
        return typer.prompt("Manage API token", hide_input=True)

    typer.echo(
        "Error: No manage token available. Run interactively, or pass "
        f"--allow-env-manage-token to read {ENV_KBC_MANAGE_API_TOKEN} "
        "from env.",
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

    - INVALID_TOKEN -> 3 (authentication error)
    - TIMEOUT / CONNECTION_ERROR / RETRY_EXHAUSTED / QUEUE_JOB_TIMEOUT -> 4
      (network/retryable; QUEUE_JOB_TIMEOUT means local gave up AND the
      remote-kill attempt also failed, so the job may still be running)
    - JOB_TIMEOUT_TERMINATED -> EXIT_JOB_TIMEOUT_TERMINATED (7)
      (local --timeout elapsed and we successfully cancelled the remote
      job; scripts can distinguish "we killed it" from "it failed on its own")
    - SESSION_EXPIRED / SESSION_NOT_FOUND / AUTH_FLOW_DENIED -> 3
      (programmatic-auth session problems are authentication errors too --
      `kbagent auth login` is the remedy in every case)
    - AUTH_FLOW_TIMEOUT -> 4 (the login flow itself timed out; retryable)
    - Everything else -> 1 (general error)
    """
    if exc.error_code in ("INVALID_TOKEN", "MISSING_MASTER_TOKEN"):
        return 3
    if exc.error_code in (
        ErrorCode.SESSION_EXPIRED,
        ErrorCode.SESSION_NOT_FOUND,
        ErrorCode.AUTH_FLOW_DENIED,
    ):
        return 3
    if exc.error_code in (
        "TIMEOUT",
        "CONNECTION_ERROR",
        "RETRY_EXHAUSTED",
        "QUEUE_JOB_TIMEOUT",
    ):
        return 4
    if exc.error_code == ErrorCode.AUTH_FLOW_TIMEOUT:
        return 4
    if exc.error_code == "JOB_TIMEOUT_TERMINATED":
        return EXIT_JOB_TIMEOUT_TERMINATED
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
    Always allows --help through (no API calls made).

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
        formatter.error(message=exc.message, error_code=ErrorCode.PERMISSION_DENIED)
        raise typer.Exit(code=EXIT_PERMISSION_DENIED) from None


def resolve_project_alias(
    ctx: typer.Context,
    formatter: OutputFormatter,
    explicit: str | None,
) -> str:
    """Resolve the effective project alias for a single-project operation.

    Precedence (first match wins):
    1. ``explicit`` (typically the CLI ``--project`` flag)
    2. ``KBAGENT_PROJECT`` env var
    3. Persisted pin (``config.default_project`` set by ``kbagent project use``)
    4. Sole registered project when exactly one exists (convenience)
    5. Exit code 5 with a CONFIG_ERROR if none of the above resolves

    Use this from write/destructive command paths where implicit fan-out
    (``resolve_projects(None)`` returning every project) would be surprising
    or unsafe. Read paths should keep their existing fan-out behavior.

    Args:
        ctx: Typer context (must contain ``project_service``).
        formatter: Output formatter for structured error emission.
        explicit: The value of the CLI --project flag, or None.

    Returns:
        The resolved project alias (guaranteed to be registered).
    """
    from ..errors import ConfigError as _ConfigError

    service = get_service(ctx, "project_service")
    try:
        alias, _source = service.resolve_pinned_alias(explicit=explicit)
    except _ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    return alias


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
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None


def resolve_branch(
    config_store: ConfigStore,
    formatter: OutputFormatter,
    project: str | None,
    branch: int | None,
    *,
    ignore_active_branch: bool = False,
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
        ignore_active_branch: When True, the implicit active_branch_id from
            config is ignored and the production endpoint (branch_id=None) is
            used unless --branch was passed explicitly. An info message is
            printed so the user can see the active dev branch was skipped.
            Intended for storage READ commands -- the Storage API
            branch-scoped endpoint returns only locally-modified resources,
            which for a freshly created dev branch is an empty set. Explicit
            --branch still overrides.

    Returns:
        Tuple of (effective_project, effective_branch_id).
    """
    if branch is not None:
        return project, branch

    if project is not None:
        proj_config = config_store.get_project(project)
        if proj_config and proj_config.active_branch_id is not None:
            if ignore_active_branch:
                if not formatter.json_mode:
                    formatter.err_console.print(
                        f"[bold blue]Info:[/bold blue] Using production branch for read "
                        f"(active dev branch '{proj_config.active_branch_id}' ignored; "
                        f"pass --branch {proj_config.active_branch_id} to override)"
                    )
                return project, None
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
            if ignore_active_branch:
                if not formatter.json_mode:
                    formatter.err_console.print(
                        f"[bold blue]Info:[/bold blue] Using production branch for read "
                        f"(active dev branch '{proj.active_branch_id}' on project '{alias}' "
                        f"ignored; pass --branch {proj.active_branch_id} to override)"
                    )
                return alias, None
            if not formatter.json_mode:
                formatter.err_console.print(
                    f"[bold blue]Info:[/bold blue] Using active branch "
                    f"(ID: {proj.active_branch_id}) for project '{alias}'"
                )
            return alias, proj.active_branch_id

    return project, None


_CONFIRM_CODE_LENGTH = 4


def require_random_code_confirmation(action_description: str) -> None:
    """Require the user to type a random hex code to confirm a high-risk action.

    Prevents AI agents from programmatically approving production-affecting
    writes (Developer Portal updates, permission policy changes). The agent
    cannot predict the code and cannot type it into stdin.

    Behaviour:
    - No TTY -> raise typer.Exit(EXIT_PERMISSION_DENIED).
    - TTY + correct code -> return None (caller proceeds).
    - TTY + wrong code / EOF / interrupt -> raise typer.Exit(EXIT_PERMISSION_DENIED).

    Args:
        action_description: Short verb phrase shown in the prompt
            (e.g. "patch keboola.ex-foo", "update permission policy").
    """
    is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
    if not is_tty:
        sys.stderr.write(
            f"\nRefusing to {action_description}: this action requires a "
            "real terminal so a human can type the confirmation code. "
            "There is no --yes bypass by design.\n"
        )
        raise typer.Exit(code=EXIT_PERMISSION_DENIED)

    code = secrets.token_hex(_CONFIRM_CODE_LENGTH)
    sys.stderr.write(f"\nTo {action_description}, type this code: {code}\n")
    sys.stderr.write("Confirmation: ")
    sys.stderr.flush()

    try:
        user_input = input().strip()
    except (EOFError, KeyboardInterrupt):
        raise typer.Exit(code=EXIT_PERMISSION_DENIED) from None

    if user_input != code:
        sys.stderr.write("Confirmation failed. Aborting.\n")
        raise typer.Exit(code=EXIT_PERMISSION_DENIED)


def resolve_identity_alias(ctx: typer.Context, explicit: str | None) -> str:
    """Resolve the dev-portal identity alias for this invocation.

    Order: explicit --identity flag > default from config > error.
    """
    if explicit:
        return explicit
    config_store: ConfigStore = get_service(ctx, "config_store")
    default = config_store.load().default_dev_portal_identity
    if not default:
        raise typer.BadParameter(
            "No Developer Portal identity selected. Pass --identity <alias>, "
            "or set a default via `kbagent dev-portal identity use <alias>`."
        )
    return default


def get_dev_portal_service(ctx: typer.Context):
    """Build a DeveloperPortalService bound to the current ConfigStore."""
    from ..dev_portal_client import DeveloperPortalClient
    from ..services.dev_portal_service import DeveloperPortalService

    config_store: ConfigStore = get_service(ctx, "config_store")
    return DeveloperPortalService(
        config_store=config_store,
        client_factory=lambda identity: DeveloperPortalClient(identity),
    )
