"""Typer root application with global options and subcommand registration."""

import logging
import sys

import typer

from .commands.config import config_app
from .commands.context import context_command
from .commands.doctor import doctor_command
from .commands.job import job_app
from .commands.lineage import lineage_app
from .commands.org import org_app
from .commands.project import project_app
from .commands.tool import tool_app
from .config_store import ConfigStore
from .output import OutputFormatter
from .services.config_service import ConfigService
from .services.doctor_service import DoctorService
from .services.job_service import JobService
from .services.lineage_service import LineageService
from .services.mcp_service import McpService
from .services.org_service import OrgService
from .services.project_service import ProjectService

app = typer.Typer(
    name="kbagent",
    help="Keboola Agent CLI -- AI-friendly interface to Keboola projects",
    no_args_is_help=True,
)

app.add_typer(project_app, name="project")
app.add_typer(config_app, name="config")
app.add_typer(job_app, name="job")
app.add_typer(lineage_app, name="lineage")
app.add_typer(org_app, name="org")
app.add_typer(tool_app, name="tool")
app.command("context")(context_command)
app.command("doctor")(doctor_command)


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
) -> None:
    """Global options applied to all commands."""
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

    config_store = ConfigStore()

    project_service = ProjectService(config_store=config_store)
    config_service = ConfigService(config_store=config_store)
    job_service = JobService(config_store=config_store)
    lineage_service = LineageService(config_store=config_store)
    org_service = OrgService(config_store=config_store)
    mcp_service = McpService(config_store=config_store)
    doctor_service = DoctorService(config_store=config_store, mcp_service=mcp_service)

    ctx.ensure_object(dict)
    ctx.obj["formatter"] = formatter
    ctx.obj["json_output"] = json_output
    ctx.obj["verbose"] = verbose
    ctx.obj["no_color"] = effective_no_color
    ctx.obj["config_store"] = config_store
    ctx.obj["project_service"] = project_service
    ctx.obj["config_service"] = config_service
    ctx.obj["job_service"] = job_service
    ctx.obj["lineage_service"] = lineage_service
    ctx.obj["org_service"] = org_service
    ctx.obj["mcp_service"] = mcp_service
    ctx.obj["doctor_service"] = doctor_service
