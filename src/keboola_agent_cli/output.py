"""Output formatting with JSON and Rich dual mode support."""

import json
import sys
from collections.abc import Callable
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

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

    def output(
        self, data: Any, human_formatter: Callable[[Console, Any], None] | None = None
    ) -> None:
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

    def error(
        self, message: str, error_code: str = "ERROR", project: str = "", retryable: bool = False
    ) -> None:
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
            console.print(
                "No configurations found. Use [bold]kbagent project add[/bold] to connect a project first."
            )
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


_JOB_STATUS_STYLES = {
    "success": "bold green",
    "error": "bold red",
    "processing": "bold blue",
    "cancelled": "bold yellow",
    "terminated": "bold yellow",
}


def format_jobs_table(console: Console, data: dict[str, Any]) -> None:
    """Render a Rich table of jobs grouped by project alias.

    Args:
        console: Rich Console instance.
        data: Dict with "jobs" (list of job dicts) and "errors" (list of error dicts).
    """
    jobs = data.get("jobs", [])
    errors = data.get("errors", [])

    for err in errors:
        console.print(
            f"[bold yellow]Warning:[/bold yellow] Project [bold]{err['project_alias']}[/bold]: "
            f"{err['message']}"
        )

    if not jobs:
        if not errors:
            console.print(
                "No jobs found. Use [bold]kbagent project add[/bold] to connect a project first."
            )
        else:
            console.print("No jobs retrieved (all projects failed).")
        return

    # Detect if jobs span multiple projects
    project_aliases = {job.get("project_alias", "unknown") for job in jobs}
    multi_project = len(project_aliases) > 1

    table = Table(title="Jobs")
    table.add_column("Project", style="bold magenta")
    table.add_column("Job ID", justify="right")
    table.add_column("Status")
    table.add_column("Component", style="bold cyan")
    table.add_column("Config ID", justify="right")
    table.add_column("Created", style="dim")
    table.add_column("Duration", justify="right")

    # Track previous alias for visual grouping (show alias only on change)
    prev_alias = None
    for job in jobs:
        alias = job.get("project_alias", "unknown")
        status = job.get("status", "unknown")
        style = _JOB_STATUS_STYLES.get(status, "")
        status_display = f"[{style}]{status}[/{style}]" if style else status

        duration = _format_duration(job)

        # In multi-project mode, show alias only on first row of each group
        if multi_project:
            display_alias = alias if alias != prev_alias else ""
        else:
            display_alias = alias if prev_alias is None else ""
        prev_alias = alias

        table.add_row(
            display_alias,
            str(job.get("id", "")),
            status_display,
            job.get("component", ""),
            str(job.get("configId", job.get("config_id", ""))),
            job.get("createdTime", ""),
            duration,
        )

    console.print(table)
    console.print()


def _format_duration(job: dict[str, Any]) -> str:
    """Format job duration from startTime/endTime or durationSeconds."""
    duration_sec = job.get("durationSeconds")
    if duration_sec is not None:
        return _seconds_to_human(int(duration_sec))

    start = job.get("startTime")
    end = job.get("endTime")
    if start and end:
        from datetime import datetime

        try:
            start_dt = datetime.fromisoformat(start)
            end_dt = datetime.fromisoformat(end)
            delta = int((end_dt - start_dt).total_seconds())
            return _seconds_to_human(delta)
        except (ValueError, TypeError):
            pass

    return "-"


def _seconds_to_human(seconds: int) -> str:
    """Convert seconds to a human-readable duration string."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m"


def format_job_detail(console: Console, data: dict[str, Any]) -> None:
    """Render detailed job information as a Rich panel.

    Args:
        console: Rich Console instance.
        data: Job detail dict from the Queue API with project_alias.
    """
    alias = data.get("project_alias", "unknown")
    job_id = data.get("id", "")
    status = data.get("status", "unknown")
    style = _JOB_STATUS_STYLES.get(status, "")
    status_display = f"[{style}]{status}[/{style}]" if style else status

    lines = [
        f"[bold]Job ID:[/bold] {job_id}",
        f"[bold]Project:[/bold] {alias}",
        f"[bold]Status:[/bold] {status_display}",
        f"[bold]Component:[/bold] {data.get('component', '')}",
        f"[bold]Config ID:[/bold] {data.get('config', data.get('configId', ''))}",
        f"[bold]Mode:[/bold] {data.get('mode', '')}",
        f"[bold]Type:[/bold] {data.get('type', '')}",
    ]

    # Timing
    created = data.get("createdTime", "")
    start = data.get("startTime", "")
    end = data.get("endTime", "")
    duration = _format_duration(data)

    if created:
        lines.append(f"[bold]Created:[/bold] {created}")
    if start:
        lines.append(f"[bold]Started:[/bold] {start}")
    if end:
        lines.append(f"[bold]Ended:[/bold] {end}")
    lines.append(f"[bold]Duration:[/bold] {duration}")

    # Branch and orchestration
    branch_id = data.get("branchId")
    if branch_id:
        lines.append(f"[bold]Branch ID:[/bold] {branch_id}")
    orch_job = data.get("orchestrationJobId")
    if orch_job:
        lines.append(f"[bold]Orchestration Job:[/bold] {orch_job}")

    # URL
    url = data.get("url", "")
    if url:
        lines.append(f"[bold]URL:[/bold] {url}")

    # Result message
    result = data.get("result", {})
    if isinstance(result, dict):
        message = result.get("message", "")
        if message:
            lines.append(f"\n[bold]Result Message:[/bold]\n{message}")

        error_info = result.get("error", {})
        if isinstance(error_info, dict) and error_info:
            error_type = error_info.get("type", "")
            if error_type:
                lines.append(f"[bold]Error Type:[/bold] {error_type}")

    panel = Panel("\n".join(lines), title=f"Job Detail - {job_id}", expand=False)
    console.print(panel)


def format_tools_table(console: Console, data: dict[str, Any]) -> None:
    """Render a Rich table of MCP tools.

    Args:
        console: Rich Console instance.
        data: Dict with "tools" list and "errors" list.
    """
    tools = data.get("tools", [])
    errors = data.get("errors", [])

    for err in errors:
        console.print(
            f"[bold yellow]Warning:[/bold yellow] Project [bold]{err['project_alias']}[/bold]: "
            f"{err['message']}"
        )

    if not tools:
        if not errors:
            console.print(
                "No MCP tools found. Ensure keboola-mcp-server is installed and a project is connected."
            )
        else:
            console.print("No tools retrieved (all projects failed).")
        return

    table = Table(title="MCP Tools")
    table.add_column("Tool Name", style="bold cyan")
    table.add_column("Multi-Project", justify="center")
    table.add_column("Description", max_width=60)

    for tool in tools:
        multi = "[green]yes[/green]" if tool.get("multi_project") else "[dim]no[/dim]"
        table.add_row(
            tool["name"],
            multi,
            tool.get("description", ""),
        )

    console.print(table)
    console.print()


def _extract_result_text(result: dict[str, Any]) -> str:
    """Extract a text representation of a tool result's content for comparison."""
    parts = []
    for item in result.get("content", []):
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            parts.append(json.dumps(item, sort_keys=True))
        else:
            parts.append(str(item))
    return "\n".join(parts)


def format_tool_result(console: Console, data: dict[str, Any]) -> None:
    """Render MCP tool call results as Rich panels.

    When all results are errors with the same message (e.g. missing parameter),
    consolidates them into a single error panel instead of repeating N times.

    Args:
        console: Rich Console instance.
        data: Dict with "results" list and "errors" list.
    """
    results = data.get("results", [])
    errors = data.get("errors", [])

    for err in errors:
        console.print(
            f"[bold yellow]Warning:[/bold yellow] Project [bold]{err['project_alias']}[/bold]: "
            f"{err['message']}"
        )

    if not results:
        if not errors:
            console.print("No results returned.")
        else:
            console.print("Tool call failed for all projects.")
        return

    # Detect all-same-error pattern: all results are errors with identical content
    all_errors = all(r.get("isError", False) for r in results)
    if all_errors and len(results) > 1:
        unique_messages = {_extract_result_text(r) for r in results}
        if len(unique_messages) == 1:
            # Consolidate: show one error panel + count
            affected = [r.get("project_alias", "unknown") for r in results]
            content = results[0].get("content", [])

            lines = [
                f"[bold]Status:[/bold] [bold red]ERROR[/bold red] (same error across {len(results)} projects)",
            ]
            for item in content:
                if isinstance(item, str):
                    lines.append(item)
                elif isinstance(item, dict):
                    lines.append(json.dumps(item, indent=2))
                else:
                    lines.append(str(item))

            lines.append(f"\n[dim]Affected projects: {', '.join(affected)}[/dim]")

            panel = Panel("\n".join(lines), title="Tool Error", expand=False)
            console.print(panel)
            return

    # Normal rendering: one panel per result
    for result in results:
        alias = result.get("project_alias", "unknown")
        is_error = result.get("isError", False)
        content = result.get("content", [])

        status_label = "[bold red]ERROR[/bold red]" if is_error else "[bold green]OK[/bold green]"
        lines = [f"[bold]Status:[/bold] {status_label}"]

        for item in content:
            if isinstance(item, str):
                lines.append(item)
            elif isinstance(item, dict):
                lines.append(json.dumps(item, indent=2))
            else:
                lines.append(str(item))

        panel = Panel("\n".join(lines), title=f"Result - {alias}", expand=False)
        console.print(panel)


_SHARING_TYPE_STYLES = {
    "organization": "bold cyan",
    "organization-project": "bold green",
    "data-science": "bold magenta",
}


def _project_label(alias: str, project_id: int, project_name: str) -> Text:
    """Create a styled project label. Unknown projects shown as dimmed #id."""
    if alias:
        return Text(alias, style="bold")
    label = Text(f"#{project_id}", style="dim")
    if project_name:
        label.append(f" ({project_name})", style="dim")
    return label


def format_lineage_table(console: Console, data: dict[str, Any]) -> None:
    """Render cross-project lineage data as Rich tables.

    Args:
        console: Rich Console instance.
        data: Dict with "edges", "shared_buckets", "linked_buckets", "summary", "errors".
    """
    edges = data.get("edges", [])
    shared_buckets = data.get("shared_buckets", [])
    linked_buckets = data.get("linked_buckets", [])
    summary = data.get("summary", {})
    errors = data.get("errors", [])

    # Show per-project errors
    for err in errors:
        console.print(
            f"[bold yellow]Warning:[/bold yellow] Project [bold]{err['project_alias']}[/bold]: "
            f"{err['message']}"
        )

    # Render summary
    _render_lineage_summary(console, summary)

    # Render edges table if available
    if edges:
        _render_edges_table(console, edges)
    elif shared_buckets or linked_buckets:
        # No edges but buckets exist -- show details
        if shared_buckets:
            _render_shared_buckets_table(console, shared_buckets)
        if linked_buckets:
            _render_linked_buckets_table(console, linked_buckets)
    elif not errors:
        console.print(
            "\nNo bucket sharing detected across queried projects."
        )


def _render_lineage_summary(console: Console, summary: dict[str, Any]) -> None:
    """Render a summary line for lineage results."""
    shared = summary.get("total_shared_buckets", 0)
    linked = summary.get("total_linked_buckets", 0)
    edge_count = summary.get("total_edges", 0)
    queried = summary.get("projects_queried", 0)

    console.print(
        f"\nFound [bold]{shared}[/bold] shared bucket(s) with "
        f"[bold]{edge_count}[/bold] link(s) across "
        f"[bold]{queried}[/bold] project(s)."
    )
    if linked:
        console.print(f"  [dim]{linked} linked bucket(s) detected.[/dim]")
    console.print()


def _render_edges_table(console: Console, edges: list[dict[str, Any]]) -> None:
    """Render the main edges table showing data flow between projects."""
    table = Table(title="Data Flow Edges")
    table.add_column("Source Project", style="bold magenta")
    table.add_column("Source Bucket", style="cyan")
    table.add_column("Sharing Type", style="dim")
    table.add_column("Target Project", style="bold magenta")
    table.add_column("Target Bucket", style="cyan")

    for edge in edges:
        source_label = _project_label(
            edge.get("source_project_alias", ""),
            edge.get("source_project_id", 0),
            edge.get("source_project_name", ""),
        )
        target_label = _project_label(
            edge.get("target_project_alias", ""),
            edge.get("target_project_id", 0),
            edge.get("target_project_name", ""),
        )
        sharing_type = edge.get("sharing_type", "")
        style = _SHARING_TYPE_STYLES.get(sharing_type, "")
        sharing_display = Text(sharing_type, style=style) if style else Text(sharing_type)

        table.add_row(
            source_label,
            edge.get("source_bucket_id", ""),
            sharing_display,
            target_label,
            edge.get("target_bucket_id", ""),
        )

    console.print(table)
    console.print()


def _render_shared_buckets_table(console: Console, shared_buckets: list[dict[str, Any]]) -> None:
    """Render a table of shared buckets (no linked targets found)."""
    table = Table(title="Shared Buckets (no linked targets found)")
    table.add_column("Project", style="bold magenta")
    table.add_column("Bucket ID", style="cyan")
    table.add_column("Bucket Name")
    table.add_column("Sharing Type", style="dim")

    for sb in shared_buckets:
        table.add_row(
            sb.get("project_alias", ""),
            sb.get("bucket_id", ""),
            sb.get("bucket_name", ""),
            sb.get("sharing_type", ""),
        )

    console.print(table)
    console.print()


def _render_linked_buckets_table(console: Console, linked_buckets: list[dict[str, Any]]) -> None:
    """Render a table of linked buckets (incoming links)."""
    table = Table(title="Linked Buckets (incoming)")
    table.add_column("Project", style="bold magenta")
    table.add_column("Bucket ID", style="cyan")
    table.add_column("Source Bucket", style="dim")
    table.add_column("Source Project", style="dim")
    table.add_column("Read-only", justify="center")

    for lb in linked_buckets:
        readonly = "[green]yes[/green]" if lb.get("is_readonly") else "[dim]no[/dim]"
        table.add_row(
            lb.get("project_alias", ""),
            lb.get("bucket_id", ""),
            lb.get("source_bucket_id", ""),
            lb.get("source_project_name", ""),
            readonly,
        )

    console.print(table)
    console.print()


def format_doctor_panel(console: Console, data: dict[str, Any]) -> None:
    """Render doctor check results as a Rich panel with colored status indicators.

    Args:
        console: Rich Console instance.
        data: Dict with "checks" list and "summary" dict from DoctorService.
    """
    status_icons = {
        "pass": "[bold green]PASS[/bold green]",
        "fail": "[bold red]FAIL[/bold red]",
        "warn": "[bold yellow]WARN[/bold yellow]",
    }

    checks = data.get("checks", [])
    lines = [
        f"  {status_icons.get(c['status'], '[dim]SKIP[/dim]')}  {c['name']}: {c['message']}"
        for c in checks
    ]

    summary = data.get("summary", {})
    parts = [f"{summary.get('total', 0)} checks"]
    if summary.get("passed"):
        parts.append(f"[green]{summary['passed']} passed[/green]")
    if summary.get("failed"):
        parts.append(f"[red]{summary['failed']} failed[/red]")
    if summary.get("warnings"):
        parts.append(f"[yellow]{summary['warnings']} warnings[/yellow]")

    lines.append("")
    lines.append(f"  Summary: {', '.join(parts)}")

    from rich.panel import Panel

    console.print(Panel("\n".join(lines), title="kbagent doctor", expand=False))
