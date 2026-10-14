"""AI task generation and AI result application for deep lineage (#846, #847).

``lineage build --ai`` writes ``.lineage_ai_tasks.json`` (one task per config
whose code an AI agent must read), the agent answers in
``.lineage_ai_results.json``, and the next ``lineage build`` folds those answers
into the graph. This module owns both halves:

- :func:`generate_ai_tasks` points each task at the code file the scan actually
  read (``Configuration.source_dir``). It used to rebuild the path as
  ``<root>/<alias>/main/<path>``, which is wrong for a flat layout, an alias that
  differs from the directory name, and any branch tree other than ``main``.
  A task whose code file does not exist is not emitted; it is reported instead.
- :func:`apply_ai_results_file` applies ``inputs`` / ``extra_inputs`` (reads),
  ``outputs`` (writes -- silently ignored before), ``external`` annotations and
  ``col_map`` column mappings, matching tables exactly instead of by substring.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .deep_lineage_service import (
    AI_RESULTS_FILE,
    AI_TASKS_FILE,
    Configuration,
    Edge,
    LineageGraph,
)

# A full Storage table id: ``in.c-bucket.table`` / ``out.c-bucket.table``.
_FULL_TABLE_ID = re.compile(r"^(in|out)\.c-[^.:]+\.[^.:]+$")

_EXTERNAL_OPS = ("read", "write")

# ``ai_outputs_unresolved[].reason`` values (part of the --json contract).
REASON_CROSS_PROJECT = "cross_project_not_supported"
REASON_AMBIGUOUS = "ambiguous"
REASON_NOT_IN_OUTPUT_MAPPING = "not_in_output_mapping"


@dataclass
class UnresolvedOutput:
    """An AI ``outputs[]`` entry that could not be mapped to one Storage table."""

    config_fqn: str
    table: str
    reason: str
    candidates: list[str] = field(default_factory=list)


@dataclass
class OutputResolution:
    """Result of resolving an AI output name: a table id, or why there is none."""

    table_id: str = ""
    reason: str = ""
    candidates: list[str] = field(default_factory=list)


@dataclass
class AiApplyStats:
    """Counters reported in ``ai_status`` after applying AI results."""

    reads_added: int = 0
    writes_added: int = 0
    columns_merged: int = 0
    external_added: int = 0
    col_map_skipped: int = 0
    unresolved: list[UnresolvedOutput] = field(default_factory=list)

    def to_status(self) -> dict[str, Any]:
        return {
            "ai_results_applied": True,
            "ai_edges_added": self.reads_added + self.writes_added,
            "ai_reads_added": self.reads_added,
            "ai_writes_added": self.writes_added,
            "ai_columns_merged": self.columns_merged,
            "ai_external_added": self.external_added,
            "ai_col_map_skipped": self.col_map_skipped,
            "ai_outputs_unresolved": [asdict(u) for u in self.unresolved],
        }


# ---------------------------------------------------------------------------
# Applying results
# ---------------------------------------------------------------------------


def apply_ai_results_file(
    graph: LineageGraph,
    root: Path,
    project_id_to_alias: dict[int, str],
) -> dict[str, Any]:
    """Apply ``root/.lineage_ai_results.json`` to ``graph`` if the file exists."""
    results_path = root / AI_RESULTS_FILE
    if not results_path.exists():
        return {"ai_results_applied": False}

    with open(results_path, encoding="utf-8") as f:
        ai_results = json.load(f)

    stats = AiApplyStats()
    for entry in ai_results.get("results", []):
        if not isinstance(entry, dict):
            continue
        config = graph.configurations.get(entry.get("config_fqn", ""))
        if config is None:
            continue
        _apply_inputs(graph, config, entry, project_id_to_alias, stats)
        _apply_outputs(graph, config, entry.get("outputs") or [], stats)
        _apply_external(config, entry.get("external") or [], stats)
        _apply_col_map(graph, config, entry.get("col_map") or [], stats)
    return stats.to_status()


def _find_edge(graph: LineageGraph, source_fqn: str, target_fqn: str) -> Edge | None:
    for edge in graph.edges:
        if edge.source_fqn == source_fqn and edge.target_fqn == target_fqn:
            return edge
    return None


def _apply_inputs(
    graph: LineageGraph,
    config: Configuration,
    entry: dict,
    project_id_to_alias: dict[int, str],
    stats: AiApplyStats,
) -> None:
    """Add ``reads`` edges for SQL-style ``inputs`` and Python-style ``extra_inputs``."""
    for inp in entry.get("inputs") or []:
        if not isinstance(inp, dict):
            continue
        ref_pid = inp.get("pid", config.project_id)
        bucket = inp.get("bucket", "")
        table = inp.get("table", "")
        if not bucket or not table:
            continue
        ref_alias = project_id_to_alias.get(ref_pid, config.project_alias)
        table_fqn = f"{ref_alias}:{bucket}.{table}"
        if _find_edge(graph, table_fqn, config.fqn):
            continue
        detection = "ai_cross_project" if ref_pid != config.project_id else "ai"
        graph.add_edge(_reads_edge(table_fqn, config.fqn, detection, inp.get("columns", [])))
        stats.reads_added += 1

    for inp in entry.get("extra_inputs") or []:
        if not isinstance(inp, dict) or not inp.get("table_id"):
            continue
        table_fqn = f"{config.project_alias}:{inp['table_id']}"
        if _find_edge(graph, table_fqn, config.fqn):
            continue
        graph.add_edge(_reads_edge(table_fqn, config.fqn, "ai", []))
        stats.reads_added += 1


def _reads_edge(table_fqn: str, config_fqn: str, detection: str, columns: list) -> Edge:
    return Edge(
        source_fqn=table_fqn,
        target_fqn=config_fqn,
        source_type="table",
        target_type="config",
        edge_type="reads",
        detection=detection,
        columns=list(columns or []),
    )


def resolve_output_table(config: Configuration, name: str) -> OutputResolution:
    """Map an AI output table name to a Storage table id of ``config``'s project.

    Precedence: a full Storage table id as-is; then the output mapping whose
    ``source`` equals ``name``; then the output mapping whose destination table
    name equals ``name``, only when exactly one destination matches.
    """
    if ":" in name:
        return OutputResolution(reason=REASON_CROSS_PROJECT)
    if _FULL_TABLE_ID.match(name):
        return OutputResolution(table_id=name)

    by_source = _distinct_destinations(config, lambda out: out.get("source") == name)
    candidates = by_source or _distinct_destinations(
        config, lambda out: str(out.get("destination", "")).rsplit(".", 1)[-1] == name
    )
    if len(candidates) == 1:
        return OutputResolution(table_id=candidates[0])
    if candidates:
        return OutputResolution(reason=REASON_AMBIGUOUS, candidates=candidates)
    return OutputResolution(reason=REASON_NOT_IN_OUTPUT_MAPPING)


def _distinct_destinations(config: Configuration, predicate: Any) -> list[str]:
    found: list[str] = []
    for out in config.output_tables:
        if not isinstance(out, dict):
            continue
        destination = out.get("destination", "")
        if destination and predicate(out) and destination not in found:
            found.append(destination)
    return found


def _apply_outputs(
    graph: LineageGraph, config: Configuration, outputs: list, stats: AiApplyStats
) -> None:
    """Add (or enrich) ``config -> table`` ``writes`` edges from AI ``outputs``."""
    for out in outputs:
        if not isinstance(out, dict) or not isinstance(out.get("table"), str) or not out["table"]:
            continue
        name = out["table"]
        columns = [c for c in out.get("columns") or [] if isinstance(c, str) and c]
        resolution = resolve_output_table(config, name)
        if not resolution.table_id:
            stats.unresolved.append(
                UnresolvedOutput(
                    config_fqn=config.fqn,
                    table=name,
                    reason=resolution.reason,
                    candidates=resolution.candidates,
                )
            )
            continue

        table_fqn = f"{config.project_alias}:{resolution.table_id}"
        existing = _find_edge(graph, config.fqn, table_fqn)
        if existing is not None:
            new_columns = [c for c in dict.fromkeys(columns) if c not in existing.columns]
            # Rebind instead of appending: the list may be shared with a Table.
            existing.columns = [*existing.columns, *new_columns]
            stats.columns_merged += len(new_columns)
            continue
        graph.add_edge(
            Edge(
                source_fqn=config.fqn,
                target_fqn=table_fqn,
                source_type="config",
                target_type="table",
                edge_type="writes",
                detection="ai",
                columns=list(dict.fromkeys(columns)),
            )
        )
        stats.writes_added += 1


def _apply_external(config: Configuration, external: list, stats: AiApplyStats) -> None:
    """Record validated, deduplicated external-system annotations on ``config``."""
    for item in external:
        if not isinstance(item, dict):
            continue
        system = item.get("system")
        op = item.get("op")
        if not isinstance(system, str) or not system.strip() or op not in _EXTERNAL_OPS:
            continue
        annotation = {"system": system.strip(), "op": op}
        if annotation in config.external:
            continue
        config.external.append(annotation)
        stats.external_added += 1


def _table_id(edge: Edge) -> str:
    return edge.source_fqn.split(":", 1)[-1]


def _resolve_col_map_edge(graph: LineageGraph, config: Configuration, in_table: str) -> Edge | None:
    """Find the single ``reads`` edge of ``config`` that ``in_table`` names exactly.

    ``in_table`` may be the input table id (or its ``alias:table`` FQN), or an
    input-mapping destination alias of this config that maps to one source.
    """
    reads = [e for e in graph.edges if e.target_fqn == config.fqn and e.edge_type == "reads"]
    matches = [e for e in reads if in_table in (e.source_fqn, _table_id(e))]
    if not matches:
        sources = {
            inp.get("source")
            for inp in config.input_tables
            if isinstance(inp, dict) and inp.get("destination") == in_table and inp.get("source")
        }
        if len(sources) == 1:
            source = sources.pop()
            matches = [e for e in reads if _table_id(e) == source]
    return matches[0] if len(matches) == 1 else None


def _apply_col_map(
    graph: LineageGraph, config: Configuration, col_map: list, stats: AiApplyStats
) -> None:
    """Attach ``out_col -> "<input table id>.<in_col>"`` to the matching reads edge."""
    for cm in col_map:
        if not isinstance(cm, dict):
            stats.col_map_skipped += 1
            continue
        out_col = cm.get("out", "")
        in_table = cm.get("in_table", "")
        in_col = cm.get("in_col", "")
        edge = (
            _resolve_col_map_edge(graph, config, in_table)
            if out_col and in_col and isinstance(in_table, str) and in_table
            else None
        )
        if edge is None:
            stats.col_map_skipped += 1
            continue
        edge.column_mapping[out_col] = f"{_table_id(edge)}.{in_col}"


# ---------------------------------------------------------------------------
# Generating tasks
# ---------------------------------------------------------------------------

_INSTRUCTIONS = (
    "For each task, read the code_file from disk and extract table dependencies. "
    "Write results to .lineage_ai_results.json (see output_format). "
    "inputs: tables the code reads (pid = project id of the table). "
    "outputs: tables the code writes -- use the full table id (e.g. out.c-bucket.table) "
    "when known; a local name is resolved through the config's output mapping (its "
    "source, else a unique destination table name), and an unresolvable or ambiguous name "
    "is reported, not guessed. "
    "external: systems outside Keboola Storage the code talks to, op is read|write. "
    "col_map: in_table must be the full input table id (or an input-mapping destination "
    "of this config); in_col is the column in that table. "
    "Then re-run `kbagent lineage build` to incorporate the results."
)


def _code_file(config: Configuration) -> Path | None:
    if not config.source_dir:
        return None
    name = "transform.sql" if config.code_type == "sql" else "code.py"
    return Path(config.source_dir).resolve() / name


def generate_ai_tasks(graph: LineageGraph, root: Path) -> dict[str, Any]:
    """Write ``root/.lineage_ai_tasks.json`` and return the task status."""
    configs_needing_ai = [
        c
        for c in graph.configurations.values()
        if c.code and ((c.code_type == "sql" and not c.input_tables) or c.code_type == "python")
    ]

    results_path = root / AI_RESULTS_FILE
    existing_hashes: dict[str, str] = {}
    if results_path.exists():
        with open(results_path, encoding="utf-8") as f:
            for entry in json.load(f).get("results", []):
                fqn = entry.get("config_fqn", "")
                h = entry.get("_code_hash", "")
                if fqn and h:
                    existing_hashes[fqn] = h

    tasks: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    already_done = 0
    for config in configs_needing_ai:
        code_hash = hashlib.sha256(config.code.encode()).hexdigest()[:16]
        if existing_hashes.get(config.fqn) == code_hash:
            already_done += 1
            continue

        code_file = _code_file(config)
        if code_file is None or not code_file.is_file():
            missing.append({"config_fqn": config.fqn, "code_file": str(code_file or "")})
            continue

        task: dict[str, Any] = {
            "config_fqn": config.fqn,
            "project_alias": config.project_alias,
            "project_id": config.project_id,
            "component_id": config.component_id,
            "config_name": config.config_name,
            "code_type": config.code_type,
            "code_file": str(code_file),
            "_code_hash": code_hash,
        }
        if config.code_type == "python":
            task["known_inputs"] = [t.get("source", "") for t in config.input_tables[:10]]
            task["known_outputs"] = [t.get("destination", "") for t in config.output_tables[:10]]
        tasks.append(task)

    tasks_data = {
        "description": "AI analysis tasks for column-level lineage.",
        "instructions": _INSTRUCTIONS,
        "output_file": str(root / AI_RESULTS_FILE),
        "output_format": {
            "results": [
                {
                    "config_fqn": "project:component/config_id",
                    "_code_hash": "hash from task",
                    "inputs": [{"pid": 123, "bucket": "in.c-x", "table": "y", "columns": ["a"]}],
                    "outputs": [{"table": "out.c-x.full_table_id", "columns": ["b"]}],
                    "col_map": [
                        {
                            "out": "b",
                            "in_table": "in.c-x.y",
                            "in_col": "a",
                            "transform": "direct",
                        }
                    ],
                    "extra_inputs": [{"table_id": "bucket.table", "evidence": "code line"}],
                    "external": [{"system": "Slack", "op": "write"}],
                }
            ]
        },
        "sql_context": (
            'Snowflake SQL uses \'KBC_USE4_{project_id}\'."bucket_id"."table_name" '
            "for cross-project references. Same-project tables may use just "
            '"bucket_id"."table_name" or aliased names from input mapping.'
        ),
        "tasks": tasks,
    }

    tasks_path = root / AI_TASKS_FILE
    with open(tasks_path, "w", encoding="utf-8") as f:
        json.dump(tasks_data, f, indent=2)

    return {
        "ai_tasks_generated": len(tasks),
        "ai_tasks_file": str(tasks_path),
        "ai_already_done": already_done,
        "ai_tasks_missing_code_file": missing,
    }
