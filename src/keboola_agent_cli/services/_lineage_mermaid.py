"""Mermaid flowchart + standalone HTML rendering for deep lineage (#848).

Split out of ``deep_lineage_service.py`` (file-size budget); the service
keeps ``render_mermaid`` / ``render_html`` as thin static delegates so every
existing caller and test keeps working.

Node ids stay derived from FQNs (``sanitize_mermaid_id``): the lineage
browser maps a clicked SVG node back to its FQN through that id, so an id
that is not a sanitized FQN is, deliberately, not navigable (the external
system annotations use exactly that).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..constants import MERMAID_SCRIPT_ATTRS

if TYPE_CHECKING:
    from .deep_lineage_service import LineageGraph

AI_DETECTIONS = frozenset({"ai", "ai_cross_project"})

_EDGE_LABELS = {"reads": "reads", "writes": "writes", "cross_project_share": "shares"}


def sanitize_mermaid_id(fqn: str) -> str:
    """Sanitize FQN into a valid mermaid node ID (alphanumeric + underscore)."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", fqn)


def escape_mermaid_label(text: str) -> str:
    """Escape characters that break mermaid label syntax, preserving <br/>."""
    text = text.replace("<br/>", "\x00BR\x00")
    text = text.replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    return text.replace("\x00BR\x00", "<br/>")


def edge_label(edge: dict) -> str:
    """What an edge MEANS (reads / writes / shares), not how it was detected."""
    return _EDGE_LABELS.get(edge.get("edge_type", ""), edge.get("edge_type", ""))


def is_ai_edge(edge: dict) -> bool:
    return edge.get("detection", "") in AI_DETECTIONS


def _project(fqn: str) -> str:
    return fqn.partition(":")[0] if ":" in fqn else ""


def _split_table_id(table_id: str) -> tuple[str, str]:
    """``out.c-b.t`` -> (``out.c-b``, ``t``); an id without a dot has no bucket."""
    bucket, _, name = table_id.rpartition(".")
    return bucket, name or table_id


def _node_line(
    fqn: str, graph: LineageGraph, *, prefix: str, show_columns: bool
) -> tuple[str, str]:
    """Return (label, css class) for one flowchart node."""
    if fqn in graph.configurations:
        cfg = graph.configurations[fqn]
        return f"{prefix}{cfg.config_name}<br/>{cfg.component_id}", "config"
    table = graph.tables.get(fqn)
    if table is None:
        bucket, name = _split_table_id(fqn.partition(":")[2] if ":" in fqn else fqn)
        parts = [f"{prefix}{name}"] + ([bucket] if bucket else [])
        return "<br/>".join(parts), "table"
    parts = [f"{prefix}{table.name}", table.bucket_id]
    parts.append(f"{len(table.columns)} cols, {table.rows_count:,} rows")
    if show_columns and table.columns:
        col_text = ", ".join(table.columns[:8])
        if len(table.columns) > 8:
            col_text += f", +{len(table.columns) - 8} more"
        parts.append(col_text)
    return "<br/>".join(parts), "table"


def _external_lines(node_fqns: list[str], graph: LineageGraph) -> list[str]:
    """Stadium nodes for systems outside Keboola that configs read from / write to.

    One node per system (shared by every config that touches it); ids are
    ``kbagentExt<n>`` -- NOT derived from an FQN, so the browser's click
    handler never mistakes them for a lineage node to navigate to.
    """
    lines: list[str] = []
    ids: dict[str, str] = {}
    for fqn in node_fqns:
        cfg = graph.configurations.get(fqn)
        for item in (cfg.external if cfg else []) or []:
            system = str(item.get("system", "")).strip()
            if not system:
                continue
            if system not in ids:
                ids[system] = f"kbagentExt{len(ids)}"
                label = escape_mermaid_label(system)
                lines.append(f'  {ids[system]}(["{label}"]):::ext')
            cfg_id = sanitize_mermaid_id(fqn)
            if item.get("op") == "read":
                lines.append(f"  {ids[system]} -.->|reads| {cfg_id}")
            else:
                lines.append(f"  {cfg_id} -.->|writes| {ids[system]}")
    return lines


def render_mermaid(
    edges: list[dict],
    graph: LineageGraph,
    direction: str,
    node_fqn: str,
    show_columns: bool = False,
    warnings: list[str] | None = None,
) -> str:
    """Render lineage edges as a mermaid flowchart.

    Always ``graph LR``: data flows left to right in every view, so the same
    edge never points the other way between the upstream and downstream
    views. ``direction`` only decides where the focus node ends up (sources
    on the left for upstream, dependents on the right for downstream); the
    focus node gets the ``focus`` class (thick border).

    Edge labels say what the edge means (``reads`` / ``writes`` /
    ``shares``); AI-detected edges are dashed with an ``AI`` badge. Labels
    show the table name, then its bucket, and carry the project alias only
    when the rendered node set spans more than one project.

    ``warnings`` (typically the ambiguity warning of #568) are rendered as
    standalone note nodes: a diagram has no metadata channel, so dropping
    them would present one project's answer as the whole picture.
    """
    del direction  # Kept for API compatibility; layout is always LR (#848).
    lines: list[str] = ["graph LR"]
    for index, warning in enumerate(warnings or []):
        warning_id = f"kbagentNote{index}"
        lines.append(f'  {warning_id}["⚠ {escape_mermaid_label(warning)}"]')
        lines.append(f"  style {warning_id} fill:#fff3cd,stroke:#856404,color:#856404")

    root_project = _project(node_fqn)
    node_fqns: set[str] = {node_fqn}
    for edge in edges:
        node_fqns.update((edge["source"], edge["target"]))
    ordered = sorted(node_fqns)
    multi_project = len({_project(fqn) for fqn in ordered}) > 1

    for fqn in ordered:
        prefix = f"{_project(fqn)}: " if multi_project and _project(fqn) else ""
        label, css_class = _node_line(fqn, graph, prefix=prefix, show_columns=show_columns)
        if css_class == "table" and root_project and _project(fqn) not in ("", root_project):
            css_class = "cross_table"
        lines.append(f'  {sanitize_mermaid_id(fqn)}["{escape_mermaid_label(label)}"]:::{css_class}')

    lines.extend(_external_lines(ordered, graph))

    seen_edges: set[tuple[str, str]] = set()
    for edge in edges:
        key = (sanitize_mermaid_id(edge["source"]), sanitize_mermaid_id(edge["target"]))
        if key in seen_edges:
            continue
        seen_edges.add(key)
        label = escape_mermaid_label(edge_label(edge))
        arrow = f"-.->|{label} · AI|" if is_ai_edge(edge) else f"-->|{label}|"
        lines.append(f"  {key[0]} {arrow} {key[1]}")

    lines.append("")
    lines.append("  classDef table fill:#e1f5fe,stroke:#0288d1")
    lines.append("  classDef config fill:#e8f5e9,stroke:#388e3c")
    lines.append("  classDef cross_table fill:#f3e5f5,stroke:#7b1fa2")
    lines.append("  classDef ext fill:#fff8e1,stroke:#f9a825,stroke-dasharray:4 2")
    lines.append("  classDef focus stroke:#202124,stroke-width:4px")
    lines.append(f"  class {sanitize_mermaid_id(node_fqn)} focus")
    return "\n".join(lines)


_SWATCH = (
    '<span style="display:inline-block;width:14px;height:14px;background:{bg};'
    'border:2px solid {border};border-radius:3px;vertical-align:middle;margin-right:4px">'
    "</span>"
)


def render_html(mermaid_code: str, title: str) -> str:
    """Wrap mermaid code in a self-contained HTML page."""
    escaped_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    legend = (
        '<div style="margin:20px 0;padding:12px 16px;background:#f5f5f5;'
        'border-radius:8px;font-size:13px;display:inline-block">'
        "<strong>Legend</strong><br/>"
        + _SWATCH.format(bg="#e1f5fe", border="#0288d1")
        + ' Table <span style="color:#888">(name, bucket &mdash; columns, rows)</span>'
        "&nbsp;&nbsp;&nbsp;"
        + _SWATCH.format(bg="#e8f5e9", border="#388e3c")
        + ' Configuration <span style="color:#888">(name, component)</span>'
        "&nbsp;&nbsp;&nbsp;"
        + _SWATCH.format(bg="#f3e5f5", border="#7b1fa2")
        + " Table in another project"
        "&nbsp;&nbsp;&nbsp;" + _SWATCH.format(bg="#fff8e1", border="#f9a825") + " External system"
        "<br/>"
        '<span style="color:#888;font-size:12px;margin-top:4px;display:block">'
        "Data flows left to right. Edge labels: reads / writes / shares (bucket sharing). "
        "Dashed edge with AI = detected by AI code analysis. "
        "Thick border = the node you queried.</span>"
        "</div>"
    )
    return (
        "<!DOCTYPE html>\n"
        "<html>\n"
        "<head>\n"
        '  <meta charset="utf-8">\n'
        f"  <title>{escaped_title}</title>\n"
        f"  <script {MERMAID_SCRIPT_ATTRS}></script>\n"
        "  <style>\n"
        "    body { font-family: system-ui, -apple-system, sans-serif;\n"
        "           max-width: 100%; padding: 20px; color: #333; }\n"
        "    h2 { margin-bottom: 4px; }\n"
        "    .mermaid { text-align: center; margin-top: 16px; }\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        f"  <h2>{escaped_title}</h2>\n"
        f"  {legend}\n"
        f'  <div class="mermaid">\n{mermaid_code}\n  </div>\n'
        "  <script>mermaid.initialize({startOnLoad: true, theme: 'default', "
        "flowchart: {curve: 'basis'}});</script>\n"
        "</body>\n"
        "</html>"
    )
