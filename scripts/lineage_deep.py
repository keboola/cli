#!/usr/bin/env python3
"""
Column-level lineage analysis for Keboola organizations.

Scans sync'd project data on disk (from `kbagent sync pull --all-projects`),
builds a comprehensive dependency graph at table and column level, and
optionally uses AI to parse SQL/Python code for hidden dependencies.

Usage:
    # Build lineage from sync'd data
    python scripts/lineage_deep.py /tmp/lineage-ro --output lineage.json

    # Query upstream dependencies of a table
    python scripts/lineage_deep.py /tmp/lineage-ro --upstream "out.c-sfdc.company" --project ir-l0-sales-marketing

    # Query downstream impact of a table
    python scripts/lineage_deep.py /tmp/lineage-ro --downstream "in.c-in_wr_storage_kbc_telemetry.kbc_project" --project ir-l0-kbc-telemetry-to-catalog

    # Build with AI analysis of SQL/Python code
    python scripts/lineage_deep.py /tmp/lineage-ro --output lineage.json --ai
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Column:
    name: str
    data_type: str = "STRING"


@dataclass
class Table:
    """A storage table in a Keboola project."""

    table_id: str  # e.g. "in.c-bucket.table_name"
    project_alias: str
    project_id: int
    bucket_id: str  # e.g. "in.c-bucket"
    name: str  # e.g. "table_name"
    columns: list[str] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    rows_count: int = 0

    @property
    def fqn(self) -> str:
        """Fully qualified name: project_alias:table_id."""
        return f"{self.project_alias}:{self.table_id}"


@dataclass
class Configuration:
    """A Keboola configuration (extractor, transformation, writer, etc.)."""

    config_id: str
    config_name: str
    component_id: str
    component_type: str  # extractor, transformation, writer, application, other
    project_alias: str
    project_id: int
    path: str  # relative path in sync directory

    # Deterministic mappings from _config.yml
    input_tables: list[dict] = field(default_factory=list)
    output_tables: list[dict] = field(default_factory=list)

    # SQL/Python code if present
    code: str = ""
    code_type: str = ""  # "sql", "python", ""

    # Config rows (for row-based components)
    rows: list[dict] = field(default_factory=list)

    @property
    def fqn(self) -> str:
        """Fully qualified name: project_alias:component_id/config_id."""
        return f"{self.project_alias}:{self.component_id}/{self.config_id}"


@dataclass
class Edge:
    """A dependency edge in the lineage graph."""

    source_fqn: str  # FQN of source node (table or config)
    target_fqn: str  # FQN of target node (table or config)
    source_type: str  # "table" or "config"
    target_type: str  # "table" or "config"
    edge_type: str  # "reads", "writes", "cross_project_share"
    detection: str  # "input_mapping", "output_mapping", "sql_regex", "sql_ai", "python_ai", "bucket_sharing"
    columns: list[str] = field(default_factory=list)  # columns involved
    column_mapping: dict[str, str] = field(default_factory=dict)  # target_col -> source_col


@dataclass
class LineageGraph:
    """Complete lineage graph for an organization."""

    tables: dict[str, Table] = field(default_factory=dict)  # fqn -> Table
    configurations: dict[str, Configuration] = field(default_factory=dict)  # fqn -> Config
    edges: list[Edge] = field(default_factory=list)

    # Indexes for fast lookup
    _upstream: dict[str, list[int]] = field(default_factory=dict)  # node_fqn -> edge indices
    _downstream: dict[str, list[int]] = field(default_factory=dict)  # node_fqn -> edge indices
    _project_id_to_alias: dict[int, str] = field(default_factory=dict)  # project_id -> alias

    def add_edge(self, edge: Edge) -> None:
        idx = len(self.edges)
        self.edges.append(edge)
        self._downstream.setdefault(edge.source_fqn, []).append(idx)
        self._upstream.setdefault(edge.target_fqn, []).append(idx)

    def get_upstream(self, fqn: str, depth: int = 1) -> list[dict]:
        """Get all upstream dependencies of a node, up to given depth."""
        visited: set[str] = set()
        result: list[dict] = []
        self._walk_upstream(fqn, depth, 0, visited, result)
        return result

    def _walk_upstream(
        self, fqn: str, max_depth: int, current_depth: int, visited: set[str], result: list[dict]
    ) -> None:
        if current_depth >= max_depth or fqn in visited:
            return
        visited.add(fqn)
        for edge_idx in self._upstream.get(fqn, []):
            edge = self.edges[edge_idx]
            result.append(
                {
                    "depth": current_depth + 1,
                    "source": edge.source_fqn,
                    "target": edge.target_fqn,
                    "edge_type": edge.edge_type,
                    "detection": edge.detection,
                    "columns": edge.columns,
                }
            )
            self._walk_upstream(edge.source_fqn, max_depth, current_depth + 1, visited, result)

    def get_downstream(self, fqn: str, depth: int = 1) -> list[dict]:
        """Get all downstream dependents of a node, up to given depth."""
        visited: set[str] = set()
        result: list[dict] = []
        self._walk_downstream(fqn, depth, 0, visited, result)
        return result

    def _walk_downstream(
        self, fqn: str, max_depth: int, current_depth: int, visited: set[str], result: list[dict]
    ) -> None:
        if current_depth >= max_depth or fqn in visited:
            return
        visited.add(fqn)
        for edge_idx in self._downstream.get(fqn, []):
            edge = self.edges[edge_idx]
            result.append(
                {
                    "depth": current_depth + 1,
                    "source": edge.source_fqn,
                    "target": edge.target_fqn,
                    "edge_type": edge.edge_type,
                    "detection": edge.detection,
                    "columns": edge.columns,
                }
            )
            self._walk_downstream(edge.target_fqn, max_depth, current_depth + 1, visited, result)

    def summary(self) -> dict:
        return {
            "tables": len(self.tables),
            "configurations": len(self.configurations),
            "edges": len(self.edges),
            "edge_types": self._count_by("edge_type"),
            "detection_methods": self._count_by("detection"),
        }

    def _count_by(self, attr: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for edge in self.edges:
            val = getattr(edge, attr)
            counts[val] = counts.get(val, 0) + 1
        return counts

    def to_dict(self) -> dict:
        return {
            "summary": self.summary(),
            "tables": {fqn: asdict(t) for fqn, t in self.tables.items()},
            "configurations": {
                fqn: {
                    "config_id": c.config_id,
                    "config_name": c.config_name,
                    "component_id": c.component_id,
                    "component_type": c.component_type,
                    "project_alias": c.project_alias,
                    "project_id": c.project_id,
                    "path": c.path,
                    "code_type": c.code_type,
                    "input_table_count": len(c.input_tables),
                    "output_table_count": len(c.output_tables),
                }
                for fqn, c in self.configurations.items()
            },
            "edges": [asdict(e) for e in self.edges],
        }


# ---------------------------------------------------------------------------
# Phase 1: Scan sync'd data from disk
# ---------------------------------------------------------------------------

# Snowflake DB pattern: "KBC_USE4_{project_id}"
KBC_DB_PATTERN = re.compile(r'"KBC_USE4_(\d+)"\.?"([^"]+)"\.?"([^"]+)"')

# SQL table components
SQL_COMPONENTS = {
    "keboola.snowflake-transformation",
    "keboola.synapse-transformation",
    "keboola.oracle-transformation",
    "keboola.redshift-sql-transformation",
}

PYTHON_COMPONENTS = {
    "keboola.python-transformation-v2",
    "kds-team.app-custom-python",
}

DBT_COMPONENTS = {
    "keboola.dbt-transformation",
    "keboola.dbt-transformation-remote-snowflake",
}


def _load_project_id_map(root: Path) -> dict[int, str]:
    """Load project_id -> alias mapping from kbagent project list."""
    mapping: dict[int, str] = {}
    try:
        result = subprocess.run(
            ["kbagent", "--json", "project", "list"],
            capture_output=True,
            text=True,
            cwd=str(root),
            timeout=30,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            for proj in data.get("data", []):
                pid = proj.get("project_id")
                alias = proj.get("alias", "")
                if pid and alias:
                    mapping[pid] = alias
            log.info("  Loaded %d project aliases from kbagent", len(mapping))
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        log.warning("Could not load project list from kbagent: %s", e)
    return mapping


def scan_projects(root: Path) -> LineageGraph:
    """Scan all sync'd projects and build the lineage graph."""
    graph = LineageGraph()

    # First, get ALL project aliases (including non-sync'd) from kbagent
    project_id_to_alias = _load_project_id_map(root)

    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir() or project_dir.name.startswith("."):
            continue
        manifest_path = project_dir / ".keboola" / "manifest.json"
        if not manifest_path.exists():
            continue

        with open(manifest_path) as f:
            manifest = json.load(f)

        project_alias = project_dir.name
        project_id = manifest.get("project", {}).get("id", 0)
        project_id_to_alias[project_id] = project_alias

        log.info("Scanning project: %s (id=%d)", project_alias, project_id)

        # --- Scan storage tables ---
        storage_dir = project_dir / "storage" / "tables"
        if storage_dir.exists():
            _scan_storage_tables(storage_dir, project_alias, project_id, graph)

        # --- Scan configurations ---
        for config_entry in manifest.get("configurations", []):
            _scan_configuration(project_dir, config_entry, project_alias, project_id, graph)

    # Store the project_id->alias mapping for cross-project resolution
    graph._project_id_to_alias = project_id_to_alias
    return graph


def _scan_storage_tables(
    storage_dir: Path, project_alias: str, project_id: int, graph: LineageGraph
) -> None:
    """Read storage table metadata from disk."""
    for bucket_dir in sorted(storage_dir.iterdir()):
        if not bucket_dir.is_dir():
            continue
        for table_file in sorted(bucket_dir.glob("*.json")):
            with open(table_file) as f:
                meta = json.load(f)

            table = Table(
                table_id=meta["id"],
                project_alias=project_alias,
                project_id=project_id,
                bucket_id=meta["id"].rsplit(".", 1)[0],
                name=meta["name"],
                columns=meta.get("columns", []),
                primary_key=meta.get("primary_key", []),
                rows_count=meta.get("rows_count", 0),
            )
            graph.tables[table.fqn] = table


def _scan_configuration(
    project_dir: Path,
    config_entry: dict,
    project_alias: str,
    project_id: int,
    graph: LineageGraph,
) -> None:
    """Read a single configuration and add it to the graph."""
    config_path = config_entry["path"]
    component_id = config_entry["componentId"]
    config_id = config_entry["id"]
    full_path = project_dir / "main" / config_path

    # Determine component type from path prefix
    component_type = config_path.split("/")[0] if "/" in config_path else "unknown"

    # Read _config.yml
    config_yml_path = full_path / "_config.yml"
    input_tables: list[dict] = []
    output_tables: list[dict] = []
    config_name = ""

    if config_yml_path.exists():
        with open(config_yml_path) as f:
            cfg = yaml.safe_load(f) or {}
        config_name = cfg.get("name", "")
        input_tables = cfg.get("input", {}).get("tables", []) or []
        output_tables = cfg.get("output", {}).get("tables", []) or []

    # Read code files
    code = ""
    code_type = ""
    transform_sql = full_path / "transform.sql"
    code_py = full_path / "code.py"

    if transform_sql.exists():
        code = transform_sql.read_text()
        code_type = "sql"
    elif code_py.exists():
        code = code_py.read_text()
        code_type = "python"

    # Read config rows
    rows: list[dict] = []
    for row_entry in config_entry.get("rows", []):
        row_path = project_dir / "main" / row_entry.get("path", "")
        row_config = row_path / "_config.yml"
        if row_config.exists():
            with open(row_config) as f:
                row_cfg = yaml.safe_load(f) or {}
            row_data = {
                "id": row_entry.get("id", ""),
                "path": row_entry.get("path", ""),
                "input_tables": row_cfg.get("input", {}).get("tables", []) or [],
                "output_tables": row_cfg.get("output", {}).get("tables", []) or [],
            }
            rows.append(row_data)
            # Add row-level input/output tables to config level
            input_tables.extend(row_data["input_tables"])
            output_tables.extend(row_data["output_tables"])

    config = Configuration(
        config_id=config_id,
        config_name=config_name,
        component_id=component_id,
        component_type=component_type,
        project_alias=project_alias,
        project_id=project_id,
        path=config_path,
        input_tables=input_tables,
        output_tables=output_tables,
        code=code,
        code_type=code_type,
        rows=rows,
    )
    graph.configurations[config.fqn] = config


# ---------------------------------------------------------------------------
# Phase 2: Build deterministic edges
# ---------------------------------------------------------------------------


def build_deterministic_edges(graph: LineageGraph) -> None:
    """Build edges from input/output mappings and SQL regex analysis."""
    for config_fqn, config in graph.configurations.items():
        # --- Input mapping edges: table -> config (reads) ---
        for inp in config.input_tables:
            source_table_id = inp.get("source", "")
            if not source_table_id:
                continue
            table_fqn = f"{config.project_alias}:{source_table_id}"
            columns = inp.get("columns", [])
            if not columns:
                # If no explicit columns, try to get all from table metadata
                table = graph.tables.get(table_fqn)
                if table:
                    columns = table.columns

            graph.add_edge(
                Edge(
                    source_fqn=table_fqn,
                    target_fqn=config_fqn,
                    source_type="table",
                    target_type="config",
                    edge_type="reads",
                    detection="input_mapping",
                    columns=columns,
                )
            )

        # --- Output mapping edges: config -> table (writes) ---
        for out in config.output_tables:
            dest_table_id = out.get("destination", "")
            if not dest_table_id:
                continue
            table_fqn = f"{config.project_alias}:{dest_table_id}"

            graph.add_edge(
                Edge(
                    source_fqn=config_fqn,
                    target_fqn=table_fqn,
                    source_type="config",
                    target_type="table",
                    edge_type="writes",
                    detection="output_mapping",
                )
            )

        # --- SQL regex: find KBC_USE4_XXX references ---
        if config.code_type == "sql" and config.code:
            _extract_sql_table_refs(graph, config)


def _extract_sql_table_refs(graph: LineageGraph, config: Configuration) -> None:
    """Extract table references from SQL code using regex for Snowflake qualified names."""
    project_id_to_alias: dict[int, str] = getattr(graph, "_project_id_to_alias", {})

    matches = KBC_DB_PATTERN.findall(config.code)
    seen: set[tuple[int, str, str]] = set()

    for ref_pid_str, ref_bucket, ref_table in matches:
        ref_pid = int(ref_pid_str)
        key = (ref_pid, ref_bucket, ref_table)
        if key in seen:
            continue
        seen.add(key)

        # Resolve project alias
        ref_alias = project_id_to_alias.get(ref_pid, f"unknown-{ref_pid}")
        table_id = f"{ref_bucket}.{ref_table}"
        table_fqn = f"{ref_alias}:{table_id}"

        # Skip if already covered by input mapping
        already_mapped = any(inp.get("source", "") == table_id for inp in config.input_tables)
        if already_mapped:
            continue

        # Try to get columns from table metadata
        table = graph.tables.get(table_fqn)
        columns = table.columns if table else []

        detection = "sql_regex"
        edge_type = "reads"

        # If cross-project, also mark as cross_project
        if ref_pid != config.project_id:
            detection = "sql_regex_cross_project"

        graph.add_edge(
            Edge(
                source_fqn=table_fqn,
                target_fqn=config.fqn,
                source_type="table",
                target_type="config",
                edge_type=edge_type,
                detection=detection,
                columns=columns,
            )
        )


# ---------------------------------------------------------------------------
# Phase 3: Cross-project lineage from kbagent
# ---------------------------------------------------------------------------


def add_cross_project_lineage(graph: LineageGraph, root: Path) -> None:
    """Get cross-project bucket sharing lineage from kbagent."""
    try:
        result = subprocess.run(
            ["kbagent", "--json", "lineage"],
            capture_output=True,
            text=True,
            cwd=str(root),
            timeout=120,
        )
        if result.returncode != 0:
            log.warning("kbagent lineage failed: %s", result.stderr[:200])
            return

        data = json.loads(result.stdout)
        edges = data.get("data", {}).get("edges", [])

        for edge_data in edges:
            source_alias = edge_data.get("source_project_alias", "")
            target_alias = edge_data.get("target_project_alias", "")
            source_bucket = edge_data.get("source_bucket_id", "")
            target_bucket = edge_data.get("target_bucket_id", "")

            if not source_alias or not target_alias:
                continue

            # Find all tables in the source bucket and create edges
            source_tables = [
                t
                for fqn, t in graph.tables.items()
                if t.project_alias == source_alias and t.bucket_id == source_bucket
            ]
            target_tables = [
                t
                for fqn, t in graph.tables.items()
                if t.project_alias == target_alias and t.bucket_id == target_bucket
            ]

            # Match tables by name between source and target bucket
            source_by_name = {t.name: t for t in source_tables}
            for target_table in target_tables:
                source_table = source_by_name.get(target_table.name)
                if source_table:
                    graph.add_edge(
                        Edge(
                            source_fqn=source_table.fqn,
                            target_fqn=target_table.fqn,
                            source_type="table",
                            target_type="table",
                            edge_type="cross_project_share",
                            detection="bucket_sharing",
                            columns=source_table.columns,
                        )
                    )

        log.info("Added %d cross-project sharing edges from kbagent lineage", len(edges))

    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
        log.warning("Could not get cross-project lineage: %s", e)


# ---------------------------------------------------------------------------
# Phase 4: AI-assisted analysis (optional)
# ---------------------------------------------------------------------------

AI_SQL_PROMPT = """Extract table dependencies from this Snowflake SQL. Tables use format "KBC_USE4_{{pid}}"."bucket"."table" or just "table_alias".
Project: {project_alias} (pid={project_id})

Return JSON only:
{{"inputs":[{{"pid":123,"bucket":"x","table":"y","columns":["a","b"]}}],"outputs":[{{"table":"local_name","columns":["c","d"]}}],"col_map":[{{"out":"c","in_table":"bucket.table","in_col":"a","transform":"direct|expression"}}]}}

SQL:
{sql_code}"""

AI_PYTHON_PROMPT = """Extract Keboola table dependencies from this Python code beyond what input/output mapping covers.
Project: {project_alias} (pid={project_id})
Known inputs: {known_inputs}
Known outputs: {known_outputs}

Return JSON only:
{{"extra_inputs":[{{"table_id":"bucket.table","evidence":"code line"}}],"extra_outputs":[{{"table_id":"bucket.table","evidence":"code line"}}],"external":[{{"system":"name","op":"read|write"}}]}}

Python:
{python_code}"""

# AI results cache file name
AI_CACHE_FILE = ".lineage_ai_cache.json"


def run_ai_analysis(
    graph: LineageGraph, root: Path, model: str = "haiku", max_workers: int = 4
) -> None:
    """Use AI to analyze SQL/Python code for hidden dependencies.

    Args:
        graph: The lineage graph to enrich.
        root: Root sync directory (for cache file location).
        model: Claude model to use - "haiku" (fast/cheap) or "sonnet" (better quality).
    """
    # Load existing AI cache
    cache_path = root / AI_CACHE_FILE
    ai_cache: dict[str, dict] = {}
    if cache_path.exists():
        with open(cache_path) as f:
            ai_cache = json.load(f)
        log.info("  Loaded AI cache with %d entries", len(ai_cache))

    configs_needing_ai = [
        c
        for c in graph.configurations.values()
        if c.code and ((c.code_type == "sql" and not c.input_tables) or c.code_type == "python")
    ]

    if not configs_needing_ai:
        log.info("No configurations need AI analysis")
        return

    # Filter out already cached
    uncached = [c for c in configs_needing_ai if c.fqn not in ai_cache]
    cached = [c for c in configs_needing_ai if c.fqn in ai_cache]

    log.info(
        "AI analysis: %d total, %d cached, %d to analyze",
        len(configs_needing_ai),
        len(cached),
        len(uncached),
    )

    # Apply cached results first
    for config in cached:
        _apply_ai_result(graph, config, ai_cache[config.fqn])

    # Analyze uncached configs in parallel
    max_workers = min(max_workers, len(uncached)) or 1

    def _analyze_one(config: Configuration) -> tuple[str, dict | None]:
        if config.code_type == "sql":
            return config.fqn, _ai_analyze_sql(config, model)
        elif config.code_type == "python":
            return config.fqn, _ai_analyze_python(config, model)
        return config.fqn, None

    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_analyze_one, c): c for c in uncached}
        for future in as_completed(futures):
            config = futures[future]
            completed += 1
            try:
                fqn, result = future.result()
                if result:
                    ai_cache[fqn] = result
                    _apply_ai_result(graph, config, result)
                    log.info(
                        "  [%d/%d] OK %s: %s",
                        completed,
                        len(uncached),
                        config.code_type,
                        config.path,
                    )
                else:
                    log.warning(
                        "  [%d/%d] FAIL %s: %s",
                        completed,
                        len(uncached),
                        config.code_type,
                        config.path,
                    )
            except Exception as e:
                log.error(
                    "  [%d/%d] ERROR %s: %s - %s",
                    completed,
                    len(uncached),
                    config.code_type,
                    config.path,
                    e,
                )

            # Save cache incrementally every 10 items
            if completed % 10 == 0:
                with open(cache_path, "w") as f:
                    json.dump(ai_cache, f, indent=2)

    # Final cache save
    with open(cache_path, "w") as f:
        json.dump(ai_cache, f, indent=2)
    log.info("  AI cache saved with %d entries", len(ai_cache))


def _ai_analyze_sql(config: Configuration, model: str) -> dict | None:
    """Analyze SQL via AI and return structured result."""
    prompt = AI_SQL_PROMPT.format(
        project_alias=config.project_alias,
        project_id=config.project_id,
        sql_code=config.code[:6000],
    )
    return _call_ai(prompt, model)


def _ai_analyze_python(config: Configuration, model: str) -> dict | None:
    """Analyze Python via AI and return structured result."""
    known_in = [t.get("source", "") for t in config.input_tables[:10]]
    known_out = [t.get("destination", "") for t in config.output_tables[:10]]
    prompt = AI_PYTHON_PROMPT.format(
        project_alias=config.project_alias,
        project_id=config.project_id,
        known_inputs=", ".join(known_in) or "none",
        known_outputs=", ".join(known_out) or "none",
        python_code=config.code[:6000],
    )
    return _call_ai(prompt, model)


def _apply_ai_result(graph: LineageGraph, config: Configuration, result: dict) -> None:
    """Apply AI analysis result to the lineage graph."""
    project_id_to_alias: dict[int, str] = getattr(graph, "_project_id_to_alias", {})

    # SQL-style results
    for inp in result.get("inputs", []):
        ref_pid = inp.get("pid", config.project_id)
        bucket = inp.get("bucket", "")
        table = inp.get("table", "")
        columns_used = inp.get("columns", [])

        if not bucket or not table:
            continue

        table_id = f"{bucket}.{table}"
        ref_alias = project_id_to_alias.get(ref_pid, config.project_alias)
        table_fqn = f"{ref_alias}:{table_id}"

        # Skip duplicates
        if any(e.source_fqn == table_fqn and e.target_fqn == config.fqn for e in graph.edges):
            continue

        detection = "sql_ai_cross_project" if ref_pid != config.project_id else "sql_ai"
        graph.add_edge(
            Edge(
                source_fqn=table_fqn,
                target_fqn=config.fqn,
                source_type="table",
                target_type="config",
                edge_type="reads",
                detection=detection,
                columns=columns_used,
            )
        )

    # Column mappings
    for cm in result.get("col_map", []):
        out_col = cm.get("out", "")
        in_table = cm.get("in_table", "")
        in_col = cm.get("in_col", "")
        if out_col and in_col:
            for edge in graph.edges:
                if edge.target_fqn == config.fqn and in_table in edge.source_fqn:
                    edge.column_mapping[out_col] = f"{in_table}.{in_col}"

    # Python-style results
    for inp in result.get("extra_inputs", []):
        table_id = inp.get("table_id", "")
        if not table_id:
            continue
        table_fqn = f"{config.project_alias}:{table_id}"
        if any(e.source_fqn == table_fqn and e.target_fqn == config.fqn for e in graph.edges):
            continue
        graph.add_edge(
            Edge(
                source_fqn=table_fqn,
                target_fqn=config.fqn,
                source_type="table",
                target_type="config",
                edge_type="reads",
                detection="python_ai",
            )
        )

    for out in result.get("extra_outputs", []):
        table_id = out.get("table_id", "")
        if not table_id:
            continue
        table_fqn = f"{config.project_alias}:{table_id}"
        if any(e.source_fqn == config.fqn and e.target_fqn == table_fqn for e in graph.edges):
            continue
        graph.add_edge(
            Edge(
                source_fqn=config.fqn,
                target_fqn=table_fqn,
                source_type="config",
                target_type="table",
                edge_type="writes",
                detection="python_ai",
            )
        )


def _call_ai(prompt: str, model: str = "haiku") -> dict | None:
    """Call AI model via Claude CLI. Returns parsed JSON or None."""
    model_map = {
        "haiku": "haiku",
        "sonnet": "sonnet",
        "opus": "opus",
    }
    claude_model = model_map.get(model, "haiku")

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", claude_model, "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            response = json.loads(result.stdout)
            text = response.get("result", "")
            parsed = _extract_json(text)
            if parsed:
                return parsed
            log.warning("  AI returned unparseable response")
    except FileNotFoundError:
        log.error("Claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code")
    except subprocess.TimeoutExpired:
        log.warning("  AI call timed out")
    except json.JSONDecodeError:
        log.warning("  AI returned invalid JSON")

    return None


def _extract_json(text: str) -> dict | None:
    """Extract JSON object from text that may contain other content."""
    # Try direct parse
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass

    if not text:
        return None

    # Try markdown code fence
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try first { ... } block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


# ---------------------------------------------------------------------------
# Query and output
# ---------------------------------------------------------------------------


def _describe_node(graph: LineageGraph, fqn: str) -> str:
    """Get a human-readable description for a node FQN."""
    if fqn in graph.tables:
        t = graph.tables[fqn]
        return f"[table] {fqn} ({len(t.columns)} cols, {t.rows_count:,} rows)"
    if fqn in graph.configurations:
        c = graph.configurations[fqn]
        return f"[{c.component_type}] {c.project_alias}:{c.config_name} ({c.component_id})"
    # Unknown node - just return FQN with type guess
    if "/" in fqn.split(":")[-1]:
        return f"[config] {fqn}"
    return f"[table] {fqn}"


def format_upstream(graph: LineageGraph, fqn: str, depth: int = 10) -> str:
    """Format upstream dependencies as readable text."""
    edges = graph.get_upstream(fqn, depth)
    if not edges:
        return f"No upstream dependencies found for {fqn}"

    lines = [f"Upstream dependencies of {_describe_node(graph, fqn)}:", ""]
    for edge in sorted(edges, key=lambda e: e["depth"]):
        indent = "  " * edge["depth"]
        cols = ""
        if edge["columns"]:
            col_list = edge["columns"][:5]
            suffix = f"... +{len(edge['columns']) - 5}" if len(edge["columns"]) > 5 else ""
            cols = f" [{', '.join(col_list)}{suffix}]"
        node_desc = _describe_node(graph, edge["source"])
        lines.append(f"{indent}<- ({edge['detection']}) {node_desc}{cols}")
    return "\n".join(lines)


def format_downstream(graph: LineageGraph, fqn: str, depth: int = 10) -> str:
    """Format downstream dependents as readable text."""
    edges = graph.get_downstream(fqn, depth)
    if not edges:
        return f"No downstream dependents found for {fqn}"

    lines = [f"Downstream dependents of {_describe_node(graph, fqn)}:", ""]
    for edge in sorted(edges, key=lambda e: e["depth"]):
        indent = "  " * edge["depth"]
        cols = ""
        if edge["columns"]:
            col_list = edge["columns"][:5]
            suffix = f"... +{len(edge['columns']) - 5}" if len(edge["columns"]) > 5 else ""
            cols = f" [{', '.join(col_list)}{suffix}]"
        node_desc = _describe_node(graph, edge["target"])
        lines.append(f"{indent}-> ({edge['detection']}) {node_desc}{cols}")
    return "\n".join(lines)


def find_node_fqn(graph: LineageGraph, identifier: str, project_alias: str = "") -> str | None:
    """Find the full FQN for a table or config node.

    Identifier can be:
      - Full FQN: "project_alias:table_id"
      - Table ID: "bucket_id.table_name" (searches all projects)
      - Config pattern: "component_id/config_id"
    """
    # If it looks like a full FQN already
    if ":" in identifier:
        if identifier in graph.tables or identifier in graph.configurations:
            return identifier
        # Check edges too
        all_fqns = set()
        for e in graph.edges:
            all_fqns.add(e.source_fqn)
            all_fqns.add(e.target_fqn)
        if identifier in all_fqns:
            return identifier
        return None

    if project_alias:
        fqn = f"{project_alias}:{identifier}"
        if fqn in graph.tables or fqn in graph.configurations:
            return fqn
        # Also check edges - node might exist only as a reference
        all_fqns = set()
        for e in graph.edges:
            all_fqns.add(e.source_fqn)
            all_fqns.add(e.target_fqn)
        if fqn in all_fqns:
            return fqn
        return None

    # Search across all projects - tables first, then configs
    all_nodes = set(graph.tables.keys()) | set(graph.configurations.keys())
    # Also include nodes referenced in edges but not in catalog
    for e in graph.edges:
        all_nodes.add(e.source_fqn)
        all_nodes.add(e.target_fqn)

    matches = [fqn for fqn in all_nodes if fqn.endswith(f":{identifier}")]
    if len(matches) == 1:
        return matches[0]
    if matches:
        log.warning(
            "Ambiguous identifier '%s' - found in %d locations: %s",
            identifier,
            len(matches),
            ", ".join(sorted(matches)[:5]),
        )
        return matches[0]

    # Partial match on table name (last segment)
    partial = [fqn for fqn in all_nodes if fqn.split(":")[-1].endswith(f".{identifier}")]
    if len(partial) == 1:
        return partial[0]
    if partial:
        log.warning(
            "Partial match for '%s' - found %d: %s",
            identifier,
            len(partial),
            ", ".join(sorted(partial)[:5]),
        )
        return partial[0]

    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def load_graph_from_cache(cache_path: Path) -> LineageGraph:
    """Load a previously saved lineage graph from JSON cache."""
    with open(cache_path) as f:
        data = json.load(f)

    graph = LineageGraph()

    # Rebuild tables
    for fqn, t_data in data.get("tables", {}).items():
        graph.tables[fqn] = Table(**t_data)

    # Rebuild configurations (minimal - no code)
    for fqn, c_data in data.get("configurations", {}).items():
        graph.configurations[fqn] = Configuration(
            config_id=c_data["config_id"],
            config_name=c_data["config_name"],
            component_id=c_data["component_id"],
            component_type=c_data["component_type"],
            project_alias=c_data["project_alias"],
            project_id=c_data["project_id"],
            path=c_data["path"],
            code_type=c_data.get("code_type", ""),
        )

    # Rebuild edges and indexes
    for e_data in data.get("edges", []):
        edge = Edge(
            source_fqn=e_data["source_fqn"],
            target_fqn=e_data["target_fqn"],
            source_type=e_data["source_type"],
            target_type=e_data["target_type"],
            edge_type=e_data["edge_type"],
            detection=e_data["detection"],
            columns=e_data.get("columns", []),
            column_mapping=e_data.get("column_mapping", {}),
        )
        graph.add_edge(edge)

    return graph


def build_or_load_graph(args: argparse.Namespace) -> LineageGraph:
    """Build graph from disk or load from cache."""
    # If --load specified, use cached graph
    if args.load:
        log.info("Loading lineage graph from cache: %s", args.load)
        graph = load_graph_from_cache(args.load)
        log.info(
            "  Loaded %d tables, %d configs, %d edges",
            len(graph.tables),
            len(graph.configurations),
            len(graph.edges),
        )
        return graph

    if not args.root or not args.root.is_dir():
        log.error("Root directory does not exist: %s", args.root)
        sys.exit(1)

    # Phase 1: Scan
    log.info("Phase 1: Scanning sync'd data from %s", args.root)
    graph = scan_projects(args.root)
    log.info("  Found %d tables, %d configurations", len(graph.tables), len(graph.configurations))

    # Phase 2: Deterministic edges
    log.info("Phase 2: Building deterministic lineage edges")
    build_deterministic_edges(graph)
    log.info("  Built %d edges", len(graph.edges))

    # Phase 3: Cross-project lineage
    log.info("Phase 3: Adding cross-project bucket sharing lineage")
    add_cross_project_lineage(graph, args.root)
    log.info("  Total edges: %d", len(graph.edges))

    # Phase 4: AI analysis (optional)
    if args.ai:
        log.info("Phase 4: AI-assisted SQL/Python analysis (model=%s)", args.ai_model)
        run_ai_analysis(graph, args.root, model=args.ai_model, max_workers=args.ai_workers)
        log.info("  Total edges after AI: %d", len(graph.edges))

    # Output summary
    summary = graph.summary()
    log.info("=== Lineage Summary ===")
    log.info("  Tables: %d", summary["tables"])
    log.info("  Configurations: %d", summary["configurations"])
    log.info("  Edges: %d", summary["edges"])
    log.info("  By type: %s", json.dumps(summary["edge_types"]))
    log.info("  By detection: %s", json.dumps(summary["detection_methods"]))

    # Save to file
    if args.output:
        with open(args.output, "w") as f:
            json.dump(graph.to_dict(), f, indent=2)
        log.info("Lineage graph saved to %s", args.output)

    return graph


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Column-level lineage analysis for Keboola organizations"
    )
    parser.add_argument("root", nargs="?", type=Path, help="Root directory with sync'd projects")
    parser.add_argument("--load", "-l", type=Path, help="Load from cached lineage JSON (skip scan)")
    parser.add_argument("--output", "-o", type=Path, help="Output JSON file for lineage graph")
    parser.add_argument("--upstream", type=str, help="Show upstream of a table or config")
    parser.add_argument("--downstream", type=str, help="Show downstream of a table or config")
    parser.add_argument("--project", "-p", type=str, help="Project alias filter for queries")
    parser.add_argument("--depth", type=int, default=10, help="Max traversal depth (default: 10)")
    parser.add_argument("--ai", action="store_true", help="Enable AI analysis of SQL/Python code")
    parser.add_argument(
        "--ai-model",
        choices=["haiku", "sonnet", "opus"],
        default="haiku",
        help="AI model for SQL/Python analysis (default: haiku)",
    )
    parser.add_argument(
        "--ai-workers", type=int, default=4, help="Number of parallel AI workers (default: 4)"
    )
    parser.add_argument("--stats", action="store_true", help="Show detailed statistics")
    parser.add_argument("--json-output", action="store_true", help="Output query results as JSON")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.root and not args.load:
        parser.error("Either root directory or --load cache file is required")

    graph = build_or_load_graph(args)

    # Stats mode
    if args.stats:
        _print_stats(graph)

    # Query mode
    if args.upstream:
        fqn = find_node_fqn(graph, args.upstream, args.project or "")
        if fqn:
            edges = graph.get_upstream(fqn, args.depth)
            if args.json_output:
                print(json.dumps({"node": fqn, "direction": "upstream", "edges": edges}, indent=2))
            else:
                print(format_upstream(graph, fqn, args.depth))
        else:
            log.error("Node not found: %s (try without --project to search all)", args.upstream)
            _suggest_similar(graph, args.upstream)
            sys.exit(1)

    if args.downstream:
        fqn = find_node_fqn(graph, args.downstream, args.project or "")
        if fqn:
            edges = graph.get_downstream(fqn, args.depth)
            if args.json_output:
                print(
                    json.dumps({"node": fqn, "direction": "downstream", "edges": edges}, indent=2)
                )
            else:
                print(format_downstream(graph, fqn, args.depth))
        else:
            log.error("Node not found: %s (try without --project to search all)", args.downstream)
            _suggest_similar(graph, args.downstream)
            sys.exit(1)


def _print_stats(graph: LineageGraph) -> None:
    """Print detailed statistics about the lineage graph."""
    summary = graph.summary()
    print("\n=== Lineage Graph Statistics ===")
    print(f"  Tables: {summary['tables']}")
    print(f"  Configurations: {summary['configurations']}")
    print(f"  Edges: {summary['edges']}")
    print("\n  Edge types:")
    for k, v in sorted(summary["edge_types"].items(), key=lambda x: -x[1]):
        print(f"    {k}: {v}")
    print("\n  Detection methods:")
    for k, v in sorted(summary["detection_methods"].items(), key=lambda x: -x[1]):
        print(f"    {k}: {v}")

    # Most connected tables
    connections: dict[str, int] = {}
    for e in graph.edges:
        connections[e.source_fqn] = connections.get(e.source_fqn, 0) + 1
        connections[e.target_fqn] = connections.get(e.target_fqn, 0) + 1

    top_nodes = sorted(connections.items(), key=lambda x: -x[1])[:15]
    print("\n  Most connected nodes:")
    for fqn, count in top_nodes:
        node_type = "table" if fqn in graph.tables else "config"
        print(f"    [{node_type}] {fqn}: {count} connections")

    # Projects by edge count
    project_edges: dict[str, int] = {}
    for e in graph.edges:
        for fqn in (e.source_fqn, e.target_fqn):
            proj = fqn.split(":")[0]
            project_edges[proj] = project_edges.get(proj, 0) + 1
    print("\n  Edges per project:")
    for proj, count in sorted(project_edges.items(), key=lambda x: -x[1]):
        print(f"    {proj}: {count}")


def _suggest_similar(graph: LineageGraph, identifier: str) -> None:
    """Suggest similar node names when lookup fails."""
    all_fqns = set(graph.tables.keys()) | set(graph.configurations.keys())
    for e in graph.edges:
        all_fqns.add(e.source_fqn)
        all_fqns.add(e.target_fqn)

    # Simple substring match
    search = identifier.lower()
    matches = [fqn for fqn in sorted(all_fqns) if search in fqn.lower()][:10]
    if matches:
        log.info("Did you mean one of these?")
        for m in matches:
            log.info("  %s", m)


if __name__ == "__main__":
    main()
