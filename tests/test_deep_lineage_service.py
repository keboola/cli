"""Tests for DeepLineageService - column-level lineage from sync'd data."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.services.deep_lineage_service import (
    Configuration,
    DeepLineageService,
    Edge,
    LineageGraph,
    Table,
    _collect_create_targets,
    _collect_cte_names,
    _read_source,
    _strip_comments_and_strings,
    extract_sql_table_refs,
)

# ---------------------------------------------------------------------------
# SQL tokenizer unit tests
# ---------------------------------------------------------------------------


class TestStripCommentsAndStrings:
    def test_line_comment(self) -> None:
        sql = "SELECT 1 -- this is a comment\nFROM t"
        result = _strip_comments_and_strings(sql)
        assert "comment" not in result
        assert "FROM t" in result

    def test_block_comment(self) -> None:
        sql = "SELECT /* hidden */ 1 FROM t"
        result = _strip_comments_and_strings(sql)
        assert "hidden" not in result
        assert "SELECT" in result
        assert "FROM t" in result

    def test_string_literal(self) -> None:
        sql = """SELECT 'text with "KBC_USE4_123"."b"."t"' FROM t"""
        result = _strip_comments_and_strings(sql)
        assert "KBC_USE4_123" not in result
        assert "FROM t" in result

    def test_escaped_quotes(self) -> None:
        sql = "SELECT 'it''s fine' FROM t"
        result = _strip_comments_and_strings(sql)
        assert "FROM t" in result

    def test_preserves_length(self) -> None:
        sql = "SELECT /* x */ 1 -- y\nFROM t"
        result = _strip_comments_and_strings(sql)
        assert len(result) == len(sql)


class TestCollectCteNames:
    def test_single_cte(self) -> None:
        sql = "WITH cte1 AS (SELECT 1) SELECT * FROM cte1"
        names = _collect_cte_names(sql)
        assert "cte1" in names

    def test_multiple_ctes(self) -> None:
        sql = "WITH a AS (SELECT 1), b AS (SELECT 2) SELECT * FROM a JOIN b"
        names = _collect_cte_names(sql)
        assert "a" in names
        assert "b" in names

    def test_quoted_cte(self) -> None:
        sql = 'WITH "MyCte" AS (SELECT 1) SELECT * FROM "MyCte"'
        names = _collect_cte_names(sql)
        assert "mycte" in names


class TestCollectCreateTargets:
    def test_create_table(self) -> None:
        sql = 'CREATE TABLE "out_table" AS SELECT 1'
        targets = _collect_create_targets(sql)
        assert ("", "out_table") in targets

    def test_create_or_replace(self) -> None:
        sql = 'CREATE OR REPLACE TABLE "result" AS SELECT 1'
        targets = _collect_create_targets(sql)
        assert ("", "result") in targets

    def test_two_part_create(self) -> None:
        sql = 'CREATE TABLE "out.c-bucket"."my_table" AS SELECT 1'
        targets = _collect_create_targets(sql)
        assert ("out.c-bucket", "my_table") in targets


class TestExtractSqlTableRefs:
    def test_three_part_ref(self) -> None:
        sql = 'SELECT * FROM "KBC_USE4_123"."in.c-bucket"."my_table"'
        refs = extract_sql_table_refs(sql, project_id=999)
        assert (123, "in.c-bucket", "my_table") in refs

    def test_two_part_ref_from(self) -> None:
        sql = 'SELECT * FROM "in.c-bucket"."my_table"'
        refs = extract_sql_table_refs(sql, project_id=42)
        assert (42, "in.c-bucket", "my_table") in refs

    def test_two_part_ref_join(self) -> None:
        sql = 'SELECT * FROM "in.c-a"."t1" JOIN "out.c-b"."t2" ON t1.id = t2.id'
        refs = extract_sql_table_refs(sql, project_id=42)
        assert (42, "in.c-a", "t1") in refs
        assert (42, "out.c-b", "t2") in refs

    def test_ignores_non_bucket_two_part(self) -> None:
        sql = 'SELECT * FROM "my_schema"."my_table"'
        refs = extract_sql_table_refs(sql, project_id=42)
        assert len(refs) == 0  # "my_schema" doesn't start with in./out.

    def test_ignores_comments(self) -> None:
        sql = '-- FROM "KBC_USE4_123"."in.c-b"."t"\nSELECT 1'
        refs = extract_sql_table_refs(sql, project_id=42)
        assert len(refs) == 0

    def test_ignores_string_literals(self) -> None:
        sql = """SELECT 'ref to "KBC_USE4_123"."in.c-b"."t"' FROM dual"""
        refs = extract_sql_table_refs(sql, project_id=42)
        assert len(refs) == 0

    def test_ignores_cte_names(self) -> None:
        # CTE name "stats" collides with table name - tokenizer correctly
        # filters it out because it can't distinguish the two.
        # Real-world: CTE names rarely collide with Keboola table names.
        sql = 'WITH stats AS (SELECT 1) SELECT * FROM "in.c-b"."stats"'
        refs = extract_sql_table_refs(sql, project_id=42)
        assert len(refs) == 0  # filtered by CTE name match

        # Non-colliding names work fine
        sql2 = 'WITH cte AS (SELECT 1) SELECT * FROM "in.c-b"."real_table"'
        refs2 = extract_sql_table_refs(sql2, project_id=42)
        assert len(refs2) == 1

    def test_deduplicates(self) -> None:
        sql = """
            SELECT * FROM "KBC_USE4_1"."in.c-b"."t"
            UNION ALL
            SELECT * FROM "KBC_USE4_1"."in.c-b"."t"
        """
        refs = extract_sql_table_refs(sql, project_id=42)
        assert len(refs) == 1

    def test_cross_project_and_same_project(self) -> None:
        sql = """
            SELECT a.*, b.*
            FROM "KBC_USE4_100"."out.c-sfdc"."company" a
            JOIN "in.c-local"."my_table" b ON a.id = b.id
        """
        refs = extract_sql_table_refs(sql, project_id=42)
        assert (100, "out.c-sfdc", "company") in refs
        assert (42, "in.c-local", "my_table") in refs


# ---------------------------------------------------------------------------
# LineageGraph unit tests
# ---------------------------------------------------------------------------


class TestLineageGraph:
    def test_add_edge_indexes(self) -> None:
        graph = LineageGraph()
        edge = Edge(
            source_fqn="p:table_a",
            target_fqn="p:config/1",
            source_type="table",
            target_type="config",
            edge_type="reads",
            detection="input_mapping",
        )
        graph.add_edge(edge)
        assert len(graph.edges) == 1
        assert graph._downstream["p:table_a"] == [0]
        assert graph._upstream["p:config/1"] == [0]

    def test_get_upstream(self) -> None:
        graph = LineageGraph()
        graph.add_edge(
            Edge(
                source_fqn="p:table_a",
                target_fqn="p:config/1",
                source_type="table",
                target_type="config",
                edge_type="reads",
                detection="input_mapping",
            )
        )
        graph.add_edge(
            Edge(
                source_fqn="p:config/1",
                target_fqn="p:table_b",
                source_type="config",
                target_type="table",
                edge_type="writes",
                detection="output_mapping",
            )
        )
        result = graph.get_upstream("p:table_b", depth=5)
        assert len(result) == 2
        assert result[0]["source"] == "p:config/1"
        assert result[1]["source"] == "p:table_a"

    def test_get_downstream(self) -> None:
        graph = LineageGraph()
        graph.add_edge(
            Edge(
                source_fqn="p:table_a",
                target_fqn="p:config/1",
                source_type="table",
                target_type="config",
                edge_type="reads",
                detection="input_mapping",
            )
        )
        result = graph.get_downstream("p:table_a", depth=1)
        assert len(result) == 1
        assert result[0]["target"] == "p:config/1"

    def test_depth_limit(self) -> None:
        graph = LineageGraph()
        graph.add_edge(
            Edge(
                source_fqn="a",
                target_fqn="b",
                source_type="table",
                target_type="config",
                edge_type="reads",
                detection="test",
            )
        )
        graph.add_edge(
            Edge(
                source_fqn="b",
                target_fqn="c",
                source_type="config",
                target_type="table",
                edge_type="writes",
                detection="test",
            )
        )
        result = graph.get_downstream("a", depth=1)
        assert len(result) == 1  # only a->b, not b->c

    def test_summary(self) -> None:
        graph = LineageGraph()
        graph.tables["p:t1"] = Table(
            table_id="t1",
            project_alias="p",
            project_id=1,
            bucket_id="b",
            name="t1",
        )
        graph.configurations["p:c/1"] = Configuration(
            config_id="1",
            config_name="test",
            component_id="c",
            component_type="transformation",
            project_alias="p",
            project_id=1,
            path="transformation/c/test",
        )
        graph.add_edge(
            Edge(
                source_fqn="p:t1",
                target_fqn="p:c/1",
                source_type="table",
                target_type="config",
                edge_type="reads",
                detection="input_mapping",
            )
        )
        s = graph.summary()
        assert s["tables"] == 1
        assert s["configurations"] == 1
        assert s["edges"] == 1
        assert s["edge_types"] == {"reads": 1}

    def test_to_dict_and_from_dict(self) -> None:
        graph = LineageGraph()
        graph.tables["p:b.t"] = Table(
            table_id="b.t",
            project_alias="p",
            project_id=1,
            bucket_id="b",
            name="t",
            columns=["a", "b"],
        )
        graph.configurations["p:c/1"] = Configuration(
            config_id="1",
            config_name="test",
            component_id="c",
            component_type="transformation",
            project_alias="p",
            project_id=1,
            path="transformation/c/test",
        )
        graph.add_edge(
            Edge(
                source_fqn="p:b.t",
                target_fqn="p:c/1",
                source_type="table",
                target_type="config",
                edge_type="reads",
                detection="sql_tokenizer",
                columns=["a"],
            )
        )
        data = graph.to_dict()
        restored = DeepLineageService._graph_from_dict(data)
        assert len(restored.tables) == 1
        assert len(restored.configurations) == 1
        assert len(restored.edges) == 1
        assert restored.edges[0].columns == ["a"]


# ---------------------------------------------------------------------------
# Service: scan + build from disk
# ---------------------------------------------------------------------------


def _create_sync_tree(tmp_path: Path) -> Path:
    """Create a minimal sync'd project structure for testing."""
    root = tmp_path / "workspace"
    root.mkdir()

    # Project: test-project
    proj = root / "test-project"
    proj.mkdir()

    # .keboola/manifest.json
    keboola = proj / ".keboola"
    keboola.mkdir()
    manifest = {
        "version": 2,
        "project": {"id": 42, "name": "Test Project"},
        "configurations": [
            {
                "branchId": 1,
                "componentId": "keboola.snowflake-transformation",
                "id": "cfg-1",
                "path": "transformation/keboola.snowflake-transformation/my-transform",
                "rows": [],
            },
            {
                "branchId": 1,
                "componentId": "keboola.ex-db-snowflake",
                "id": "cfg-2",
                "path": "extractor/keboola.ex-db-snowflake/my-extractor",
                "rows": [
                    {
                        "id": "row-1",
                        "path": "extractor/keboola.ex-db-snowflake/my-extractor/rows/row-1",
                    }
                ],
            },
        ],
        "branches": [],
    }
    (keboola / "manifest.json").write_text(json.dumps(manifest))

    # Storage tables
    storage = proj / "storage" / "tables" / "in-c-source"
    storage.mkdir(parents=True)
    (storage / "accounts.json").write_text(
        json.dumps(
            {
                "id": "in.c-source.accounts",
                "name": "accounts",
                "columns": ["id", "name", "email"],
                "primary_key": ["id"],
                "rows_count": 1000,
            }
        )
    )

    out_storage = proj / "storage" / "tables" / "out-c-result"
    out_storage.mkdir(parents=True)
    (out_storage / "summary.json").write_text(
        json.dumps(
            {
                "id": "out.c-result.summary",
                "name": "summary",
                "columns": ["account_id", "total"],
                "primary_key": [],
                "rows_count": 50,
            }
        )
    )

    # Transformation config
    transform_dir = (
        proj / "main" / "transformation" / "keboola.snowflake-transformation" / "my-transform"
    )
    transform_dir.mkdir(parents=True)
    (transform_dir / "_config.yml").write_text(
        yaml.dump(
            {
                "version": 2,
                "name": "My Transform",
                "input": {"tables": []},
                "output": {
                    "tables": [
                        {"source": "summary", "destination": "out.c-result.summary"},
                    ]
                },
            }
        )
    )
    (transform_dir / "transform.sql").write_text(
        'CREATE TABLE "summary" AS\n'
        'SELECT "id" AS "account_id", COUNT(*) AS "total"\n'
        'FROM "KBC_USE4_42"."in.c-source"."accounts"\n'
        'GROUP BY "id"'
    )

    # Extractor config with row
    extractor_dir = proj / "main" / "extractor" / "keboola.ex-db-snowflake" / "my-extractor"
    extractor_dir.mkdir(parents=True)
    (extractor_dir / "_config.yml").write_text(
        yaml.dump(
            {
                "version": 2,
                "name": "My Extractor",
                "input": {"tables": []},
                "output": {"tables": []},
            }
        )
    )
    row_dir = extractor_dir / "rows" / "row-1"
    row_dir.mkdir(parents=True)
    (row_dir / "_config.yml").write_text(
        yaml.dump(
            {
                "version": 2,
                "name": "Accounts Row",
                "input": {"tables": []},
                "output": {
                    "tables": [
                        {"source": "accounts", "destination": "in.c-source.accounts"},
                    ]
                },
            }
        )
    )

    return root


class TestDeepLineageServiceScan:
    def test_scan_and_build(self, tmp_path: Path) -> None:
        root = _create_sync_tree(tmp_path)
        store = ConfigStore(config_dir=tmp_path / "cfg")
        (tmp_path / "cfg").mkdir()
        service = DeepLineageService(config_store=store)

        with patch.object(service, "_add_cross_project_lineage"):
            result = service.build_lineage(root)

        assert result["summary"]["tables"] == 2
        assert result["summary"]["configurations"] == 2
        # Edges: extractor row->table (output_mapping) + transform->table (output_mapping)
        #        + transform reads from table (sql_tokenizer)
        edges = result["edges"]
        detections = [e["detection"] for e in edges]
        assert "output_mapping" in detections
        assert "sql_tokenizer" in detections

    def test_query_upstream(self, tmp_path: Path) -> None:
        root = _create_sync_tree(tmp_path)
        store = ConfigStore(config_dir=tmp_path / "cfg")
        (tmp_path / "cfg").mkdir()
        service = DeepLineageService(config_store=store)

        with patch.object(service, "_add_cross_project_lineage"):
            result = service.build_lineage(root)

        graph = service._graph_from_dict(result)
        upstream = service.query_upstream(graph, "out.c-result.summary", "test-project")
        assert "error" not in upstream
        assert upstream["node"] == "test-project:out.c-result.summary"
        assert len(upstream["edges"]) >= 1

    def test_query_downstream(self, tmp_path: Path) -> None:
        root = _create_sync_tree(tmp_path)
        store = ConfigStore(config_dir=tmp_path / "cfg")
        (tmp_path / "cfg").mkdir()
        service = DeepLineageService(config_store=store)

        with patch.object(service, "_add_cross_project_lineage"):
            result = service.build_lineage(root)

        graph = service._graph_from_dict(result)
        downstream = service.query_downstream(graph, "in.c-source.accounts", "test-project")
        assert "error" not in downstream
        assert len(downstream["edges"]) >= 1

    def test_query_not_found(self, tmp_path: Path) -> None:
        root = _create_sync_tree(tmp_path)
        store = ConfigStore(config_dir=tmp_path / "cfg")
        (tmp_path / "cfg").mkdir()
        service = DeepLineageService(config_store=store)

        with patch.object(service, "_add_cross_project_lineage"):
            result = service.build_lineage(root)

        graph = service._graph_from_dict(result)
        res = service.query_upstream(graph, "nonexistent.table")
        assert "error" in res

    def test_cache_roundtrip(self, tmp_path: Path) -> None:
        root = _create_sync_tree(tmp_path)
        cache_path = tmp_path / "lineage.json"
        store = ConfigStore(config_dir=tmp_path / "cfg")
        (tmp_path / "cfg").mkdir()
        service = DeepLineageService(config_store=store)

        with patch.object(service, "_add_cross_project_lineage"):
            service.build_and_cache(root, cache_path)

        assert cache_path.exists()
        graph = service.load_from_cache(cache_path)
        assert len(graph.tables) == 2
        assert len(graph.edges) >= 2


# ---------------------------------------------------------------------------
# CLI tests via CliRunner
# ---------------------------------------------------------------------------


class TestLineageDeepCli:
    def test_help(self) -> None:
        from keboola_agent_cli.cli import app

        runner_local = __import__("typer.testing", fromlist=["CliRunner"]).CliRunner()
        # Strip ANSI helper
        import re as _re

        def _strip_ansi(s: str) -> str:
            return _re.sub(r"\x1b\[[0-9;]*m", "", s)

        # Test build help
        result = runner_local.invoke(app, ["lineage", "build", "--help"])
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "Build column-level lineage" in output
        assert "--output" in output
        assert "--refresh" in output
        assert "--ai" in output

        # Test show help
        result = runner_local.invoke(app, ["lineage", "show", "--help"])
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "--upstream" in output
        assert "--downstream" in output
        assert "--columns" in output
        assert "project-alias:bucket_id.table_name" in result.output

    def test_load_and_query_json(self, tmp_path: Path) -> None:
        from keboola_agent_cli.cli import app

        root = _create_sync_tree(tmp_path)
        cache_path = tmp_path / "lineage.json"
        store = ConfigStore(config_dir=tmp_path / "cfg")
        (tmp_path / "cfg").mkdir()
        service = DeepLineageService(config_store=store)

        with patch.object(service, "_add_cross_project_lineage"):
            service.build_and_cache(root, cache_path)

        runner_local = __import__("typer.testing", fromlist=["CliRunner"]).CliRunner()
        result = runner_local.invoke(
            app,
            [
                "--json",
                "lineage",
                "show",
                "--load",
                str(cache_path),
                "--upstream",
                "test-project:out.c-result.summary",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["data"]["node"] == "test-project:out.c-result.summary"

    def test_missing_cache_file(self) -> None:
        from keboola_agent_cli.cli import app

        runner_local = __import__("typer.testing", fromlist=["CliRunner"]).CliRunner()
        result = runner_local.invoke(
            app,
            [
                "--json",
                "lineage",
                "show",
                "--load",
                "/nonexistent/lineage.json",
            ],
        )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Sync layout handling: flat (single project in CWD) vs. nested (multi-project)
# ---------------------------------------------------------------------------


def _create_flat_project(
    root: Path,
    *,
    project_id: int = 42,
    with_storage: bool = True,
    with_config: bool = True,
) -> None:
    """Create a single-project flat layout directly under ``root``.

    This mirrors what ``kbagent sync pull --project X`` produces: the
    ``.keboola/manifest.json`` lives at the provided root (no alias subdir).
    """
    keboola = root / ".keboola"
    keboola.mkdir(parents=True)
    configurations: list[dict] = []
    if with_config:
        configurations.append(
            {
                "branchId": 1,
                "componentId": "keboola.snowflake-transformation",
                "id": "cfg-flat",
                "path": "transformation/keboola.snowflake-transformation/flat-transform",
                "rows": [],
            }
        )
    manifest = {
        "version": 2,
        "project": {"id": project_id, "name": "Flat Project"},
        "configurations": configurations,
        "branches": [],
    }
    (keboola / "manifest.json").write_text(json.dumps(manifest))

    if with_storage:
        storage = root / "storage" / "tables" / "in-c-flat"
        storage.mkdir(parents=True)
        (storage / "accounts.json").write_text(
            json.dumps(
                {
                    "id": "in.c-flat.accounts",
                    "name": "accounts",
                    "columns": ["id", "name"],
                    "primary_key": ["id"],
                    "rows_count": 10,
                }
            )
        )

    if with_config:
        transform_dir = (
            root / "main" / "transformation" / "keboola.snowflake-transformation" / "flat-transform"
        )
        transform_dir.mkdir(parents=True)
        (transform_dir / "_config.yml").write_text(
            yaml.dump(
                {
                    "version": 2,
                    "name": "Flat Transform",
                    "input": {
                        "tables": [
                            {"source": "in.c-flat.accounts", "destination": "accounts"},
                        ]
                    },
                    "output": {"tables": []},
                }
            )
        )
        (transform_dir / "transform.sql").write_text('SELECT * FROM "in.c-flat"."accounts"\n')


class TestDeepLineageLayouts:
    """Covers the flat vs. nested sync-layout detection in ``_scan_projects``."""

    def test_flat_layout_detects_single_project(self, tmp_path: Path) -> None:
        """``root/.keboola/manifest.json`` is treated as a single project."""
        root = tmp_path / "synced-foo"
        root.mkdir()
        _create_flat_project(root)

        store = ConfigStore(config_dir=tmp_path / "cfg")
        (tmp_path / "cfg").mkdir()
        service = DeepLineageService(config_store=store)

        with patch.object(service, "_add_cross_project_lineage"):
            result = service.build_lineage(root)

        assert result["summary"]["tables"] == 1
        assert result["summary"]["configurations"] == 1
        # Edges: input_mapping + sql_tokenizer (table referenced in SQL)
        assert result["summary"]["edges"] >= 1
        assert result["warnings"] == []

    def test_flat_layout_uses_config_store_alias_when_available(self, tmp_path: Path) -> None:
        """In flat mode the alias comes from ConfigStore, not the CWD name."""
        from keboola_agent_cli.models import AppConfig, ProjectConfig

        root = tmp_path / "some-random-cwd"
        root.mkdir()
        _create_flat_project(root, project_id=77)

        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        store = ConfigStore(config_dir=cfg_dir)
        config = AppConfig()
        config.projects["prod"] = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="test-token",
            project_id=77,
        )
        store.save(config)

        service = DeepLineageService(config_store=store)
        with patch.object(service, "_add_cross_project_lineage"):
            result = service.build_lineage(root)

        # Exactly one table keyed by the ConfigStore alias, not by dir name.
        assert list(result["tables"].keys()) == ["prod:in.c-flat.accounts"]

    def test_flat_layout_falls_back_to_dir_name_without_config_store_match(
        self, tmp_path: Path
    ) -> None:
        """If no alias matches ``project.id`` we keep the directory name."""
        root = tmp_path / "my-alias"
        root.mkdir()
        _create_flat_project(root, project_id=999)

        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        store = ConfigStore(config_dir=cfg_dir)  # empty store
        service = DeepLineageService(config_store=store)

        with patch.object(service, "_add_cross_project_lineage"):
            result = service.build_lineage(root)

        assert list(result["tables"].keys()) == ["my-alias:in.c-flat.accounts"]

    def test_nested_layout_still_works(self, tmp_path: Path) -> None:
        """Regression guard: nested ``sync pull --all-projects`` layout."""
        root = _create_sync_tree(tmp_path)  # nested layout helper
        store = ConfigStore(config_dir=tmp_path / "cfg")
        (tmp_path / "cfg").mkdir()
        service = DeepLineageService(config_store=store)

        with patch.object(service, "_add_cross_project_lineage"):
            result = service.build_lineage(root)

        assert result["summary"]["tables"] == 2
        assert result["summary"]["configurations"] == 2
        assert result["warnings"] == []

    def test_empty_directory_emits_warning(self, tmp_path: Path) -> None:
        """Neither flat nor nested layout -> zero-scan + hint warning."""
        root = tmp_path / "empty"
        root.mkdir()
        store = ConfigStore(config_dir=tmp_path / "cfg")
        (tmp_path / "cfg").mkdir()
        service = DeepLineageService(config_store=store)

        with patch.object(service, "_add_cross_project_lineage"):
            result = service.build_lineage(root)

        assert result["summary"]["tables"] == 0
        assert result["summary"]["configurations"] == 0
        assert len(result["warnings"]) == 1
        warning = result["warnings"][0]
        assert "No synced projects found" in warning
        assert "flat layout" in warning
        assert "nested layout" in warning
        assert "sync pull --all-projects" in warning


class TestLineageBuildCli:
    """CLI-layer smoke tests for the flat/empty layouts."""

    def _runner(self):
        return __import__("typer.testing", fromlist=["CliRunner"]).CliRunner()

    def test_build_flat_layout_non_empty_graph(self, tmp_path: Path) -> None:
        """``lineage build`` against a flat-layout dir produces a non-empty graph."""
        from keboola_agent_cli.cli import app

        # Place synced project next to cfg dir so cwd doesn't matter.
        synced = tmp_path / "synced"
        synced.mkdir()
        _create_flat_project(synced)
        cache_path = tmp_path / "lineage.json"

        with patch(
            "keboola_agent_cli.services.deep_lineage_service."
            "DeepLineageService._add_cross_project_lineage"
        ):
            result = self._runner().invoke(
                app,
                [
                    "--json",
                    "--config-dir",
                    str(tmp_path / "cfg"),
                    "lineage",
                    "build",
                    "--directory",
                    str(synced),
                    "--output",
                    str(cache_path),
                ],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["summary"]["tables"] == 1
        assert data["summary"]["configurations"] == 1
        assert data["warnings"] == []
        assert cache_path.exists()

    def test_build_empty_directory_exits_zero_with_warning(self, tmp_path: Path) -> None:
        """Empty dir -> exit 0 with a warning describing the expected layouts."""
        from keboola_agent_cli.cli import app

        empty = tmp_path / "empty"
        empty.mkdir()
        cache_path = tmp_path / "lineage.json"

        with patch(
            "keboola_agent_cli.services.deep_lineage_service."
            "DeepLineageService._add_cross_project_lineage"
        ):
            result = self._runner().invoke(
                app,
                [
                    "--json",
                    "--config-dir",
                    str(tmp_path / "cfg"),
                    "lineage",
                    "build",
                    "--directory",
                    str(empty),
                    "--output",
                    str(cache_path),
                ],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["summary"]["tables"] == 0
        assert len(data["warnings"]) == 1
        assert "No synced projects found" in data["warnings"][0]
        assert cache_path.exists()


# ---------------------------------------------------------------------------
# Issue #269 sec-05: HTML/Mermaid output XSS regression
# ---------------------------------------------------------------------------


class TestRenderErDiagramXssRegression:
    """Issue #269 sec-05 -- entity / config names from the API must not be
    embeddable as HTML in the lineage HTML output."""

    def _make_graph(self, table_name: str, config_name: str) -> tuple[LineageGraph, list[dict]]:
        graph = LineageGraph()
        table_fqn = f"prod:in.c-bucket.{table_name}"
        config_fqn = "prod:keboola.snowflake-transformation/cfg-1"
        graph.tables[table_fqn] = Table(
            table_id=f"in.c-bucket.{table_name}",
            project_alias="prod",
            project_id=1,
            bucket_id="in.c-bucket",
            name=table_name,
            primary_key=[],
            columns=["id", "value"],
            rows_count=100,
        )
        graph.configurations[config_fqn] = Configuration(
            config_id="cfg-1",
            config_name=config_name,
            component_id="keboola.snowflake-transformation",
            component_type="transformation",
            project_alias="prod",
            project_id=1,
            path="transformation/keboola.snowflake-transformation/cfg-1",
        )
        edges = [
            {
                "source": table_fqn,
                "target": config_fqn,
                "edge_type": "input_mapping",
                "column_mapping": {},
            },
            {
                "source": config_fqn,
                "target": table_fqn,
                "edge_type": "output_mapping",
                "column_mapping": {},
            },
        ]
        return graph, edges

    def test_table_name_with_html_is_escaped(self) -> None:
        """A Keboola table named with </div><script> survives sanitization
        without injecting raw HTML into the diagram body."""
        graph, edges = self._make_graph(
            table_name="</div><script>alert(1)</script>",
            config_name="ok",
        )
        rendered = DeepLineageService.render_er_diagram(
            edges=edges,
            graph=graph,
            node_fqn=next(iter(graph.tables)),
            show_columns=False,
        )
        assert "<script>" not in rendered
        assert "</div>" not in rendered
        assert "&lt;script&gt;" in rendered or "&lt;/script&gt;" in rendered

    def test_config_name_with_html_is_escaped(self) -> None:
        """A Keboola config_name with </div><img> is also escaped."""
        graph, edges = self._make_graph(
            table_name="users",
            config_name="</div><img src=x onerror=alert(1)>",
        )
        rendered = DeepLineageService.render_er_diagram(
            edges=edges,
            graph=graph,
            node_fqn=next(iter(graph.tables)),
            show_columns=False,
        )
        assert "<img" not in rendered
        assert "&lt;img" in rendered


class TestSourceReadsAreUtf8:
    """Transformation code must decode the same on every host (issue #570).

    `Path.read_text()` uses the platform default encoding. On a Czech or Polish
    Windows box that is cp1250, so one accented character in a SQL comment
    aborted the entire `lineage build` with `UnicodeDecodeError`. These run
    everywhere by driving the decode explicitly rather than by depending on the
    host's locale.
    """

    def test_utf8_content_survives_a_cp1250_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact reported crash: UTF-8 bytes read on a cp1250 host."""
        sql = tmp_path / "transform.sql"
        # U+0159 encodes to 0xC5 0x99 in UTF-8; 0x99 is undefined in cp1250,
        # which is what raised in the report.
        sql.write_bytes("-- příprava dat\nSELECT 1 FROM t;\n".encode())

        real_read_text = Path.read_text

        def cp1250_default(
            self: Path, encoding: str | None = None, errors: str | None = None
        ) -> str:
            # Mimic a Windows host: no explicit encoding means cp1250.
            return real_read_text(self, encoding=encoding or "cp1250", errors=errors)

        monkeypatch.setattr(Path, "read_text", cp1250_default)

        code = _read_source(sql)
        assert "SELECT 1 FROM t" in code
        assert "příprava" in code, "UTF-8 content must round-trip intact"

    def test_genuinely_undecodable_bytes_do_not_abort_the_build(self, tmp_path: Path) -> None:
        """A locally-edited file saved in the OS codepage costs a comment, not the graph."""
        sql = tmp_path / "transform.sql"
        # 0x81 is invalid UTF-8 and is the byte from the report's traceback.
        sql.write_bytes(b"-- koment\x81\nSELECT 1 FROM in.c_bucket.tbl;\n")

        code = _read_source(sql)

        # The table reference -- the only thing lineage actually needs -- survives.
        assert "in.c_bucket.tbl" in code
