"""Typer root application with global options and subcommand registration."""

import sys

import typer

from .commands.config import config_app
from .commands.context import context_command
from .commands.doctor import doctor_command
from .commands.project import project_app
from .config_store import ConfigStore
from .output import OutputFormatter
from .services.project_service import ProjectService

app = typer.Typer(
    name="kbagent",
    help="Keboola Agent CLI -- AI-friendly interface to Keboola projects",
    no_args_is_help=True,
)

app.add_typer(project_app, name="project")
app.add_typer(config_app, name="config")
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
    is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    effective_no_color = no_color or not is_tty

    formatter = OutputFormatter(
        json_mode=json_output,
        no_color=effective_no_color,
        verbose=verbose,
    )

    config_store = ConfigStore()

    project_service = ProjectService(config_store=config_store)

    ctx.ensure_object(dict)
    ctx.obj["formatter"] = formatter
    ctx.obj["json_output"] = json_output
    ctx.obj["verbose"] = verbose
    ctx.obj["no_color"] = effective_no_color
    ctx.obj["config_store"] = config_store
    ctx.obj["project_service"] = project_service
