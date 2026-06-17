"""Typer root application with global options and subcommand registration."""

import logging
import sys
from pathlib import Path

import typer

from .commands.agent import agent_app
from .commands.branch import branch_app
from .commands.changelog import changelog_command
from .commands.component import component_app
from .commands.config import config_app
from .commands.context import context_command
from .commands.data_app import data_app_app
from .commands.dev_portal import dev_portal_app
from .commands.doctor import doctor_command
from .commands.encrypt import encrypt_app
from .commands.feature import feature_app
from .commands.flow import flow_app
from .commands.http_client import http_app
from .commands.init import init_command
from .commands.job import job_app
from .commands.kai import kai_app
from .commands.lineage import lineage_app
from .commands.org import org_app
from .commands.permissions import permissions_app
from .commands.project import project_app
from .commands.repl import repl_command
from .commands.schedule import schedule_app
from .commands.search import search_command
from .commands.semantic_layer import semantic_layer_app
from .commands.serve import serve_command
from .commands.sharing import sharing_app
from .commands.storage import storage_app
from .commands.stream import stream_app
from .commands.sync import sync_app
from .commands.tool import tool_app
from .commands.version import update_command, version_command
from .commands.workspace import workspace_app
from .config_store import ConfigStore, resolve_config_dir
from .constants import EXIT_PERMISSION_DENIED
from .errors import ErrorCode, PermissionDeniedError
from .models import PermissionPolicy
from .output import OutputFormatter
from .permissions import PermissionEngine
from .services.agent_service import AgentService
from .services.branch_service import BranchService
from .services.component_service import ComponentService
from .services.config_service import ConfigService
from .services.data_app_git_service import DataAppGitService
from .services.data_app_service import DataAppService
from .services.deep_lineage_service import DeepLineageService
from .services.doctor_service import DoctorService
from .services.encrypt_service import EncryptService
from .services.feature_service import FeatureService
from .services.flow_service import FlowService
from .services.http_forwarder_service import HttpForwarderService
from .services.job_service import JobService
from .services.kai_service import KaiService
from .services.lineage_service import LineageService
from .services.mcp_service import McpService
from .services.member_service import MemberService
from .services.org_service import OrgService
from .services.project_service import ProjectService
from .services.repo_validate_service import RepoValidateService
from .services.schedule_service import ScheduleService
from .services.search_service import SearchService
from .services.semantic_layer_service import SemanticLayerService
from .services.sharing_service import SharingService
from .services.storage_service import StorageService
from .services.stream_service import StreamService
from .services.sync_service import SyncService
from .services.variables_service import VariablesService
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
app.command("changelog", rich_help_panel=_SETUP)(changelog_command)
app.command("context", rich_help_panel=_SETUP)(context_command)
app.command("repl", rich_help_panel=_SETUP)(repl_command)
app.command("serve", rich_help_panel=_SETUP)(serve_command)
app.add_typer(permissions_app, name="permissions", rich_help_panel=_SETUP)

# -- Project Management --
_PROJ = "Project Management"
app.add_typer(project_app, name="project", rich_help_panel=_PROJ)
app.add_typer(org_app, name="org", rich_help_panel=_PROJ)
app.add_typer(feature_app, name="feature", rich_help_panel=_PROJ)

# -- Browse & Inspect --
_BROWSE = "Browse & Inspect"
app.add_typer(component_app, name="component", rich_help_panel=_BROWSE)
app.add_typer(config_app, name="config", rich_help_panel=_BROWSE)
app.command(
    "search",
    rich_help_panel=_BROWSE,
    help="Search for items (tables, buckets, configs, flows, …) by name or content.",
    no_args_is_help=True,
)(search_command)
app.add_typer(data_app_app, name="data-app", rich_help_panel=_BROWSE)
app.add_typer(job_app, name="job", rich_help_panel=_BROWSE)
app.add_typer(storage_app, name="storage", rich_help_panel=_BROWSE)
app.add_typer(stream_app, name="stream", rich_help_panel=_BROWSE)
app.add_typer(sharing_app, name="sharing", rich_help_panel=_BROWSE)
app.add_typer(lineage_app, name="lineage", rich_help_panel=_BROWSE)
app.add_typer(kai_app, name="kai", rich_help_panel=_BROWSE)

# -- Flows --
_FLOWS = "Flows"
app.add_typer(flow_app, name="flow", rich_help_panel=_FLOWS)
app.add_typer(schedule_app, name="schedule", rich_help_panel=_FLOWS)

# -- Development --
_DEV = "Development"
app.add_typer(branch_app, name="branch", rich_help_panel=_DEV)
app.add_typer(workspace_app, name="workspace", rich_help_panel=_DEV)
app.add_typer(tool_app, name="tool", rich_help_panel=_DEV)
app.add_typer(sync_app, name="sync", rich_help_panel=_DEV)
app.add_typer(encrypt_app, name="encrypt", rich_help_panel=_DEV)
app.add_typer(semantic_layer_app, name="semantic-layer", rich_help_panel=_DEV)
app.add_typer(semantic_layer_app, name="sl", rich_help_panel=_DEV, hidden=True)
app.add_typer(http_app, name="http", rich_help_panel=_DEV)
app.add_typer(agent_app, name="agent", rich_help_panel=_DEV)
app.add_typer(dev_portal_app, name="dev-portal", rich_help_panel=_DEV)


def apply_firewall_flags(
    persisted: PermissionPolicy | None,
    *,
    deny_writes: bool,
    deny_destructive: bool,
) -> PermissionPolicy | None:
    """Merge --deny-writes / --deny-destructive into the active policy for this invocation.

    Session-only: does NOT touch config.json. If neither flag is set, the
    persisted policy is returned unchanged (possibly None).

    Merge semantics:
    - A fresh session policy synthesized from the flags uses mode='allow'
      so everything is allowed unless matched by the deny list.
    - When a persisted policy already exists, the flag-implied deny patterns
      are appended to its deny list (dedup); the mode is preserved. This is
      strictly additive -- adding a flag never relaxes the persisted policy.
    """
    if not deny_writes and not deny_destructive:
        return persisted

    extra_deny: list[str] = []
    if deny_writes:
        # cli:write pattern intentionally spans write+destructive+admin
        # (permissions._matches_pattern lines 175-178). tool:write spans
        # tool write+destructive. Wide net: --deny-writes blocks anything
        # that mutates state.
        extra_deny.extend(["cli:write", "tool:write"])
    if deny_destructive:
        # cli:destructive narrowly matches only ops categorized 'destructive'
        # (data destruction). Admin and pure-write are left allowed by design:
        # the two flags exist precisely so callers can opt into the narrower
        # block without forfeiting writes (e.g. allow create-bucket, block
        # delete-bucket).
        extra_deny.extend(["cli:destructive", "tool:destructive"])

    if persisted is None:
        return PermissionPolicy(mode="allow", allow=[], deny=extra_deny)

    # Preserve persisted mode, allow list; extend deny list without duplicates.
    merged_deny = list(persisted.deny)
    for pattern in extra_deny:
        if pattern not in merged_deny:
            merged_deny.append(pattern)

    return PermissionPolicy(
        mode=persisted.mode,
        allow=list(persisted.allow),
        deny=merged_deny,
    )


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
    allow_env_manage_token: bool = typer.Option(
        False,
        "--allow-env-manage-token",
        help="Read KBC_MANAGE_API_TOKEN from the environment. Without this "
        "flag the env var is ignored (with a warning) and an interactive "
        "TTY prompt is required. Default-deny since 0.28.0; closes the "
        "AI-exfiltration risk where subprocesses inherit the manage token.",
    ),
) -> None:
    """Global options applied to all commands."""
    from .auto_update import maybe_auto_update, show_post_update_changelog

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

    project_service = ProjectService(config_store=config_store)
    component_service = ComponentService(config_store=config_store)
    config_service = ConfigService(config_store=config_store)
    job_service = JobService(config_store=config_store)
    lineage_service = LineageService(config_store=config_store)
    deep_lineage_service = DeepLineageService(config_store=config_store)
    org_service = OrgService(config_store=config_store)
    member_service = MemberService(config_store=config_store)
    feature_service = FeatureService(config_store=config_store)
    mcp_service = McpService(config_store=config_store)
    branch_service = BranchService(config_store=config_store)
    sharing_service = SharingService(config_store=config_store)
    search_service = SearchService(config_store=config_store)
    storage_service = StorageService(config_store=config_store)
    stream_service = StreamService(config_store=config_store)
    sync_service = SyncService(config_store=config_store)
    variables_service = VariablesService(config_store=config_store)
    encrypt_service = EncryptService(config_store=config_store)
    flow_service = FlowService(config_store=config_store)
    schedule_service = ScheduleService(config_store=config_store)
    workspace_service = WorkspaceService(config_store=config_store)
    data_app_service = DataAppService(config_store=config_store)
    data_app_git_service = DataAppGitService(config_store=config_store)
    semantic_layer_service = SemanticLayerService(config_store=config_store)
    repo_validate_service = RepoValidateService(config_store=config_store)
    kai_service = KaiService(config_store=config_store)
    doctor_service = DoctorService(config_store=config_store, mcp_service=mcp_service)
    version_service = VersionService()
    http_forwarder_service = HttpForwarderService()
    agent_service = AgentService(config_store=config_store, mcp_service=mcp_service)

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

    ctx.ensure_object(dict)
    ctx.obj["formatter"] = formatter
    ctx.obj["json_output"] = json_output
    ctx.obj["permission_engine"] = permission_engine
    ctx.obj["verbose"] = verbose
    ctx.obj["no_color"] = effective_no_color
    ctx.obj["deny_writes"] = deny_writes
    ctx.obj["deny_destructive"] = deny_destructive
    ctx.obj["allow_env_manage_token"] = allow_env_manage_token
    ctx.obj["config_store"] = config_store
    ctx.obj["project_service"] = project_service
    ctx.obj["component_service"] = component_service
    ctx.obj["config_service"] = config_service
    ctx.obj["job_service"] = job_service
    ctx.obj["lineage_service"] = lineage_service
    ctx.obj["deep_lineage_service"] = deep_lineage_service
    ctx.obj["org_service"] = org_service
    ctx.obj["member_service"] = member_service
    ctx.obj["feature_service"] = feature_service
    ctx.obj["mcp_service"] = mcp_service
    ctx.obj["branch_service"] = branch_service
    ctx.obj["sharing_service"] = sharing_service
    ctx.obj["search_service"] = search_service
    ctx.obj["storage_service"] = storage_service
    ctx.obj["stream_service"] = stream_service
    ctx.obj["sync_service"] = sync_service
    ctx.obj["variables_service"] = variables_service
    ctx.obj["encrypt_service"] = encrypt_service
    ctx.obj["flow_service"] = flow_service
    ctx.obj["schedule_service"] = schedule_service
    ctx.obj["workspace_service"] = workspace_service
    ctx.obj["data_app_service"] = data_app_service
    ctx.obj["data_app_git_service"] = data_app_git_service
    ctx.obj["semantic_layer_service"] = semantic_layer_service
    ctx.obj["repo_validate_service"] = repo_validate_service
    ctx.obj["kai_service"] = kai_service
    ctx.obj["doctor_service"] = doctor_service
    ctx.obj["version_service"] = version_service
    ctx.obj["http_forwarder_service"] = http_forwarder_service
    ctx.obj["agent_service"] = agent_service

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
