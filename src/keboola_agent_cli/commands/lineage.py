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
import threading
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import typer
from rich.markup import escape

from ..constants import MERMAID_SCRIPT_ATTRS
from ..errors import ErrorCode
from ..services.deep_lineage_service import DeepLineageService, LineageGraph
from ._helpers import (
    check_cli_permission,
    get_formatter,
    get_service,
    read_text_arg,
)
from ._lineage_build_output import print_ai_status
from ._lineage_tree import (
    DIAGRAM_FORMATS,
    output_diagram,
    print_column_trace,
    print_lineage_tree,
    resolve_depth,
)

lineage_app = typer.Typer(
    help="Column-level data lineage across projects.\n\n"
    "Build a dependency graph from sync'd data, then query upstream/downstream."
)


@lineage_app.callback(invoke_without_command=True)
def _lineage_callback(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "lineage")


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
        print_ai_status(formatter, result.get("ai_status", {}))
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
        raise typer.Exit(code=2)

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
        help="Trace one column of a TABLE hop by hop (text or --json only). Follows "
        "column mappings recorded by `lineage build --ai`; stops, and says so, where no "
        "mapping is known.",
    ),
    columns: bool = typer.Option(
        False,
        "--columns",
        help="Show column detail: mappings per edge in the text tree, column lists in "
        "mermaid/html table nodes (-f er always lists columns).",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        "-p",
        help="Project alias filter for queries.",
    ),
    depth: int | None = typer.Option(
        None, "--depth", help="Max traversal depth (default: 3 for -f html, else 10)."
    ),
    format: str = typer.Option(
        "text",
        "--format",
        "-f",
        help="Output format: text, mermaid, html, or er (entity-relationship, always "
        "with table columns).",
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

      er       Mermaid entity-relationship diagram (tables with their columns)

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
        raise typer.Exit(code=2)

    graph = service.load_from_cache(load)

    if not upstream and not downstream:
        formatter.error(
            message="Specify --upstream or --downstream to query.\n"
            "Use `kbagent lineage info -l FILE` to see what's in the graph.",
            error_code=ErrorCode.MISSING_QUERY,
        )
        raise typer.Exit(code=2)

    if column and format != "text":
        formatter.error(
            message=f"--column traces text or --json output only, not -f {format}.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)
    depth = resolve_depth(depth, format, formatter.json_mode)

    # Passing both --upstream and --downstream emits both, in order.
    for direction, identifier in (("upstream", upstream), ("downstream", downstream)):
        if not identifier:
            continue
        if column:
            query_result = service.query_column(
                graph, identifier, column, project or "", depth, direction=direction
            )
        else:
            query_result = service.query(
                graph, identifier, project or "", depth, direction=direction
            )
        if "error" in query_result:
            msg = query_result["error"]
            if query_result.get("usage_error"):
                formatter.error(message=msg, error_code=ErrorCode.INVALID_ARGUMENT)
                raise typer.Exit(code=2)
            if query_result.get("suggestions"):
                msg += "\nDid you mean: " + ", ".join(query_result["suggestions"][:5])
            formatter.error(message=msg, error_code=ErrorCode.NODE_NOT_FOUND)
            raise typer.Exit(code=1)

        for warning in query_result.get("warnings", []):
            formatter.warning(escape(str(warning)))  # JSON: payload and warnings[]

        if formatter.json_mode:
            formatter.output(query_result)
        elif format in DIAGRAM_FORMATS:
            output_diagram(formatter, graph, query_result, direction, format, columns)
        elif column:
            print_column_trace(formatter, graph, query_result, direction)
        else:
            print_lineage_tree(formatter, graph, query_result, direction, columns, depth)


# -- lineage serve ---------------------------------------------------------

# The two Mermaid <script> tags get the pinned URL and SRI hash from
# MERMAID_SCRIPT_ATTRS (the .replace() at the end of the template).
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
    <span style="color:#888">Edges: reads / writes / shares; dashed + AI = AI-detected; thick border = focus</span>
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

<script __MERMAID_SCRIPT_ATTRS__></script>
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

    // "both" is answered server-side as ONE union, rendered once (#848).
    diagramTitle.textContent = (direction === "both" ? "Both directions" :
      direction.charAt(0).toUpperCase() + direction.slice(1)) + " of " + fqn + ", depth " + depth;

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

      var ext = (queryResult.node_info || {}).external || [];
      if (edges.length === 0 && ext.length === 0) {
        if (direction === "both") {
          mermaidOutput.innerHTML = '<p style="color:#888;padding:20px">No dependencies found in either direction.</p>';
          return;
        }
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
      '  <meta charset="utf-8">\n' +
      '  <title>' + escapeHtml(title) + '</title>\n' +
      '  <script __MERMAID_SCRIPT_ATTRS__><' + '/script>\n' +
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
      '    Edge labels: reads / writes / shares; dashed + AI = AI-detected</span>\n' +
      '  </div>\n' +
      '  <div class="mermaid">\n' + mermaidCode + '\n  </div>\n' +
      '  <script>mermaid.initialize({startOnLoad:true, theme:"default", ' +
      'flowchart:{useMaxWidth:false,htmlLabels:true}, securityLevel:"loose"});<' + '/script>\n' +
      '</body>\n</html>';
  }
})();
</script>
</body>
</html>""".replace("__MERMAID_SCRIPT_ATTRS__", MERMAID_SCRIPT_ATTRS)


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
        """Handle /api/query?node=FQN&direction=upstream|downstream|both&depth=3."""
        params = parse_qs(parsed.query)
        node = params.get("node", [""])[0]
        if not node:
            self._serve(json.dumps({"error": "Missing 'node' parameter"}), "application/json")
            return
        assert self.service is not None and self.graph is not None
        direction = params.get("direction", ["downstream"])[0]
        if direction not in ("upstream", "both"):
            direction = "downstream"
        depth = int(params.get("depth", ["3"])[0])
        result = self.service.query(self.graph, node, "", depth, direction=direction)
        self._serve(json.dumps(result), "application/json")

    def _handle_mermaid(self, parsed) -> None:
        """Handle /api/mermaid?node=FQN&direction=...&depth=3[&view=er][&columns=true]."""
        params = parse_qs(parsed.query)
        node = params.get("node", [""])[0]
        if not node:
            self._serve("graph LR\n  empty[No node specified]", "text/plain")
            return
        assert self.service is not None and self.graph is not None
        code = self.service.diagram(
            self.graph,
            node,
            params.get("direction", ["downstream"])[0],
            int(params.get("depth", ["3"])[0]),
            view=params.get("view", ["flow"])[0],
            show_columns=params.get("columns", [""])[0] == "true",
        )
        self._serve(code, "text/plain")

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
        raise typer.Exit(code=2)

    try:
        raw_data = json.loads(read_text_arg(formatter, load, label="--load"))
    except json.JSONDecodeError as exc:
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
