"""Typer root application with global options and subcommand registration."""

import importlib
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
import typer.main
from typer.core import TyperGroup

from . import telemetry
from .config_store import ConfigStore, resolve_config_dir
from .constants import EXIT_PERMISSION_DENIED
from .errors import ErrorCode, PermissionDeniedError
from .output import OutputFormatter, force_utf8_when_redirected

# `apply_firewall_flags` lives in permissions.py so `server/app.py` composes the
# very same policy for the REST surface; re-exported here because callers (and
# tests) have imported it from `cli` since 0.22.0.
from .permissions import PermissionEngine, apply_firewall_flags

# At import, not inside the root callback: Click renders `--help` while parsing,
# before any callback runs, and `--help` is one of the surfaces that crashed.
force_utf8_when_redirected()

# ---------------------------------------------------------------------------
# Lazy command registration (issue #801)
#
# Importing every command module (and through them every service, prompt_toolkit,
# the AI / Data Science clients, ...) cost ~0.2 s on EVERY invocation, although
# an invocation only ever runs one command. Commands are therefore registered
# by NAME: the root group lists every name up front and imports a command's
# module only when that command is looked up. `kbagent job list` imports only
# `commands.job`; `kbagent --help` looks every command up (it needs each one's
# help text), so its output is unchanged.
#
# The order below IS the order of `kbagent --help` within each panel: plain
# commands first, then groups -- the order Typer produced when these were
# registered eagerly with `app.command()` / `app.add_typer()`.
# tests/test_startup_imports.py guards the import budget.
# ---------------------------------------------------------------------------

_SETUP = "Setup & Info"
_PROJ = "Project Management"
_BROWSE = "Browse & Inspect"
_FLOWS = "Flows"
_DEV = "Development"


@dataclass(frozen=True)
class LazyCommand:
    """A root command registered by name; its module is imported on first lookup.

    ``target`` is ``"<module under commands/>:<attribute>"``. A ``typer.Typer``
    attribute becomes a group (the ``app.add_typer`` path), anything else a
    command (the ``app.command`` path) -- with exactly the options the eager
    registration passed, so the resulting Click objects are identical.
    """

    name: str
    target: str
    panel: str
    hidden: bool = False
    help: str | None = None
    no_args_is_help: bool = False

    def load(self) -> Any:
        """Import the command module and build the Click command/group for it."""
        module_name, attr = self.target.split(":")
        obj = getattr(importlib.import_module(f".commands.{module_name}", __package__), attr)
        # A throwaway Typer records the registration exactly as the root app did
        # before #801; Typer's own converters then build the Click object.
        holder = typer.Typer()
        if isinstance(obj, typer.Typer):
            extra: dict[str, Any] = {"hidden": True} if self.hidden else {}
            holder.add_typer(obj, name=self.name, rich_help_panel=self.panel, **extra)
            return typer.main.get_group_from_info(
                holder.registered_groups[0],
                pretty_exceptions_short=app.pretty_exceptions_short,
                suggest_commands=app.suggest_commands,
                rich_markup_mode=app.rich_markup_mode,
            )
        holder.command(
            self.name,
            rich_help_panel=self.panel,
            help=self.help,
            no_args_is_help=self.no_args_is_help,
        )(obj)
        return typer.main.get_command_from_info(
            holder.registered_commands[0],
            pretty_exceptions_short=app.pretty_exceptions_short,
            rich_markup_mode=app.rich_markup_mode,
        )


LAZY_COMMANDS: tuple[LazyCommand, ...] = (
    # -- plain commands --
    LazyCommand("init", "init:init_command", _SETUP),
    LazyCommand("doctor", "doctor:doctor_command", _SETUP),
    LazyCommand("version", "version:version_command", _SETUP),
    LazyCommand("update", "version:update_command", _SETUP),
    LazyCommand("changelog", "changelog:changelog_command", _SETUP),
    LazyCommand("context", "context:context_command", _SETUP),
    LazyCommand("repl", "repl:repl_command", _SETUP),
    LazyCommand("serve", "serve:serve_command", _SETUP),
    LazyCommand(
        "search",
        "search:search_command",
        _BROWSE,
        help="Search for items (tables, buckets, configs, flows, …) by name or content.",
        no_args_is_help=True,
    ),
    # -- groups --
    LazyCommand("permissions", "permissions:permissions_app", _SETUP),
    LazyCommand("auth", "auth:auth_app", _SETUP),
    LazyCommand("project", "project:project_app", _PROJ),
    LazyCommand("org", "org:org_app", _PROJ),
    LazyCommand("feature", "feature:feature_app", _PROJ),
    LazyCommand("token", "token:token_app", _PROJ),
    LazyCommand("billing", "billing:billing_app", _PROJ),
    LazyCommand("component", "component:component_app", _BROWSE),
    LazyCommand("config", "config:config_app", _BROWSE),
    LazyCommand("data-app", "data_app:data_app_app", _BROWSE),
    LazyCommand("job", "job:job_app", _BROWSE),
    LazyCommand("storage", "storage:storage_app", _BROWSE),
    LazyCommand("stream", "stream:stream_app", _BROWSE),
    LazyCommand("sharing", "sharing:sharing_app", _BROWSE),
    LazyCommand("lineage", "lineage:lineage_app", _BROWSE),
    LazyCommand("kai", "kai:kai_app", _BROWSE),
    LazyCommand("docs", "docs:docs_app", _BROWSE),
    LazyCommand("transformation", "transformation:transformation_app", _BROWSE),
    LazyCommand("flow", "flow:flow_app", _FLOWS),
    LazyCommand("schedule", "schedule:schedule_app", _FLOWS),
    LazyCommand("notification", "notification:notification_app", _FLOWS),
    LazyCommand("branch", "branch:branch_app", _DEV),
    LazyCommand("merge-request", "merge_request:merge_request_app", _DEV),
    LazyCommand("mr", "merge_request:merge_request_app", _DEV, hidden=True),
    LazyCommand("workspace", "workspace:workspace_app", _DEV),
    LazyCommand("sync", "sync:sync_app", _DEV),
    LazyCommand("encrypt", "encrypt:encrypt_app", _DEV),
    LazyCommand("semantic-layer", "semantic_layer:semantic_layer_app", _DEV),
    LazyCommand("sl", "semantic_layer:semantic_layer_app", _DEV, hidden=True),
    LazyCommand("http", "http_client:http_app", _DEV),
    LazyCommand("agent", "agent:agent_app", _DEV),
    LazyCommand("dev-portal", "dev_portal:dev_portal_app", _DEV),
)


class _LazyCommandTable(dict[str, Any]):
    """The root group's ``commands`` mapping, resolving a name on first lookup.

    Every name is a key from the start, so everything that only needs names --
    typo suggestions, ``name in commands``, iteration order -- behaves as with
    eager registration. Looking a command up (``[]``, ``get``, ``values``,
    ``items``) replaces its :class:`LazyCommand` with the real Click object in
    place, which keeps the key order. It stays a ``dict`` so existing tree
    walkers (telemetry, the permission-registry test, tooling scripts) need no
    change.
    """

    def __getitem__(self, name: str) -> Any:
        value = super().__getitem__(name)
        if isinstance(value, LazyCommand):
            value = value.load()
            super().__setitem__(name, value)
        return value

    def get(self, name: str, default: Any = None) -> Any:  # ty: ignore[invalid-method-override]
        try:
            return self[name]
        except KeyError:
            return default

    def values(self) -> list[Any]:  # ty: ignore[invalid-method-override]
        return [self[name] for name in self]

    def items(self) -> list[tuple[str, Any]]:  # ty: ignore[invalid-method-override]
        return [(name, self[name]) for name in self]


class LazyRootGroup(TyperGroup):
    """Root Click group whose commands are imported on demand (see above)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        table = _LazyCommandTable(self.commands)
        for entry in LAZY_COMMANDS:
            table[entry.name] = entry
        self.commands = table


app = typer.Typer(
    name="kbagent",
    help="Keboola Agent CLI -- AI-friendly interface to Keboola projects",
    invoke_without_command=True,
    cls=LazyRootGroup,
)


# ---------------------------------------------------------------------------
# Lazy services (issue #801)
#
# ctx.obj key -> service class; each key is also the module name under
# `services/`. A service is built on its first `ctx.obj[key]` (`_ServiceMap`),
# and its class is resolved through this module's attribute (`__getattr__`
# below), so `mock.patch("keboola_agent_cli.cli.JobService")` -- the pattern the
# test-suite uses throughout -- keeps working.
# ---------------------------------------------------------------------------

_SERVICES: dict[str, str] = {
    "project_service": "ProjectService",
    "component_service": "ComponentService",
    "config_service": "ConfigService",
    "job_service": "JobService",
    "lineage_service": "LineageService",
    "deep_lineage_service": "DeepLineageService",
    "org_service": "OrgService",
    "member_service": "MemberService",
    "feature_service": "FeatureService",
    "branch_service": "BranchService",
    "merge_request_service": "MergeRequestService",
    "sharing_service": "SharingService",
    "search_service": "SearchService",
    "snapshot_service": "SnapshotService",
    "storage_service": "StorageService",
    "stream_service": "StreamService",
    "token_service": "TokenService",
    "sync_service": "SyncService",
    "variables_service": "VariablesService",
    "encrypt_service": "EncryptService",
    "flow_service": "FlowService",
    "schedule_service": "ScheduleService",
    "notification_service": "NotificationService",
    "workspace_service": "WorkspaceService",
    "data_app_service": "DataAppService",
    "data_app_git_service": "DataAppGitService",
    "semantic_layer_service": "SemanticLayerService",
    "repo_validate_service": "RepoValidateService",
    "kai_service": "KaiService",
    "docs_service": "DocsService",
    "doctor_service": "DoctorService",
    "version_service": "VersionService",
    "http_forwarder_service": "HttpForwarderService",
    "agent_service": "AgentService",
    "auth_service": "AuthService",
    "billing_service": "BillingService",
}
# Services whose constructor takes no ConfigStore.
_STATELESS_SERVICES = frozenset({"version_service", "http_forwarder_service"})
_SERVICE_MODULE_BY_CLASS = {cls: key for key, cls in _SERVICES.items()}


def __getattr__(name: str) -> Any:
    """Import a service class on first access as ``cli.<ServiceClass>``."""
    module_key = _SERVICE_MODULE_BY_CLASS.get(name)
    if module_key is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    cls = getattr(importlib.import_module(f".services.{module_key}", __package__), name)
    globals()[name] = cls
    return cls


class _ServiceMap(dict[str, Any]):
    """``ctx.obj``: a plain dict that constructs a service on its first lookup."""

    def __init__(self, config_store: ConfigStore) -> None:
        super().__init__()
        self._config_store = config_store

    def __missing__(self, key: str) -> Any:
        cls_name = _SERVICES.get(key)
        if cls_name is None:
            raise KeyError(key)
        cls = getattr(sys.modules[__name__], cls_name)
        service = cls() if key in _STATELESS_SERVICES else cls(config_store=self._config_store)
        self[key] = service
        return service

    def __contains__(self, key: object) -> bool:
        return super().__contains__(key) or key in _SERVICES

    def get(self, key: str, default: Any = None) -> Any:  # ty: ignore[invalid-method-override]
        try:
            return self[key]
        except KeyError:
            return default


def _version_callback(value: bool) -> None:
    """Print version and exit -- standard `--version` flag for CLI tools."""
    if value:
        from . import __version__

        typer.echo(f"kbagent v{__version__}")
        raise typer.Exit()


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
    from .constants import ENV_CONVERSATION_ID

    # Published into the environment rather than threaded through the Typer
    # context because that is where every consumer already reads it:
    # `BaseHttpClient.__init__` stamps the header from os.environ for all
    # seven clients, `doctor` reports on it, and scheduled-agent subprocesses
    # inherit it for free. `serve` sets it the same way. The flag wins over an
    # inherited env var -- it is the more specific instruction (issue #716).
    if conversation_id is not None:
        os.environ[ENV_CONVERSATION_ID] = conversation_id

    maybe_auto_update()

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
        is_interactive = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
        if is_interactive and not json_output:
            # Defer REPL launch until after context setup (below)
            ctx.ensure_object(dict)
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

    # Services are built on first use (issue #801); anything already in ctx.obj
    # (the REPL marker set above) is carried over.
    obj = _ServiceMap(config_store)
    obj.update(ctx.ensure_object(dict))
    ctx.obj = obj
    obj["formatter"] = formatter
    obj["json_output"] = json_output
    obj["permission_engine"] = permission_engine
    obj["verbose"] = verbose
    obj["no_color"] = effective_no_color
    obj["deny_writes"] = deny_writes
    obj["deny_destructive"] = deny_destructive
    obj["allow_env_manage_token"] = allow_env_manage_token
    obj["config_store"] = config_store

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
