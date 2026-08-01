"""Output formatting with JSON and Rich dual mode support."""

import contextlib
import json
import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .models import ErrorResponse, SuccessResponse


def _stdout_is_utf8() -> bool:
    """Whether ``sys.stdout`` already encodes as UTF-8 (or a UTF-8 alias)."""
    encoding = getattr(sys.stdout, "encoding", None)
    if not encoding:
        return False
    return encoding.lower().replace("-", "").replace("_", "") in ("utf8", "utf8sig")


def force_utf8_stdout() -> None:
    """Re-encode ``sys.stdout`` as UTF-8 for machine-readable output.

    Machine output (``--json``, ``kbagent http``, agent run-event streams) is
    written for a downstream consumer -- a file, a pipe, an LLM, ``jq`` -- so it
    must not depend on the console codepage of the terminal that happens to be
    attached. On Windows the inherited codepage is a legacy single-byte encoding
    (cp1250 on Czech/Polish/Hungarian locales), and a single non-ASCII character
    anywhere in the payload -- an arrow in a flow name, an accented config name,
    an emoji -- made ``sys.stdout.write`` raise ``UnicodeEncodeError`` and killed
    the command instead of printing JSON (issue #546).

    Unlike the ``kbagent serve`` startup banner (issue #522), transliterating to
    ASCII is *not* an option here: the banner is decoration, this is data.

    Best effort and idempotent: streams that cannot be reconfigured (a
    ``StringIO`` in tests, an already-detached stream) are left alone and the
    write-time fallback in :func:`write_machine_output` covers them.
    """
    if _stdout_is_utf8():
        return
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is None:
        return
    # Non-critical on failure: write_machine_output() still has a byte-level path.
    with contextlib.suppress(OSError, ValueError, LookupError):
        reconfigure(encoding="utf-8")


def write_machine_output(text: str) -> None:
    """Write machine-readable text to stdout as UTF-8, never as the console codepage.

    :func:`force_utf8_stdout` handles the normal case up front; this stays
    defensive for the streams it could not reconfigure. On
    ``UnicodeEncodeError`` the text is encoded to UTF-8 bytes and written to the
    underlying binary buffer, which bypasses the text layer's codec entirely.
    The text stream is flushed first so the bytes cannot overtake previously
    buffered output.

    Args:
        text: The already-serialized payload (typically JSON), including any
            trailing newline.
    """
    force_utf8_stdout()
    try:
        sys.stdout.write(text)
        return
    except UnicodeEncodeError as exc:
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is None:
            # No binary layer to fall back to (an exotic stream). Surfacing the
            # original error beats silently dropping or mangling machine output.
            raise exc

    # The failed write left nothing valid to flush, but anything written before
    # it must reach the stream ahead of the bytes below.
    with contextlib.suppress(UnicodeEncodeError):
        sys.stdout.flush()
    buffer.write(text.encode("utf-8"))
    buffer.flush()


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
        self, data: Any, human_formatter: Callable[[Console, Any], object] | None = None
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
            write_machine_output(response.model_dump_json(indent=2) + "\n")
        else:
            if human_formatter is not None:
                human_formatter(self.console, data)
            else:
                self.console.print(data)

    def error(
        self,
        message: str,
        error_code: str = "ERROR",
        project: str = "",
        retryable: bool = False,
        error_type: str = "",
        details: dict | None = None,
    ) -> None:
        """Output an error message.

        Args:
            message: Human-readable error description.
            error_code: Machine-readable error code.
            project: Project alias related to the error.
            retryable: Whether the operation can be retried.
            error_type: Broad error category. If empty, derived from error_code.
            details: Optional structured context (e.g. {"logTail": [...]}).
                When empty/None, the field is omitted from JSON output so
                consumers can key off presence.
        """
        if self.json_mode:
            if not error_type:
                from .errors import map_error_code_to_type

                error_type = map_error_code_to_type(error_code)
            err = ErrorResponse(
                code=error_code,
                error_type=error_type,
                message=message,
                project=project,
                retryable=retryable,
                details=details if details else None,
            )
            error_envelope = {
                "status": "error",
                "error": err.model_dump(exclude_none=True),
            }
            write_machine_output(json.dumps(error_envelope, indent=2) + "\n")
        else:
            self.err_console.print(f"[bold red]Error:[/bold red] {message}")

    def success(self, message: str) -> None:
        """Output a success message.

        Args:
            message: The success message to display.
        """
        if self.json_mode:
            response = SuccessResponse(status="ok", data={"message": message})
            write_machine_output(response.model_dump_json(indent=2) + "\n")
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
        table.add_column("Folder", style="dim")
        table.add_column("Last Modified", style="dim")
        table.add_column("Modified By", style="dim")

        for cfg in project_configs:
            # Format last_modified to shorter form if present
            last_mod = cfg.get("last_modified", "")
            if last_mod and "T" in last_mod:
                last_mod = last_mod.split("T")[0]  # just date part

            table.add_row(
                cfg["component_id"],
                cfg["component_type"],
                cfg["config_id"],
                cfg["config_name"],
                cfg.get("folder", ""),
                last_mod,
                cfg.get("last_modified_by", ""),
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
        config_str = json.dumps(configuration, indent=2)
        lines.append(f"\n[bold]Configuration:[/bold]\n{config_str}")

    # keboola.sandboxes annotation (issue #304 bod #3): make explicit that
    # ``parameters.id`` is NOT the Storage workspace ID, and show the real
    # mapping resolved by the command layer.
    annotation = data.get("sandbox_annotation")
    if annotation:
        sb_id = annotation.get("sandbox_service_id")
        ws_id = annotation.get("storage_workspace_id")
        lines.append("\n[bold yellow]Sandbox / Workspace mapping:[/bold yellow]")
        lines.append(
            f"  [dim]parameters.id (sandbox-service):[/dim] {sb_id if sb_id is not None else '[dim](absent)[/dim]'}"
        )
        if ws_id is not None:
            lines.append(
                f"  [bold]Storage workspace ID:[/bold] [green]{ws_id}[/green]  "
                f"[dim](use with `kbagent workspace detail --workspace-id {ws_id}`)[/dim]"
            )
        else:
            lines.append(
                "  [bold]Storage workspace ID:[/bold] [dim]none[/dim] "
                "[yellow](no workspace currently backed by this config -- orphan sandbox)[/yellow]"
            )
        lines.append(f"  [dim]{annotation.get('note', '')}[/dim]")

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


def _format_tool_params(schema: dict[str, Any]) -> str:
    """Format inputSchema properties as a compact param string.

    Required params are marked with *, optional are dim.
    Example: "sql_query*, query_name*"
    """
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    if not props:
        return "[dim](none)[/dim]"
    parts = []
    for name in props:
        if name in required:
            parts.append(f"[bold]{name}[/bold]*")
        else:
            parts.append(f"[dim]{name}[/dim]")
    return ", ".join(parts)


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
    table.add_column("CLI equivalent", style="green")
    table.add_column("Parameters")
    table.add_column("Multi-Project", justify="center")
    table.add_column("Description", max_width=60)

    for tool in tools:
        multi = "[green]yes[/green]" if tool.get("multi_project") else "[dim]no[/dim]"
        params_str = _format_tool_params(tool.get("inputSchema", {}))
        table.add_row(
            tool["name"],
            tool.get("cli_equivalent", ""),
            params_str,
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
        lines: list[str] = [f"[bold]Status:[/bold] {status_label}"]

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
        console.print("\nNo bucket sharing detected across queried projects.")


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


def format_branches_table(console: Console, data: dict[str, Any]) -> None:
    """Render a Rich table of development branches grouped by project alias.

    Args:
        console: Rich Console instance.
        data: Dict with "branches" (list of branch dicts) and "errors" (list of error dicts).
    """
    branches = data.get("branches", [])
    errors = data.get("errors", [])

    for err in errors:
        console.print(
            f"[bold yellow]Warning:[/bold yellow] Project [bold]{err['project_alias']}[/bold]: "
            f"{err['message']}"
        )

    if not branches:
        if not errors:
            console.print(
                "No branches found. Use [bold]kbagent project add[/bold] to connect a project first."
            )
        else:
            console.print("No branches retrieved (all projects failed).")
        return

    active_branches = data.get("active_branches", {})

    table = Table(title="Development Branches")
    table.add_column("Project", style="bold magenta")
    table.add_column("Branch ID", justify="right")
    table.add_column("Name", style="bold cyan")
    table.add_column("Default", justify="center")
    table.add_column("Active", justify="center")
    table.add_column("Description", style="dim", max_width=40)
    table.add_column("Created", style="dim")

    prev_alias = None
    for branch in branches:
        alias = branch.get("project_alias", "unknown")
        is_default = branch.get("isDefault", False)
        default_display = "[green]yes[/green]" if is_default else "[dim]no[/dim]"

        branch_id = branch.get("id")
        active_id = active_branches.get(alias)
        # Compare as int to handle potential type mismatch from API
        is_active = (
            branch_id is not None and active_id is not None and int(branch_id) == int(active_id)
        )
        active_display = "[bold green]>>>[/bold green]" if is_active else ""

        display_alias = alias if alias != prev_alias else ""
        prev_alias = alias

        table.add_row(
            display_alias,
            str(branch.get("id", "")),
            branch.get("name", ""),
            default_display,
            active_display,
            branch.get("description", ""),
            branch.get("created", ""),
        )

    console.print(table)
    console.print()


def format_branch_metadata_table(console: Console, data: dict[str, Any]) -> None:
    """Render branch metadata entries as a Rich table.

    Args:
        console: Rich Console instance.
        data: Dict with project_alias, branch_id, and metadata (list of entries).
    """
    entries = data.get("metadata", [])
    alias = data.get("project_alias", "unknown")
    branch_id = data.get("branch_id", "default")

    if not entries:
        console.print(
            f"No metadata on branch [bold]{branch_id}[/bold] of project "
            f"[bold magenta]{alias}[/bold magenta]."
        )
        return

    table = Table(title=f"Branch Metadata  -  project: {alias}  -  branch: {branch_id}")
    table.add_column("ID", justify="right", style="dim")
    table.add_column("Key", style="bold cyan")
    table.add_column("Value", max_width=60)
    table.add_column("Provider", style="dim")
    table.add_column("Timestamp", style="dim")

    for entry in entries:
        value = str(entry.get("value", ""))
        if "\n" in value:
            first_line = value.splitlines()[0]
            value = f"{first_line} [dim](+{len(value.splitlines()) - 1} more lines)[/dim]"
        table.add_row(
            str(entry.get("id", "")),
            str(entry.get("key", "")),
            value,
            str(entry.get("provider", "")),
            str(entry.get("timestamp", "")),
        )

    console.print(table)


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

    console.print(Panel("\n".join(lines), title="kbagent doctor", expand=False))


def format_workspaces_table(console: Console, data: dict[str, Any]) -> None:
    """Render a Rich table of workspaces grouped by project alias.

    Args:
        console: Rich Console instance.
        data: Dict with "workspaces" and "errors" lists.
    """
    workspaces = data.get("workspaces", [])
    errors = data.get("errors", [])

    for err in errors:
        console.print(
            f"[bold yellow]Warning:[/bold yellow] Project [bold]{err['project_alias']}[/bold]: "
            f"{err['message']}"
        )

    if not workspaces:
        if not errors:
            console.print(
                "No workspaces found. Use [bold]kbagent workspace create[/bold] to create one."
            )
        else:
            console.print("No workspaces retrieved (all projects failed).")
        return

    table = Table(title="Workspaces")
    table.add_column("Project", style="bold magenta")
    table.add_column("ID", justify="right")
    table.add_column("Name", style="bold cyan")
    table.add_column("Backend")
    table.add_column("Schema")
    # Login type + RO + QS surface the three Query-Service compatibility signals
    # that were previously invisible to data-app developers (issue #304).
    # Width-clamped because ``snowflake-service-keypair`` is the longest known
    # value; clamp leaves room for the wider table on standard terminals.
    table.add_column("Login Type", max_width=24)
    table.add_column("RO", justify="center")
    table.add_column("QS", justify="center")
    table.add_column("Created", style="dim")

    prev_alias = None
    for ws in workspaces:
        alias = ws.get("project_alias", "unknown")
        display_alias = alias if alias != prev_alias else ""
        prev_alias = alias

        login_type = ws.get("login_type", "") or "[dim]?[/dim]"
        ro_cell = "[green]yes[/green]" if ws.get("read_only") else "no"
        if ws.get("qs_compatible") is True:
            qs_cell = "[green]yes[/green]"
        elif ws.get("login_type"):
            # We have a loginType but it is not on the confirmed whitelist.
            # Yellow rather than red: the policy varies per stack (see
            # snowflake-legacy-service in constants.py) and we do not want to
            # actively discourage a workspace that might in fact work.
            qs_cell = "[yellow]?[/yellow]"
        else:
            qs_cell = "[dim]?[/dim]"

        table.add_row(
            display_alias,
            str(ws.get("workspace_id", ws.get("id", ""))),
            ws.get("name", ""),
            ws.get("backend", ""),
            ws.get("schema", ""),
            login_type,
            ro_cell,
            qs_cell,
            ws.get("created", ""),
        )

    console.print(table)
    console.print(
        "[dim]RO = read-only storage access. QS = Query Service compatible "
        "(yes = confirmed whitelist, ? = loginType not on confirmed list, "
        "may still work). See `kbagent workspace list --qs-compatible` to "
        "filter to data-app-ready workspaces.[/dim]"
    )
    console.print()


def format_query_results(console: Console, data: dict[str, Any]) -> None:
    """Render SQL query results.

    Shows query status plus, per statement, a Rich table built from the
    structured ``columns``/``rows`` returned by the fast inline path. Falls back
    to a CSV preview when only ``csv_data`` is present (the ``--full`` export
    path, which returns a CSV string without structured columns).

    Args:
        console: Rich Console instance.
        data: Dict with query execution results.
    """
    alias = data.get("project_alias", "unknown")
    workspace_id = data.get("workspace_id", "")
    status = data.get("status", "unknown")

    console.print(
        Panel(
            f"[bold]Project:[/bold] {alias}\n"
            f"[bold]Workspace:[/bold] {workspace_id}\n"
            f"[bold]Status:[/bold] {status}",
            title=f"Query Results - Workspace {workspace_id}",
            expand=False,
        )
    )

    statements = data.get("statements", [])
    for i, stmt in enumerate(statements):
        console.print(
            f"\n[bold]Statement {i + 1}:[/bold] "
            f"{stmt.get('status', 'unknown')} ・ {stmt.get('rows_affected', 0)} rows"
        )
        _render_statement_result(console, stmt)


def _render_statement_result(console: Console, stmt: dict[str, Any]) -> None:
    """Render a single statement's result set (structured table or CSV preview)."""
    columns = stmt.get("columns")
    rows = stmt.get("rows")
    if columns and rows is not None:
        table = Table(show_lines=False)
        for col in columns:
            table.add_column(str(col.get("name", "")))
        for row in rows:
            table.add_row(*["" if value is None else str(value) for value in row])
        console.print(table)
        if stmt.get("truncated"):
            total = stmt.get("total_rows")
            shown = stmt.get("row_count", len(rows))
            suffix = f" of {total}" if total is not None else ""
            console.print(
                f"  [dim]Showing first {shown}{suffix} rows. "
                f"Use --full for the complete result set.[/dim]"
            )
        return

    # Fallback: --full export path returns a CSV string with no structured columns.
    csv_data = stmt.get("csv_data", "")
    if not csv_data:
        return
    csv_lines = csv_data.strip().split("\n")
    preview_count = min(len(csv_lines), 11)  # header + 10 rows
    console.print("  [bold]Results:[/bold]")
    for csv_line in csv_lines[:preview_count]:
        console.print(f"    {csv_line}")
    if len(csv_lines) > preview_count:
        console.print(f"    ... ({len(csv_lines) - preview_count} more rows)")


def format_search_results(console: Console, data: dict[str, Any]) -> None:
    """Render search results as a Rich table with match locations.

    Args:
        console: Rich Console instance.
        data: Dict with "matches", "errors", and "stats".
    """
    matches = data.get("matches", [])
    errors = data.get("errors", [])
    stats = data.get("stats", {})

    for err in errors:
        console.print(
            f"[bold yellow]Warning:[/bold yellow] Project [bold]{err['project_alias']}[/bold]: "
            f"{err['message']}"
        )

    if not matches:
        console.print(
            f"No matches found. Searched {stats.get('configs_searched', 0)} "
            f"configurations across {stats.get('projects_searched', 0)} project(s)."
        )
        return

    table = Table(title="Search Results")
    table.add_column("Project", style="bold cyan")
    table.add_column("Component", style="dim")
    table.add_column("Config ID", justify="right")
    table.add_column("Config Name")
    table.add_column("Hits", justify="right", style="bold yellow")
    table.add_column("Match Locations", style="dim", max_width=60)

    for match in matches:
        locations = match.get("match_locations", [])
        # Show first 3 locations, truncate the rest
        display_locations = ", ".join(locations[:3])
        if len(locations) > 3:
            display_locations += f", ... (+{len(locations) - 3})"

        table.add_row(
            match["project_alias"],
            match["component_id"],
            match["config_id"],
            match["config_name"],
            str(match.get("match_count", len(locations))),
            display_locations,
        )

    console.print(table)
    console.print(
        f"\n[bold]{stats.get('matches_found', 0)}[/bold] matching config(s) "
        f"in {stats.get('configs_searched', 0)} searched "
        f"across {stats.get('projects_searched', 0)} project(s)."
    )
