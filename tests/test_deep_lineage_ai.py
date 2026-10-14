"""Tests for deep-lineage scan paths and AI task/result handling (#846, #847).

Covers:
- branch tree resolution (``branchId`` 0 / missing / registered, custom default dir name)
- rows read relative to their configuration directory (what ``sync pull`` writes)
- AI task ``code_file`` paths on a flat layout whose alias differs from the dir name
- AI ``outputs[]`` -> ``writes`` edges with output-mapping resolution precedence
- AI ``external[]`` annotations (validation, dedupe, cache round-trip)
- AI ``col_map`` exact table matching (no substring matches)
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import AppConfig, ProjectConfig
from keboola_agent_cli.services.deep_lineage_service import (
    AI_RESULTS_FILE,
    AI_TASKS_FILE,
    Configuration,
    DeepLineageService,
    Edge,
    LineageGraph,
)

SQL_COMPONENT = "keboola.snowflake-transformation"


def _service(tmp_path: Path) -> DeepLineageService:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    return DeepLineageService(config_store=ConfigStore(config_dir=cfg_dir))


def _write_config(
    config_dir: Path,
    *,
    name: str,
    inputs: list[dict] | None = None,
    outputs: list[dict] | None = None,
    sql: str | None = None,
) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "_config.yml").write_text(
        yaml.dump(
            {
                "version": 2,
                "name": name,
                "input": {"tables": inputs or []},
                "output": {"tables": outputs or []},
            }
        )
    )
    if sql is not None:
        (config_dir / "transform.sql").write_text(sql)


def _write_manifest(
    project_dir: Path,
    *,
    project_id: int,
    branches: list[dict],
    configurations: list[dict],
) -> None:
    keboola = project_dir / ".keboola"
    keboola.mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": 2,
        "project": {"id": project_id, "name": "P"},
        "branches": branches,
        "configurations": configurations,
    }
    (keboola / "manifest.json").write_text(json.dumps(manifest))


def _config_entry(config_id: str, path: str, **extra: Any) -> dict:
    entry: dict[str, Any] = {"componentId": SQL_COMPONENT, "id": config_id, "path": path}
    entry.update(extra)
    return entry


def _build(service: DeepLineageService, root: Path, **kwargs: Any) -> dict:
    with patch.object(service, "_add_cross_project_lineage"):
        return service.build_lineage(root, **kwargs)


# ---------------------------------------------------------------------------
# A1/A2: branch tree resolution
# ---------------------------------------------------------------------------


class TestBranchTreeResolution:
    CFG_PATH = "transformation/keboola.snowflake-transformation/t1"

    def _project(self, tmp_path: Path, entry_extra: dict) -> Path:
        root = tmp_path / "ws"
        proj = root / "proj"
        _write_manifest(
            proj,
            project_id=1,
            branches=[{"id": 555, "path": "production"}],
            configurations=[_config_entry("c1", self.CFG_PATH, **entry_extra)],
        )
        _write_config(proj / "production" / self.CFG_PATH, name="From production tree")
        return root

    def test_branch_id_zero_resolves_to_default_tree(self, tmp_path: Path) -> None:
        root = self._project(tmp_path, {"branchId": 0})
        result = _build(_service(tmp_path), root)
        assert result["configurations"]["proj:keboola.snowflake-transformation/c1"][
            "config_name"
        ] == ("From production tree")

    def test_missing_branch_id_resolves_to_default_tree(self, tmp_path: Path) -> None:
        root = self._project(tmp_path, {})
        result = _build(_service(tmp_path), root)
        cfg = result["configurations"]["proj:keboola.snowflake-transformation/c1"]
        assert cfg["config_name"] == "From production tree"

    def test_registered_branch_id_uses_its_tree(self, tmp_path: Path) -> None:
        root = self._project(tmp_path, {"branchId": 555})
        result = _build(_service(tmp_path), root)
        cfg = result["configurations"]["proj:keboola.snowflake-transformation/c1"]
        assert cfg["config_name"] == "From production tree"

    def test_source_dir_points_at_resolved_tree(self, tmp_path: Path) -> None:
        root = self._project(tmp_path, {"branchId": 555})
        service = _service(tmp_path)
        graph = service._scan_projects(root, {}, [])
        cfg = graph.configurations["proj:keboola.snowflake-transformation/c1"]
        assert Path(cfg.source_dir) == root / "proj" / "production" / self.CFG_PATH

    def test_multiple_trees_scan_only_default_and_warn(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        proj = root / "proj"
        _write_manifest(
            proj,
            project_id=1,
            branches=[{"id": 1, "path": "main"}, {"id": 2, "path": "dev-feature"}],
            configurations=[
                _config_entry("prod", "transformation/x/prod", branchId=1),
                _config_entry("dev", "transformation/x/dev", branchId=2),
            ],
        )
        _write_config(proj / "main" / "transformation/x/prod", name="Prod")
        _write_config(proj / "dev-feature" / "transformation/x/dev", name="Dev")

        result = _build(_service(tmp_path), root)

        assert list(result["configurations"]) == ["proj:keboola.snowflake-transformation/prod"]
        assert len(result["warnings"]) == 1
        warning = result["warnings"][0]
        assert "1 configuration" in warning
        assert "main" in warning
        assert "proj" in warning


# ---------------------------------------------------------------------------
# A3: rows are relative to their configuration directory
# ---------------------------------------------------------------------------


class TestRowPaths:
    def test_row_path_is_relative_to_config_dir(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        proj = root / "proj"
        cfg_path = "extractor/keboola.ex-db-snowflake/my-extractor"
        _write_manifest(
            proj,
            project_id=1,
            branches=[{"id": 1, "path": "main"}],
            configurations=[
                {
                    "branchId": 1,
                    "componentId": "keboola.ex-db-snowflake",
                    "id": "ex",
                    "path": cfg_path,
                    "rows": [{"id": "r1", "path": "rows/row-1"}],
                }
            ],
        )
        cfg_dir = proj / "main" / cfg_path
        _write_config(cfg_dir, name="Extractor")
        _write_config(
            cfg_dir / "rows" / "row-1",
            name="Row",
            outputs=[{"source": "accounts", "destination": "in.c-src.accounts"}],
        )

        result = _build(_service(tmp_path), root)

        targets = [e["target_fqn"] for e in result["edges"] if e["detection"] == "output_mapping"]
        assert targets == ["proj:in.c-src.accounts"]


# ---------------------------------------------------------------------------
# A4: AI task code_file paths
# ---------------------------------------------------------------------------


class TestAiTaskCodeFile:
    def test_flat_layout_alias_differs_from_dir_name(self, tmp_path: Path) -> None:
        """#846: flat layout + ConfigStore alias != dir name -> code_file must exist."""
        root = tmp_path / "some-random-cwd"
        cfg_path = "transformation/keboola.snowflake-transformation/needs-ai"
        _write_manifest(
            root,
            project_id=77,
            branches=[{"id": 9, "path": "main"}],
            configurations=[_config_entry("c1", cfg_path, branchId=9)],
        )
        _write_config(root / "main" / cfg_path, name="Needs AI", sql="SELECT * FROM foo")

        service = _service(tmp_path)
        app_config = AppConfig()
        app_config.projects["prod"] = ProjectConfig(
            stack_url="https://connection.keboola.com", token="901-test-token", project_id=77
        )
        service._config_store.save(app_config)

        result = _build(service, root, generate_ai_tasks=True)

        assert result["ai_status"]["ai_tasks_generated"] == 1
        tasks = json.loads((root / AI_TASKS_FILE).read_text())["tasks"]
        code_file = Path(tasks[0]["code_file"])
        assert code_file.exists()
        assert code_file == (root / "main" / cfg_path / "transform.sql").resolve()
        assert tasks[0]["config_fqn"] == f"prod:{SQL_COMPONENT}/c1"

    def test_missing_code_file_is_not_emitted(self, tmp_path: Path) -> None:
        graph = LineageGraph()
        cfg = Configuration(
            config_id="c1",
            config_name="Gone",
            component_id=SQL_COMPONENT,
            component_type="transformation",
            project_alias="p",
            project_id=1,
            path="transformation/x/gone",
            code="SELECT 1",
            code_type="sql",
            source_dir=str(tmp_path / "does-not-exist"),
        )
        graph.configurations[cfg.fqn] = cfg

        status = _service(tmp_path)._generate_ai_tasks(graph, tmp_path)

        assert status["ai_tasks_generated"] == 0
        assert status["ai_already_done"] == 0
        missing = status["ai_tasks_missing_code_file"]
        assert len(missing) == 1
        assert missing[0]["config_fqn"] == cfg.fqn
        assert missing[0]["code_file"].endswith("transform.sql")
        tasks = json.loads((tmp_path / AI_TASKS_FILE).read_text())
        assert tasks["tasks"] == []

    def test_instructions_describe_output_rules(self, tmp_path: Path) -> None:
        _service(tmp_path)._generate_ai_tasks(LineageGraph(), tmp_path)
        text = json.dumps(json.loads((tmp_path / AI_TASKS_FILE).read_text()))
        assert "full table id" in text.lower()
        assert "read|write" in text
        assert "output mapping" in text.lower()


# ---------------------------------------------------------------------------
# A5-A8: applying AI results
# ---------------------------------------------------------------------------

CFG_FQN = f"p:{SQL_COMPONENT}/c1"


def _graph(
    *,
    inputs: list[dict] | None = None,
    outputs: list[dict] | None = None,
) -> LineageGraph:
    graph = LineageGraph()
    cfg = Configuration(
        config_id="c1",
        config_name="T",
        component_id=SQL_COMPONENT,
        component_type="transformation",
        project_alias="p",
        project_id=1,
        path="transformation/x/t",
        input_tables=inputs or [],
        output_tables=outputs or [],
    )
    graph.configurations[cfg.fqn] = cfg
    return graph


def _reads(graph: LineageGraph, table_fqn: str) -> None:
    graph.add_edge(
        Edge(
            source_fqn=table_fqn,
            target_fqn=CFG_FQN,
            source_type="table",
            target_type="config",
            edge_type="reads",
            detection="input_mapping",
        )
    )


def _apply(tmp_path: Path, graph: LineageGraph, entry: dict) -> dict:
    entry = {"config_fqn": CFG_FQN, **entry}
    (tmp_path / AI_RESULTS_FILE).write_text(json.dumps({"results": [entry]}))
    return _service(tmp_path)._apply_ai_results_file(graph, tmp_path, {1: "p"})


def _writes_targets(graph: LineageGraph) -> list[str]:
    return [e.target_fqn for e in graph.edges if e.edge_type == "writes"]


class TestAiOutputs:
    def test_full_table_id_resolves_in_own_project(self, tmp_path: Path) -> None:
        graph = _graph()
        status = _apply(tmp_path, graph, {"outputs": [{"table": "out.c-x.t", "columns": ["a"]}]})
        assert _writes_targets(graph) == ["p:out.c-x.t"]
        edge = graph.edges[0]
        assert edge.source_fqn == CFG_FQN
        assert edge.detection == "ai"
        assert edge.columns == ["a"]
        assert status["ai_writes_added"] == 1
        assert status["ai_edges_added"] == 1

    def test_output_mapping_source_match_wins_over_basename(self, tmp_path: Path) -> None:
        graph = _graph(
            outputs=[
                {"source": "tmp", "destination": "out.c-a.summary"},
                {"source": "summary", "destination": "out.c-b.final"},
            ]
        )
        _apply(tmp_path, graph, {"outputs": [{"table": "summary"}]})
        assert _writes_targets(graph) == ["p:out.c-b.final"]

    def test_unique_destination_basename(self, tmp_path: Path) -> None:
        graph = _graph(outputs=[{"source": "s1", "destination": "out.c-a.orders"}])
        _apply(tmp_path, graph, {"outputs": [{"table": "orders"}]})
        assert _writes_targets(graph) == ["p:out.c-a.orders"]

    def test_ambiguous_basename_is_unresolved(self, tmp_path: Path) -> None:
        graph = _graph(
            outputs=[
                {"source": "s1", "destination": "out.c-a.orders"},
                {"source": "s2", "destination": "out.c-b.orders"},
            ]
        )
        status = _apply(tmp_path, graph, {"outputs": [{"table": "orders"}]})
        assert _writes_targets(graph) == []
        assert status["ai_outputs_unresolved"] == [
            {
                "config_fqn": CFG_FQN,
                "table": "orders",
                "reason": "ambiguous",
                "candidates": ["out.c-a.orders", "out.c-b.orders"],
            }
        ]

    def test_cross_project_is_unresolved(self, tmp_path: Path) -> None:
        graph = _graph()
        status = _apply(tmp_path, graph, {"outputs": [{"table": "other:out.c-a.t"}]})
        assert _writes_targets(graph) == []
        unresolved = status["ai_outputs_unresolved"]
        assert unresolved[0]["reason"] == "cross_project_not_supported"
        assert unresolved[0]["candidates"] == []

    def test_unknown_local_name_is_unresolved(self, tmp_path: Path) -> None:
        graph = _graph(outputs=[{"source": "s1", "destination": "out.c-a.orders"}])
        status = _apply(tmp_path, graph, {"outputs": [{"table": "nothing"}]})
        assert _writes_targets(graph) == []
        assert status["ai_outputs_unresolved"][0]["reason"] == "not_in_output_mapping"

    def test_columns_merge_into_existing_output_mapping_edge(self, tmp_path: Path) -> None:
        graph = _graph(outputs=[{"source": "orders", "destination": "out.c-a.orders"}])
        graph.add_edge(
            Edge(
                source_fqn=CFG_FQN,
                target_fqn="p:out.c-a.orders",
                source_type="config",
                target_type="table",
                edge_type="writes",
                detection="output_mapping",
                columns=["id"],
            )
        )
        status = _apply(
            tmp_path, graph, {"outputs": [{"table": "orders", "columns": ["id", "total"]}]}
        )
        assert len(graph.edges) == 1
        assert graph.edges[0].columns == ["id", "total"]
        assert graph.edges[0].detection == "output_mapping"
        assert status["ai_columns_merged"] == 1
        assert status["ai_writes_added"] == 0
        assert status["ai_edges_added"] == 0

    def test_reads_and_writes_counted_separately(self, tmp_path: Path) -> None:
        graph = _graph()
        status = _apply(
            tmp_path,
            graph,
            {
                "inputs": [{"pid": 1, "bucket": "in.c-a", "table": "src"}],
                "outputs": [{"table": "out.c-a.dst"}],
            },
        )
        assert status["ai_reads_added"] == 1
        assert status["ai_writes_added"] == 1
        assert status["ai_edges_added"] == 2


class TestAiExternal:
    def test_validate_and_dedupe(self, tmp_path: Path) -> None:
        graph = _graph()
        external = [
            {"system": "Slack", "op": "write"},
            {"system": "Slack", "op": "write"},
            {"system": "", "op": "read"},
            {"system": "S3", "op": "delete"},
            {"system": 5, "op": "read"},
            "junk",
            {"system": "S3", "op": "read"},
        ]
        status = _apply(tmp_path, graph, {"external": external})
        assert graph.configurations[CFG_FQN].external == [
            {"system": "Slack", "op": "write"},
            {"system": "S3", "op": "read"},
        ]
        assert status["ai_external_added"] == 2

        again = _service(tmp_path)._apply_ai_results_file(graph, tmp_path, {1: "p"})
        assert again["ai_external_added"] == 0
        assert len(graph.configurations[CFG_FQN].external) == 2

    def test_external_round_trips_through_cache(self) -> None:
        graph = _graph()
        cfg = graph.configurations[CFG_FQN]
        cfg.external = [{"system": "Slack", "op": "write"}]
        cfg.source_dir = "/somewhere"
        data = graph.to_dict()
        assert data["configurations"][CFG_FQN]["external"] == [{"system": "Slack", "op": "write"}]
        assert "source_dir" not in data["configurations"][CFG_FQN]
        restored = DeepLineageService._graph_from_dict(json.loads(json.dumps(data)))
        assert restored.configurations[CFG_FQN].external == [{"system": "Slack", "op": "write"}]
        assert restored.configurations[CFG_FQN].source_dir == ""

    def test_old_cache_without_external_loads_empty(self) -> None:
        data = _graph().to_dict()
        del data["configurations"][CFG_FQN]["external"]
        restored = DeepLineageService._graph_from_dict(data)
        assert restored.configurations[CFG_FQN].external == []


class TestAiColMap:
    def _two_reads(self) -> LineageGraph:
        graph = _graph(inputs=[{"source": "in.c-a.orders", "destination": "orders_alias"}])
        _reads(graph, "p:in.c-a.orders")
        _reads(graph, "p:in.c-a.orders_archive")
        return graph

    def _mappings(self, graph: LineageGraph) -> dict[str, dict[str, str]]:
        return {e.source_fqn: e.column_mapping for e in graph.edges}

    def test_exact_table_id_does_not_match_prefix(self, tmp_path: Path) -> None:
        graph = self._two_reads()
        _apply(
            tmp_path,
            graph,
            {"col_map": [{"out": "x", "in_table": "in.c-a.orders", "in_col": "id"}]},
        )
        assert self._mappings(graph) == {
            "p:in.c-a.orders": {"x": "in.c-a.orders.id"},
            "p:in.c-a.orders_archive": {},
        }

    def test_empty_in_table_is_skipped(self, tmp_path: Path) -> None:
        graph = self._two_reads()
        status = _apply(
            tmp_path, graph, {"col_map": [{"out": "x", "in_table": "", "in_col": "id"}]}
        )
        assert all(not m for m in self._mappings(graph).values())
        assert status["ai_col_map_skipped"] == 1

    def test_unknown_in_table_is_skipped(self, tmp_path: Path) -> None:
        graph = self._two_reads()
        status = _apply(
            tmp_path, graph, {"col_map": [{"out": "x", "in_table": "orders", "in_col": "id"}]}
        )
        assert all(not m for m in self._mappings(graph).values())
        assert status["ai_col_map_skipped"] == 1

    def test_input_mapping_alias_resolves_to_source(self, tmp_path: Path) -> None:
        graph = self._two_reads()
        status = _apply(
            tmp_path,
            graph,
            {"col_map": [{"out": "x", "in_table": "orders_alias", "in_col": "id"}]},
        )
        assert self._mappings(graph)["p:in.c-a.orders"] == {"x": "in.c-a.orders.id"}
        assert self._mappings(graph)["p:in.c-a.orders_archive"] == {}
        assert status["ai_col_map_skipped"] == 0


class TestLineageBuildHumanOutput:
    def test_unresolved_outputs_summary_is_bounded(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from keboola_agent_cli.cli import app

        root = tmp_path / "ws"
        cfg_path = "transformation/keboola.snowflake-transformation/t"
        _write_manifest(
            root,
            project_id=5,
            branches=[{"id": 1, "path": "main"}],
            configurations=[_config_entry("c1", cfg_path, branchId=1)],
        )
        _write_config(root / "main" / cfg_path, name="T", sql="SELECT 1")
        outputs = [{"table": f"[bold]missing_{i}"} for i in range(12)]
        (root / AI_RESULTS_FILE).write_text(
            json.dumps({"results": [{"config_fqn": f"ws:{SQL_COMPONENT}/c1", "outputs": outputs}]})
        )

        with patch(
            "keboola_agent_cli.services.deep_lineage_service."
            "DeepLineageService._add_cross_project_lineage"
        ):
            result = CliRunner().invoke(
                app,
                [
                    "--config-dir",
                    str(tmp_path / "cfg"),
                    "lineage",
                    "build",
                    "--directory",
                    str(root),
                    "--output",
                    str(tmp_path / "lineage.json"),
                ],
            )

        assert result.exit_code == 0, result.output
        assert "AI outputs not resolved to a table: 12" in result.output
        assert "[bold]missing_9" in result.output  # escaped, printed literally
        assert "missing_10" not in result.output
        assert "... and 2 more" in result.output


class TestAiColMapSameSourceTwice:
    """A config reading one table under two aliases has two reads edges (review)."""

    def _graph(self) -> LineageGraph:
        graph = _graph(
            inputs=[
                {"source": "in.c-a.orders", "destination": "o1"},
                {"source": "in.c-a.orders", "destination": "o2"},
            ]
        )
        _reads(graph, "p:in.c-a.orders")
        _reads(graph, "p:in.c-a.orders")
        return graph

    def test_exact_table_id_maps_onto_every_edge_of_that_source(self, tmp_path: Path) -> None:
        graph = self._graph()
        status = _apply(
            tmp_path,
            graph,
            {"col_map": [{"out": "x", "in_table": "in.c-a.orders", "in_col": "id"}]},
        )
        assert [e.column_mapping for e in graph.edges] == [{"x": "in.c-a.orders.id"}] * 2
        assert status["ai_col_map_skipped"] == 0

    def test_alias_maps_onto_every_edge_of_that_source(self, tmp_path: Path) -> None:
        graph = self._graph()
        status = _apply(
            tmp_path, graph, {"col_map": [{"out": "x", "in_table": "o2", "in_col": "id"}]}
        )
        assert [e.column_mapping for e in graph.edges] == [{"x": "in.c-a.orders.id"}] * 2
        assert status["ai_col_map_skipped"] == 0

    def test_two_distinct_sources_stay_ambiguous(self, tmp_path: Path) -> None:
        graph = _graph()
        _reads(graph, "p:in.c-a.orders")
        _reads(graph, "q:in.c-a.orders")
        status = _apply(
            tmp_path,
            graph,
            {"col_map": [{"out": "x", "in_table": "in.c-a.orders", "in_col": "id"}]},
        )
        assert all(not e.column_mapping for e in graph.edges)
        assert status["ai_col_map_skipped"] == 1


class TestAiStaleResults:
    """A result whose `_code_hash` no longer matches the code is not applied (review)."""

    CODE = "SELECT * FROM src"

    def _graph(self) -> LineageGraph:
        graph = _graph()
        graph.configurations[CFG_FQN].code = self.CODE
        graph.configurations[CFG_FQN].code_type = "sql"
        return graph

    def _entry(self, code_hash: str | None) -> dict:
        entry: dict = {
            "inputs": [{"pid": 1, "bucket": "in.c-a", "table": "src"}],
            "outputs": [{"table": "out.c-a.dst"}],
            "external": [{"system": "Slack", "op": "write"}],
        }
        if code_hash is not None:
            entry["_code_hash"] = code_hash
        return entry

    def test_mismatched_hash_is_not_applied(self, tmp_path: Path) -> None:
        graph = self._graph()
        status = _apply(tmp_path, graph, self._entry("0000000000000000"))
        assert graph.edges == []
        assert graph.configurations[CFG_FQN].external == []
        assert status["ai_results_stale"] == [CFG_FQN]
        assert status["ai_edges_added"] == 0

    def test_matching_hash_is_applied(self, tmp_path: Path) -> None:
        import hashlib

        graph = self._graph()
        current = hashlib.sha256(self.CODE.encode()).hexdigest()[:16]
        status = _apply(tmp_path, graph, self._entry(current))
        assert status["ai_edges_added"] == 2
        assert status["ai_results_stale"] == []

    def test_missing_hash_is_applied(self, tmp_path: Path) -> None:
        graph = self._graph()
        status = _apply(tmp_path, graph, self._entry(None))
        assert status["ai_edges_added"] == 2
        assert status["ai_results_stale"] == []

    def test_human_build_output_shows_stale_count(self) -> None:
        import io

        from rich.console import Console

        from keboola_agent_cli.commands._lineage_build_output import print_ai_status
        from keboola_agent_cli.output import OutputFormatter

        formatter = OutputFormatter(no_color=True)
        buffer = io.StringIO()
        formatter.console = Console(file=buffer, width=200, no_color=True)
        print_ai_status(
            formatter, {"ai_results_applied": True, "ai_results_stale": [CFG_FQN, "p:x/y"]}
        )
        output = buffer.getvalue()
        assert "AI results stale (code changed, not applied): 2" in output
        assert CFG_FQN in output
