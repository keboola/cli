"""Output formatting with JSON and Rich dual mode support."""

import json
import sys
from typing import Any, Callable

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .models import ErrorResponse, SuccessResponse


class OutputFormatter:
    """Formats CLI output as either JSON (for machines/agents) or Rich (for humans).

    In JSON mode, all output goes to stdout as valid JSON.
    In human mode, Rich console is used for formatted tables and panels.
    """

    def __init__(
        self,
        json_mode: bool = False,
        no_color: bool = False,
        verbose: bool = False,
    ) -> None:
        self.json_mode = json_mode
        self.verbose = verbose
        is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
        force_terminal = None if is_tty and not no_color else False
        self.console = Console(
            no_color=no_color,
            force_terminal=force_terminal,
        )
        self.err_console = Console(
            stderr=True,
            no_color=no_color,
            force_terminal=force_terminal,
        )

    def output(self, data: Any, human_formatter: Callable[[Console, Any], None] | None = None) -> None:
        """Output data in the appropriate format.

        Args:
            data: The data to output. In JSON mode, this is serialized directly.
                  In human mode, it's passed to human_formatter.
            human_formatter: A callable that takes (Console, data) and prints
                           human-friendly output. If None in human mode, prints repr.
        """
        if self.json_mode:
            response = SuccessResponse(status="ok", data=data)
            sys.stdout.write(response.model_dump_json(indent=2) + "\n")
        else:
            if human_formatter is not None:
                human_formatter(self.console, data)
            else:
                self.console.print(data)

    def error(self, message: str, error_code: str = "ERROR", project: str = "", retryable: bool = False) -> None:
        """Output an error message.

        Args:
            message: Human-readable error description.
            error_code: Machine-readable error code.
            project: Project alias related to the error.
            retryable: Whether the operation can be retried.
        """
        if self.json_mode:
            err = ErrorResponse(
                code=error_code,
                message=message,
                project=project,
                retryable=retryable,
            )
            error_envelope = {"status": "error", "error": err.model_dump()}
            sys.stdout.write(json.dumps(error_envelope, indent=2) + "\n")
        else:
            self.err_console.print(f"[bold red]Error:[/bold red] {message}")

    def success(self, message: str) -> None:
        """Output a success message.

        Args:
            message: The success message to display.
        """
        if self.json_mode:
            response = SuccessResponse(status="ok", data={"message": message})
            sys.stdout.write(response.model_dump_json(indent=2) + "\n")
        else:
            self.console.print(f"[bold green]Success:[/bold green] {message}")

    def warning(self, message: str) -> None:
        """Output a warning message to stderr (human mode only).

        In JSON mode, warnings are not printed separately -- they are
        embedded in the structured response via the errors list.

        Args:
            message: The warning message to display.
        """
        if not self.json_mode:
            self.err_console.print(f"[bold yellow]Warning:[/bold yellow] {message}")


def format_configs_table(console: Console, data: dict[str, Any]) -> None:
    """Render a Rich table of configurations grouped by project alias.

    Args:
        console: Rich Console instance.
        data: Dict with "configs" (list of config dicts) and "errors" (list of error dicts).
    """
    configs = data.get("configs", [])
    errors = data.get("errors", [])

    # Show per-project errors as warnings
    for err in errors:
        console.print(
            f"[bold yellow]Warning:[/bold yellow] Project [bold]{err['project_alias']}[/bold]: "
            f"{err['message']}"
        )

    if not configs:
        if not errors:
            console.print("No configurations found. Use [bold]kbagent project add[/bold] to connect a project first.")
        else:
            console.print("No configurations retrieved (all projects failed).")
        return

    # Group configs by project alias
    projects_order: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for cfg in configs:
        alias = cfg["project_alias"]
        if alias not in grouped:
            projects_order.append(alias)
            grouped[alias] = []
        grouped[alias].append(cfg)

    for alias in projects_order:
        project_configs = grouped[alias]
        table = Table(title=f"Configurations - {alias}")
        table.add_column("Component", style="bold cyan")
        table.add_column("Type", style="dim")
        table.add_column("Config ID", justify="right")
        table.add_column("Config Name")
        table.add_column("Description", style="dim", max_width=40)

        for cfg in project_configs:
            table.add_row(
                cfg["component_id"],
                cfg["component_type"],
                cfg["config_id"],
                cfg["config_name"],
                cfg.get("config_description", ""),
            )

        console.print(table)
        console.print()


def format_config_detail(console: Console, data: dict[str, Any]) -> None:
    """Render detailed configuration information.

    Args:
        console: Rich Console instance.
        data: Configuration detail dict from the API.
    """
    alias = data.get("project_alias", "unknown")
    name = data.get("name", "Unknown")
    config_id = data.get("id", "")
    description = data.get("description", "")
    component_id = data.get("component_id", data.get("componentId", ""))

    header = f"Configuration Detail - {alias}"

    lines = [
        f"[bold]Name:[/bold] {name}",
        f"[bold]Config ID:[/bold] {config_id}",
        f"[bold]Component:[/bold] {component_id}",
    ]
    if description:
        lines.append(f"[bold]Description:[/bold] {description}")

    # Show configuration parameters if present
    configuration = data.get("configuration", {})
    if configuration:
        import json as _json
        config_str = _json.dumps(configuration, indent=2)
        lines.append(f"\n[bold]Configuration:[/bold]\n{config_str}")

    # Show rows if present
    rows = data.get("rows", [])
    if rows:
        lines.append(f"\n[bold]Rows:[/bold] {len(rows)} row(s)")
        for row in rows[:10]:  # Show at most 10 rows
            row_name = row.get("name", row.get("id", ""))
            lines.append(f"  - {row_name}")
        if len(rows) > 10:
            lines.append(f"  ... and {len(rows) - 10} more")

    panel = Panel("\n".join(lines), title=header, expand=False)
    console.print(panel)
