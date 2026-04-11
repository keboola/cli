"""Typer root application with global options and subcommand registration."""

import logging
import sys
from pathlib import Path

import typer

from .commands.branch import branch_app
from .commands.component import component_app
from .commands.config import config_app
from .commands.context import context_command
from .commands.doctor import doctor_command
from .commands.encrypt import encrypt_app
from .commands.init import init_command
from .commands.job import job_app
from .commands.lineage import lineage_app
from .commands.org import org_app
from .commands.permissions import permissions_app
from .commands.project import project_app
from .commands.repl import repl_command
from .commands.sharing import sharing_app
from .commands.storage import storage_app
from .commands.sync import sync_app
from .commands.tool import tool_app
from .commands.version import update_command, version_command
from .commands.workspace import workspace_app
from .config_store import ConfigStore, resolve_config_dir
from .constants import EXIT_PERMISSION_DENIED
from .errors import PermissionDeniedError
from .output import OutputFormatter
from .permissions import PermissionEngine
from .services.branch_service import BranchService
from .services.component_service import ComponentService
from .services.config_service import ConfigService
from .services.doctor_service import DoctorService
from .services.encrypt_service import EncryptService
from .services.job_service import JobService
from .services.lineage_service import LineageService
from .services.mcp_service import McpService
from .services.org_service import OrgService
from .services.project_service import ProjectService
from .services.sharing_service import SharingService
from .services.storage_service import StorageService
from .services.sync_service import SyncService
from .services.version_service import VersionService
from .services.workspace_service import WorkspaceService

app = typer.Typer(
    name="kbagent",
    help="Keboola Agent CLI -- AI-friendly interface to Keboola projects",
    invoke_without_command=True,
)

# -- Setup & Info --
_SETUP = "Setup & Info"
app.command("init", rich_help_panel=_SETUP)(init_command)
app.command("doctor", rich_help_panel=_SETUP)(doctor_command)
app.command("version", rich_help_panel=_SETUP)(version_command)
app.command("update", rich_help_panel=_SETUP)(update_command)
app.command("context", rich_help_panel=_SETUP)(context_command)
app.command("repl", rich_help_panel=_SETUP)(repl_command)
app.add_typer(permissions_app, name="permissions", rich_help_panel=_SETUP)

# -- Project Management --
_PROJ = "Project Management"
app.add_typer(project_app, name="project", rich_help_panel=_PROJ)
app.add_typer(org_app, name="org", rich_help_panel=_PROJ)

# -- Browse & Inspect --
_BROWSE = "Browse & Inspect"
app.add_typer(component_app, name="component", rich_help_panel=_BROWSE)
app.add_typer(config_app, name="config", rich_help_panel=_BROWSE)
app.add_typer(job_app, name="job", rich_help_panel=_BROWSE)
app.add_typer(storage_app, name="storage", rich_help_panel=_BROWSE)
app.add_typer(sharing_app, name="sharing", rich_help_panel=_BROWSE)
app.add_typer(lineage_app, name="lineage", rich_help_panel=_BROWSE)

# -- Development --
_DEV = "Development"
app.add_typer(branch_app, name="branch", rich_help_panel=_DEV)
app.add_typer(workspace_app, name="workspace", rich_help_panel=_DEV)
app.add_typer(tool_app, name="tool", rich_help_panel=_DEV)
app.add_typer(sync_app, name="sync", rich_help_panel=_DEV)
app.add_typer(encrypt_app, name="encrypt", rich_help_panel=_DEV)


@app.callback()
def main(
    ctx: typer.Context,
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
) -> None:
    """Global options applied to all commands."""
    from .auto_update import maybe_auto_update

    maybe_auto_update()

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

    project_service = ProjectService(config_store=config_store)
    component_service = ComponentService(config_store=config_store)
    config_service = ConfigService(config_store=config_store)
    job_service = JobService(config_store=config_store)
    lineage_service = LineageService(config_store=config_store)
    org_service = OrgService(config_store=config_store)
    mcp_service = McpService(config_store=config_store)
    branch_service = BranchService(config_store=config_store)
    sharing_service = SharingService(config_store=config_store)
    storage_service = StorageService(config_store=config_store)
    sync_service = SyncService(config_store=config_store)
    encrypt_service = EncryptService(config_store=config_store)
    workspace_service = WorkspaceService(config_store=config_store)
    doctor_service = DoctorService(config_store=config_store, mcp_service=mcp_service)
    version_service = VersionService()

    try:
        config = config_store.load()
        permission_engine = PermissionEngine(config.permissions)
    except Exception:
        # Config may be invalid (e.g. corrupted JSON) -- skip permission check
        permission_engine = PermissionEngine(None)

    ctx.ensure_object(dict)
    ctx.obj["formatter"] = formatter
    ctx.obj["json_output"] = json_output
    ctx.obj["permission_engine"] = permission_engine
    ctx.obj["verbose"] = verbose
    ctx.obj["no_color"] = effective_no_color
    ctx.obj["config_store"] = config_store
    ctx.obj["project_service"] = project_service
    ctx.obj["component_service"] = component_service
    ctx.obj["config_service"] = config_service
    ctx.obj["job_service"] = job_service
    ctx.obj["lineage_service"] = lineage_service
    ctx.obj["org_service"] = org_service
    ctx.obj["mcp_service"] = mcp_service
    ctx.obj["branch_service"] = branch_service
    ctx.obj["sharing_service"] = sharing_service
    ctx.obj["storage_service"] = storage_service
    ctx.obj["sync_service"] = sync_service
    ctx.obj["encrypt_service"] = encrypt_service
    ctx.obj["workspace_service"] = workspace_service
    ctx.obj["doctor_service"] = doctor_service
    ctx.obj["version_service"] = version_service

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
            pass  # Don't let warning check crash the CLI

    # Enforce permissions for top-level commands (sub-app commands use callbacks)
    _top_level_commands = {"init", "doctor", "version", "update", "context", "repl"}
    _is_help = "--help" in sys.argv or "-h" in sys.argv
    if ctx.invoked_subcommand in _top_level_commands and not _is_help:
        try:
            permission_engine.check_or_raise(ctx.invoked_subcommand)
        except PermissionDeniedError as exc:
            formatter.error(message=exc.message, error_code="PERMISSION_DENIED")
            raise typer.Exit(code=EXIT_PERMISSION_DENIED) from None

    # Launch REPL if no subcommand was given (set above)
    if ctx.obj.get("_launch_repl"):
        from .commands.repl import _run_repl

        _run_repl(
            json_mode=json_output,
            verbose=verbose,
            no_color=effective_no_color,
            config_dir=config_dir,
        )
        raise typer.Exit()
