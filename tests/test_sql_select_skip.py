"""The SELECT-skip rule of the Snowflake / BigQuery transformation runtime.

``SnowflakeTransformation::executeQueries`` (and the BigQuery twin) skip a
``script[]`` element whose comment-free text starts with ``SELECT`` at its
first character. kbagent mirrors that rule to warn before a write.
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


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM table_that_does_not_exist;",
        "select 1;",
        "Select\n1;",
        "/* explain */SELECT 1;",
        "/* a *//* b */select 1;",
    ],
)
def test_skipped_statements(statement: str) -> None:
    """The comment-free text starts with SELECT at its first character."""
    assert is_skipped_by_runtime(statement) is True


@pytest.mark.parametrize(
    "statement",
    [
        "-- note\nSELECT 1;",  # the newline after a line comment stays
        "# note\nSELECT 1;",
        " SELECT 1;",  # leading whitespace: substr() sees the space
        "\nSELECT 1;",
        "/* x */ SELECT 1;",
        "WITH a AS (SELECT 1) SELECT * FROM a;",
        'CREATE TABLE "t" AS SELECT * FROM "src";',
        "'SELECT';",
        "SELEC 1;",
        "/* never closed SELECT 1;",  # all comment: skipped as empty, not as a SELECT
        "",
    ],
)
def test_statements_that_run(statement: str) -> None:
    assert is_skipped_by_runtime(statement) is False


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
