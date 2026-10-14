"""Graph traversal for deep lineage: node walks, direction unions, column traces.

Kept out of ``deep_lineage_service.py`` (file-size budget) and free of I/O:
every function here reads a ``LineageGraph`` and returns plain dicts, so the
CLI, ``kbagent lineage server`` and the FastAPI routes share one answer.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .deep_lineage_service import Edge, LineageGraph

UPSTREAM = "upstream"
DOWNSTREAM = "downstream"

# Terminal reasons of a column trace (JSON contract, see trace_column).
REASON_MAPPING_UNKNOWN = "column_mapping_unknown"
REASON_AMBIGUOUS_OUTPUT = "ambiguous_output_table"
REASON_OUTPUT_UNKNOWN = "output_table_unknown"


def edge_dict(edge: Edge, depth: int) -> dict[str, Any]:
    """Serialize an edge the way every lineage query result carries it."""
    return {
        "depth": depth,
        "source": edge.source_fqn,
        "target": edge.target_fqn,
        "edge_type": edge.edge_type,
        "detection": edge.detection,
        "columns": edge.columns,
        "column_mapping": edge.column_mapping,
    }


def walk_edges(graph: LineageGraph, fqn: str, depth: int, direction: str) -> list[dict]:
    """Breadth-first walk from ``fqn``; each edge carries its BFS distance.

    BFS expands every node exactly once, at its shallowest distance -- i.e.
    with the largest remaining budget. The former depth-first walk marked a
    node visited on the FIRST path that reached it, so a longer path that got
    there first cut off descendants a shorter path would still have reached
    within ``depth`` (R->A->X->C beside R->X->C->D lost C->D at depth 3).
    """
    upstream = direction == UPSTREAM
    index = graph._upstream if upstream else graph._downstream
    reached: set[str] = {fqn}
    frontier = [fqn]
    seen_edges: set[int] = set()
    result: list[dict] = []
    level = 0
    while frontier and level < depth:
        level += 1
        next_frontier: list[str] = []
        for node in frontier:
            for edge_idx in index.get(node, []):
                if edge_idx in seen_edges:
                    continue
                seen_edges.add(edge_idx)
                edge = graph.edges[edge_idx]
                result.append(edge_dict(edge, level))
                next_fqn = edge.source_fqn if upstream else edge.target_fqn
                if next_fqn not in reached:
                    reached.add(next_fqn)
                    next_frontier.append(next_fqn)
        frontier = next_frontier
    return result


def union_edges(*edge_lists: list[dict]) -> list[dict]:
    """Concatenate edge lists, merging repeats of the same edge.

    Two edges with the same endpoints, type and detection are one edge for the
    reader -- but a configuration that loads one source table through several
    input mappings (different column selections) has one graph edge per
    mapping. Keeping only the first dropped the other mappings' columns, so
    the repeats are folded in: ``columns`` unioned in order, ``column_mapping``
    merged. The merged dict is a copy; the graph's own lists are not touched.
    """
    merged: dict[tuple[str, str, str, str], dict] = {}
    for edges in edge_lists:
        for edge in edges:
            key = (edge["source"], edge["target"], edge["edge_type"], edge["detection"])
            first = merged.get(key)
            if first is None:
                merged[key] = {
                    **edge,
                    "columns": list(edge.get("columns") or []),
                    "column_mapping": dict(edge.get("column_mapping") or {}),
                }
                continue
            first["columns"] += [c for c in edge.get("columns") or [] if c not in first["columns"]]
            for out_col, src in (edge.get("column_mapping") or {}).items():
                first["column_mapping"].setdefault(out_col, src)
    return list(merged.values())


# ---------------------------------------------------------------------------
# Column trace
# ---------------------------------------------------------------------------


@dataclass
class _Trace:
    """Accumulated result of one column trace."""

    edges: list[dict] = field(default_factory=list)
    terminals: list[dict] = field(default_factory=list)
    _edge_keys: set[tuple[str, str, str, str]] = field(default_factory=set)
    _terminal_keys: set[tuple[str, str, str]] = field(default_factory=set)

    def add_edge(self, edge: Edge, depth: int, source_column: str, target_column: str) -> None:
        key = (edge.source_fqn, edge.target_fqn, source_column.lower(), target_column.lower())
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        data = edge_dict(edge, depth)
        data["source_column"] = source_column
        data["target_column"] = target_column
        mapped = {
            out: src
            for out, src in edge.column_mapping.items()
            if out.lower() == target_column.lower()
        }
        data["column_mapping"] = mapped
        self.edges.append(data)

    def stop(
        self, node: str, column: str, reason: str, candidates: list[str] | None = None
    ) -> None:
        key = (node, column.lower(), reason)
        if key in self._terminal_keys:
            return
        self._terminal_keys.add(key)
        terminal: dict[str, Any] = {"node": node, "column": column, "reason": reason}
        if candidates:
            terminal["candidates"] = candidates
        self.terminals.append(terminal)


def is_config_node(graph: LineageGraph, fqn: str) -> bool:
    """Configuration nodes are ``alias:component/config``; a table id never has a slash."""
    return fqn in graph.configurations or "/" in _table_id(fqn)


def _table_id(fqn: str) -> str:
    return fqn.partition(":")[2] if ":" in fqn else fqn


def _split_mapping_value(value: str) -> tuple[str, str]:
    """``"in.c-b.t.col"`` -> ``("in.c-b.t", "col")``; a bare column has no table."""
    table, _, column = value.rpartition(".")
    return table, column


def _value_points_at(value: str, table_fqn: str) -> bool:
    """True when a mapping value names ``table_fqn`` -- or names no table at all.

    Caches written before exact col_map matching (#846) may carry a mapping
    on every input edge whose id merely CONTAINED the AI's ``in_table``; a
    value that names a different full table id is one of those strays.
    """
    table, _ = _split_mapping_value(value)
    if not table or table.lower() == _table_id(table_fqn).lower():
        return True
    return not table.startswith(("in.c-", "out.c-"))


def _known_columns(graph: LineageGraph, table_fqn: str, edge: Edge) -> list[str] | None:
    """Columns known for ``table_fqn`` (metadata first, then the edge); None if unknown."""
    table = graph.tables.get(table_fqn)
    if table and table.columns:
        return table.columns
    return edge.columns or None


def _has(columns: list[str] | None, column: str) -> bool:
    return columns is not None and column.lower() in {c.lower() for c in columns}


def _edges(graph: LineageGraph, fqn: str, *, incoming: bool) -> list[Edge]:
    index = graph._upstream if incoming else graph._downstream
    return [graph.edges[i] for i in index.get(fqn, [])]


def _step_upstream(
    graph: LineageGraph, trace: _Trace, node: str, column: str, depth: int
) -> list[tuple[str, str]]:
    """One upstream hop from ``(node, column)``; returns the states to continue at."""
    next_states: list[tuple[str, str]] = []
    if is_config_node(graph, node):
        matched = False
        for edge in _edges(graph, node, incoming=True):
            for out_col, value in edge.column_mapping.items():
                if out_col.lower() != column.lower() or not _value_points_at(
                    value, edge.source_fqn
                ):
                    continue
                matched = True
                in_col = _split_mapping_value(value)[1]
                trace.add_edge(edge, depth, in_col, column)
                next_states.append((edge.source_fqn, in_col))
        if not matched:
            trace.stop(node, column, REASON_MAPPING_UNKNOWN)
        return next_states

    for edge in _edges(graph, node, incoming=True):
        if is_config_node(graph, edge.source_fqn):
            trace.add_edge(edge, depth, column, column)
            producer = edge.source_fqn
            written = [
                out.target_fqn
                for out in _edges(graph, producer, incoming=False)
                if out.target_fqn != node
                and _has(_known_columns(graph, out.target_fqn, out), column)
            ]
            if written:
                trace.stop(producer, column, REASON_AMBIGUOUS_OUTPUT, [node, *written])
            else:
                next_states.append((producer, column))
        else:
            # Table -> table (bucket sharing): the column keeps its name.
            trace.add_edge(edge, depth, column, column)
            next_states.append((edge.source_fqn, column))
    return next_states


def _step_downstream(
    graph: LineageGraph, trace: _Trace, node: str, column: str, depth: int
) -> list[tuple[str, str]]:
    """One downstream hop from ``(node, column)``; returns the states to continue at."""
    next_states: list[tuple[str, str]] = []
    if is_config_node(graph, node):
        outputs = _edges(graph, node, incoming=False)
        with_column = [e for e in outputs if _has(_known_columns(graph, e.target_fqn, e), column)]
        unknown = [e for e in outputs if _known_columns(graph, e.target_fqn, e) is None]
        if len(with_column) > 1:
            trace.stop(node, column, REASON_AMBIGUOUS_OUTPUT, [e.target_fqn for e in with_column])
        elif with_column or (len(outputs) == 1 and unknown):
            edge = (with_column or unknown)[0]
            trace.add_edge(edge, depth, column, column)
            next_states.append((edge.target_fqn, column))
        else:
            trace.stop(node, column, REASON_OUTPUT_UNKNOWN)
        return next_states

    for edge in _edges(graph, node, incoming=False):
        if not is_config_node(graph, edge.target_fqn):
            trace.add_edge(edge, depth, column, column)
            next_states.append((edge.target_fqn, column))
            continue
        if edge.columns and not _has(edge.columns, column):
            continue  # the consumer's input mapping does not load this column
        outs = [
            out_col
            for out_col, value in edge.column_mapping.items()
            if _split_mapping_value(value)[1].lower() == column.lower()
            and _value_points_at(value, node)
        ]
        if not outs and edge.column_mapping:
            continue  # mapped consumer, but nothing it outputs comes from this column
        if not outs:
            trace.add_edge(edge, depth, column, column)
            trace.stop(edge.target_fqn, column, REASON_MAPPING_UNKNOWN)
            continue
        for out_col in outs:
            trace.add_edge(edge, depth, column, out_col)
            next_states.append((edge.target_fqn, out_col))
    return next_states


def trace_column(
    graph: LineageGraph, fqn: str, column: str, direction: str, depth: int
) -> dict[str, Any]:
    """Follow ``column`` of table ``fqn`` hop by hop through recorded column mappings.

    The state is ``(node, column)``; for a configuration the column is the
    OUTPUT column it produces. Depth counts graph edges, so table -> config
    -> table costs 2. Mappings come from ``lineage build --ai`` (``col_map``
    on the config's input edges: ``out_col -> "<input table id>.<in_col>"``).

    Where the next hop is not known the trace stops with a terminal
    ``{node, column, reason}`` instead of guessing:

    - ``column_mapping_unknown``: no mapping says where the column comes from
      (upstream) or what a consumer turns it into (downstream).
    - ``ambiguous_output_table``: the config writes several tables carrying a
      column of that name, so a mapping keyed by the bare name cannot tell
      which one it means; ``candidates`` lists them.
    - ``output_table_unknown``: downstream, the config writes no table known
      to carry the produced column.

    The edge INTO a terminal config is kept; its other inputs are not
    expanded (they are unrelated to the column). Returned edges carry
    ``source_column`` / ``target_column`` and only the relevant mapping entry.
    """
    trace = _Trace()
    step = _step_upstream if direction == UPSTREAM else _step_downstream
    best: dict[tuple[str, str], int] = {(fqn, column.lower()): depth}
    queue: deque[tuple[str, str, int]] = deque([(fqn, column, depth)])
    while queue:
        node, col, remaining = queue.popleft()
        if remaining <= 0:
            continue
        hop = depth - remaining + 1
        for next_node, next_col in step(graph, trace, node, col, hop):
            key = (next_node, next_col.lower())
            if best.get(key, -1) >= remaining - 1:
                continue
            best[key] = remaining - 1
            queue.append((next_node, next_col, remaining - 1))
    return {"edges": trace.edges, "terminals": trace.terminals}
