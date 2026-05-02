"""Permission management commands - list, show, set, reset, check.

Thin CLI layer for managing the firewall-style permission policy.
No business logic belongs here -- the PermissionEngine handles evaluation.

Security: set and reset require interactive confirmation (type a random code)
so that an AI agent constrained by the policy cannot bypass it programmatically.
"""

import secrets
import sys
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from ..config_store import ConfigStore
from ..constants import EXIT_PERMISSION_DENIED
from ..errors import ErrorCode
from ..models import PermissionPolicy
from ..permissions import PermissionEngine
from ._helpers import get_formatter, get_service

permissions_app = typer.Typer(help="Manage operation permissions (firewall rules)")

# Length of the random confirmation code
_CONFIRM_CODE_LENGTH = 4


def _require_interactive_confirmation(action_description: str) -> bool:
    """Require the user to type a random code to confirm a destructive permission change.

    This prevents AI agents from programmatically bypassing permission policies.
    The agent cannot predict the code, and cannot type it into stdin.

    Returns True if confirmed, False if cancelled or non-interactive.
    """
    is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
    if not is_tty:
        return False

    code = secrets.token_hex(_CONFIRM_CODE_LENGTH)
    sys.stderr.write(f"\nTo {action_description}, type this code: {code}\n")
    sys.stderr.write("Confirmation: ")
    sys.stderr.flush()

    try:
        user_input = input().strip()
    except (EOFError, KeyboardInterrupt):
        return False

    return user_input == code


def _format_operations_table(
    console: Console,
    operations: list[dict[str, Any]],
    category_filter: str | None = None,
) -> None:
    """Render a Rich table of operations with their status."""
    if category_filter:
        operations = [op for op in operations if op["category"] == category_filter]

    table = Table(title="Operations")
    table.add_column("Operation", style="bold cyan")
    table.add_column("Type", style="dim")
    table.add_column("Category")
    table.add_column("Status", justify="center")
    table.add_column("Description", style="dim")

    category_styles = {
        "read": "green",
        "write": "yellow",
        "destructive": "red",
        "admin": "bold red",
    }

    for op in operations:
        cat = op["category"]
        cat_styled = f"[{category_styles.get(cat, '')}]{cat}[/{category_styles.get(cat, '')}]"
        status = op["status"]
        status_styled = (
            f"[green]{status}[/green]" if status == "allowed" else f"[red]{status}[/red]"
        )
        desc = op.get("description", "")
        table.add_row(op["name"], op["type"], cat_styled, status_styled, desc)

    console.print(table)


@permissions_app.command("list")
def permissions_list(
    ctx: typer.Context,
    category: str | None = typer.Option(
        None,
        "--category",
        "-c",
        help="Filter by risk category: read, write, destructive, admin",
    ),
) -> None:
    """List all operations with their risk category and current allowed/denied status.

    The allowed/denied column reflects the EFFECTIVE policy for this
    invocation -- i.e. the persisted policy merged with any top-level
    session flags like ``--deny-writes`` / ``--deny-destructive``. This
    matches what a command will actually do right now.
    """
    from ..cli import apply_firewall_flags

    formatter = get_formatter(ctx)
    config_store: ConfigStore = get_service(ctx, "config_store")
    config = config_store.load()

    deny_writes = bool(ctx.obj.get("deny_writes")) if ctx.obj else False
    deny_destructive = bool(ctx.obj.get("deny_destructive")) if ctx.obj else False
    effective_policy = apply_firewall_flags(
        config.permissions,
        deny_writes=deny_writes,
        deny_destructive=deny_destructive,
    )

    engine = PermissionEngine(effective_policy)
    ops = engine.list_operations()

    if formatter.json_mode:
        if category:
            ops = [op for op in ops if op["category"] == category]
        formatter.output(ops)
    else:
        _format_operations_table(formatter.console, ops, category_filter=category)
        if not engine.active:
            formatter.err_console.print(
                "\n[dim]No permission policy active. All operations are allowed.[/dim]"
            )
        elif deny_writes or deny_destructive:
            active_flags = []
            if deny_writes:
                active_flags.append("--deny-writes")
            if deny_destructive:
                active_flags.append("--deny-destructive")
            formatter.err_console.print(
                f"\n[dim]Session firewall active: {' '.join(active_flags)} (not persisted).[/dim]"
            )


@permissions_app.command("show")
def permissions_show(
    ctx: typer.Context,
) -> None:
    """Show the current active permission policy.

    Reports both the PERSISTED policy (from config.json) and any SESSION
    firewall layered on top via top-level ``--deny-writes`` /
    ``--deny-destructive`` flags. Session flags are shown but are never
    written to config.json -- they apply to this invocation only.
    """
    formatter = get_formatter(ctx)
    config_store: ConfigStore = get_service(ctx, "config_store")
    config = config_store.load()

    deny_writes = bool(ctx.obj.get("deny_writes")) if ctx.obj else False
    deny_destructive = bool(ctx.obj.get("deny_destructive")) if ctx.obj else False
    # Session-only AI-exfil flag. The persisted equivalent lives on
    # AppConfig.allow_env_manage_token and is reported separately below.
    # The session flag is meaningful only when the persisted policy still
    # allows env-var reads — otherwise the persisted denial is what's
    # active and the session flag adds nothing.
    no_env_manage_token = (
        ctx.obj.get("allow_manage_env") is False if ctx.obj else False
    ) and config.allow_env_manage_token
    session_flags: list[str] = []
    if deny_writes:
        session_flags.append("--deny-writes")
    if deny_destructive:
        session_flags.append("--deny-destructive")
    if no_env_manage_token:
        session_flags.append("--no-env-manage-token")

    persisted = config.permissions
    persisted_manage_env_denied = not config.allow_env_manage_token

    if persisted is None and not session_flags and not persisted_manage_env_denied:
        if formatter.json_mode:
            formatter.output(
                {
                    "active": False,
                    "message": "No permission policy configured",
                    "session_flags": [],
                    "allow_env_manage_token": config.allow_env_manage_token,
                }
            )
        else:
            formatter.console.print("No permission policy configured. All operations are allowed.")
        return

    policy_data: dict[str, Any] = {
        "active": (persisted is not None or bool(session_flags) or persisted_manage_env_denied),
        "persisted": (
            None
            if persisted is None
            else {
                "mode": persisted.mode,
                "allow": persisted.allow,
                "deny": persisted.deny,
            }
        ),
        "session_flags": session_flags,
        "allow_env_manage_token": config.allow_env_manage_token,
    }

    # Keep legacy top-level keys when a persisted policy exists so existing
    # JSON consumers that read policy_data["mode"] / ["allow"] / ["deny"]
    # remain compatible. Clients that need the new session-layer view read
    # ``session_flags`` and ``persisted``.
    if persisted is not None:
        policy_data["mode"] = persisted.mode
        policy_data["allow"] = persisted.allow
        policy_data["deny"] = persisted.deny

    if formatter.json_mode:
        formatter.output(policy_data)
        return

    if persisted is not None:
        mode_desc = (
            "default-allow (everything allowed unless denied)"
            if persisted.mode == "allow"
            else "default-deny (everything denied unless allowed)"
        )
        formatter.console.print(f"[bold]Mode:[/bold] {mode_desc}")
        if persisted.allow:
            formatter.console.print(f"[bold]Allow:[/bold] {', '.join(persisted.allow)}")
        if persisted.deny:
            formatter.console.print(f"[bold]Deny:[/bold] {', '.join(persisted.deny)}")
    else:
        formatter.console.print("[dim]No persisted permission policy (config.json is clean).[/dim]")

    if session_flags:
        formatter.console.print(
            f"[bold yellow]Session firewall:[/bold yellow] {' '.join(session_flags)} "
            "[dim](active for this invocation only; not persisted)[/dim]"
        )

    if persisted_manage_env_denied:
        formatter.console.print(
            "[bold red]Env-var manage tokens DENIED[/bold red] "
            "[dim](resolve_manage_token refuses KBC_MANAGE_API_TOKEN and "
            "KBC_MANAGE_TOKEN_<SUFFIX>; TTY prompt only)[/dim]"
        )


@permissions_app.command("set")
def permissions_set(
    ctx: typer.Context,
    mode: str = typer.Option(
        ...,
        "--mode",
        "-m",
        help="Base mode: 'allow' (default-allow) or 'deny' (default-deny)",
    ),
    allow: list[str] | None = typer.Option(
        None,
        "--allow",
        "-a",
        help="Allowed operation patterns (repeatable)",
    ),
    deny: list[str] | None = typer.Option(
        None,
        "--deny",
        "-d",
        help="Denied operation patterns (repeatable)",
    ),
) -> None:
    """Set the permission policy (firewall rules).

    Requires interactive confirmation (type a random code) to prevent
    AI agents from modifying permissions programmatically.

    Examples:
      # Block all write operations (Vojta's use case):
      kbagent permissions set --mode allow --deny "cli:write" --deny "tool:write"

      # Allow only read operations:
      kbagent permissions set --mode deny --allow "cli:read" --allow "tool:read"

      # Block specific operations:
      kbagent permissions set --mode allow --deny "branch.delete" --deny "tool:delete_*"
    """
    formatter = get_formatter(ctx)

    if mode not in ("allow", "deny"):
        formatter.error(
            message="Mode must be 'allow' or 'deny'",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
        raise typer.Exit(code=2) from None

    if not _require_interactive_confirmation("update permission policy"):
        formatter.error(
            message="Confirmation failed. Permission policy not changed.",
            error_code=ErrorCode.PERMISSION_DENIED,
        )
        raise typer.Exit(code=EXIT_PERMISSION_DENIED) from None

    config_store: ConfigStore = get_service(ctx, "config_store")
    config = config_store.load()

    policy = PermissionPolicy(
        mode=mode,
        allow=allow or [],
        deny=deny or [],
    )
    config.permissions = policy
    config_store.save(config)

    if formatter.json_mode:
        formatter.output(
            {
                "status": "ok",
                "mode": mode,
                "allow": policy.allow,
                "deny": policy.deny,
            }
        )
    else:
        formatter.console.print("[green]Permission policy updated.[/green]")
        mode_desc = (
            "default-allow (everything allowed unless denied)"
            if mode == "allow"
            else "default-deny (everything denied unless allowed)"
        )
        formatter.console.print(f"  Mode: {mode_desc}")
        if policy.allow:
            formatter.console.print(f"  Allow: {', '.join(policy.allow)}")
        if policy.deny:
            formatter.console.print(f"  Deny: {', '.join(policy.deny)}")


@permissions_app.command("reset")
def permissions_reset(
    ctx: typer.Context,
) -> None:
    """Remove all permission restrictions.

    Requires interactive confirmation (type a random code) to prevent
    AI agents from removing the policy programmatically.
    """
    formatter = get_formatter(ctx)

    if not _require_interactive_confirmation("remove permission policy"):
        formatter.error(
            message="Confirmation failed. Permission policy not changed.",
            error_code=ErrorCode.PERMISSION_DENIED,
        )
        raise typer.Exit(code=EXIT_PERMISSION_DENIED) from None

    config_store: ConfigStore = get_service(ctx, "config_store")
    config = config_store.load()

    config.permissions = None
    # `reset` clears the firewall but preserves the manage-env policy --
    # users who set --deny-manage-env should not have it silently undone
    # by `reset`. Use `permissions allow-manage-env` to explicitly re-enable.
    config_store.save(config)

    if formatter.json_mode:
        formatter.output({"status": "ok", "message": "Permission policy removed"})
    else:
        formatter.console.print(
            "[green]Permission policy removed. All operations are allowed.[/green]"
        )


@permissions_app.command("deny-manage-env")
def permissions_deny_manage_env(
    ctx: typer.Context,
) -> None:
    """Refuse to read manage tokens from environment variables.

    Persists ``allow_env_manage_token=False`` in config.json. Once set,
    every kbagent invocation refuses ``KBC_MANAGE_API_TOKEN`` and
    ``KBC_MANAGE_TOKEN_<SUFFIX>`` -- a TTY prompt is the only path. Use
    inside ``kbagent init --read-only`` workspaces (where the AI agent
    cannot ``chmod`` the config back) to close the env-var exfiltration
    window: env vars sit outside kbagent's permission firewall, so any
    subprocess can ``curl`` the Manage API directly while ``--deny-
    writes`` etc. silently let it through.

    Requires interactive confirmation (random code) to prevent AI agents
    from re-enabling env-var reads programmatically.
    """
    formatter = get_formatter(ctx)

    if not _require_interactive_confirmation("deny env-var manage tokens"):
        formatter.error(
            message="Confirmation failed. Manage-env policy not changed.",
            error_code=ErrorCode.PERMISSION_DENIED,
        )
        raise typer.Exit(code=EXIT_PERMISSION_DENIED) from None

    config_store: ConfigStore = get_service(ctx, "config_store")
    config = config_store.load()
    config.allow_env_manage_token = False
    config_store.save(config)

    if formatter.json_mode:
        formatter.output(
            {
                "status": "ok",
                "allow_env_manage_token": False,
                "message": "Env-var manage tokens are now denied. TTY prompt only.",
            }
        )
    else:
        formatter.console.print(
            "[bold red]Env-var manage tokens DENIED.[/bold red] "
            "Future kbagent invocations refuse KBC_MANAGE_API_TOKEN and "
            "KBC_MANAGE_TOKEN_<SUFFIX>; TTY prompt only."
        )


@permissions_app.command("allow-manage-env")
def permissions_allow_manage_env(
    ctx: typer.Context,
) -> None:
    """Re-allow reading manage tokens from environment variables (default).

    Reverts ``allow_env_manage_token`` to True. Use to undo
    ``kbagent permissions deny-manage-env`` when leaving an AI-sandbox
    workspace or moving the install to a CI runner.

    Requires interactive confirmation (random code).
    """
    formatter = get_formatter(ctx)

    if not _require_interactive_confirmation("allow env-var manage tokens"):
        formatter.error(
            message="Confirmation failed. Manage-env policy not changed.",
            error_code=ErrorCode.PERMISSION_DENIED,
        )
        raise typer.Exit(code=EXIT_PERMISSION_DENIED) from None

    config_store: ConfigStore = get_service(ctx, "config_store")
    config = config_store.load()
    config.allow_env_manage_token = True
    config_store.save(config)

    if formatter.json_mode:
        formatter.output(
            {
                "status": "ok",
                "allow_env_manage_token": True,
                "message": "Env-var manage tokens are now allowed.",
            }
        )
    else:
        formatter.console.print(
            "[green]Env-var manage tokens ALLOWED.[/green] "
            "KBC_MANAGE_API_TOKEN and KBC_MANAGE_TOKEN_<SUFFIX> may be used "
            "(unless overridden by the session flag --no-env-manage-token)."
        )


@permissions_app.command("check")
def permissions_check(
    ctx: typer.Context,
    operation: str = typer.Argument(
        help="Operation to check, e.g. 'branch.delete', 'tool:create_config'",
    ),
) -> None:
    """Check if a specific operation is allowed.

    Exit code 0 = allowed, 6 = denied.
    """
    formatter = get_formatter(ctx)
    config_store: ConfigStore = get_service(ctx, "config_store")
    config = config_store.load()

    engine = PermissionEngine(config.permissions)
    allowed = engine.is_allowed(operation)

    if formatter.json_mode:
        formatter.output(
            {
                "operation": operation,
                "allowed": allowed,
            }
        )
    else:
        if allowed:
            formatter.console.print(f"[green]ALLOWED[/green] {operation}")
        else:
            formatter.console.print(f"[red]DENIED[/red] {operation}")

    if not allowed:
        raise typer.Exit(code=EXIT_PERMISSION_DENIED) from None
