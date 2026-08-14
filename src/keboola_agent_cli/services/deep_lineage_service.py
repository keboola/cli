"""Deep lineage service - column-level lineage from sync'd data on disk.

Scans sync'd project data (from `kbagent sync pull --all-projects`),
builds a comprehensive dependency graph at table and column level,
and optionally uses AI to parse SQL/Python code for hidden dependencies.

Architecture: reads from disk only, no API calls. Requires sync'd data.
"""

import hashlib
import html
import json
import logging
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config_store import ConfigStore

logger = logging.getLogger(__name__)

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

AI_TASKS_FILE = ".lineage_ai_tasks.json"
AI_RESULTS_FILE = ".lineage_ai_results.json"


# ---------------------------------------------------------------------------
# SQL tokenizer for table reference extraction
# ---------------------------------------------------------------------------

# Matches: "KBC_USE4_123"."bucket"."table" OR "bucket"."table"
_QUALIFIED_3 = re.compile(r'"KBC_USE4_(\d+)"\s*\.\s*"([^"]+)"\s*\.\s*"([^"]+)"')
_QUALIFIED_2 = re.compile(r'"([^"]+)"\s*\.\s*"([^"]+)"')


def _read_source(path: Path) -> str:
    """Read a transformation's code, whatever bytes the file actually holds.

    ``Path.read_text()`` decodes with the platform default, which on a Czech or
    Polish Windows box is cp1250 -- so a single accented character in a SQL
    comment aborted the whole lineage build with ``UnicodeDecodeError`` (issue
    #570). Keboola serves this content as UTF-8, so UTF-8 is the right guess
    everywhere, and forcing it also makes a build reproducible across machines.

    ``errors="replace"`` because the goal here is finding table references: a
    file that genuinely holds non-UTF-8 bytes (a locally-edited transformation
    saved in the OS codepage) should cost a mangled comment, never the entire
    graph. Table identifiers are ASCII, so nothing load-bearing is lost.
    """
    return path.read_text(encoding="utf-8", errors="replace")


def extract_sql_table_refs(sql: str, project_id: int) -> list[tuple[int, str, str]]:
    """Extract table references from Snowflake SQL using a state machine.

    Strips comments and string literals first, then finds qualified table
    references in FROM/JOIN context. Returns list of (project_id, bucket, table).

    Catches two patterns:
    - 3-part: "KBC_USE4_{pid}"."bucket"."table"  (cross-project or explicit)
    - 2-part: "bucket"."table"                     (same-project, implicit)

    Filters out:
    - References inside comments (-- and /* */)
    - References inside string literals ('...')
    - CTE names (WITH x AS ...)
    - CREATE TABLE targets (output tables, not inputs)
    """
    cleaned = _strip_comments_and_strings(sql)
    cte_names = _collect_cte_names(cleaned)
    create_targets = _collect_create_targets(cleaned)

    refs: list[tuple[int, str, str]] = []
    seen: set[tuple[int, str, str]] = set()

    # Pass 1: 3-part references (explicit project)
    for match in _QUALIFIED_3.finditer(cleaned):
        pid, bucket, table = int(match.group(1)), match.group(2), match.group(3)
        key = (pid, bucket, table)
        if key not in seen:
            seen.add(key)
            refs.append(key)

    # Pass 2: 2-part references in FROM/JOIN context (same project)
    # Find all FROM/JOIN keywords and scan what follows
    for kw_match in re.finditer(r"\b(?:FROM|JOIN)\s+", cleaned, re.IGNORECASE):
        after = cleaned[kw_match.end() :]
        m2 = _QUALIFIED_2.match(after)
        if not m2:
            continue
        part1, part2 = m2.group(1), m2.group(2)
        # Skip if this is actually a 3-part ref (already captured)
        if part1.startswith("KBC_USE4_"):
            continue
        # Skip CTE aliases and CREATE TABLE targets
        if part1.lower() in cte_names or part2.lower() in cte_names:
            continue
        if (part1, part2) in create_targets:
            continue
        # Only accept bucket-shaped first part (in.c-* or out.c-*)
        if not (part1.startswith("in.") or part1.startswith("out.")):
            continue
        key = (project_id, part1, part2)
        if key not in seen:
            seen.add(key)
            refs.append(key)

    return refs


def _strip_comments_and_strings(sql: str) -> str:
    """Remove SQL comments and string literals, replacing with spaces."""
    result: list[str] = []
    i = 0
    length = len(sql)
    while i < length:
        # Line comment
        if sql[i : i + 2] == "--":
            end = sql.find("\n", i)
            if end == -1:
                break
            result.append(" " * (end - i))
            i = end
        # Block comment
        elif sql[i : i + 2] == "/*":
            end = sql.find("*/", i + 2)
            if end == -1:
                break
            result.append(" " * (end + 2 - i))
            i = end + 2
        # String literal
        elif sql[i] == "'":
            j = i + 1
            while j < length:
                if sql[j] == "'" and (j + 1 >= length or sql[j + 1] != "'"):
                    break
                if sql[j] == "'" and j + 1 < length and sql[j + 1] == "'":
                    j += 2  # escaped quote
                    continue
                j += 1
            result.append(" " * (j + 1 - i))
            i = j + 1
        else:
            result.append(sql[i])
            i += 1
    return "".join(result)


def _collect_cte_names(sql: str) -> set[str]:
    """Collect CTE names from WITH clauses."""
    names: set[str] = set()
    for m in re.finditer(r"\bWITH\s+", sql, re.IGNORECASE):
        rest = sql[m.end() :]
        # Parse: name AS (, name AS (, ...
        while True:
            nm = re.match(r'\s*"?(\w+)"?\s+AS\s*\(', rest, re.IGNORECASE)
            if not nm:
                break
            names.add(nm.group(1).lower())
            # Skip past the balanced parens to find next CTE or main query
            depth = 0
            j = nm.end() - 1  # start at the opening paren
            while j < len(rest):
                if rest[j] == "(":
                    depth += 1
                elif rest[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            rest = rest[j + 1 :]
            # Check for comma (another CTE) or end
            rest = rest.lstrip()
            if rest.startswith(","):
                rest = rest[1:]
            else:
                break
    return names


def _collect_create_targets(sql: str) -> set[tuple[str, str]]:
    """Collect CREATE TABLE / INSERT INTO target tables (outputs, not inputs)."""
    targets: set[tuple[str, str]] = set()
    for m in re.finditer(
        r"\b(?:CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP\s+|TEMPORARY\s+)?TABLE|INSERT\s+(?:INTO|OVERWRITE))\s+",
        sql,
        re.IGNORECASE,
    ):
        after = sql[m.end() :]
        # Match "part1"."part2" or just "name"
        m2 = _QUALIFIED_2.match(after)
        if m2:
            targets.add((m2.group(1), m2.group(2)))
        else:
            m1 = re.match(r'"([^"]+)"', after)
            if m1:
                targets.add(("", m1.group(1)))
    return targets


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Table:
    """A storage table in a Keboola project."""

    table_id: str
    project_alias: str
    project_id: int
    bucket_id: str
    name: str
    columns: list[str] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    rows_count: int = 0

    @property
    def fqn(self) -> str:
        return f"{self.project_alias}:{self.table_id}"


@dataclass
class Configuration:
    """A Keboola configuration."""

    config_id: str
    config_name: str
    component_id: str
    component_type: str
    project_alias: str
    project_id: int
    path: str
    input_tables: list[dict] = field(default_factory=list)
    output_tables: list[dict] = field(default_factory=list)
    code: str = ""
    code_type: str = ""

    @property
    def fqn(self) -> str:
        return f"{self.project_alias}:{self.component_id}/{self.config_id}"


@dataclass
class Edge:
    """A dependency edge in the lineage graph."""

    source_fqn: str
    target_fqn: str
    source_type: str
    target_type: str
    edge_type: str
    detection: str
    columns: list[str] = field(default_factory=list)
    column_mapping: dict[str, str] = field(default_factory=dict)


@dataclass
class LineageGraph:
    """Complete lineage graph for an organization."""

    tables: dict[str, Table] = field(default_factory=dict)
    configurations: dict[str, Configuration] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    _upstream: dict[str, list[int]] = field(default_factory=dict)
    _downstream: dict[str, list[int]] = field(default_factory=dict)
    _project_id_to_alias: dict[int, str] = field(default_factory=dict)

    def add_edge(self, edge: Edge) -> None:
        idx = len(self.edges)
        self.edges.append(edge)
        self._downstream.setdefault(edge.source_fqn, []).append(idx)
        self._upstream.setdefault(edge.target_fqn, []).append(idx)

    def get_upstream(self, fqn: str, depth: int = 10) -> list[dict]:
        visited: set[str] = set()
        result: list[dict] = []
        self._walk(fqn, depth, 0, visited, result, direction="upstream")
        return result

    def get_downstream(self, fqn: str, depth: int = 10) -> list[dict]:
        visited: set[str] = set()
        result: list[dict] = []
        self._walk(fqn, depth, 0, visited, result, direction="downstream")
        return result

    def _walk(
        self,
        fqn: str,
        max_depth: int,
        current_depth: int,
        visited: set[str],
        result: list[dict],
        direction: str,
    ) -> None:
        if current_depth >= max_depth or fqn in visited:
            return
        visited.add(fqn)
        index = self._upstream if direction == "upstream" else self._downstream
        for edge_idx in index.get(fqn, []):
            edge = self.edges[edge_idx]
            next_fqn = edge.source_fqn if direction == "upstream" else edge.target_fqn
            result.append(
                {
                    "depth": current_depth + 1,
                    "source": edge.source_fqn,
                    "target": edge.target_fqn,
                    "edge_type": edge.edge_type,
                    "detection": edge.detection,
                    "columns": edge.columns,
                    "column_mapping": edge.column_mapping,
                }
            )
            self._walk(next_fqn, max_depth, current_depth + 1, visited, result, direction)

    def summary(self) -> dict:
        edge_types: dict[str, int] = {}
        detection_methods: dict[str, int] = {}
        for edge in self.edges:
            edge_types[edge.edge_type] = edge_types.get(edge.edge_type, 0) + 1
            detection_methods[edge.detection] = detection_methods.get(edge.detection, 0) + 1
        return {
            "tables": len(self.tables),
            "configurations": len(self.configurations),
            "edges": len(self.edges),
            "edge_types": edge_types,
            "detection_methods": detection_methods,
        }

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
# Service
# ---------------------------------------------------------------------------


# Keep a warning readable when a bare table name matches across many buckets.
_MAX_LISTED_CANDIDATES = 5


def _format_candidates(items: list[str]) -> str:
    """Join ids for display, trimming a long tail rather than printing all of it."""
    head = items[:_MAX_LISTED_CANDIDATES]
    tail = f", +{len(items) - len(head)} more" if len(items) > len(head) else ""
    return ", ".join(head) + tail


def _ambiguity_warning(identifier: str, candidates: list[str]) -> str:
    """Build the warning shown when an identifier matches more than one node.

    Two different shapes reach this. The one #568 reports is the same
    ``bucket_id.table_name`` living in several project namespaces (a bucket
    shared from one project and linked into another) -- there every candidate
    differs only by project, and ``<project>:<identifier>`` is a retry that
    resolves. The other comes from the name-only fallback in
    ``_find_node_candidates``, which matches a bare table name across buckets:
    those candidates can share a single project, so counting them as projects
    would tell the user a table "exists in 2 projects (alpha, alpha)", and
    ``<project>:<identifier>`` would not resolve because the real node ids
    carry a bucket. That case gets the full ids instead.
    """
    shown = candidates[0]
    if all(fqn.partition(":")[2] == identifier for fqn in candidates):
        projects = sorted({fqn.partition(":")[0] for fqn in candidates})
        return (
            f"'{identifier}' exists in {len(projects)} projects "
            f"({_format_candidates(projects)}); showing '{shown}' only. "
            f"Query a specific one with '--upstream/--downstream "
            f"<project>:{identifier}' or scope with --project."
        )
    return (
        f"'{identifier}' matches {len(candidates)} nodes "
        f"({_format_candidates(candidates)}); showing '{shown}' only. "
        f"Query a specific one by its full id, e.g. '--upstream/--downstream {shown}'."
    )


class DeepLineageService:
    """Business logic for column-level lineage from sync'd data on disk.

    Scans project directories, builds deterministic lineage from config mappings,
    detects hidden SQL dependencies via regex, and optionally uses AI for
    column-level analysis.
    """

    def __init__(self, config_store: ConfigStore) -> None:
        self._config_store = config_store

    def build_lineage(
        self,
        root: Path,
        *,
        generate_ai_tasks: bool = False,
    ) -> dict[str, Any]:
        """Build comprehensive lineage graph from sync'd data.

        Automatically applies AI results from .lineage_ai_results.json
        if present. Use generate_ai_tasks=True to write a task file
        for the AI agent to process.

        Supports both sync layouts:
          - Flat (``sync pull --project X``): ``root/.keboola/manifest.json``
          - Nested (``sync pull --all-projects``): ``root/<alias>/.keboola/manifest.json``

        Args:
            root: Root directory containing sync'd project data. Can be either
                the project directory itself (flat layout) or a parent directory
                with one subdirectory per project (nested layout).
            generate_ai_tasks: If True, write .lineage_ai_tasks.json for AI.

        Returns:
            Dict with lineage graph data, summary, ai_status, and ``warnings``
            (list of human-readable warnings emitted during the build, e.g. when
            no sync'd projects were found).
        """
        project_id_to_alias = self._build_project_map()
        warnings: list[str] = []

        # Phase 1: Scan
        graph = self._scan_projects(root, project_id_to_alias)

        # Empty-scan warning -- most often caused by a layout mismatch between
        # ``sync pull --project X`` (flat) and ``sync pull --all-projects`` (nested).
        if not graph.tables and not graph.configurations:
            warning = (
                f"No synced projects found in {root}. Expected either:\n"
                f"  - {root}/.keboola/manifest.json (single-project flat layout), or\n"
                f"  - {root}/<alias>/.keboola/manifest.json (multi-project nested layout)\n"
                "Hint: run 'kbagent sync pull --all-projects' or pass --directory "
                "pointing to the parent of your synced projects."
            )
            warnings.append(warning)
            logger.warning(warning)

        # Phase 2: Deterministic edges
        self._build_deterministic_edges(graph, project_id_to_alias)

        # Phase 3: Cross-project sharing
        self._add_cross_project_lineage(graph, root)

        # Phase 4a: Apply existing AI results (if any)
        ai_status = self._apply_ai_results_file(graph, root, project_id_to_alias)

        # Phase 4b: Generate AI task file (if requested)
        if generate_ai_tasks:
            task_status = self._generate_ai_tasks(graph, root)
            ai_status.update(task_status)

        result = graph.to_dict()
        result["ai_status"] = ai_status
        result["warnings"] = warnings
        return result

    def build_and_cache(
        self,
        root: Path,
        cache_path: Path,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build lineage and save to cache file."""
        result = self.build_lineage(root, **kwargs)
        with open(cache_path, "w") as f:
            json.dump(result, f, indent=2)
        return result

    def load_from_cache(self, cache_path: Path) -> LineageGraph:
        """Load a previously saved lineage graph from JSON cache."""
        with open(cache_path) as f:
            data = json.load(f)
        return self._graph_from_dict(data)

    def query_upstream(
        self,
        graph: LineageGraph,
        identifier: str,
        project: str = "",
        depth: int = 10,
    ) -> dict[str, Any]:
        """Query upstream dependencies of a node."""
        return self._query(graph, identifier, project, depth, direction="upstream")

    def query_downstream(
        self,
        graph: LineageGraph,
        identifier: str,
        project: str = "",
        depth: int = 10,
    ) -> dict[str, Any]:
        """Query downstream dependents of a node."""
        return self._query(graph, identifier, project, depth, direction="downstream")

    def _query(
        self,
        graph: LineageGraph,
        identifier: str,
        project: str,
        depth: int,
        *,
        direction: str,
    ) -> dict[str, Any]:
        """Resolve ``identifier`` and walk the graph in ``direction``.

        When an unqualified identifier resolves to more than one project (the
        same ``bucket_id.table_name`` exists in several project namespaces --
        typically a shared/linked bucket), the first candidate is still used,
        but the result carries ``ambiguous_matches`` and a human-readable
        ``warnings`` entry so callers can surface the ambiguity instead of
        presenting one project's answer as the whole picture.
        """
        candidates = self._find_node_candidates(graph, identifier, project)
        if not candidates:
            return {
                "error": f"Node not found: {identifier}",
                "suggestions": self._suggest(graph, identifier),
            }

        fqn = candidates[0]
        result: dict[str, Any] = {
            "node": fqn,
            "direction": direction,
            "node_info": self._node_info(graph, fqn),
            "edges": (
                graph.get_upstream(fqn, depth)
                if direction == "upstream"
                else graph.get_downstream(fqn, depth)
            ),
        }
        if len(candidates) > 1:
            result["ambiguous_matches"] = candidates
            result["warnings"] = [_ambiguity_warning(identifier, candidates)]
        return result

    # --- Internal methods ---

    def _build_project_map(self) -> dict[int, str]:
        """Build project_id -> alias mapping from config store."""
        mapping: dict[int, str] = {}
        try:
            app_config = self._config_store.load()
            for alias, project in app_config.projects.items():
                if project.project_id:
                    mapping[project.project_id] = alias
        except Exception:
            pass
        return mapping

    def _scan_projects(self, root: Path, project_id_to_alias: dict[int, str]) -> LineageGraph:
        """Scan all sync'd projects and build initial graph.

        Supports two directory layouts (written by ``kbagent sync pull``):
          - **Flat** (``sync pull --project X``): manifest lives directly at
            ``root/.keboola/manifest.json`` and ``root`` itself *is* the project.
          - **Nested** (``sync pull --all-projects``): each project is a
            subdirectory, i.e. ``root/<alias>/.keboola/manifest.json``.

        Flat layout is detected first and, when present, returned exclusively
        (nested iteration would be redundant -- in the flat case there are no
        sibling project directories under ``root``).
        """
        graph = LineageGraph()

        flat_manifest = root / ".keboola" / "manifest.json"
        if flat_manifest.exists():
            project_alias = self._resolve_alias_from_manifest(flat_manifest, fallback=root.name)
            self._scan_one_project(root, project_alias, project_id_to_alias, graph)
        else:
            for project_dir in sorted(root.iterdir()):
                if not project_dir.is_dir() or project_dir.name.startswith("."):
                    continue
                manifest_path = project_dir / ".keboola" / "manifest.json"
                if not manifest_path.exists():
                    continue
                self._scan_one_project(project_dir, project_dir.name, project_id_to_alias, graph)

        # Store mapping for cross-project resolution
        graph._project_id_to_alias = project_id_to_alias
        return graph

    def _resolve_alias_from_manifest(self, manifest_path: Path, *, fallback: str) -> str:
        """Resolve a project alias for a flat-layout root.

        In a flat layout the parent directory name is meaningless (it's the
        user's CWD, not the project alias). We prefer the alias configured in
        ``ConfigStore`` for the project id recorded in the manifest; if no such
        mapping exists, we fall back to the directory name so the graph stays
        consistent but still disambiguated from other projects on disk.
        """
        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
            project_id = int(manifest.get("project", {}).get("id", 0) or 0)
        except (OSError, json.JSONDecodeError, ValueError):
            return fallback

        if project_id:
            app_config = self._config_store.load()
            for alias, project in app_config.projects.items():
                if project.project_id == project_id:
                    return alias
        return fallback

    def _scan_one_project(
        self,
        project_dir: Path,
        project_alias: str,
        project_id_to_alias: dict[int, str],
        graph: LineageGraph,
    ) -> None:
        """Populate ``graph`` with tables and configs from a single project dir."""
        manifest_path = project_dir / ".keboola" / "manifest.json"
        with open(manifest_path) as f:
            manifest = json.load(f)

        project_id = manifest.get("project", {}).get("id", 0)
        project_id_to_alias[project_id] = project_alias

        # Scan storage tables
        storage_dir = project_dir / "storage" / "tables"
        if storage_dir.exists():
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

        # Scan configurations
        for config_entry in manifest.get("configurations", []):
            self._scan_configuration(project_dir, config_entry, project_alias, project_id, graph)

    def _scan_configuration(
        self,
        project_dir: Path,
        config_entry: dict,
        project_alias: str,
        project_id: int,
        graph: LineageGraph,
    ) -> None:
        config_path = config_entry["path"]
        component_id = config_entry["componentId"]
        config_id = config_entry["id"]
        full_path = project_dir / "main" / config_path

        component_type = config_path.split("/")[0] if "/" in config_path else "unknown"

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

        code = ""
        code_type = ""
        transform_sql = full_path / "transform.sql"
        code_py = full_path / "code.py"

        if transform_sql.exists():
            code = _read_source(transform_sql)
            code_type = "sql"
        elif code_py.exists():
            code = _read_source(code_py)
            code_type = "python"

        # Include config row mappings
        for row_entry in config_entry.get("rows", []):
            row_path = project_dir / "main" / row_entry.get("path", "")
            row_config = row_path / "_config.yml"
            if row_config.exists():
                with open(row_config) as f:
                    row_cfg = yaml.safe_load(f) or {}
                input_tables.extend(row_cfg.get("input", {}).get("tables", []) or [])
                output_tables.extend(row_cfg.get("output", {}).get("tables", []) or [])

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
        )
        graph.configurations[config.fqn] = config

    def _build_deterministic_edges(
        self, graph: LineageGraph, project_id_to_alias: dict[int, str]
    ) -> None:
        for config in graph.configurations.values():
            # Input mapping: table -> config
            for inp in config.input_tables:
                source_table_id = inp.get("source", "")
                if not source_table_id:
                    continue
                table_fqn = f"{config.project_alias}:{source_table_id}"
                columns = inp.get("columns", [])
                if not columns:
                    table = graph.tables.get(table_fqn)
                    if table:
                        columns = table.columns
                graph.add_edge(
                    Edge(
                        source_fqn=table_fqn,
                        target_fqn=config.fqn,
                        source_type="table",
                        target_type="config",
                        edge_type="reads",
                        detection="input_mapping",
                        columns=columns,
                    )
                )

            # Output mapping: config -> table
            for out in config.output_tables:
                dest_table_id = out.get("destination", "")
                if not dest_table_id:
                    continue
                table_fqn = f"{config.project_alias}:{dest_table_id}"
                graph.add_edge(
                    Edge(
                        source_fqn=config.fqn,
                        target_fqn=table_fqn,
                        source_type="config",
                        target_type="table",
                        edge_type="writes",
                        detection="output_mapping",
                    )
                )

            # SQL tokenizer: extract table references from code
            if config.code_type == "sql" and config.code:
                refs = extract_sql_table_refs(config.code, config.project_id)
                for ref_pid, ref_bucket, ref_table in refs:
                    ref_alias = project_id_to_alias.get(ref_pid, f"unknown-{ref_pid}")
                    table_id = f"{ref_bucket}.{ref_table}"
                    table_fqn = f"{ref_alias}:{table_id}"
                    if any(inp.get("source", "") == table_id for inp in config.input_tables):
                        continue
                    table = graph.tables.get(table_fqn)
                    columns = table.columns if table else []
                    detection = (
                        "sql_tokenizer_cross_project"
                        if ref_pid != config.project_id
                        else "sql_tokenizer"
                    )
                    graph.add_edge(
                        Edge(
                            source_fqn=table_fqn,
                            target_fqn=config.fqn,
                            source_type="table",
                            target_type="config",
                            edge_type="reads",
                            detection=detection,
                            columns=columns,
                        )
                    )

    def _add_cross_project_lineage(self, graph: LineageGraph, root: Path) -> None:
        try:
            result = subprocess.run(
                ["kbagent", "--json", "sharing", "edges"],
                capture_output=True,
                text=True,
                cwd=str(root),
                timeout=120,
            )
            if result.returncode != 0:
                return
            data = json.loads(result.stdout)
            for edge_data in data.get("data", {}).get("edges", []):
                source_alias = edge_data.get("source_project_alias", "")
                target_alias = edge_data.get("target_project_alias", "")
                source_bucket = edge_data.get("source_bucket_id", "")
                target_bucket = edge_data.get("target_bucket_id", "")
                if not source_alias or not target_alias:
                    continue
                source_tables = {
                    t.name: t
                    for t in graph.tables.values()
                    if t.project_alias == source_alias and t.bucket_id == source_bucket
                }
                for t in graph.tables.values():
                    if t.project_alias == target_alias and t.bucket_id == target_bucket:
                        source_table = source_tables.get(t.name)
                        if source_table:
                            graph.add_edge(
                                Edge(
                                    source_fqn=source_table.fqn,
                                    target_fqn=t.fqn,
                                    source_type="table",
                                    target_type="table",
                                    edge_type="cross_project_share",
                                    detection="bucket_sharing",
                                    columns=source_table.columns,
                                )
                            )
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            pass

    def _apply_ai_results_file(
        self,
        graph: LineageGraph,
        root: Path,
        project_id_to_alias: dict[int, str],
    ) -> dict[str, Any]:
        """Apply AI results from .lineage_ai_results.json if present."""
        results_path = root / AI_RESULTS_FILE
        if not results_path.exists():
            return {"ai_results_applied": False}

        with open(results_path) as f:
            ai_results = json.load(f)

        applied = 0
        for entry in ai_results.get("results", []):
            config_fqn = entry.get("config_fqn", "")
            config = graph.configurations.get(config_fqn)
            if not config:
                continue

            # SQL-style results: inputs with pid/bucket/table
            for inp in entry.get("inputs", []):
                ref_pid = inp.get("pid", config.project_id)
                bucket = inp.get("bucket", "")
                table = inp.get("table", "")
                if not bucket or not table:
                    continue
                table_id = f"{bucket}.{table}"
                ref_alias = project_id_to_alias.get(ref_pid, config.project_alias)
                table_fqn = f"{ref_alias}:{table_id}"
                if any(
                    e.source_fqn == table_fqn and e.target_fqn == config_fqn for e in graph.edges
                ):
                    continue
                detection = "ai_cross_project" if ref_pid != config.project_id else "ai"
                graph.add_edge(
                    Edge(
                        source_fqn=table_fqn,
                        target_fqn=config_fqn,
                        source_type="table",
                        target_type="config",
                        edge_type="reads",
                        detection=detection,
                        columns=inp.get("columns", []),
                    )
                )
                applied += 1

            # Python-style results: extra_inputs with table_id
            for inp in entry.get("extra_inputs", []):
                table_id = inp.get("table_id", "")
                if not table_id:
                    continue
                table_fqn = f"{config.project_alias}:{table_id}"
                if any(
                    e.source_fqn == table_fqn and e.target_fqn == config_fqn for e in graph.edges
                ):
                    continue
                graph.add_edge(
                    Edge(
                        source_fqn=table_fqn,
                        target_fqn=config_fqn,
                        source_type="table",
                        target_type="config",
                        edge_type="reads",
                        detection="ai",
                    )
                )
                applied += 1

            # Column mappings
            for cm in entry.get("col_map", []):
                out_col = cm.get("out", "")
                in_table = cm.get("in_table", "")
                in_col = cm.get("in_col", "")
                if out_col and in_col:
                    for edge in graph.edges:
                        if edge.target_fqn == config_fqn and in_table in edge.source_fqn:
                            edge.column_mapping[out_col] = f"{in_table}.{in_col}"

        return {"ai_results_applied": True, "ai_edges_added": applied}

    def _generate_ai_tasks(self, graph: LineageGraph, root: Path) -> dict[str, Any]:
        """Generate .lineage_ai_tasks.json for AI agent to process."""
        configs_needing_ai = [
            c
            for c in graph.configurations.values()
            if c.code and ((c.code_type == "sql" and not c.input_tables) or c.code_type == "python")
        ]

        # Check which already have results
        results_path = root / AI_RESULTS_FILE
        existing_hashes: dict[str, str] = {}
        if results_path.exists():
            with open(results_path) as f:
                for entry in json.load(f).get("results", []):
                    fqn = entry.get("config_fqn", "")
                    h = entry.get("_code_hash", "")
                    if fqn and h:
                        existing_hashes[fqn] = h

        tasks = []
        for config in configs_needing_ai:
            code_hash = hashlib.sha256(config.code.encode()).hexdigest()[:16]
            if existing_hashes.get(config.fqn) == code_hash:
                continue  # already analyzed, code unchanged

            # Resolve the code file path on disk
            project_dir = root / config.project_alias
            full_path = project_dir / "main" / config.path
            code_file = full_path / ("transform.sql" if config.code_type == "sql" else "code.py")

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
                task["known_outputs"] = [
                    t.get("destination", "") for t in config.output_tables[:10]
                ]
            tasks.append(task)

        tasks_data = {
            "description": "AI analysis tasks for column-level lineage.",
            "instructions": (
                "For each task, read the code_file from disk and extract table dependencies. "
                "Write results to .lineage_ai_results.json (see output_format). "
                "Then re-run `kbagent lineage build` to incorporate the results."
            ),
            "output_file": str(root / AI_RESULTS_FILE),
            "output_format": {
                "results": [
                    {
                        "config_fqn": "project:component/config_id",
                        "_code_hash": "hash from task",
                        "inputs": [
                            {"pid": 123, "bucket": "in.c-x", "table": "y", "columns": ["a"]}
                        ],
                        "outputs": [{"table": "local_name", "columns": ["b"]}],
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
        with open(tasks_path, "w") as f:
            json.dump(tasks_data, f, indent=2)

        return {
            "ai_tasks_generated": len(tasks),
            "ai_tasks_file": str(tasks_path),
            "ai_already_done": len(configs_needing_ai) - len(tasks),
        }

    @staticmethod
    def _graph_from_dict(data: dict) -> LineageGraph:
        graph = LineageGraph()
        for fqn, t_data in data.get("tables", {}).items():
            graph.tables[fqn] = Table(**t_data)
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
        for e_data in data.get("edges", []):
            graph.add_edge(
                Edge(
                    source_fqn=e_data["source_fqn"],
                    target_fqn=e_data["target_fqn"],
                    source_type=e_data["source_type"],
                    target_type=e_data["target_type"],
                    edge_type=e_data["edge_type"],
                    detection=e_data["detection"],
                    columns=e_data.get("columns", []),
                    column_mapping=e_data.get("column_mapping", {}),
                )
            )
        return graph

    def _find_node(self, graph: LineageGraph, identifier: str, project: str = "") -> str | None:
        candidates = self._find_node_candidates(graph, identifier, project)
        return candidates[0] if candidates else None

    def _find_node_candidates(
        self, graph: LineageGraph, identifier: str, project: str = ""
    ) -> list[str]:
        """Return every FQN ``identifier`` could refer to, best match first.

        A fully-qualified ``project:bucket_id.table_name`` (or an identifier
        combined with an explicit ``project``) is unambiguous by construction,
        so at most one candidate comes back. A bare ``bucket_id.table_name``
        can exist in several project namespaces at once -- a bucket shared
        from one project and linked into another yields a node per project --
        in which case every match is returned so the caller can report the
        ambiguity rather than silently answering for one of them.
        """
        all_fqns = set(graph.tables) | set(graph.configurations)
        for e in graph.edges:
            all_fqns.add(e.source_fqn)
            all_fqns.add(e.target_fqn)

        if ":" in identifier:
            return [identifier] if identifier in all_fqns else []

        if project:
            fqn = f"{project}:{identifier}"
            return [fqn] if fqn in all_fqns else []

        matches = sorted(f for f in all_fqns if f.endswith(f":{identifier}"))
        if matches:
            return matches

        return sorted(f for f in all_fqns if f.split(":")[-1].endswith(f".{identifier}"))

    # --- Mermaid / HTML rendering ---

    @staticmethod
    def _sanitize_mermaid_id(fqn: str) -> str:
        """Sanitize FQN into a valid mermaid node ID (alphanumeric + underscore)."""
        return re.sub(r"[^a-zA-Z0-9_]", "_", fqn)

    @staticmethod
    def _escape_mermaid_label(text: str) -> str:
        """Escape characters that break mermaid label syntax, preserving <br/>."""
        # Temporarily protect <br/>, escape everything, restore
        text = text.replace("<br/>", "\x00BR\x00")
        text = text.replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
        return text.replace("\x00BR\x00", "<br/>")

    @staticmethod
    def render_mermaid(
        edges: list[dict],
        graph: LineageGraph,
        direction: str,
        node_fqn: str,
        show_columns: bool = False,
    ) -> str:
        """Render lineage edges as a mermaid flowchart.

        Args:
            edges: List of edge dicts from query_upstream/query_downstream.
            graph: The lineage graph (for node metadata).
            direction: "upstream" or "downstream".
            node_fqn: The FQN of the queried node.
            show_columns: If True, include column names in table labels.

        Returns:
            Mermaid flowchart source code.
        """
        sanitize = DeepLineageService._sanitize_mermaid_id
        escape = DeepLineageService._escape_mermaid_label

        graph_dir = "RL" if direction == "upstream" else "LR"
        lines: list[str] = [f"graph {graph_dir}"]

        # Determine the root node's project for cross-project detection
        root_project = node_fqn.split(":")[0] if ":" in node_fqn else ""

        # Collect all unique node FQNs (including the root node)
        node_fqns: set[str] = {node_fqn}
        for edge in edges:
            node_fqns.add(edge["source"])
            node_fqns.add(edge["target"])

        # Emit node definitions with labels and classes
        for fqn in sorted(node_fqns):
            node_id = sanitize(fqn)
            node_project = fqn.split(":")[0] if ":" in fqn else ""
            is_cross = node_project and root_project and node_project != root_project

            if fqn in graph.tables:
                t = graph.tables[fqn]
                label_parts = [f"{t.project_alias}:{t.table_id}"]
                label_parts.append(f"{len(t.columns)} cols, {t.rows_count:,} rows")
                if show_columns and t.columns:
                    col_display = t.columns[:8]
                    col_text = ", ".join(col_display)
                    if len(t.columns) > 8:
                        col_text += f", +{len(t.columns) - 8} more"
                    label_parts.append(col_text)
                label = escape("<br/>".join(label_parts))
                css_class = "cross_table" if is_cross else "table"
                lines.append(f'  {node_id}["{label}"]:::{css_class}')
            elif fqn in graph.configurations:
                c = graph.configurations[fqn]
                label = escape(f"{c.project_alias}:{c.config_name}<br/>{c.component_id}")
                lines.append(f'  {node_id}["{label}"]:::config')
            else:
                label = escape(fqn)
                css_class = "cross_table" if is_cross else "table"
                lines.append(f'  {node_id}["{label}"]:::{css_class}')

        # Emit edges
        seen_edges: set[tuple[str, str]] = set()
        for edge in edges:
            src_id = sanitize(edge["source"])
            tgt_id = sanitize(edge["target"])
            edge_key = (src_id, tgt_id)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            detection = escape(edge["detection"])
            lines.append(f"  {src_id} -->|{detection}| {tgt_id}")

        # Style definitions
        lines.append("")
        lines.append("  classDef table fill:#e1f5fe,stroke:#0288d1")
        lines.append("  classDef config fill:#e8f5e9,stroke:#388e3c")
        lines.append("  classDef cross_table fill:#f3e5f5,stroke:#7b1fa2")

        return "\n".join(lines)

    @staticmethod
    def render_er_diagram(
        edges: list[dict],
        graph: LineageGraph,
        node_fqn: str,
        show_columns: bool = False,
    ) -> str:
        """Render lineage as a mermaid ER diagram.

        Without show_columns: entities with name/row count + relationships.
        With show_columns: full column list with PK markers and AI mappings.
        """
        lines: list[str] = ["erDiagram"]

        # Collect all table FQNs and their column mappings
        table_fqns: set[str] = set()
        config_fqns: set[str] = set()
        # edge source/target -> column_mapping
        col_maps: dict[str, dict[str, str]] = {}

        if node_fqn in graph.tables:
            table_fqns.add(node_fqn)

        for edge in edges:
            src, tgt = edge["source"], edge["target"]
            if src in graph.tables:
                table_fqns.add(src)
            if tgt in graph.tables:
                table_fqns.add(tgt)
            if src in graph.configurations:
                config_fqns.add(src)
            if tgt in graph.configurations:
                config_fqns.add(tgt)
            cm = edge.get("column_mapping", {})
            if cm:
                # Key by config FQN (the node that transforms data)
                cfg = tgt if tgt in graph.configurations else src
                col_maps[cfg] = {**col_maps.get(cfg, {}), **cm}

        # Emit entity definitions for tables
        for fqn in sorted(table_fqns):
            t = graph.tables.get(fqn)
            if not t:
                continue
            entity_name = f"{t.project_alias}:{t.name}"
            # html.escape covers <>&" so an API-supplied table or config
            # name like </div><script>...</script> cannot inject HTML into
            # the generated lineage page (issue #269 sec-05). Mermaid
            # renders HTML entities back to their characters in SVG text.
            safe_name = html.escape(entity_name, quote=True)

            if not show_columns:
                # Compact: just entity name with row count as single attribute
                lines.append(f'    "{safe_name}" {{')
                lines.append(f'        int rows "{t.rows_count:,} rows, {len(t.columns)} cols"')
                lines.append("    }")
                continue

            # Full column list
            lines.append(f'    "{safe_name}" {{')
            pk_cols = set(t.primary_key) if t.primary_key else set()
            for col in t.columns[:30]:
                safe_col = re.sub(r"[^a-zA-Z0-9_]", "_", col)
                pk_marker = " PK" if col in pk_cols else ""
                comment = ""
                for cfg_fqn, cm in col_maps.items():
                    for out_col, src_expr in cm.items():
                        if src_expr.endswith(f".{col}") and fqn in src_expr.replace(f".{col}", ""):
                            cfg = graph.configurations.get(cfg_fqn)
                            cfg_label = cfg.config_name if cfg else cfg_fqn.split("/")[-1]
                            safe_label = html.escape(cfg_label, quote=True)
                            comment = f' "to {out_col} via {safe_label}"'
                            break
                        if out_col == col:
                            src_short = src_expr.split(".")[-1]
                            comment = f' "from {src_short}"'
                            break
                lines.append(f"        string {safe_col}{pk_marker}{comment}")
            if len(t.columns) > 30:
                lines.append(f'        string _more_ "+{len(t.columns) - 30} more columns"')
            lines.append("    }")

        # Emit relationships: table --via config--> table
        # Find pairs: input table -> config -> output table
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
            cfg = graph.configurations.get(cfg_fqn)
            cfg_label = cfg.config_name if cfg else cfg_fqn.split("/")[-1]
            safe_label = html.escape(cfg_label, quote=True)
            inputs = input_of.get(cfg_fqn, [])
            outputs = output_of.get(cfg_fqn, [])
            for inp_fqn in inputs:
                for out_fqn in outputs:
                    inp_t = graph.tables.get(inp_fqn)
                    out_t = graph.tables.get(out_fqn)
                    if not inp_t or not out_t:
                        continue
                    key = (inp_fqn, out_fqn)
                    if key in seen_rels:
                        continue
                    seen_rels.add(key)
                    inp_name = html.escape(f"{inp_t.project_alias}:{inp_t.name}", quote=True)
                    out_name = html.escape(f"{out_t.project_alias}:{out_t.name}", quote=True)
                    lines.append(f'    "{inp_name}" ||--o{{ "{out_name}" : "{safe_label}"')

            # If config has only inputs and no outputs (writer), show as relationship to config
            if inputs and not outputs:
                for inp_fqn in inputs:
                    inp_t = graph.tables.get(inp_fqn)
                    if not inp_t:
                        continue
                    inp_name = html.escape(f"{inp_t.project_alias}:{inp_t.name}", quote=True)
                    lines.append(f'    "{inp_name}" }}o--|| "{safe_label}" : "writes"')

        return "\n".join(lines)

    @staticmethod
    def render_html(mermaid_code: str, title: str) -> str:
        """Wrap mermaid code in a self-contained HTML page.

        Args:
            mermaid_code: Mermaid flowchart source.
            title: Page title / heading.

        Returns:
            Complete HTML document string.
        """
        escaped_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        legend = (
            '<div style="margin:20px 0;padding:12px 16px;background:#f5f5f5;'
            'border-radius:8px;font-size:13px;display:inline-block">'
            "<strong>Legend</strong><br/>"
            '<span style="display:inline-block;width:14px;height:14px;'
            "background:#e1f5fe;border:2px solid #0288d1;border-radius:3px;"
            'vertical-align:middle;margin-right:4px"></span> Table '
            '<span style="color:#888">(project:bucket.table &mdash; columns, rows)</span>'
            "&nbsp;&nbsp;&nbsp;"
            '<span style="display:inline-block;width:14px;height:14px;'
            "background:#e8f5e9;border:2px solid #388e3c;border-radius:3px;"
            'vertical-align:middle;margin-right:4px"></span> Configuration '
            '<span style="color:#888">(transformation, extractor, writer, app)</span>'
            "<br/>"
            "<br/>"
            '<span style="display:inline-block;width:20px;height:3px;'
            "background:#7c4dff;vertical-align:middle;margin-right:4px;"
            'border-radius:2px"></span> Cross-project edge '
            '<span style="color:#888">'
            "(sql_tokenizer_cross_project, bucket_sharing, ai_cross_project)</span>"
            "<br/>"
            '<span style="color:#888;font-size:12px;margin-top:4px;display:block">'
            "Edge labels: input_mapping / output_mapping (deterministic from config) "
            "| sql_tokenizer (parsed from SQL code) | bucket_sharing (shared buckets) "
            "| ai (AI-detected from code analysis)</span>"
            "</div>"
        )
        return (
            "<!DOCTYPE html>\n"
            "<html>\n"
            "<head>\n"
            f"  <title>{escaped_title}</title>\n"
            '  <script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>\n'
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

    @staticmethod
    def _suggest(graph: LineageGraph, identifier: str) -> list[str]:
        all_fqns = set(graph.tables) | set(graph.configurations)
        for e in graph.edges:
            all_fqns.add(e.source_fqn)
            all_fqns.add(e.target_fqn)
        search = identifier.lower()
        return sorted(f for f in all_fqns if search in f.lower())[:10]

    @staticmethod
    def _node_info(graph: LineageGraph, fqn: str) -> dict:
        if fqn in graph.tables:
            t = graph.tables[fqn]
            return {"type": "table", "fqn": fqn, "columns": len(t.columns), "rows": t.rows_count}
        if fqn in graph.configurations:
            c = graph.configurations[fqn]
            return {
                "type": c.component_type,
                "fqn": fqn,
                "name": c.config_name,
                "component": c.component_id,
            }
        return {"type": "unknown", "fqn": fqn}
