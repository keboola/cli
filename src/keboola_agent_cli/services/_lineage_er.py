"""Mermaid ER-diagram rendering for deep lineage.

Split out of ``deep_lineage_service.py`` (file-size budget); the service keeps
``render_er_diagram`` as a static delegate so existing callers keep working.

Every API-derived string embedded in the diagram goes through
``html.escape(quote=True)``: a table or config named
``</div><script>...</script>`` must not inject HTML into the lineage page
(issue #269 sec-05). Mermaid renders the entities back in SVG text.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .deep_lineage_service import LineageGraph

# Bound the attribute list of one entity; the rest is summarised.
_MAX_ER_COLUMNS = 30


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _config_label(graph: LineageGraph, cfg_fqn: str) -> str:
    cfg = graph.configurations.get(cfg_fqn)
    return cfg.config_name if cfg else cfg_fqn.split("/")[-1]


def _table_entity(graph: LineageGraph, fqn: str) -> str:
    t = graph.tables[fqn]
    return _esc(f"{t.project_alias}:{t.name}")


def _column_comment(
    graph: LineageGraph, fqn: str, col: str, col_maps: dict[str, dict[str, str]]
) -> str:
    """`"to OUT via CONFIG"` / `"from SRC"` for a column a mapping mentions."""
    for cfg_fqn, cm in col_maps.items():
        for out_col, src_expr in cm.items():
            if src_expr.endswith(f".{col}") and fqn in src_expr.replace(f".{col}", ""):
                label = _esc(_config_label(graph, cfg_fqn))
                return f' "to {_esc(out_col)} via {label}"'
            if out_col == col:
                return f' "from {_esc(src_expr.split(".")[-1])}"'
    return ""


def _entity_lines(graph: LineageGraph, fqn: str, col_maps: dict[str, dict[str, str]]) -> list[str]:
    """One table entity, its columns ALWAYS listed as attributes.

    The ER view exists to show columns; without them it was a row-count line
    per table (the rows/cols summary is kept only for a table with no known
    columns, since mermaid needs at least one attribute inside braces).
    """
    t = graph.tables[fqn]
    lines = [f'    "{_table_entity(graph, fqn)}" {{']
    if not t.columns:
        lines.append(f'        int rows "{t.rows_count:,} rows, 0 cols"')
    pk_cols = set(t.primary_key or [])
    for col in t.columns[:_MAX_ER_COLUMNS]:
        safe_col = re.sub(r"[^a-zA-Z0-9_]", "_", col)
        pk_marker = " PK" if col in pk_cols else ""
        lines.append(
            f"        string {safe_col}{pk_marker}{_column_comment(graph, fqn, col, col_maps)}"
        )
    if len(t.columns) > _MAX_ER_COLUMNS:
        lines.append(f'        string _more_ "+{len(t.columns) - _MAX_ER_COLUMNS} more columns"')
    lines.append("    }")
    return lines


def _external_lines(
    graph: LineageGraph,
    config_fqns: set[str],
    input_of: dict[str, list[str]],
    output_of: dict[str, list[str]],
) -> list[str]:
    """Relate each AI-recorded external system to the tables its config touches.

    A config that touches no table (an external-only notifier) is related to
    an entity named after the config itself, so the diagram is never empty.
    """
    lines: list[str] = []
    for cfg_fqn in sorted(config_fqns):
        cfg = graph.configurations.get(cfg_fqn)
        if not cfg or not cfg.external:
            continue
        label = _esc(_config_label(graph, cfg_fqn))
        touched = [
            _table_entity(graph, t)
            for t in dict.fromkeys(input_of.get(cfg_fqn, []) + output_of.get(cfg_fqn, []))
            if t in graph.tables
        ] or [label]
        for item in cfg.external:
            system = _esc(f"ext:{item.get('system', '')}")
            op = _esc(str(item.get("op", "")))
            for entity in touched:
                lines.append(f'    "{entity}" }}o--o{{ "{system}" : "{op} via {label}"')
    return lines


def render_er_diagram(
    edges: list[dict],
    graph: LineageGraph,
    node_fqn: str,
    warnings: list[str] | None = None,
) -> str:
    """Render lineage as a mermaid ER diagram.

    Tables are entities with their columns (PK markers, AI column mappings as
    comments); relationships are ``input table --config--> output table``;
    AI-recorded external systems are ``ext:<system>`` entities.

    ``warnings`` carries non-fatal notes -- the ambiguity warning of #568,
    typically. ``erDiagram`` has no free-standing annotation the way a
    flowchart does, and the HTTP surfaces return this as plain text with
    no metadata channel, so the notes ride as one relationship-less entity.
    Dropping them would leave an ER viewer with one project's answer
    looking like the whole picture, which is the bug itself (#584).
    """
    lines: list[str] = ["erDiagram"]

    if warnings:
        lines.append('    "⚠ note" {')
        for index, warning in enumerate(warnings, start=1):
            # _esc also neutralises the double quotes that would otherwise
            # terminate the attribute comment.
            attribute = "warning" if len(warnings) == 1 else f"warning_{index}"
            lines.append(f'        string {attribute} "{_esc(warning)}"')
        lines.append("    }")

    table_fqns: set[str] = {node_fqn} if node_fqn in graph.tables else set()
    config_fqns: set[str] = {node_fqn} if node_fqn in graph.configurations else set()
    # config FQN (the node that transforms data) -> merged column mapping
    col_maps: dict[str, dict[str, str]] = {}
    for edge in edges:
        src, tgt = edge["source"], edge["target"]
        table_fqns.update(f for f in (src, tgt) if f in graph.tables)
        config_fqns.update(f for f in (src, tgt) if f in graph.configurations)
        cm = edge.get("column_mapping", {})
        if cm:
            cfg = tgt if tgt in graph.configurations else src
            col_maps[cfg] = {**col_maps.get(cfg, {}), **cm}

    for fqn in sorted(table_fqns):
        lines.extend(_entity_lines(graph, fqn, col_maps))

    input_of: dict[str, list[str]] = {}  # config -> [input table fqns]
    output_of: dict[str, list[str]] = {}  # config -> [output table fqns]
    for edge in edges:
        src, tgt = edge["source"], edge["target"]
        if src in graph.tables and tgt in config_fqns:
            input_of.setdefault(tgt, []).append(src)
        if src in config_fqns and tgt in graph.tables:
            output_of.setdefault(src, []).append(tgt)

    seen_rels: set[tuple[str, str]] = set()
    for cfg_fqn in config_fqns:
        safe_label = _esc(_config_label(graph, cfg_fqn))
        inputs = [t for t in input_of.get(cfg_fqn, []) if t in graph.tables]
        outputs = [t for t in output_of.get(cfg_fqn, []) if t in graph.tables]
        for inp_fqn in inputs:
            for out_fqn in outputs:
                if (inp_fqn, out_fqn) in seen_rels:
                    continue
                seen_rels.add((inp_fqn, out_fqn))
                inp, out = _table_entity(graph, inp_fqn), _table_entity(graph, out_fqn)
                lines.append(f'    "{inp}" ||--o{{ "{out}" : "{safe_label}"')
        # A config with inputs and no outputs (a writer): relate to the config.
        if inputs and not outputs:
            for inp_fqn in inputs:
                lines.append(
                    f'    "{_table_entity(graph, inp_fqn)}" }}o--|| "{safe_label}" : "writes"'
                )

    lines.extend(_external_lines(graph, config_fqns, input_of, output_of))
    return "\n".join(lines)
