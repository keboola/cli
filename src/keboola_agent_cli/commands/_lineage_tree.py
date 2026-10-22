"""Human output for `kbagent lineage show`: text tree, column trace, diagrams (#848).

Split out of ``commands/lineage.py`` (over its file-size budget). Rendering
only -- every traversal decision is made by the service
(``services/_lineage_trace.py``); this module turns its result into text.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Hashable
from typing import Any

import typer
from rich.markup import escape

from ..errors import ErrorCode
from ..output import OutputFormatter
from ..services._lineage_mermaid import edge_label, is_ai_edge
from ..services.deep_lineage_service import DeepLineageService, LineageGraph

DIAGRAM_FORMATS = ("mermaid", "html", "er")

# `--format html` renders a picture; at depth 10 a real project's fan-in renders
# unreadably small, so html alone defaults shallower (#848). Explicit
# `--depth` always wins, and `--json` keeps the data default.
HTML_DEFAULT_DEPTH = 3
DEFAULT_DEPTH = 10

_TERMINAL_TEXT = {
    "column_mapping_unknown": "column mapping unknown beyond this point",
    "ambiguous_output_table": "ambiguous: several output tables carry this column",
    "output_table_unknown": "no output table known to carry this column",
}


def resolve_depth(depth: int | None, output_format: str, json_mode: bool) -> int:
    """`--depth` when given, else 3 for a rendered html diagram and 10 otherwise."""
    if depth is not None:
        return depth
    return HTML_DEFAULT_DEPTH if output_format == "html" and not json_mode else DEFAULT_DEPTH


def node_description(graph: LineageGraph, fqn: str) -> str:
    """Plain (unescaped) one-line description of a node, type prefix first."""
    if fqn in graph.tables:
        t = graph.tables[fqn]
        return f"[table] {fqn} ({len(t.columns)} cols, {t.rows_count:,} rows)"
    if fqn in graph.configurations:
        c = graph.configurations[fqn]
        return f"[{c.component_type}] {c.project_alias}:{c.config_name} ({c.component_id})"
    return fqn


def _edge_text(edge: dict) -> str:
    """`reads` / `writes` / `shares`, plus an `[AI]` badge for AI-detected edges."""
    return edge_label(edge) + (" [AI]" if is_ai_edge(edge) else "")


def _print(formatter: OutputFormatter, text: str) -> None:
    # soft_wrap: never hard-wrap a tree line -- the indentation is the structure.
    formatter.console.print(text, soft_wrap=True)


def _header(
    formatter: OutputFormatter, graph: LineageGraph, result: dict, direction: str, suffix: str = ""
) -> None:
    label = "Upstream dependencies" if direction == "upstream" else "Downstream dependents"
    desc = escape(node_description(graph, result["node"]))
    _print(formatter, f"\n[bold]{label} of {desc}[/bold]{suffix}\n")


def _print_tree(
    formatter: OutputFormatter,
    root: Hashable,
    children: dict[Hashable, list[tuple[dict, Hashable]]],
    max_depth: int,
    render: Callable[[dict, Hashable], str],
    after: Callable[[Hashable, int], None],
    arrow: str,
    detail: Callable[[dict, int], None] | None = None,
) -> None:
    """Depth-first print of a real tree: every child under its actual parent.

    A node already expanded is printed again as a leaf with ``(see above)``
    -- unless this occurrence leaves it MORE depth budget than the earlier
    one had, in which case it is expanded again so nothing within ``depth``
    is hidden behind a back-reference.

    ``detail(edge, level)`` runs under EVERY printed line, back-references
    included: it describes the edge, not the node. ``after(child, level)``
    runs only where the child is expanded: it describes the node.
    """
    expanded: dict[Hashable, int] = {root: max_depth}

    def visit(key: Hashable, level: int) -> None:
        for edge, child in children.get(key, []):
            remaining = max_depth - level
            line = f"{'  ' * level}{arrow} {render(edge, child)}"
            if expanded.get(child, -1) >= remaining:
                _print(formatter, line + " [dim](see above)[/dim]")
                if detail:
                    detail(edge, level)
                continue
            _print(formatter, line)
            if detail:
                detail(edge, level)
            expanded[child] = remaining
            after(child, level + 1)
            if remaining > 0:
                visit(child, level + 1)

    visit(root, 1)


def _children(
    edges: list[dict], direction: str, key: Callable[[dict, str], Hashable]
) -> dict[Hashable, list[tuple[dict, Hashable]]]:
    """Index edges by parent: upstream parents are targets, downstream parents sources."""
    upstream = direction == "upstream"
    index: dict[Hashable, list[tuple[dict, Hashable]]] = {}
    for edge in edges:
        parent, child = ("target", "source") if upstream else ("source", "target")
        index.setdefault(key(edge, parent), []).append((edge, key(edge, child)))
    return index


def print_lineage_tree(
    formatter: OutputFormatter,
    graph: LineageGraph,
    result: dict,
    direction: str,
    show_columns: bool = False,
    depth: int = DEFAULT_DEPTH,
) -> None:
    """Print a node's upstream/downstream lineage as a real tree."""
    _header(formatter, graph, result, direction)
    node = result["node"]
    edges = result.get("edges", [])
    cfg = graph.configurations.get(node)
    _print_external(formatter, cfg.external if cfg else [], "  ")
    if not edges:
        if not (cfg and cfg.external):
            _print(formatter, "  (none found)")
        return

    def render(edge: dict, child: Hashable) -> str:
        text = f"{escape(_edge_text(edge))} {escape(node_description(graph, str(child)))}"
        cols = edge.get("columns") or []
        if cols and not show_columns:
            more = f"... +{len(cols) - 5}" if len(cols) > 5 else ""
            text += " " + escape(f"[{', '.join(cols[:5])}{more}]")
        return text

    def detail(edge: dict, level: int) -> None:
        if show_columns:
            _print_edge_columns(formatter, edge, level)

    def after(child: Hashable, level: int) -> None:
        child_cfg = graph.configurations.get(str(child))
        _print_external(formatter, child_cfg.external if child_cfg else [], "  " * (level + 1))

    max_depth = max([depth, *(e["depth"] for e in edges)])
    arrow = "<-" if direction == "upstream" else "->"
    _print_tree(
        formatter,
        node,
        _children(edges, direction, lambda e, end: e[end]),
        max_depth,
        render,
        after,
        arrow,
        detail,
    )


def _print_external(formatter: OutputFormatter, external: list[dict], indent: str) -> None:
    """One `external: <system> (<op>)` line per AI-recorded external system."""
    for item in external or []:
        system, op = escape(str(item.get("system", ""))), escape(str(item.get("op", "")))
        _print(formatter, f"{indent}external: {system} ({op})")


def _print_edge_columns(formatter: OutputFormatter, edge: dict, level: int) -> None:
    """`--columns`: list the mapping (or the column list) of THIS edge.

    Per edge, not per child node: two configs reading the same table each
    get their own mapping under their own occurrence of that table (#849).
    """
    indent = "  " * (level + 1)
    for out_col, src in (edge.get("column_mapping") or {}).items():
        _print(formatter, f"{indent}[dim]{escape(out_col)}[/dim] <- {escape(src)}")
    if not edge.get("column_mapping"):
        cols = edge.get("columns") or []
        for col in cols[:10]:
            _print(formatter, f"{indent}[dim]{escape(col)}[/dim]")
        if len(cols) > 10:
            _print(formatter, f"{indent}[dim]... +{len(cols) - 10} more[/dim]")


def print_column_trace(
    formatter: OutputFormatter, graph: LineageGraph, result: dict, direction: str
) -> None:
    """Print a `--column` trace: one line per hop, terminals marked inline."""
    column = result["column"]
    _header(formatter, graph, result, direction, f"  [dim](column: {escape(column)})[/dim]")
    edges = result.get("edges", [])
    terminals: dict[Hashable, list[dict]] = {}
    for terminal in result.get("terminals", []):
        terminals.setdefault((terminal["node"], terminal["column"].lower()), []).append(terminal)

    def key(edge: dict, end: str) -> tuple[str, str]:
        return edge[end], edge[f"{end}_column"].lower()

    def render(edge: dict, child: Hashable) -> str:
        end = "source" if direction == "upstream" else "target"
        col = edge[f"{end}_column"]
        return escape(f"{_edge_text(edge)} {node_description(graph, edge[end])} · column {col}")

    def after(child: Hashable, level: int) -> None:
        for terminal in terminals.get(child, []):
            text = _TERMINAL_TEXT.get(terminal["reason"], terminal["reason"])
            if terminal.get("candidates"):
                text += ": " + ", ".join(terminal["candidates"])
            _print(formatter, f"{'  ' * (level + 1)}[yellow]! {escape(text)}[/yellow]")

    root = (result["node"], column.lower())
    after(root, 0)
    if not edges:
        if not terminals:
            _print(formatter, "  (no column lineage found)")
        return
    max_depth = max(e["depth"] for e in edges)
    arrow = "<-" if direction == "upstream" else "->"
    _print_tree(formatter, root, _children(edges, direction, key), max_depth, render, after, arrow)


def output_diagram(
    formatter: OutputFormatter,
    graph: LineageGraph,
    query_result: dict,
    direction: str,
    output_format: str,
    show_columns: bool,
) -> None:
    """Render a query result as mermaid, er, or an html file."""
    node_fqn = query_result["node"]
    edges = query_result.get("edges", [])
    warnings: Any = query_result.get("warnings")
    if output_format == "er":
        typer.echo(
            # ER always lists columns; `--columns` only shapes the flowcharts.
            DeepLineageService.render_er_diagram(edges, graph, node_fqn, warnings=warnings)
        )
        return
    code = DeepLineageService.render_mermaid(
        edges, graph, direction, node_fqn, show_columns=show_columns, warnings=warnings
    )
    if output_format == "mermaid":
        typer.echo(code)
        return
    html_content = DeepLineageService.render_html(code, f"Lineage {direction} of {node_fqn}")
    # `--columns` renders a different diagram: its own name, so the two runs
    # do not silently overwrite each other.
    suffix = "_columns" if show_columns else ""
    filename = f"lineage_{direction}_{re.sub(r'[^a-zA-Z0-9_]', '_', node_fqn)}{suffix}.html"
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html_content)
    except OSError as exc:
        formatter.error(
            message=f"Cannot write HTML file '{filename}': {exc}", error_code=ErrorCode.WRITE_ERROR
        )
        raise typer.Exit(code=1) from None
    if not formatter.json_mode:
        formatter.console.print(f"HTML lineage diagram saved to: {escape(filename)}")
