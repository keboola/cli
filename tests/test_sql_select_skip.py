"""The SELECT-skip rule of the Snowflake / BigQuery transformation runtime.

The Snowflake and BigQuery transformation runtimes skip a ``script[]``
element whose comment-free text, leading whitespace ignored, starts with
``SELECT``. kbagent mirrors that rule to warn before a write.
"""

import pytest

from keboola_agent_cli.sync.sql_select_skip import (
    SQL_SELECT_SKIPPED_CODE,
    SkippedSelect,
    is_skipped_by_runtime,
    skipped_select_statements,
    skipped_select_warnings,
)

SNOWFLAKE = "keboola.snowflake-transformation"
BIGQUERY = "keboola.google-bigquery-transformation"


def _blocks(*scripts: list[str] | str) -> list[dict]:
    return [
        {
            "name": "Block 1",
            "codes": [{"name": f"Code {i}", "script": s} for i, s in enumerate(scripts)],
        }
    ]


@pytest.mark.parametrize("component_id", [SNOWFLAKE, BIGQUERY])
@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM table_that_does_not_exist;",
        "select 1;",
        "Select\n1;",
        "SELECT*FROM t;",
        "  SELECT 1;",  # leading whitespace is ignored (keboola/snowflake-transformation#42)
        "\tSELECT 1;",
        "\nSELECT 1;",
        "\r\nSELECT 1;",
        "-- note\nSELECT 1;",
        "# note\nSELECT 1;",
        "/* explain */SELECT 1;",
        "/* x */ SELECT 1;",
        "/* a */ -- b\n  /* c */\nselect 1;",
    ],
)
def test_skipped_statements(component_id: str, statement: str) -> None:
    """The comment-free text, leading whitespace ignored, starts with SELECT."""
    assert is_skipped_by_runtime(component_id, statement) is True


@pytest.mark.parametrize("component_id", [SNOWFLAKE, BIGQUERY])
@pytest.mark.parametrize(
    "statement",
    [
        "WITH a AS (SELECT 1) SELECT * FROM a;",
        'CREATE TABLE "t" AS SELECT * FROM "src";',
        '  CREATE TABLE "t" AS SELECT 1;',
        'INSERT INTO "t" SELECT 1;',
        "// note\nSELECT 1;",  # SqlFormatter does not treat // as a comment
        "'SELECT';",
        "SELEC 1;",
        "/* never closed SELECT 1;",  # all comment: skipped as empty, not as a SELECT
        "",
    ],
)
def test_statements_that_run(component_id: str, statement: str) -> None:
    assert is_skipped_by_runtime(component_id, statement) is False


@pytest.mark.parametrize("statement", ["SELECTT * FROM t;", "SELECTED_ROWS;", "  select_x;"])
def test_select_must_be_a_whole_word_on_snowflake_only(statement: str) -> None:
    """Snowflake matches SELECT as a whole word; BigQuery compares 6 characters."""
    assert is_skipped_by_runtime(SNOWFLAKE, statement) is False
    assert is_skipped_by_runtime(BIGQUERY, statement) is True


def test_findings_name_block_code_and_index() -> None:
    blocks = _blocks(
        ['CREATE TABLE "a" AS SELECT 1;', "SELECT * FROM missing;"],
        ["select 2;"],
    )

    found = skipped_select_statements(SNOWFLAKE, blocks)

    assert found == [
        SkippedSelect(block="Block 1", code="Code 0", index=1, excerpt="SELECT * FROM missing;"),
        SkippedSelect(block="Block 1", code="Code 1", index=0, excerpt="select 2;"),
    ]


def test_bigquery_is_checked_and_string_script_is_one_statement() -> None:
    found = skipped_select_statements(BIGQUERY, _blocks("SELECT 1;"))
    assert [f.index for f in found] == [0]


@pytest.mark.parametrize(
    "component_id",
    ["keboola.synapse-transformation", "keboola.python-transformation-v2", "ex-generic-v2"],
)
def test_other_components_are_ignored(component_id: str) -> None:
    assert skipped_select_statements(component_id, _blocks(["SELECT 1;"])) == []


def test_malformed_blocks_are_ignored() -> None:
    assert skipped_select_statements(SNOWFLAKE, None) == []
    assert skipped_select_statements(SNOWFLAKE, ["x", {"codes": ["y", {"script": 5}]}]) == []


def test_excerpt_is_one_line_and_capped() -> None:
    statement = "SELECT\n" + ", ".join(f"col_{i}" for i in range(50)) + " FROM t;"
    (finding,) = skipped_select_statements(SNOWFLAKE, _blocks([statement]))
    assert "\n" not in finding.excerpt
    assert len(finding.excerpt) == 80
    assert finding.excerpt.endswith("...")


def test_warning_entries() -> None:
    configuration = {"parameters": {"blocks": _blocks(["SELECT 1;", "DELETE FROM t;"])}}

    (entry,) = skipped_select_warnings(SNOWFLAKE, configuration, config_id="123", path="")

    assert entry["code"] == SQL_SELECT_SKIPPED_CODE
    assert entry["component_id"] == SNOWFLAKE
    assert entry["config_id"] == "123"
    assert "path" not in entry  # empty identity fields are left out
    assert entry["statement"] == {
        "block": "Block 1",
        "code": "Code 0",
        "index": 0,
        "excerpt": "SELECT 1;",
    }
    assert f"{SNOWFLAKE}/123" in entry["message"]


def test_no_warnings_without_blocks() -> None:
    assert skipped_select_warnings(SNOWFLAKE, {"parameters": {}}) == []
    assert skipped_select_warnings(SNOWFLAKE, None) == []
