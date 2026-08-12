"""Lineage commands - column-level dependency analysis across projects.

Thin CLI layer: parses arguments, calls DeepLineageService, formats output.
No business logic belongs here.

Four subcommands:
  build -- scan sync'd projects, build lineage graph, save cache
  show  -- query upstream/downstream from cached graph
  serve -- start local web server with interactive lineage browser
"""

import http.server
import json
import re
import threading
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import typer

from ..errors import ErrorCode
from ..services.deep_lineage_service import DeepLineageService, LineageGraph
from ._helpers import (
    check_cli_permission,
    get_formatter,
    get_service,
)

lineage_app = typer.Typer(
    help="Column-level data lineage across projects.\n\n"
    "Build a dependency graph from sync'd data, then query upstream/downstream."
)


@lineage_app.callback(invoke_without_command=True)
def _lineage_callback(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "lineage")
    if ctx.invoked_subcommand is None:
        # No subcommand -> show help
        click_cmd = typer.main.get_command(lineage_app)
        ctx_help = click_cmd.make_context("lineage", [])
        typer.echo(click_cmd.get_help(ctx_help))


# -- lineage build ---------------------------------------------------------


@lineage_app.command("build")
def lineage_build(
    ctx: typer.Context,
    directory: Path = typer.Option(
        Path("."),
        "--directory",
        "-d",
        help="Root directory with sync'd projects (default: current directory).",
    ),
    output: Path = typer.Option(
        ...,
        "--output",
        "-o",
        help="Output JSON file for the lineage graph (required).",
    ),
    ai: bool = typer.Option(
        False,
        "--ai",
        help="Generate AI task file for SQL/Python analysis (2-step: AI processes, then re-build).",
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="Sync pull all projects first, then rebuild.",
    ),
) -> None:
    """Build column-level lineage graph from sync'd data.

    Scans all sync'd projects (from `sync pull --all-projects`), detects
    table dependencies via config mappings and SQL parsing, and saves the
    graph to a JSON cache file for fast queries with `lineage show`.

    AI-enhanced analysis is a 2-step process:

      1. kbagent lineage build -d /path -o lineage.json --ai
         (builds deterministic graph + generates .lineage_ai_tasks.json)

      2. AI agent reads the task file, analyzes each code file from disk,
         writes results to .lineage_ai_results.json

      3. kbagent lineage build -d /path -o lineage.json
         (automatically applies AI results if .lineage_ai_results.json exists)
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "deep_lineage_service")

    root = directory.resolve()
    if not root.is_dir():
        formatter.error(message=f"Directory not found: {root}", error_code=ErrorCode.DIR_NOT_FOUND)
        raise typer.Exit(code=1)

    # --refresh: sync pull all projects first
    if refresh:
        if not formatter.json_mode:
            formatter.console.print("[bold]Syncing all projects...[/bold]")
        sync_service = get_service(ctx, "sync_service")
        sync_result = sync_service.pull_all(base_dir=root)
        summary = sync_result.get("summary", {})
        if not formatter.json_mode:
            formatter.console.print(
                f"  Synced {summary.get('success', 0)}/{summary.get('total', 0)} projects"
                f" ({summary.get('failed', 0)} failed)\n"
            )

    result = service.build_lineage(root, generate_ai_tasks=ai)

    try:
        with open(output, "w") as f:
            json.dump(result, f, indent=2)
    except OSError as exc:
        formatter.error(
            message=f"Cannot write output file '{output}': {exc}",
            error_code=ErrorCode.WRITE_ERROR,
        )
        raise typer.Exit(code=1) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        summary = result.get("summary", {})
        formatter.console.print("\n[bold]Lineage graph built[/bold]")
        formatter.console.print(f"  Tables: {summary.get('tables', 0)}")
        formatter.console.print(f"  Configurations: {summary.get('configurations', 0)}")
        formatter.console.print(f"  Edges: {summary.get('edges', 0)}")
        if summary.get("detection_methods"):
            formatter.console.print("\n  Detection methods:")
            for k, v in sorted(summary["detection_methods"].items(), key=lambda x: -x[1]):
                formatter.console.print(f"    {k}: {v}")
        # AI status
        ai_status = result.get("ai_status", {})
        if ai_status.get("ai_results_applied"):
            formatter.console.print(
                f"\n  AI results applied: {ai_status.get('ai_edges_added', 0)} edges added"
            )
        if ai_status.get("ai_tasks_generated"):
            formatter.console.print(
                f"\n  [bold]AI tasks generated: {ai_status['ai_tasks_generated']}[/bold]"
                f" (already done: {ai_status.get('ai_already_done', 0)})"
            )
            formatter.console.print(f"  Task file: {ai_status['ai_tasks_file']}")
            formatter.console.print(
                "  Next: let your AI agent process the tasks, then re-run this command."
            )
        # Surface warnings (e.g. empty scan) so users notice layout issues.
        for warning in result.get("warnings", []) or []:
            formatter.console.print(f"\n[yellow]Warning:[/yellow] {warning}")
        formatter.console.print(f"\n  Saved to: {output}")


# -- lineage info ----------------------------------------------------------


@lineage_app.command("info")
def lineage_info(
    ctx: typer.Context,
    load: Path = typer.Option(
        ...,
        "--load",
        "-l",
        help="Lineage JSON cache file (from `lineage build`).",
    ),
) -> None:
    """Show what's in a cached lineage graph.

    Displays per-project breakdown (tables, configs) and lists the most
    connected tables -- good starting points for upstream/downstream queries.

    Example:

      kbagent lineage info -l lineage.json
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "deep_lineage_service")

    if not load.exists():
        formatter.error(
            message=f"Cache file not found: {load}", error_code=ErrorCode.FILE_NOT_FOUND
        )
        raise typer.Exit(code=1)

    graph = service.load_from_cache(load)

    if formatter.json_mode:
        formatter.output(graph.to_dict())
        return

    summary = graph.summary()
    formatter.console.print("\n[bold]Lineage Graph Contents[/bold]")
    formatter.console.print(f"  Tables: {summary.get('tables', 0)}")
    formatter.console.print(f"  Configurations: {summary.get('configurations', 0)}")
    formatter.console.print(f"  Edges: {summary.get('edges', 0)}")
    if summary.get("detection_methods"):
        formatter.console.print("\n  Detection methods:")
        for k, v in sorted(summary["detection_methods"].items(), key=lambda x: -x[1]):
            formatter.console.print(f"    {k}: {v}")

    # Per-project breakdown
    proj_tables: dict[str, int] = {}
    proj_configs: dict[str, int] = {}
    for t in graph.tables.values():
        proj_tables[t.project_alias] = proj_tables.get(t.project_alias, 0) + 1
    for c in graph.configurations.values():
        proj_configs[c.project_alias] = proj_configs.get(c.project_alias, 0) + 1
    all_projects = sorted(set(proj_tables) | set(proj_configs))
    if all_projects:
        formatter.console.print("\n  [bold]Projects:[/bold]")
        for proj in all_projects:
            nt = proj_tables.get(proj, 0)
            nc = proj_configs.get(proj, 0)
            formatter.console.print(f"    {proj:40s} {nt:4d} tables, {nc:4d} configs")

    # Most connected tables
    edge_counts: dict[str, int] = {}
    for e in graph.edges:
        for fqn in (e.source_fqn, e.target_fqn):
            if fqn in graph.tables:
                edge_counts[fqn] = edge_counts.get(fqn, 0) + 1
    top = sorted(edge_counts.items(), key=lambda x: -x[1])[:15]
    if top:
        formatter.console.print(
            "\n  [bold]Most connected tables[/bold]"
            " (use with [cyan]lineage show --upstream/--downstream[/cyan]):"
        )
        for fqn, count in top:
            t = graph.tables[fqn]
            formatter.console.print(f"    {fqn:60s} {count:3d} edges, {t.rows_count:>12,} rows")


# -- lineage show ----------------------------------------------------------


@lineage_app.command("show")
def lineage_show(
    ctx: typer.Context,
    load: Path = typer.Option(
        ...,
        "--load",
        "-l",
        help="Lineage JSON cache file (from `lineage build`).",
    ),
    upstream: str | None = typer.Option(
        None,
        "--upstream",
        help="Show upstream dependencies. Use 'project:table_id' or just 'table_id'.",
    ),
    downstream: str | None = typer.Option(
        None,
        "--downstream",
        help="Show downstream dependents. Use 'project:table_id' or just 'table_id'.",
    ),
    column: str | None = typer.Option(
        None,
        "--column",
        "-c",
        help="Trace a specific column (use with --upstream/--downstream).",
    ),
    columns: bool = typer.Option(
        False,
        "--columns",
        help="Show column-level mapping detail on edges.",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        "-p",
        help="Project alias filter for queries.",
    ),
    depth: int = typer.Option(10, "--depth", help="Max traversal depth (default: 10)."),
    format: str = typer.Option(
        "text",
        "--format",
        "-f",
        help="Output format: text, mermaid, html, or er (entity-relationship).",
    ),
) -> None:
    """Query upstream/downstream dependencies from a cached lineage graph.

    Requires a lineage cache file built with `lineage build`.

    Node identifiers for --upstream/--downstream:

      Full FQN:    project-alias:bucket_id.table_name

      Table only:  bucket_id.table_name  (auto-resolves, warns if ambiguous)

    Output formats (--format):

      text     Rich tree (default)

      mermaid  Mermaid flowchart source code

      html     Self-contained HTML file with embedded mermaid diagram

    Examples:

      kbagent lineage show -l lineage.json --downstream "project:table"

      kbagent lineage show -l lineage.json --upstream "project:table" --columns

      kbagent lineage show -l lineage.json --upstream "project:table" -c "col_name"

      kbagent lineage show -l lineage.json --downstream "project:table" -f mermaid

      kbagent lineage show -l lineage.json --downstream "project:table" -f html
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "deep_lineage_service")

    valid_formats = ("text", "mermaid", "html", "er")
    if format not in valid_formats:
        formatter.error(
            message=f"Invalid format '{format}'. Must be one of: {', '.join(valid_formats)}",
            error_code=ErrorCode.INVALID_FORMAT,
        )
        raise typer.Exit(code=2)

    if not load.exists():
        formatter.error(
            message=f"Cache file not found: {load}", error_code=ErrorCode.FILE_NOT_FOUND
        )
        raise typer.Exit(code=1)

    graph = service.load_from_cache(load)

    if not upstream and not downstream:
        formatter.error(
            message="Specify --upstream or --downstream to query.\n"
            "Use `kbagent lineage info -l FILE` to see what's in the graph.",
            error_code=ErrorCode.MISSING_QUERY,
        )
        raise typer.Exit(code=2)

    display_opts = {"show_columns": columns, "filter_column": column}

    # Both directions render identically; only the service call and the label
    # differ. Passing both --upstream and --downstream emits both, in order.
    for direction, identifier in (("upstream", upstream), ("downstream", downstream)):
        if not identifier:
            continue
        query = service.query_upstream if direction == "upstream" else service.query_downstream
        query_result = query(graph, identifier, project or "", depth)
        if "error" in query_result:
            suggestions = query_result.get("suggestions", [])
            msg = query_result["error"]
            if suggestions:
                msg += "\nDid you mean: " + ", ".join(suggestions[:5])
            formatter.error(message=msg, error_code=ErrorCode.NODE_NOT_FOUND)
            raise typer.Exit(code=1)

        _emit_query_warnings(formatter, query_result)

        if formatter.json_mode:
            if column:
                query_result = _filter_column_json(query_result, column)
            formatter.output(query_result)
        elif format in ("mermaid", "html", "er"):
            _output_mermaid_or_html(formatter, service, graph, query_result, direction, format)
        else:
            _format_lineage_tree(formatter, graph, query_result, direction, **display_opts)


# -- Output formatting helpers ----------------------------------------------


def _emit_query_warnings(formatter, query_result: dict) -> None:
    """Print non-fatal warnings carried by a lineage query result.

    Currently only the ambiguous-identifier warning (the same table id exists
    in several projects). ``formatter.warning`` is a no-op in JSON mode --
    there the warnings ride along in the emitted payload instead.
    """
    for warning in query_result.get("warnings", []):
        formatter.warning(warning)


def _output_mermaid_or_html(
    formatter,
    service,
    graph,
    query_result: dict,
    direction: str,
    output_format: str,
) -> None:
    """Render query result as mermaid, html, or er diagram and output it."""
    from ..services.deep_lineage_service import DeepLineageService

    node_fqn = query_result["node"]
    edges = query_result.get("edges", [])

    if output_format == "er":
        er_code = DeepLineageService.render_er_diagram(edges, graph, node_fqn)
        typer.echo(er_code)
        return

    mermaid_code = DeepLineageService.render_mermaid(edges, graph, direction, node_fqn)

    if output_format == "mermaid":
        typer.echo(mermaid_code)
    elif output_format == "html":
        title = f"Lineage {direction} of {node_fqn}"
        html_content = DeepLineageService.render_html(mermaid_code, title)
        sanitized_node = re.sub(r"[^a-zA-Z0-9_]", "_", node_fqn)
        filename = f"lineage_{direction}_{sanitized_node}.html"
        try:
            with open(filename, "w") as f:
                f.write(html_content)
            if not formatter.json_mode:
                formatter.console.print(f"HTML lineage diagram saved to: {filename}")
        except OSError as exc:
            formatter.error(
                message=f"Cannot write HTML file '{filename}': {exc}",
                error_code=ErrorCode.WRITE_ERROR,
            )
            raise typer.Exit(code=1) from None


def _filter_column_json(result: dict, column_name: str) -> dict:
    """Filter JSON query result to only edges relevant to a specific column."""
    filtered_edges = []
    col_lower = column_name.lower()
    for edge in result.get("edges", []):
        col_map = edge.get("column_mapping", {})
        edge_columns = edge.get("columns", [])
        mapped_keys = [k for k in col_map if k.lower() == col_lower]
        mapped_vals = [k for k, v in col_map.items() if v.lower().endswith(f".{col_lower}")]
        col_match = any(c.lower() == col_lower for c in edge_columns)
        if mapped_keys or mapped_vals or col_match:
            relevant_map = {
                k: v for k, v in col_map.items() if k in mapped_keys or k in mapped_vals
            }
            edge_copy = dict(edge)
            if relevant_map:
                edge_copy["column_mapping"] = relevant_map
            filtered_edges.append(edge_copy)
        elif not col_map and not edge_columns:
            filtered_edges.append(edge)
    result_copy = dict(result)
    result_copy["edges"] = filtered_edges
    result_copy["column_filter"] = column_name
    return result_copy


def _format_lineage_tree(
    formatter,
    graph,
    result: dict,
    direction: str,
    show_columns: bool = False,
    filter_column: str | None = None,
) -> None:
    """Format lineage query result as a human-readable tree."""
    from ..services.deep_lineage_service import LineageGraph

    node_fqn = result["node"]
    node_info = result.get("node_info", {})
    edges = result.get("edges", [])

    arrow = "<-" if direction == "upstream" else "->"
    label = "Upstream dependencies" if direction == "upstream" else "Downstream dependents"

    node_type = node_info.get("type", "unknown")
    if node_type == "table":
        n_cols = node_info.get("columns", 0)
        rows = node_info.get("rows", 0)
        desc = f"[table] {node_fqn} ({n_cols} cols, {rows:,} rows)"
    elif node_info.get("name"):
        desc = f"[{node_type}] {node_info['name']} ({node_info.get('component', '')})"
    else:
        desc = f"[{node_type}] {node_fqn}"

    header = f"\n[bold]{label} of {desc}[/bold]"
    if filter_column:
        header += f"  [dim](column: {filter_column})[/dim]"
    formatter.console.print(header + "\n")

    if not edges:
        formatter.console.print("  (none found)")
        return

    col_lower = filter_column.lower() if filter_column else None

    for edge in sorted(edges, key=lambda e: e["depth"]):
        col_map = edge.get("column_mapping", {})
        edge_columns = edge.get("columns", [])

        if col_lower:
            has_in_map = any(
                k.lower() == col_lower or v.lower().endswith(f".{col_lower}")
                for k, v in col_map.items()
            )
            has_in_cols = any(c.lower() == col_lower for c in edge_columns)
            is_structural = not col_map and not edge_columns
            if not has_in_map and not has_in_cols and not is_structural:
                continue

        indent = "  " * edge["depth"]
        target_fqn = edge["source"] if direction == "upstream" else edge["target"]

        if isinstance(graph, LineageGraph):
            if target_fqn in graph.tables:
                t = graph.tables[target_fqn]
                node_desc = f"[table] {target_fqn} ({len(t.columns)} cols, {t.rows_count:,} rows)"
            elif target_fqn in graph.configurations:
                c = graph.configurations[target_fqn]
                node_desc = (
                    f"[{c.component_type}] {c.project_alias}:{c.config_name} ({c.component_id})"
                )
            else:
                node_desc = target_fqn
        else:
            node_desc = target_fqn

        col_hint = ""
        if not show_columns and edge_columns:
            col_list = edge_columns[:5]
            suffix = f"... +{len(edge_columns) - 5}" if len(edge_columns) > 5 else ""
            col_hint = f" [{', '.join(col_list)}{suffix}]"

        formatter.console.print(f"{indent}{arrow} ({edge['detection']}) {node_desc}{col_hint}")

        if show_columns and col_map:
            map_indent = "  " * (edge["depth"] + 1)
            items = list(col_map.items())
            if col_lower:
                items = [
                    (k, v)
                    for k, v in items
                    if k.lower() == col_lower or v.lower().endswith(f".{col_lower}")
                ]
            for out_col, src_expr in items:
                src_short = src_expr.split(".")[-1] if "." in src_expr else src_expr
                src_table = ".".join(src_expr.split(".")[:-1]) if "." in src_expr else ""
                if src_table:
                    formatter.console.print(
                        f"{map_indent}[dim]{out_col}[/dim] <- {src_table}.[bold]{src_short}[/bold]"
                    )
                else:
                    formatter.console.print(f"{map_indent}[dim]{out_col}[/dim] <- {src_expr}")
        elif show_columns and edge_columns:
            map_indent = "  " * (edge["depth"] + 1)
            show_cols = edge_columns
            if col_lower:
                show_cols = [c for c in edge_columns if c.lower() == col_lower]
            for c in show_cols[:10]:
                formatter.console.print(f"{map_indent}[dim]{c}[/dim]")
            if len(show_cols) > 10:
                formatter.console.print(f"{map_indent}[dim]... +{len(show_cols) - 10} more[/dim]")


# -- lineage serve ---------------------------------------------------------

_LINEAGE_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Keboola Lineage Browser</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  height: 100vh; overflow: hidden; display: flex; background: #fff; color: #333;
}
/* -- Sidebar -- */
#sidebar {
  width: 280px; min-width: 280px; background: #f8f9fa;
  border-right: 1px solid #e0e0e0; display: flex; flex-direction: column;
  height: 100vh; overflow: hidden;
}
#sidebar-header {
  padding: 16px; border-bottom: 1px solid #e0e0e0;
}
#sidebar-header h1 {
  font-size: 15px; font-weight: 700; color: #1a73e8; margin-bottom: 12px;
}
#project-select {
  width: 100%; padding: 6px 8px; border: 1px solid #dadce0; border-radius: 4px;
  font-size: 13px; background: #fff; color: #333; outline: none;
}
#project-select:focus { border-color: #1a73e8; }

/* Tabs */
#tabs {
  display: flex; border-bottom: 1px solid #e0e0e0;
}
.tab {
  flex: 1; padding: 8px 0; text-align: center; font-size: 12px; font-weight: 600;
  cursor: pointer; color: #5f6368; border-bottom: 2px solid transparent;
  background: none; border-top: none; border-left: none; border-right: none;
}
.tab.active { color: #1a73e8; border-bottom-color: #1a73e8; }
.tab:hover { background: #e8eaed; }

/* Search */
#node-search {
  margin: 8px 12px; padding: 6px 8px; border: 1px solid #dadce0; border-radius: 4px;
  font-size: 13px; outline: none; width: calc(100% - 24px);
}
#node-search:focus { border-color: #1a73e8; }

/* Node list */
#node-list {
  flex: 1; overflow-y: auto; padding: 0;
}
.node-item {
  padding: 6px 12px; cursor: pointer; font-size: 12px; color: #333;
  border-bottom: 1px solid #f0f0f0; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis;
}
.node-item:hover { background: #e8f0fe; }
.node-item.selected { background: #d2e3fc; font-weight: 600; }
.node-item .node-meta {
  font-size: 11px; color: #888; margin-top: 1px;
}

/* Controls below list */
#query-controls {
  padding: 12px; border-top: 1px solid #e0e0e0; background: #f8f9fa;
}
#query-controls label { font-size: 12px; font-weight: 600; color: #5f6368; }
.radio-group {
  display: flex; gap: 12px; margin: 4px 0 8px 0;
}
.radio-group label { font-weight: 400; font-size: 12px; cursor: pointer; }
.radio-group input { margin-right: 3px; }
#depth-row { display: flex; align-items: center; gap: 8px; }
#depth-slider { flex: 1; }
#depth-value { font-size: 12px; color: #333; min-width: 16px; }

/* -- Main area -- */
#main {
  flex: 1; display: flex; flex-direction: column; overflow: hidden;
}
#main-header {
  padding: 12px 20px; border-bottom: 1px solid #e0e0e0;
  display: flex; align-items: center; justify-content: space-between;
  min-height: 48px; background: #fff;
}
#main-header h2 {
  font-size: 14px; font-weight: 600; color: #333; margin: 0;
}
#main-stats {
  font-size: 12px; color: #888;
}
#export-buttons {
  display: flex; gap: 6px;
}
#export-buttons button {
  padding: 4px 10px; font-size: 11px; border: 1px solid #dadce0;
  border-radius: 4px; background: #fff; color: #333; cursor: pointer;
}
#export-buttons button:hover { background: #f1f3f4; }

/* Diagram area */
#diagram-area {
  flex: 1; overflow: auto; padding: 20px; display: flex;
  align-items: flex-start; justify-content: center;
}
#diagram-area .mermaid-container {
  max-width: 100%; overflow: auto;
}
#diagram-area .mermaid-container svg {
  max-width: none;
}
#placeholder {
  color: #999; font-size: 14px; text-align: center; margin-top: 100px;
}
#placeholder p { margin: 8px 0; }

/* Loading indicator */
#loading {
  display: none; color: #1a73e8; font-size: 13px; text-align: center;
  margin-top: 80px;
}

/* Responsive: collapse sidebar on narrow screens */
@media (max-width: 700px) {
  #sidebar { width: 220px; min-width: 220px; }
}
@media (max-width: 500px) {
  body { flex-direction: column; }
  #sidebar { width: 100%; min-width: 100%; height: 40vh; }
  #main { height: 60vh; }
}
</style>
</head>
<body>

<!-- Sidebar -->
<div id="sidebar">
  <div id="sidebar-header">
    <h1>Keboola Lineage Browser</h1>
    <select id="project-select"><option value="">Loading...</option></select>
  </div>
  <div id="tabs">
    <button class="tab active" data-type="tables">Tables</button>
    <button class="tab" data-type="configs">Configs</button>
  </div>
  <input id="node-search" type="text" placeholder="Filter nodes..." autocomplete="off">
  <div id="node-list"></div>
  <div id="query-controls">
    <label>Direction</label>
    <div class="radio-group">
      <label><input type="radio" name="direction" value="upstream" checked> Upstream</label>
      <label><input type="radio" name="direction" value="downstream"> Downstream</label>
      <label><input type="radio" name="direction" value="both"> Both</label>
    </div>
    <label>Depth</label>
    <div id="depth-row">
      <input type="range" id="depth-slider" min="1" max="10" value="3">
      <span id="depth-value">3</span>
    </div>
  </div>
</div>

<!-- Main -->
<div id="main">
  <div id="main-header">
    <h2 id="diagram-title">Select a node to explore lineage</h2>
    <span id="main-stats"></span>
    <div id="export-buttons" style="display:none">
      <label style="font-size:12px;margin-right:8px;cursor:pointer">
        <input type="radio" name="view-mode" value="flow" checked> Flow
      </label>
      <label style="font-size:12px;margin-right:12px;cursor:pointer">
        <input type="radio" name="view-mode" value="er"> ER
      </label>
      <label style="font-size:12px;margin-right:12px;cursor:pointer">
        <input type="checkbox" id="show-columns"> Columns
      </label>
      <button id="btn-mermaid">Download Mermaid</button>
      <button id="btn-json">Download JSON</button>
      <button id="btn-html">Download HTML</button>
    </div>
  </div>
  <div id="legend" style="display:none;padding:4px 16px;font-size:11px;background:#f8f9fa;border-bottom:1px solid #e0e0e0;gap:16px;flex-wrap:wrap;align-items:center">
    <span><span style="display:inline-block;width:12px;height:12px;background:#e1f5fe;border:2px solid #0288d1;border-radius:2px;vertical-align:middle"></span> Table</span>
    <span><span style="display:inline-block;width:12px;height:12px;background:#e8f5e9;border:2px solid #388e3c;border-radius:2px;vertical-align:middle"></span> Configuration</span>
    <span><span style="display:inline-block;width:12px;height:12px;background:#f3e5f5;border:2px solid #7b1fa2;border-radius:2px;vertical-align:middle"></span> Table from another project</span>
    <span style="color:#888">Edges: input_mapping | output_mapping | sql_tokenizer | bucket_sharing | ai</span>
  </div>
  <div id="diagram-area">
    <div id="placeholder">
      <p>Choose a project and click a table or configuration to visualize its lineage.</p>
      <p style="font-size:12px;color:#bbb">
        Use the sidebar to browse nodes, then click to query upstream or downstream dependencies.
      </p>
    </div>
    <div id="loading">Querying lineage...</div>
    <div id="zoom-controls" style="display:none;position:absolute;top:60px;right:20px;z-index:10">
      <button onclick="zoomDiagram(1.2)" title="Zoom in" style="padding:4px 10px;font-size:18px;cursor:pointer">+</button>
      <button onclick="zoomDiagram(1/1.2)" title="Zoom out" style="padding:4px 10px;font-size:18px;cursor:pointer">&minus;</button>
      <button onclick="zoomDiagram(0)" title="Reset zoom" style="padding:4px 10px;font-size:12px;cursor:pointer">Reset</button>
    </div>
    <div id="mermaid-output" class="mermaid-container" style="overflow:auto;transform-origin:top left"></div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<script>
(function() {
  // State
  var allData = null;       // full lineage data from /data.json
  var edgeCounts = {};      // fqn -> number of connections
  var currentTab = "tables";
  var selectedProject = "";
  var selectedNode = null;  // FQN of the selected node
  var lastQueryResult = null;
  var lastMermaidCode = null;
  var renderCounter = 0;    // unique ID for mermaid renders

  // DOM refs
  var projectSelect = document.getElementById("project-select");
  var nodeSearch = document.getElementById("node-search");
  var nodeList = document.getElementById("node-list");
  var depthSlider = document.getElementById("depth-slider");
  var depthValue = document.getElementById("depth-value");
  var diagramTitle = document.getElementById("diagram-title");
  var mainStats = document.getElementById("main-stats");
  var exportBtns = document.getElementById("export-buttons");
  var mermaidOutput = document.getElementById("mermaid-output");
  var placeholder = document.getElementById("placeholder");
  var loading = document.getElementById("loading");

  // Initialize mermaid
  mermaid.initialize({
    startOnLoad: false,
    theme: "default",
    flowchart: { useMaxWidth: false, htmlLabels: true },
    securityLevel: "loose"
  });

  // -- Data loading --
  fetch("/data.json")
    .then(function(r) { return r.json(); })
    .then(function(data) {
      allData = data;
      // Pre-compute edge counts per node
      edgeCounts = {};
      (data.edges || []).forEach(function(e) {
        edgeCounts[e.source_fqn] = (edgeCounts[e.source_fqn] || 0) + 1;
        edgeCounts[e.target_fqn] = (edgeCounts[e.target_fqn] || 0) + 1;
      });
      populateProjects();
    })
    .catch(function(err) {
      placeholder.innerHTML = "<p style='color:#d93025'>Failed to load lineage data: " +
        err.message + "</p>";
    });

  function populateProjects() {
    var projects = {};
    var tables = allData.tables || {};
    var configs = allData.configurations || {};
    for (var fqn in tables) {
      var pa = tables[fqn].project_alias || fqn.split(":")[0] || "";
      if (pa) projects[pa] = true;
    }
    for (var cfqn in configs) {
      var cpa = configs[cfqn].project_alias || cfqn.split(":")[0] || "";
      if (cpa) projects[cpa] = true;
    }
    var sorted = Object.keys(projects).sort();
    projectSelect.innerHTML = '<option value="">-- Select project --</option>';
    for (var i = 0; i < sorted.length; i++) {
      var opt = document.createElement("option");
      opt.value = sorted[i];
      opt.textContent = sorted[i];
      projectSelect.appendChild(opt);
    }
    // Auto-select first project if only one
    if (sorted.length === 1) {
      projectSelect.value = sorted[0];
      selectedProject = sorted[0];
      renderNodeList();
    }
  }

  // -- Event handlers --
  projectSelect.addEventListener("change", function() {
    selectedProject = this.value;
    selectedNode = null;
    nodeSearch.value = "";
    renderNodeList();
  });

  document.querySelectorAll(".tab").forEach(function(tab) {
    tab.addEventListener("click", function() {
      document.querySelectorAll(".tab").forEach(function(t) { t.classList.remove("active"); });
      tab.classList.add("active");
      currentTab = tab.getAttribute("data-type");
      selectedNode = null;
      renderNodeList();
    });
  });

  nodeSearch.addEventListener("input", function() {
    renderNodeList();
  });

  depthSlider.addEventListener("input", function() {
    depthValue.textContent = this.value;
  });

  // Re-query when direction or depth changes (if a node is selected)
  document.querySelectorAll('input[name="direction"]').forEach(function(radio) {
    radio.addEventListener("change", function() {
      if (selectedNode) queryNode(selectedNode);
    });
  });
  depthSlider.addEventListener("change", function() {
    if (selectedNode) queryNode(selectedNode);
  });

  // Export buttons
  document.getElementById("btn-mermaid").addEventListener("click", function() {
    if (lastMermaidCode) downloadFile("lineage.mmd", lastMermaidCode, "text/plain");
  });
  document.getElementById("btn-json").addEventListener("click", function() {
    if (lastQueryResult) {
      downloadFile("lineage.json", JSON.stringify(lastQueryResult, null, 2), "application/json");
    }
  });
  document.getElementById("btn-html").addEventListener("click", function() {
    if (lastMermaidCode) {
      var html = buildStandaloneHtml(lastMermaidCode, diagramTitle.textContent);
      downloadFile("lineage.html", html, "text/html");
    }
  });
  document.getElementById("show-columns").addEventListener("change", function() {
    if (selectedNode) queryNode(selectedNode);
  });
  document.querySelectorAll('input[name="view-mode"]').forEach(function(r) {
    r.addEventListener("change", function() { if (selectedNode) queryNode(selectedNode); });
  });

  // -- Render node list (grouped by bucket / component type) --
  function renderNodeList() {
    nodeList.innerHTML = "";
    if (!allData || !selectedProject) return;

    var query = (nodeSearch.value || "").toLowerCase().trim();
    // groups: { groupName: [items] }
    var groups = {};

    if (currentTab === "tables") {
      var tables = allData.tables || {};
      for (var fqn in tables) {
        var t = tables[fqn];
        var pa = t.project_alias || fqn.split(":")[0] || "";
        if (pa !== selectedProject) continue;
        var tableId = t.table_id || fqn.split(":").slice(1).join(":") || fqn;
        if (query && tableId.toLowerCase().indexOf(query) < 0) continue;
        var colCount = Array.isArray(t.columns) ? t.columns.length : (t.columns || 0);
        var ec = edgeCounts[fqn] || 0;
        // Group by bucket: "in.c-bucket" or "out.c-bucket"
        var bucketId = t.bucket_id || tableId.split(".").slice(0, -1).join(".") || "other";
        var tableName = t.name || tableId.split(".").pop() || tableId;
        if (!groups[bucketId]) groups[bucketId] = [];
        groups[bucketId].push({
          fqn: fqn, name: tableName, edges: ec,
          meta: ec + " edges, " + colCount + " cols, " + (t.rows_count || 0).toLocaleString() + " rows"
        });
      }
    } else {
      var configs = allData.configurations || {};
      for (var cfqn in configs) {
        var c = configs[cfqn];
        var cpa = c.project_alias || cfqn.split(":")[0] || "";
        if (cpa !== selectedProject) continue;
        var configName = c.config_name || c.name || cfqn;
        if (query && configName.toLowerCase().indexOf(query) < 0 &&
            (c.component_id || "").toLowerCase().indexOf(query) < 0) continue;
        var cec = edgeCounts[cfqn] || 0;
        // Group by component_id
        var compId = c.component_id || "other";
        if (!groups[compId]) groups[compId] = [];
        groups[compId].push({
          fqn: cfqn, name: configName, edges: cec,
          meta: cec + " edges"
        });
      }
    }

    // Sort groups: by total edges in group descending
    var groupNames = Object.keys(groups);
    groupNames.sort(function(a, b) {
      var sumA = groups[a].reduce(function(s, i) { return s + i.edges; }, 0);
      var sumB = groups[b].reduce(function(s, i) { return s + i.edges; }, 0);
      return sumB - sumA || a.localeCompare(b);
    });

    for (var g = 0; g < groupNames.length; g++) {
      var gName = groupNames[g];
      var gItems = groups[gName];
      gItems.sort(function(a, b) { return (b.edges || 0) - (a.edges || 0) || a.name.localeCompare(b.name); });

      // Foldable group header
      var totalEdges = gItems.reduce(function(s, i) { return s + i.edges; }, 0);
      var header = document.createElement("div");
      header.style.cssText = "padding:8px 8px;font-size:11px;font-weight:600;color:#1a73e8;" +
        "background:#e8f0fe;border-bottom:1px solid #d2e3fc;cursor:pointer;user-select:none;" +
        "display:flex;justify-content:space-between;align-items:center";
      header.innerHTML = '<span>\u25BC ' + escapeHtml(gName) + '</span>' +
        '<span style="color:#5f6368;font-weight:400">' + gItems.length + ', ' + totalEdges + ' edges</span>';
      var groupContainer = document.createElement("div");
      header.addEventListener("click", (function(container, hdr) {
        return function() {
          var hidden = container.style.display === "none";
          container.style.display = hidden ? "block" : "none";
          hdr.querySelector("span").textContent = (hidden ? "\u25BC " : "\u25B6 ") +
            hdr.querySelector("span").textContent.substring(2);
        };
      })(groupContainer, header));
      nodeList.appendChild(header);

      for (var i = 0; i < gItems.length; i++) {
        var item = gItems[i];
        var div = document.createElement("div");
        div.className = "node-item" + (item.fqn === selectedNode ? " selected" : "");
        div.setAttribute("data-fqn", item.fqn);
        div.innerHTML = '<div>' + escapeHtml(item.name) + '</div>' +
          '<div class="node-meta">' + escapeHtml(item.meta) + '</div>';
        div.addEventListener("click", (function(fqn) {
          return function() { onNodeClick(fqn); };
        })(item.fqn));
        groupContainer.appendChild(div);
      }
      nodeList.appendChild(groupContainer);
    }
  }

  function onNodeClick(fqn) {
    selectedNode = fqn;
    // Update selection highlight
    document.querySelectorAll(".node-item").forEach(function(el) {
      el.classList.toggle("selected", el.getAttribute("data-fqn") === fqn);
    });
    queryNode(fqn);
  }

  // -- Query API --
  function getDirection() {
    var radios = document.querySelectorAll('input[name="direction"]');
    for (var i = 0; i < radios.length; i++) {
      if (radios[i].checked) return radios[i].value;
    }
    return "upstream";
  }

  function getDepth() {
    return parseInt(depthSlider.value, 10) || 3;
  }

  function queryNode(fqn) {
    var direction = getDirection();
    var depth = getDepth();

    var showCols = document.getElementById("show-columns").checked;
    var viewMode = document.querySelector('input[name="view-mode"]:checked').value;
    var cp = (showCols ? "&columns=true" : "") + (viewMode === "er" ? "&view=er" : "");

    placeholder.style.display = "none";
    mermaidOutput.innerHTML = "";
    loading.style.display = "block";
    exportBtns.style.display = "none";
    mainStats.textContent = "";

    if (direction === "both") {
      // Fetch both directions and merge
      diagramTitle.textContent = "Both directions of " + fqn + ", depth " + depth;
      var enc = encodeURIComponent(fqn);
      Promise.all([
        fetch("/api/query?node=" + enc + "&direction=upstream&depth=" + depth).then(function(r){return r.json();}),
        fetch("/api/query?node=" + enc + "&direction=downstream&depth=" + depth).then(function(r){return r.json();}),
        fetch("/api/mermaid?node=" + enc + "&direction=upstream&depth=" + depth + cp).then(function(r){return r.text();}),
        fetch("/api/mermaid?node=" + enc + "&direction=downstream&depth=" + depth + cp).then(function(r){return r.text();})
      ]).then(function(res) {
        loading.style.display = "none";
        var upQ = res[0], downQ = res[1], upM = res[2], downM = res[3];
        var allEdges = (upQ.edges || []).concat(downQ.edges || []);
        var merged = {node: fqn, edges: allEdges};
        // Merge mermaid from both directions
        var mermaidCode;
        if (viewMode === "er") {
          // ER: merge entity/relationship lines, deduplicate
          var allL = (upM + "\n" + downM).split("\n");
          var seenER = {}; var erL = ["erDiagram"];
          allL.forEach(function(l) {
            var t = l.trim();
            if (!t || t === "erDiagram") return;
            if (!seenER[t]) { seenER[t] = true; erL.push(l); }
          });
          mermaidCode = erL.join("\n");
        } else {
          // Flowchart: merge node/edge lines, deduplicate
          var upLines = upM.split("\n"); var downLines = downM.split("\n");
          var seen = {}; upLines.forEach(function(l){seen[l.trim()]=true;});
          var extra = downLines.filter(function(l){return l.trim() && !seen[l.trim()] && !l.trim().startsWith("graph ") && !l.trim().startsWith("classDef ");});
          var combined = upLines.slice(0,-2).concat(extra).concat(upLines.slice(-2));
          combined[0] = "graph LR";
          mermaidCode = combined.join("\n");
        }
        lastQueryResult = merged; lastMermaidCode = mermaidCode;
        var nodeSet = {}; if(merged.node) nodeSet[merged.node]=true;
        allEdges.forEach(function(e){nodeSet[e.source]=true;nodeSet[e.target]=true;});
        mainStats.textContent = Object.keys(nodeSet).length+" nodes, "+allEdges.length+" edges";
        exportBtns.style.display = "flex";
        if(allEdges.length===0){mermaidOutput.innerHTML='<p style="color:#888;padding:20px">No dependencies found in either direction.</p>';return;}
        buildIdMap(merged); renderMermaid(mermaidCode);
      });
      return;
    }

    diagramTitle.textContent = direction.charAt(0).toUpperCase() + direction.slice(1) +
      " of " + fqn + ", depth " + depth;

    var queryUrl = "/api/query?node=" + encodeURIComponent(fqn) +
      "&direction=" + direction + "&depth=" + depth;
    var mermaidUrl = "/api/mermaid?node=" + encodeURIComponent(fqn) +
      "&direction=" + direction + "&depth=" + depth + cp;

    Promise.all([
      fetch(queryUrl).then(function(r) { return r.json(); }),
      fetch(mermaidUrl).then(function(r) { return r.text(); })
    ]).then(function(results) {
      var queryResult = results[0];
      var mermaidCode = results[1];
      loading.style.display = "none";

      if (queryResult.error) {
        mermaidOutput.innerHTML = '<p style="color:#d93025;padding:20px">' +
          escapeHtml(queryResult.error) + '</p>';
        return;
      }

      lastQueryResult = queryResult;
      lastMermaidCode = mermaidCode;

      var edges = queryResult.edges || [];
      var nodeSet = {};
      if (queryResult.node) nodeSet[queryResult.node] = true;
      for (var i = 0; i < edges.length; i++) {
        nodeSet[edges[i].source] = true;
        nodeSet[edges[i].target] = true;
      }
      var nodeCount = Object.keys(nodeSet).length;
      mainStats.textContent = nodeCount + " nodes, " + edges.length + " edges";
      exportBtns.style.display = "flex";

      if (edges.length === 0) {
        var opposite = direction === "upstream" ? "downstream" : "upstream";
        mermaidOutput.innerHTML = '<p style="color:#888;padding:20px">No ' +
          direction + ' dependencies found.</p>' +
          '<p style="padding:0 20px"><a href="#" style="color:#1a73e8" onclick="' +
          "document.querySelector('input[name=direction][value=" + opposite + "]').checked=true;" +
          "document.querySelector('.node-item.selected').click();return false;" +
          '">Try ' + opposite + ' direction</a> ' +
          'or <a href="#" style="color:#1a73e8" onclick="' +
          "document.querySelector('input[name=direction][value=both]').checked=true;" +
          "document.querySelector('.node-item.selected').click();return false;" +
          '">show both directions</a></p>';
        return;
      }

      // Build ID map for click traversal, then render
      buildIdMap(queryResult);
      renderMermaid(mermaidCode);
    }).catch(function(err) {
      loading.style.display = "none";
      mermaidOutput.innerHTML = '<p style="color:#d93025;padding:20px">Query failed: ' +
        escapeHtml(err.message) + '</p>';
    });
  }

  // Map sanitized mermaid node IDs back to FQNs for click traversal
  var lastIdToFqn = {};

  function sanitizeFqn(fqn) {
    return fqn.replace(/[^a-zA-Z0-9_]/g, "_");
  }

  function buildIdMap(queryResult) {
    lastIdToFqn = {};
    if (!queryResult || !queryResult.edges) return;
    var fqns = {};
    if (queryResult.node) fqns[queryResult.node] = true;
    queryResult.edges.forEach(function(e) {
      fqns[e.source] = true;
      fqns[e.target] = true;
    });
    for (var fqn in fqns) {
      lastIdToFqn[sanitizeFqn(fqn)] = fqn;
    }
  }

  function attachDiagramClickHandlers() {
    var nodes = mermaidOutput.querySelectorAll(".node");
    nodes.forEach(function(el) {
      el.style.cursor = "pointer";
      el.addEventListener("click", function() {
        // Extract the mermaid node ID from the element
        var nodeId = el.id || "";
        // Mermaid wraps IDs: "flowchart-{id}-{n}" or just the id
        for (var sid in lastIdToFqn) {
          if (nodeId.indexOf(sid) >= 0) {
            var fqn = lastIdToFqn[sid];
            // Navigate to this node
            selectedNode = fqn;
            queryNode(fqn);
            // Update sidebar selection
            var proj = fqn.split(":")[0] || "";
            if (proj !== selectedProject) {
              projectSelect.value = proj;
              selectedProject = proj;
              renderNodeList();
            }
            document.querySelectorAll(".node-item").forEach(function(item) {
              item.classList.toggle("selected", item.getAttribute("data-fqn") === fqn);
            });
            break;
          }
        }
      });
    });
  }

  function renderMermaid(code) {
    renderCounter++;
    var id = "mermaid-diagram-" + renderCounter;
    mermaidOutput.innerHTML = "";
    mermaid.render(id, code).then(function(result) {
      mermaidOutput.innerHTML = result.svg;
      // Fit SVG to fill the diagram area
      var svg = mermaidOutput.querySelector("svg");
      if (svg) {
        var area = document.getElementById("diagram-area");
        var w = area.clientWidth - 20;
        var h = area.clientHeight - 20;
        svg.setAttribute("width", w);
        svg.setAttribute("height", h);
        svg.style.display = "block";
      }
      currentZoom = 1;
      document.getElementById("zoom-controls").style.display = "block";
      document.getElementById("legend").style.display = "flex";
      attachDiagramClickHandlers();
    }).catch(function(err) {
      mermaidOutput.innerHTML = '<p style="color:#d93025;padding:20px">' +
        'Mermaid render error: ' + escapeHtml(err.message) + '</p>' +
        '<pre style="padding:12px;background:#f5f5f5;border-radius:4px;' +
        'font-size:11px;overflow:auto;max-height:300px">' +
        escapeHtml(code) + '</pre>';
    });
  }

  // -- Helpers --
  var currentZoom = 1;
  window.zoomDiagram = function(factor) {
    if (factor === 0) { currentZoom = 1; } else { currentZoom *= factor; }
    currentZoom = Math.max(0.2, Math.min(3, currentZoom));
    var svg = mermaidOutput.querySelector("svg");
    if (svg) { svg.style.transform = "scale(" + currentZoom + ")"; svg.style.transformOrigin = "top left"; }
  };

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.appendChild(document.createTextNode(text || ""));
    return div.innerHTML;
  }

  function downloadFile(filename, content, mimeType) {
    var blob = new Blob([content], { type: mimeType });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  function buildStandaloneHtml(mermaidCode, title) {
    return '<!DOCTYPE html>\n<html>\n<head>\n' +
      '  <title>' + escapeHtml(title) + '</title>\n' +
      '  <script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"><' + '/script>\n' +
      '  <style>\n' +
      '    body { font-family: system-ui, -apple-system, sans-serif;\n' +
      '           max-width: 100%; padding: 20px; color: #333; }\n' +
      '    h2 { margin-bottom: 12px; }\n' +
      '    .mermaid { text-align: center; margin-top: 16px; }\n' +
      '    .legend { margin: 16px 0; padding: 12px 16px; background: #f5f5f5;\n' +
      '             border-radius: 8px; font-size: 13px; display: inline-block; }\n' +
      '    .legend-swatch { display: inline-block; width: 14px; height: 14px;\n' +
      '                     border-radius: 3px; vertical-align: middle; margin-right: 4px; }\n' +
      '  </style>\n</head>\n<body>\n' +
      '  <h2>' + escapeHtml(title) + '</h2>\n' +
      '  <div class="legend">\n' +
      '    <strong>Legend</strong><br/>\n' +
      '    <span class="legend-swatch" style="background:#e1f5fe;border:2px solid #0288d1"></span> Table\n' +
      '    &nbsp;&nbsp;\n' +
      '    <span class="legend-swatch" style="background:#e8f5e9;border:2px solid #388e3c"></span> Configuration\n' +
      '    <br/><span style="color:#888;font-size:12px">' +
      '    Edge labels: input_mapping / output_mapping | sql_tokenizer | bucket_sharing | ai</span>\n' +
      '  </div>\n' +
      '  <div class="mermaid">\n' + mermaidCode + '\n  </div>\n' +
      '  <script>mermaid.initialize({startOnLoad:true, theme:"default", ' +
      'flowchart:{useMaxWidth:false,htmlLabels:true}, securityLevel:"loose"});<' + '/script>\n' +
      '</body>\n</html>';
  }
})();
</script>
</body>
</html>"""


class _LineageHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for the lineage browser - serves HTML, data JSON, and query APIs."""

    html_content: str = ""
    json_content: str = ""
    service: DeepLineageService | None = None
    graph: LineageGraph | None = None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._serve(self.html_content, "text/html")
        elif path == "/data.json":
            self._serve(self.json_content, "application/json")
        elif path == "/api/query":
            self._handle_query(parsed)
        elif path == "/api/mermaid":
            self._handle_mermaid(parsed)
        else:
            self.send_error(404)

    def _handle_query(self, parsed) -> None:
        """Handle /api/query?node=FQN&direction=upstream&depth=3."""
        params = parse_qs(parsed.query)
        node = params.get("node", [""])[0]
        direction = params.get("direction", ["downstream"])[0]
        depth = int(params.get("depth", ["3"])[0])

        if not node:
            self._serve(json.dumps({"error": "Missing 'node' parameter"}), "application/json")
            return

        assert self.service is not None and self.graph is not None
        if direction == "upstream":
            result = self.service.query_upstream(self.graph, node, depth=depth)
        else:
            result = self.service.query_downstream(self.graph, node, depth=depth)

        self._serve(json.dumps(result), "application/json")

    def _handle_mermaid(self, parsed) -> None:
        """Handle /api/mermaid?node=FQN&direction=upstream&depth=3."""
        from ..services.deep_lineage_service import DeepLineageService

        params = parse_qs(parsed.query)
        node = params.get("node", [""])[0]
        direction = params.get("direction", ["downstream"])[0]
        depth = int(params.get("depth", ["3"])[0])

        if not node:
            self._serve("graph LR\n  empty[No node specified]", "text/plain")
            return

        assert self.service is not None and self.graph is not None
        if direction == "upstream":
            result = self.service.query_upstream(self.graph, node, depth=depth)
        else:
            result = self.service.query_downstream(self.graph, node, depth=depth)

        if "error" in result:
            self._serve(
                "graph LR\n  error[" + result["error"].replace('"', "'") + "]", "text/plain"
            )
            return

        edges = result.get("edges", [])
        view = params.get("view", ["flow"])[0]
        show_cols = params.get("columns", [""])[0] == "true"

        if view == "er":
            mermaid_code = DeepLineageService.render_er_diagram(
                edges,
                self.graph,
                node,
                show_columns=show_cols,
            )
        else:
            mermaid_code = DeepLineageService.render_mermaid(
                edges,
                self.graph,
                direction,
                node,
                show_columns=show_cols,
            )
        self._serve(mermaid_code, "text/plain")

    def _serve(self, content: str, content_type: str) -> None:
        encoded = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        """Silence default stderr logging."""


@lineage_app.command("server")
def lineage_serve(
    ctx: typer.Context,
    load: Path = typer.Option(
        ...,
        "--load",
        "-l",
        help="Lineage JSON cache file (from `lineage build`).",
    ),
    port: int = typer.Option(
        8088,
        "--port",
        help="Port to serve on.",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Host to bind to.",
    ),
) -> None:
    """Start a local web server with interactive lineage browser.

    Serves an interactive lineage browser from a cached lineage file.
    Browse projects, tables, and configurations in the sidebar, then
    click a node to query and visualize its upstream or downstream
    dependencies as a mermaid diagram.

    Example:

      kbagent lineage server -l lineage.json
      kbagent lineage server -l lineage.json --port 9000
    """
    formatter = get_formatter(ctx)

    if not load.exists():
        formatter.error(
            message=f"Cache file not found: {load}", error_code=ErrorCode.FILE_NOT_FOUND
        )
        raise typer.Exit(code=1)

    try:
        raw_data = json.loads(load.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        formatter.error(message=f"Cannot read lineage file: {exc}", error_code=ErrorCode.READ_ERROR)
        raise typer.Exit(code=1) from None

    # Load the graph via the service for API queries
    service = get_service(ctx, "deep_lineage_service")
    graph = service.load_from_cache(load)

    # Attach content + service/graph to the handler class
    _LineageHandler.html_content = _LINEAGE_HTML_TEMPLATE
    _LineageHandler.json_content = json.dumps(raw_data)
    _LineageHandler.service = service
    _LineageHandler.graph = graph

    server = http.server.HTTPServer((host, port), _LineageHandler)
    url = f"http://{host}:{port}"

    if formatter.json_mode:
        formatter.output({"url": url, "host": host, "port": port})
    else:
        formatter.console.print("\n[bold]Lineage browser server[/bold]")
        formatter.console.print(f"  URL: {url}")
        formatter.console.print(f"  Data: {load.resolve()}")
        formatter.console.print("  Press Ctrl+C to stop.\n")

    # Open browser in a separate thread to avoid blocking
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if not formatter.json_mode:
            formatter.console.print("\nServer stopped.")
