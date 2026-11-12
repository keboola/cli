"""Typer root application with global options. The commands are in ``root_command.COMMANDS``."""

import importlib
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

import typer

from . import telemetry
from .config_store import ConfigStore, resolve_config_dir
from .constants import ENV_CONFIG_DIR, ENV_CONVERSATION_ID, EXIT_PERMISSION_DENIED
from .effective_branch import record_targets
from .errors import ErrorCode, PermissionDeniedError
from .output import OutputFormatter, force_utf8_when_redirected

# `apply_firewall_flags` lives in permissions.py so `server/app.py` composes the
# very same policy for the REST surface; re-exported here because callers (and
# tests) have imported it from `cli` since 0.22.0.
from .permissions import PermissionEngine, apply_firewall_flags
from .root_command import MissingCommandError, RootGroup

# At import, not inside the root callback: Click renders `--help` while parsing,
# before any callback runs, and `--help` is one of the surfaces that crashed.
force_utf8_when_redirected()

app = typer.Typer(
    name="kbagent",
    help="Keboola Agent CLI -- AI-friendly interface to Keboola projects",
    invoke_without_command=True,
    # Translates a project ID given as --project to its alias (CLI-22); one JSON
    # document for every exit code, the root flags after the subcommand, and the
    # repeated-option check (#791, root_command.py).
    cls=RootGroup,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def __getattr__(name: str) -> Any:
    """A service class, imported on first use; tests patch it here (``cli.JobService``)."""
    if not name.endswith("Service"):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return getattr(importlib.import_module(f".services.{module}", __package__), name)


class _Services(dict[str, Any]):
    """``ctx.obj``: ``ctx.obj["job_service"]`` builds ``JobService`` on its first use."""

    def __missing__(self, key: str) -> Any:
        if not key.endswith("_service"):
            raise KeyError(key)
        service_class = getattr(sys.modules[__name__], key.title().replace("_", ""))
        if key in ("version_service", "http_forwarder_service"):
            self[key] = service_class()
        else:
            self[key] = service_class(config_store=self["config_store"])
        return self[key]


def _version_callback(value: bool) -> None:
    """Print version and exit -- standard `--version` flag for CLI tools."""
    if value:
        from . import __version__

        typer.echo(f"kbagent v{__version__}")
        raise typer.Exit()


def _from_environment(ctx: typer.Context, name: str) -> bool:
    """True when Click took the value of option ``name`` from its ``envvar``."""
    source = ctx.get_parameter_source(name)
    return source is not None and source.name == "ENVIRONMENT"


@app.callback()
def main(
    ctx: typer.Context,
    _version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        "-j",
        help="Output in JSON format (for machine consumption)",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose output",
    ),
    no_color: bool = typer.Option(
        False,
        "--no-color",
        help="Disable colored output",
    ),
    config_dir: str | None = typer.Option(
        None,
        "--config-dir",
        envvar=ENV_CONFIG_DIR,
        help="Override config directory path.",
    ),
    deny_writes: bool = typer.Option(
        False,
        "--deny-writes",
        help="Session-only firewall: block write, destructive, AND admin "
        "operations (the wide net -- project add/remove/edit, org setup, "
        "storage writes and deletes, etc.). Merges with any persisted policy.",
    ),
    deny_destructive: bool = typer.Option(
        False,
        "--deny-destructive",
        help="Session-only firewall: block ONLY data-destructive operations "
        "(storage delete-table/delete-bucket/delete-column, job terminate, "
        "branch delete, etc.). Admin ops like 'project remove' and 'org setup' "
        "are NOT blocked -- use --deny-writes for the wide net.",
    ),
    conversation_id: str | None = typer.Option(
        None,
        "--conversation-id",
        envvar=ENV_CONVERSATION_ID,
        help="Conversation/session ID sent as the X-Conversation-ID header on "
        "every API request (platform observability). Equivalent to setting "
        "KBAGENT_CONVERSATION_ID, and takes precedence over it. Exists because "
        "agent harnesses do not persist shell state between tool calls, so a "
        "standalone `export` cannot set it -- and prefixing every command with "
        "`export ...` stops the command matching a `Bash(kbagent ...)` "
        "permission allow-rule.",
    ),
    allow_env_manage_token: bool = typer.Option(
        False,
        "--allow-env-manage-token",
        help="Read KBC_MANAGE_API_TOKEN from the environment. Without this "
        "flag the env var is ignored (with a warning) and an interactive "
        "TTY prompt is required. Default-deny since 0.29.0; closes the "
        "AI-exfiltration risk where subprocesses inherit the manage token.",
    ),
) -> None:
    """Global options applied to all commands."""
    import os

    from .auto_update import maybe_auto_update, show_post_update_changelog

    # `envvar=` on these two options names the variable in --help (EV1). A value
    # that Click took from the environment counts as no flag, so the code below
    # reads the variable itself as before: same precedence, and the config
    # directory keeps its "env-var" source label.
    if _from_environment(ctx, "config_dir"):
        config_dir = None
    if _from_environment(ctx, "conversation_id"):
        conversation_id = None

    # Published into the environment rather than threaded through the Typer
    # context because that is where every consumer already reads it:
    # `BaseHttpClient.__init__` stamps the header from os.environ for all
    # seven clients, `doctor` reports on it, and scheduled-agent subprocesses
    # inherit it for free. `serve` sets it the same way. The flag wins over an
    # inherited env var -- it is the more specific instruction (issue #716).
    if conversation_id is not None:
        os.environ[ENV_CONVERSATION_ID] = conversation_id

    maybe_auto_update(json_mode=json_output)

    # If the user explicitly asked for `kbagent changelog`, they'll see the
    # full changelog below -- prepending the "What's new" summary is pure
    # duplication. Consume the trigger env var so it does not fire later
    # on a different command.
    if ctx.invoked_subcommand == "changelog":
        import os as _os

        from .changelog import ENV_UPDATED_FROM as _ENV_UPDATED_FROM

        _os.environ.pop(_ENV_UPDATED_FROM, None)
    else:
        show_post_update_changelog()

    # If no subcommand given, launch REPL on TTY or show help otherwise
    if ctx.invoked_subcommand is None:
        if json_output:
            # O1: exit 2, help on stderr, a USAGE_ERROR envelope on stdout.
            raise MissingCommandError("Missing command.", ctx)
        is_interactive = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
        if is_interactive:
            # Defer REPL launch until after context setup (below)
            ctx.ensure_object(_Services)
            ctx.obj["_launch_repl"] = True
        else:
            # Non-interactive: show help
            click_cmd = typer.main.get_command(app)
            with click_cmd.make_context("kbagent", []) as help_ctx:
                sys.stdout.write(click_cmd.get_help(help_ctx) + "\n")
            raise typer.Exit()

    log_level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    effective_no_color = no_color or not is_tty

    formatter = OutputFormatter(
        json_mode=json_output,
        no_color=effective_no_color,
        verbose=verbose,
    )
    # Record the project and branch of this command for the output (#766). Not
    # for the REPL shell (each line records on its own) nor for `serve`, whose
    # request threads would all add to one record for the server's lifetime.
    if ctx.invoked_subcommand not in (None, "repl", "serve"):
        ctx.with_resource(record_targets(formatter.report_target))

    resolved_dir, source = resolve_config_dir(cli_config_dir=config_dir)
    config_store = ConfigStore(config_dir=resolved_dir, source=source)

    try:
        config = config_store.load()
        persisted_policy = config.permissions
    except Exception:
        # Config may be invalid (e.g. corrupted JSON) -- skip persisted policy
        persisted_policy = None

    session_policy = apply_firewall_flags(
        persisted_policy,
        deny_writes=deny_writes,
        deny_destructive=deny_destructive,
    )
    permission_engine = PermissionEngine(session_policy)

    ctx.ensure_object(_Services)
    ctx.obj["formatter"] = formatter
    ctx.obj["json_output"] = json_output
    ctx.obj["permission_engine"] = permission_engine
    ctx.obj["verbose"] = verbose
    ctx.obj["no_color"] = effective_no_color
    ctx.obj["deny_writes"] = deny_writes
    ctx.obj["deny_destructive"] = deny_destructive
    ctx.obj["allow_env_manage_token"] = allow_env_manage_token
    ctx.obj["config_store"] = config_store

    # Warn if empty local config shadows global with projects (#104)
    if source == "local" and not json_output and ctx.invoked_subcommand != "init":
        try:
            local_config = config_store.load()
            if not local_config.projects:
                import platformdirs as _platformdirs

                _global_dir = Path(_platformdirs.user_config_dir("keboola-agent-cli"))
                _global_path = _global_dir / "config.json"
                if _global_path.is_file():
                    _global_store = ConfigStore(config_dir=_global_dir, source="global")
                    _global_config = _global_store.load()
                    if _global_config.projects:
                        _count = len(_global_config.projects)
                        formatter.warning(
                            f"Local workspace has no projects but global config has {_count}. "
                            f"Run 'kbagent init --from-global' to copy them, "
                            f"or remove {config_store.config_path.parent}/ to use global config."
                        )
        except Exception:
            logging.getLogger(__name__).debug("startup config-warning check failed", exc_info=True)

    # Enforce permissions for top-level commands (sub-app commands use callbacks)
    _top_level_commands = {
        "init",
        "doctor",
        "version",
        "update",
        "changelog",
        "context",
        "repl",
        "serve",
        "search",
    }
    _is_help = "--help" in sys.argv or "-h" in sys.argv

    if ctx.invoked_subcommand in _top_level_commands and not _is_help:
        try:
            permission_engine.check_or_raise(ctx.invoked_subcommand)
        except PermissionDeniedError as exc:
            formatter.error(message=exc.message, error_code=ErrorCode.PERMISSION_DENIED)
            raise typer.Exit(code=EXIT_PERMISSION_DENIED) from None

    # Launch REPL if no subcommand was given (set above)
    if ctx.obj.get("_launch_repl"):
        from .commands.repl import _run_repl

        _run_repl(
            json_mode=json_output,
            verbose=verbose,
            no_color=effective_no_color,
            config_dir=config_dir,
            deny_writes=deny_writes,
            deny_destructive=deny_destructive,
        )
        raise typer.Exit()


def run() -> None:
    """Console-script entry point.

    Wraps ``app()`` so exactly one best-effort usage event is posted per
    invocation (see :mod:`telemetry`), carrying the command's exit code and
    wall-clock duration. Telemetry never changes the exit code, swallows the
    raised exception, or stalls the command.
    """
    telemetry.reset()
    start = time.monotonic()
    exit_code = 0
    error: BaseException | None = None
    interrupted = False
    try:
        app()
    except KeyboardInterrupt:
        interrupted = True
        raise
    except SystemExit as exc:
        code = exc.code
        exit_code = code if isinstance(code, int) else (0 if code is None else 1)
        raise
    except BaseException as exc:  # recorded for telemetry, then re-raised
        exit_code = 1
        error = exc
        raise
    finally:
        if not interrupted:
            telemetry.emit_cli_invocation(sys.argv, exit_code, error, time.monotonic() - start)
