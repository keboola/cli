"""Tests for lineage traversal, text tree, column trace and diagrams (issue #848)."""

import io
import json
import re
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.services._lineage_trace import trace_column
from keboola_agent_cli.services.deep_lineage_service import (
    Configuration,
    DeepLineageService,
    Edge,
    LineageGraph,
    Table,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Graph builders
# ---------------------------------------------------------------------------


def _table(graph: LineageGraph, fqn: str, columns: list[str] | None = None) -> None:
    alias, _, table_id = fqn.partition(":")
    bucket, _, name = table_id.rpartition(".")
    graph.tables[fqn] = Table(
        table_id=table_id,
        project_alias=alias,
        project_id=1,
        bucket_id=bucket,
        name=name,
        columns=columns or [],
    )


def _config(graph: LineageGraph, fqn: str, name: str, external: list[dict] | None = None) -> None:
    alias, _, rest = fqn.partition(":")
    component_id, _, config_id = rest.partition("/")
    graph.configurations[fqn] = Configuration(
        config_id=config_id,
        config_name=name,
        component_id=component_id,
        component_type="transformation",
        project_alias=alias,
        project_id=1,
        path=f"transformation/{component_id}/{config_id}",
    )
    if external is not None:
        graph.configurations[fqn].external = external


def _reads(
    graph: LineageGraph,
    table: str,
    config: str,
    *,
    detection: str = "input_mapping",
    mapping: dict[str, str] | None = None,
    columns: list[str] | None = None,
) -> None:
    graph.add_edge(
        Edge(
            source_fqn=table,
            target_fqn=config,
            source_type="table",
            target_type="config",
            edge_type="reads",
            detection=detection,
            columns=columns or [],
            column_mapping=mapping or {},
        )
    )


def _writes(
    graph: LineageGraph,
    config: str,
    table: str,
    *,
    detection: str = "output_mapping",
    columns: list[str] | None = None,
) -> None:
    graph.add_edge(
        Edge(
            source_fqn=config,
            target_fqn=table,
            source_type="config",
            target_type="table",
            edge_type="writes",
            detection=detection,
            columns=columns or [],
        )
    )


def _share(graph: LineageGraph, source: str, target: str) -> None:
    graph.add_edge(
        Edge(
            source_fqn=source,
            target_fqn=target,
            source_type="table",
            target_type="table",
            edge_type="cross_project_share",
            detection="bucket_sharing",
        )
    )


RECON = "p:out.c-recon.RECON_MATCHED"
RECON_CFG = "p:keboola.snowflake-transformation/recon"
GROUPREP = "p:out.c-onestream.OS_GROUPREP_RESULT"
JOURNAL = "p:out.c-journal.FT_FIN_JOURNAL_UK"
FDX = "p:in.c-actuals.fdx_export"
GROUPREP_CFG = "p:keboola.snowflake-transformation/grouprep"
JOURNAL_CFG = "p:keboola.snowflake-transformation/journal"


def _issue_848_graph() -> LineageGraph:
    """The shape reported in #848: two producers under different input tables."""
    graph = LineageGraph()
    for fqn in (RECON, GROUPREP, JOURNAL, FDX):
        _table(graph, fqn)
    _config(graph, RECON_CFG, "Reconciliation")
    _config(graph, GROUPREP_CFG, "GroupRep Import")
    _config(graph, JOURNAL_CFG, "Journal Entries UK")
    _writes(graph, RECON_CFG, RECON)
    _reads(graph, GROUPREP, RECON_CFG, detection="ai")
    _reads(graph, JOURNAL, RECON_CFG, detection="ai")
    _reads(graph, FDX, RECON_CFG, detection="ai")
    _writes(graph, GROUPREP_CFG, GROUPREP)
    _writes(graph, JOURNAL_CFG, JOURNAL)
    return graph


def _cache(tmp_path: Path, graph: LineageGraph, name: str = "lineage.json") -> Path:
    data = graph.to_dict()
    # to_dict does not carry `external` yet (Part A adds it); patch it in so
    # the CLI tests see annotations however the cache was written.
    for fqn, cfg in graph.configurations.items():
        data["configurations"][fqn]["external"] = cfg.external
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return path


def _service(tmp_path: Path) -> DeepLineageService:
    (tmp_path / "cfg").mkdir(exist_ok=True)
    return DeepLineageService(config_store=ConfigStore(config_dir=tmp_path / "cfg"))


def _show(tmp_path: Path, graph: LineageGraph, *args: str, json_mode: bool = False):
    cache = _cache(tmp_path, graph)
    argv = (["--json"] if json_mode else []) + ["lineage", "show", "-l", str(cache), *args]
    return runner.invoke(app, argv)


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _line_of(lines: list[str], needle: str, start: int = 0) -> int:
    for index in range(start, len(lines)):
        if needle in lines[index]:
            return index
    raise AssertionError(f"{needle!r} not found in:\n" + "\n".join(lines))


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


# ---------------------------------------------------------------------------
# B1: traversal
# ---------------------------------------------------------------------------


class TestWalk:
    def test_unequal_length_diamond_keeps_descendants(self) -> None:
        """R->A->X->C and R->X->C->D at depth 3 must still include C->D."""
        graph = LineageGraph()
        for source, target in (("R", "A"), ("A", "X"), ("R", "X"), ("X", "C"), ("C", "D")):
            graph.add_edge(Edge(source, target, "table", "table", "writes", "test"))

        edges = graph.get_downstream("R", depth=3)

        pairs = {(e["source"], e["target"]): e["depth"] for e in edges}
        assert pairs[("C", "D")] == 3
        assert pairs[("X", "C")] == 2
        assert pairs[("R", "X")] == 1
        assert len(edges) == len(pairs)  # deduplicated

    def test_upstream_mirrors(self) -> None:
        graph = LineageGraph()
        for source, target in (("A", "R"), ("X", "A"), ("X", "R"), ("C", "X"), ("D", "C")):
            graph.add_edge(Edge(source, target, "table", "table", "writes", "test"))

        edges = graph.get_upstream("R", depth=3)

        assert ("D", "C") in {(e["source"], e["target"]) for e in edges}


# ---------------------------------------------------------------------------
# B2/B3/B4: text tree
# ---------------------------------------------------------------------------


class TestTextTree:
    def test_producers_nest_under_their_own_table(self, tmp_path: Path) -> None:
        result = _show(tmp_path, _issue_848_graph(), "--upstream", RECON, "--depth", "3")

        assert result.exit_code == 0, result.output
        lines = _plain(result.output).splitlines()
        grouprep_table = _line_of(lines, "OS_GROUPREP_RESULT")
        grouprep_cfg = _line_of(lines, "GroupRep Import")
        journal_table = _line_of(lines, "FT_FIN_JOURNAL_UK")
        journal_cfg = _line_of(lines, "Journal Entries UK")
        fdx = _line_of(lines, "fdx_export")

        # Each producer sits directly below (and deeper than) its own table.
        assert grouprep_cfg == grouprep_table + 1
        assert _indent(lines[grouprep_cfg]) > _indent(lines[grouprep_table])
        assert journal_cfg == journal_table + 1
        assert _indent(lines[journal_cfg]) > _indent(lines[journal_table])
        # fdx_export has no producer: nothing is nested under it.
        if fdx + 1 < len(lines) and lines[fdx + 1].strip():
            assert _indent(lines[fdx + 1]) <= _indent(lines[fdx])

    def test_repeated_node_prints_see_above(self, tmp_path: Path) -> None:
        graph = _issue_848_graph()
        # A second consumer of GROUPREP that also feeds RECON_CFG.
        other_cfg = "p:keboola.snowflake-transformation/other"
        other_table = "p:out.c-other.OTHER"
        _config(graph, other_cfg, "Other")
        _table(graph, other_table)
        _reads(graph, GROUPREP, other_cfg)
        _writes(graph, other_cfg, other_table)
        _reads(graph, other_table, RECON_CFG)

        result = _show(tmp_path, graph, "--upstream", RECON, "--depth", "6")

        output = _plain(result.output)
        assert result.exit_code == 0, output
        grouprep_lines = [ln for ln in output.splitlines() if "OS_GROUPREP_RESULT" in ln]
        assert len(grouprep_lines) == 2
        assert "(see above)" not in grouprep_lines[0]
        assert "(see above)" in grouprep_lines[1]

    def test_node_type_prefix_is_escaped(self, tmp_path: Path) -> None:
        result = _show(tmp_path, _issue_848_graph(), "--upstream", RECON)

        output = _plain(result.output)
        assert "[table] " in output
        assert "[transformation] " in output

    def test_edges_labelled_by_meaning_with_ai_badge(self, tmp_path: Path) -> None:
        graph = _issue_848_graph()
        shared = "q:in.c-shared.RECON_MATCHED"
        _table(graph, shared)
        _share(graph, RECON, shared)

        up = _plain(_show(tmp_path, graph, "--upstream", RECON).output)
        down = _plain(_show(tmp_path, graph, "--downstream", GROUPREP).output)

        assert "<- writes" in up
        assert "<- reads [AI]" in up
        assert "(output_mapping)" not in up
        assert "(ai)" not in up
        assert "-> shares" in _plain(_show(tmp_path, graph, "--downstream", RECON).output)
        assert "-> reads [AI]" in down

    def test_external_annotations_printed_for_config_without_edges(self, tmp_path: Path) -> None:
        # Drives the renderer directly: loading `external` from a cache file is
        # the cache loader's job, not the tree's.
        from keboola_agent_cli.commands._lineage_tree import print_lineage_tree
        from keboola_agent_cli.output import OutputFormatter

        graph = LineageGraph()
        cfg = "p:kds-team.app-custom-python/slack"
        _config(graph, cfg, "Slack notifier", external=[{"system": "Slack", "op": "write"}])
        result = _service(tmp_path).query_downstream(graph, cfg)
        formatter = OutputFormatter(no_color=True)
        buffer = io.StringIO()
        formatter.console = Console(file=buffer, width=200, no_color=True)

        print_lineage_tree(formatter, graph, result, "downstream")

        output = buffer.getvalue()
        assert "external: Slack (write)" in output
        assert "(none found)" not in output


# ---------------------------------------------------------------------------
# B5: column trace
# ---------------------------------------------------------------------------

SRC = "p:in.c-src.customers"
MID_CFG = "p:keboola.snowflake-transformation/clean"
MID = "p:out.c-clean.customers"
END_CFG = "p:keboola.snowflake-transformation/report"
END = "p:out.c-report.summary"


def _column_chain() -> LineageGraph:
    """SRC.cust_name -> clean.NAME -> summary.CUSTOMER (renamed twice)."""
    graph = LineageGraph()
    _table(graph, SRC, ["id", "cust_name"])
    _table(graph, MID, ["ID", "NAME"])
    _table(graph, END, ["CUSTOMER", "TOTAL"])
    _config(graph, MID_CFG, "Clean")
    _config(graph, END_CFG, "Report")
    _reads(graph, SRC, MID_CFG, mapping={"NAME": "in.c-src.customers.cust_name"})
    _writes(graph, MID_CFG, MID)
    _reads(graph, MID, END_CFG, mapping={"CUSTOMER": "out.c-clean.customers.NAME"})
    _writes(graph, END_CFG, END)
    return graph


class TestColumnTrace:
    def test_upstream_follows_renamed_columns(self) -> None:
        trace = trace_column(_column_chain(), END, "CUSTOMER", "upstream", 10)

        hops = {
            (e["source"], e["source_column"], e["target"], e["target_column"])
            for e in trace["edges"]
        }
        assert (END_CFG, "CUSTOMER", END, "CUSTOMER") in hops
        assert (MID, "NAME", END_CFG, "CUSTOMER") in hops
        assert (MID_CFG, "NAME", MID, "NAME") in hops
        assert (SRC, "cust_name", MID_CFG, "NAME") in hops
        assert trace["terminals"] == []

    def test_downstream_follows_renamed_columns(self) -> None:
        trace = trace_column(_column_chain(), SRC, "cust_name", "downstream", 10)

        targets = {(e["target"], e["target_column"]) for e in trace["edges"]}
        assert (END, "CUSTOMER") in targets
        assert (MID, "NAME") in targets

    def test_depth_counts_graph_edges(self) -> None:
        trace = trace_column(_column_chain(), END, "CUSTOMER", "upstream", 2)

        assert {e["depth"] for e in trace["edges"]} == {1, 2}
        assert MID in {e["source"] for e in trace["edges"]}
        assert MID_CFG not in {e["source"] for e in trace["edges"]}

    def test_unknown_mapping_stops_with_terminal(self) -> None:
        graph = _column_chain()
        graph.edges[2].column_mapping = {}  # MID -> END_CFG loses its mapping

        trace = trace_column(graph, END, "CUSTOMER", "upstream", 10)

        assert {"node": END_CFG, "column": "CUSTOMER", "reason": "column_mapping_unknown"} in [
            {k: t[k] for k in ("node", "column", "reason")} for t in trace["terminals"]
        ]
        # The edge to the terminal config is kept, its inputs are not expanded.
        assert (END_CFG, END) in {(e["source"], e["target"]) for e in trace["edges"]}
        assert MID not in {e["source"] for e in trace["edges"]}

    def test_ambiguous_output_table_stops(self) -> None:
        graph = _column_chain()
        twin = "p:out.c-report.summary_copy"
        _table(graph, twin, ["CUSTOMER"])
        _writes(graph, END_CFG, twin)

        trace = trace_column(graph, END, "CUSTOMER", "upstream", 10)

        reasons = [t["reason"] for t in trace["terminals"]]
        assert reasons == ["ambiguous_output_table"]
        assert MID not in {e["source"] for e in trace["edges"]}

    def test_share_edges_preserve_column(self) -> None:
        graph = _column_chain()
        linked = "q:out.c-report.summary"
        _table(graph, linked, ["CUSTOMER"])
        _share(graph, END, linked)

        trace = trace_column(graph, linked, "CUSTOMER", "upstream", 10)

        assert (SRC, "cust_name") in {(e["source"], e["source_column"]) for e in trace["edges"]}

    def test_json_output_carries_terminals(self, tmp_path: Path) -> None:
        graph = _column_chain()
        graph.edges[2].column_mapping = {}

        result = _show(tmp_path, graph, "--upstream", END, "--column", "CUSTOMER", json_mode=True)

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)["data"]
        assert data["column"] == "CUSTOMER"
        assert "column_filter" not in data
        assert data["terminals"][0]["reason"] == "column_mapping_unknown"
        assert data["terminals"][0]["node"] == END_CFG

    def test_text_output_marks_unknown_point(self, tmp_path: Path) -> None:
        graph = _column_chain()
        graph.edges[2].column_mapping = {}

        result = _show(tmp_path, graph, "--upstream", END, "--column", "CUSTOMER")

        output = _plain(result.output)
        assert result.exit_code == 0, output
        assert "column: CUSTOMER" in output
        assert "column mapping unknown beyond this point" in output

    def test_config_focus_is_usage_error(self, tmp_path: Path) -> None:
        result = _show(
            tmp_path, _column_chain(), "--upstream", END_CFG, "--column", "CUSTOMER", json_mode=True
        )

        assert result.exit_code == 2, result.output

    @pytest.mark.parametrize("fmt", ["html", "mermaid", "er"])
    def test_column_with_diagram_format_is_usage_error(self, tmp_path: Path, fmt: str) -> None:
        result = _show(
            tmp_path, _column_chain(), "--upstream", END, "--column", "CUSTOMER", "--format", fmt
        )

        assert result.exit_code == 2, result.output


# ---------------------------------------------------------------------------
# B6/B7: mermaid
# ---------------------------------------------------------------------------


class TestMermaid:
    def test_always_left_to_right_with_focus_class(self) -> None:
        graph = _issue_848_graph()
        edges = graph.get_upstream(RECON, 3)

        code = DeepLineageService.render_mermaid(edges, graph, "upstream", RECON)

        assert code.splitlines()[0] == "graph LR"
        focus_id = DeepLineageService._sanitize_mermaid_id(RECON)
        assert f"class {focus_id} focus" in code
        assert "classDef focus" in code

    def test_ai_edges_dashed_with_badge(self) -> None:
        graph = _issue_848_graph()
        code = DeepLineageService.render_mermaid(
            graph.get_upstream(RECON, 3), graph, "upstream", RECON
        )

        assert "-.->|reads · AI|" in code
        assert "-->|writes|" in code
        assert "|ai|" not in code and "output_mapping" not in code

    def test_external_annotations_become_nodes(self) -> None:
        graph = LineageGraph()
        cfg = "p:kds-team.app-custom-python/slack"
        _config(
            graph,
            cfg,
            "Slack notifier",
            external=[{"system": "Slack", "op": "write"}, {"system": "Gmail", "op": "read"}],
        )

        code = DeepLineageService.render_mermaid([], graph, "downstream", cfg)

        cfg_id = DeepLineageService._sanitize_mermaid_id(cfg)
        ext_ids = re.findall(r"^\s*(\w+)\(\[\"(Slack|Gmail)\"\]\):::ext", code, re.MULTILINE)
        ids = {system: node_id for node_id, system in ext_ids}
        assert set(ids) == {"Slack", "Gmail"}
        assert f"{cfg_id} -.->|writes| {ids['Slack']}" in code
        assert f"{ids['Gmail']} -.->|reads| {cfg_id}" in code
        # External nodes must not be clickable: their ids carry no node FQN.
        assert all(cfg_id not in node_id for node_id in ids.values())

    def test_labels_name_then_bucket_without_project_when_single(self) -> None:
        graph = _issue_848_graph()
        code = DeepLineageService.render_mermaid(
            graph.get_upstream(RECON, 1), graph, "upstream", RECON
        )

        assert '"RECON_MATCHED<br/>out.c-recon' in code
        assert '"Reconciliation<br/>keboola.snowflake-transformation"' in code
        assert '"p: ' not in code and "p:RECON" not in code  # no alias prefix

    def test_project_prefix_when_multi_project(self) -> None:
        graph = _issue_848_graph()
        shared = "q:in.c-shared.RECON_MATCHED"
        _table(graph, shared)
        _share(graph, RECON, shared)

        code = DeepLineageService.render_mermaid(
            graph.get_downstream(RECON, 1), graph, "downstream", RECON
        )

        assert '"p: RECON_MATCHED<br/>' in code
        assert '"q: RECON_MATCHED<br/>' in code


# ---------------------------------------------------------------------------
# B8: default depth
# ---------------------------------------------------------------------------


def _long_chain(length: int) -> LineageGraph:
    graph = LineageGraph()
    for index in range(length):
        graph.add_edge(
            Edge(f"p:in.c-b.t{index}", f"p:in.c-b.t{index + 1}", "table", "table", "writes", "test")
        )
    return graph


class TestDefaultDepth:
    def test_html_defaults_to_depth_3(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = _show(tmp_path, _long_chain(6), "--downstream", "p:in.c-b.t0", "--format", "html")

        assert result.exit_code == 0, result.output
        page = next(tmp_path.glob("lineage_downstream_*.html")).read_text()
        assert "p_in_c_b_t3" in page
        assert "p_in_c_b_t4" not in page

    def test_explicit_depth_wins_for_html(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = _show(
            tmp_path,
            _long_chain(6),
            "--downstream",
            "p:in.c-b.t0",
            "--format",
            "html",
            "--depth",
            "5",
        )

        assert result.exit_code == 0, result.output
        page = next(tmp_path.glob("lineage_downstream_*.html")).read_text()
        assert "p_in_c_b_t5" in page

    def test_json_html_keeps_depth_10(self, tmp_path: Path) -> None:
        result = _show(
            tmp_path,
            _long_chain(12),
            "--downstream",
            "p:in.c-b.t0",
            "--format",
            "html",
            json_mode=True,
        )

        data = json.loads(result.stdout)["data"]
        assert max(e["depth"] for e in data["edges"]) == 10

    def test_mermaid_keeps_depth_10(self, tmp_path: Path) -> None:
        result = _show(
            tmp_path, _long_chain(12), "--downstream", "p:in.c-b.t0", "--format", "mermaid"
        )

        assert "p_in_c_b_t10" in result.stdout
        assert "p_in_c_b_t11" not in result.stdout


# ---------------------------------------------------------------------------
# B9: both directions, rendered once
# ---------------------------------------------------------------------------


class TestBothDirections:
    def test_query_both_unions_edges(self, tmp_path: Path) -> None:
        graph = _column_chain()
        result = _service(tmp_path).query(graph, MID, "", 10, direction="both")

        pairs = [(e["source"], e["target"]) for e in result["edges"]]
        assert result["direction"] == "both"
        assert (MID_CFG, MID) in pairs and (MID, END_CFG) in pairs
        assert len(pairs) == len(set(pairs))

    def test_flowchart_rendered_once(self, tmp_path: Path) -> None:
        code = _service(tmp_path).diagram(_column_chain(), MID, "both", 10)

        assert code.count("graph LR") == 1
        assert DeepLineageService._sanitize_mermaid_id(SRC) in code
        assert DeepLineageService._sanitize_mermaid_id(END) in code

    def test_er_braces_balanced(self, tmp_path: Path) -> None:
        code = _service(tmp_path).diagram(_column_chain(), MID, "both", 10, view="er")

        lines = [ln.strip() for ln in code.splitlines()]
        opened = sum(1 for ln in lines if ln.endswith("{") and not ln.startswith('"⚠'))
        closed = sum(1 for ln in lines if ln == "}")
        assert opened == closed == 3
        assert code.count("erDiagram") == 1

    def test_external_only_config_renders(self, tmp_path: Path) -> None:
        graph = LineageGraph()
        cfg = "p:kds-team.app-custom-python/slack"
        _config(graph, cfg, "Slack notifier", external=[{"system": "Slack", "op": "write"}])

        result = _service(tmp_path).query(graph, cfg, "", 3, direction="both")
        code = _service(tmp_path).diagram(graph, cfg, "both", 3)

        assert result["node_info"]["external"] == [{"system": "Slack", "op": "write"}]
        assert "Slack" in code

    def test_fastapi_walk_and_mermaid_accept_both(self, tmp_path: Path) -> None:
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from keboola_agent_cli.server import create_app

        cache = _cache(tmp_path, _column_chain())
        (tmp_path / "srv").mkdir()
        client = TestClient(create_app(config_dir=str(tmp_path / "srv"), auth_token="t"))
        headers = {"Authorization": "Bearer t"}
        params = {"load": str(cache), "node": MID, "direction": "both", "depth": 3}

        walk = client.get("/lineage/walk", params=params, headers=headers)
        mermaid = client.get("/lineage/mermaid", params={**params, "view": "er"}, headers=headers)

        assert walk.status_code == 200, walk.text
        assert walk.json()["direction"] == "both"
        assert mermaid.status_code == 200, mermaid.text
        assert mermaid.text.count("erDiagram") == 1

    def test_browser_template_no_longer_merges_in_js(self) -> None:
        from keboola_agent_cli.commands.lineage import _LINEAGE_HTML_TEMPLATE

        assert "seenER" not in _LINEAGE_HTML_TEMPLATE
        assert "direction=upstream&depth" not in _LINEAGE_HTML_TEMPLATE


class TestHtmlCharset:
    """Standalone HTML declares UTF-8: labels carry non-ASCII (`·`, `⚠`, names)."""

    def test_render_html_declares_utf8_before_title(self) -> None:
        from keboola_agent_cli.services._lineage_mermaid import render_html

        html = render_html("graph LR\n  a -.->|reads · AI| b", "t")
        head = html.split("<title>", 1)[0]
        assert '<meta charset="utf-8">' in head

    def test_server_download_html_declares_utf8(self) -> None:
        from keboola_agent_cli.commands import lineage as lineage_cmd

        source = Path(lineage_cmd.__file__).read_text(encoding="utf-8")
        builder = source.split("function buildStandaloneHtml", 1)[1].split("<title>", 1)[0]
        assert '<meta charset="utf-8">' in builder


# ---------------------------------------------------------------------------
# PR #849 review fixes
# ---------------------------------------------------------------------------

T_SRC = "p:in.c-t.t"
A_TBL = "p:out.c-a.a"
B_TBL = "p:out.c-b.b"
FINAL = "p:out.c-f.final"
C1 = "p:keboola.snowflake-transformation/c1"
C2 = "p:keboola.snowflake-transformation/c2"
C3 = "p:keboola.snowflake-transformation/c3"


def _diamond(external: list[dict] | None = None) -> LineageGraph:
    """FINAL <- C3 <- {A <- C1 <- T, B <- C2 <- T}; C1 and C2 map T differently."""
    graph = LineageGraph()
    for fqn in (T_SRC, A_TBL, B_TBL, FINAL):
        _table(graph, fqn, ["x", "y"])
    _config(graph, C1, "One", external=external)
    _config(graph, C2, "Two")
    _config(graph, C3, "Three")
    _reads(graph, T_SRC, C1, mapping={"XX": "in.c-t.t.x"})
    _reads(graph, T_SRC, C2, mapping={"YY": "in.c-t.t.y"})
    _writes(graph, C1, A_TBL)
    _writes(graph, C2, B_TBL)
    _reads(graph, A_TBL, C3)
    _reads(graph, B_TBL, C3)
    _writes(graph, C3, FINAL)
    return graph


class TestReviewFixes:
    def test_union_merges_columns_of_parallel_input_mappings(self, tmp_path: Path) -> None:
        graph = LineageGraph()
        _table(graph, SRC, ["id", "a", "b"])
        _config(graph, MID_CFG, "Clean")
        _reads(graph, SRC, MID_CFG, columns=["id", "a"], mapping={"A": "in.c-src.customers.a"})
        _reads(graph, SRC, MID_CFG, columns=["id", "b"], mapping={"B": "in.c-src.customers.b"})

        result = _service(tmp_path).query(graph, MID_CFG, "", 3, direction="upstream")

        assert len(result["edges"]) == 1
        edge = result["edges"][0]
        assert edge["columns"] == ["id", "a", "b"]
        assert edge["column_mapping"] == {
            "A": "in.c-src.customers.a",
            "B": "in.c-src.customers.b",
        }
        # The graph itself is not mutated by the merge.
        assert graph.edges[0].columns == ["id", "a"]
        assert graph.edges[0].column_mapping == {"A": "in.c-src.customers.a"}

    def test_columns_printed_per_edge_in_diamond(self, tmp_path: Path) -> None:
        result = _show(tmp_path, _diamond(), "--upstream", FINAL, "--depth", "6", "--columns")

        output = _plain(result.output)
        assert result.exit_code == 0, output
        lines = output.splitlines()
        t_lines = [i for i, ln in enumerate(lines) if "in.c-t.t " in ln]
        assert len(t_lines) == 2
        found = []
        for index in t_lines:
            detail = lines[index + 1]
            assert ("XX <- " in detail) != ("YY <- " in detail), detail
            found.append("XX" if "XX <- " in detail else "YY")
        assert sorted(found) == ["XX", "YY"]

    def test_external_printed_under_non_focus_config(self, tmp_path: Path) -> None:
        from keboola_agent_cli.commands._lineage_tree import print_lineage_tree
        from keboola_agent_cli.output import OutputFormatter

        graph = _diamond(external=[{"system": "Slack", "op": "write"}])
        result = _service(tmp_path).query_upstream(graph, FINAL)
        formatter = OutputFormatter(no_color=True)
        buffer = io.StringIO()
        formatter.console = Console(file=buffer, width=200, no_color=True)

        print_lineage_tree(formatter, graph, result, "upstream", depth=6)

        lines = buffer.getvalue().splitlines()
        cfg_line = _line_of(lines, "One")
        ext_line = _line_of(lines, "external: Slack (write)")
        assert ext_line == cfg_line + 1
        assert _indent(lines[ext_line]) > _indent(lines[cfg_line])
        assert sum("external: Slack" in ln for ln in lines) == 1

    def test_er_always_lists_columns(self, tmp_path: Path) -> None:
        code = _service(tmp_path).diagram(_column_chain(), MID, "both", 10, view="er")

        assert "string cust_name" in code
        assert "rows," not in code
        cli = _show(tmp_path, _column_chain(), "--downstream", SRC, "--format", "er")
        assert cli.exit_code == 0, cli.output
        assert "string cust_name" in cli.output

    def test_er_renders_external_systems(self, tmp_path: Path) -> None:
        graph = _diamond(external=[{"system": "Slack", "op": "write"}])
        code = _service(tmp_path).diagram(graph, C1, "both", 3, view="er")

        assert '"ext:Slack"' in code
        rels = [ln for ln in code.splitlines() if '"ext:Slack"' in ln and " : " in ln]
        assert any("p:t" in ln for ln in rels), code
        assert any("p:a" in ln for ln in rels), code

    def test_er_external_only_config_is_not_empty(self, tmp_path: Path) -> None:
        graph = LineageGraph()
        cfg = "p:kds-team.app-custom-python/slack"
        _config(graph, cfg, "Slack notifier", external=[{"system": "Slack", "op": "write"}])

        code = _service(tmp_path).diagram(graph, cfg, "both", 3, view="er")

        assert code.strip() != "erDiagram"
        assert '"Slack notifier"' in code and '"ext:Slack"' in code

    def test_split_table_id_returns_named_parts(self) -> None:
        from keboola_agent_cli.services._lineage_mermaid import _split_table_id

        parts = _split_table_id("out.c-b.t")
        assert (parts.bucket, parts.name) == ("out.c-b", "t")
        assert _split_table_id("t").bucket == ""

    def test_html_with_columns_gets_its_own_filename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        graph = _column_chain()
        plain = _show(tmp_path, graph, "--downstream", SRC, "--format", "html")
        with_cols = _show(tmp_path, graph, "--downstream", SRC, "--format", "html", "--columns")

        assert plain.exit_code == 0 and with_cols.exit_code == 0
        names = sorted(p.name for p in tmp_path.glob("lineage_*.html"))
        assert len(names) == 2
        assert names[0].endswith("_columns.html") or names[1].endswith("_columns.html")
        assert sum(n.endswith("_columns.html") for n in names) == 1
