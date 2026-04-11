"""Tests for the SQL statement splitter state machine.

Test cases ported from keboola-as-code (Go) sql_test.go and extended.
"""

import pytest

from keboola_agent_cli.sync.sql_split import join_statements, split_statements


class TestSplitStatements:
    """Tests for split_statements state machine."""

    def test_empty(self) -> None:
        assert split_statements("") == []

    def test_whitespace_only(self) -> None:
        assert split_statements("   \n\n\n  ") == []

    def test_one_statement(self) -> None:
        assert split_statements("SELECT * FROM bar") == ["SELECT * FROM bar"]

    def test_one_statement_with_semicolon(self) -> None:
        assert split_statements("SELECT * FROM bar;") == ["SELECT * FROM bar;"]

    def test_one_statement_whitespace_padding(self) -> None:
        assert split_statements("   \n\n\nSELECT * FROM bar\t\n  ") == ["SELECT * FROM bar"]

    def test_multiple_statements(self) -> None:
        sql = "SELECT 1;\nINSERT INTO bar VALUES('x', 'y');\nTRUNCATE records;"
        result = split_statements(sql)
        assert result == [
            "SELECT 1;",
            "INSERT INTO bar VALUES('x', 'y');",
            "TRUNCATE records;",
        ]

    def test_multiple_with_extra_whitespace(self) -> None:
        sql = "   \n\n\nSELECT * FROM [bar];\t\n  INSERT INTO bar VALUES('x', 'y'); TRUNCATE records;;;"
        result = split_statements(sql)
        assert result == [
            "SELECT * FROM [bar];",
            "INSERT INTO bar VALUES('x', 'y');",
            "TRUNCATE records;",
        ]

    def test_split_simple_queries(self) -> None:
        sql = "SELECT 1;\nSelect 2;\nSELECT 3;"
        result = split_statements(sql)
        assert result == ["SELECT 1;", "Select 2;", "SELECT 3;"]

    def test_block_comment(self) -> None:
        sql = "SELECT 1;\n/*\n  Select 2;\n*/\nSELECT 3;"
        result = split_statements(sql)
        assert result == ["SELECT 1;", "/*\n  Select 2;\n*/\nSELECT 3;"]

    def test_line_comment_dash(self) -> None:
        sql = "SELECT 1;\n-- Select 2;\nSELECT 3;"
        result = split_statements(sql)
        assert result == ["SELECT 1;", "-- Select 2;\nSELECT 3;"]

    def test_line_comment_hash(self) -> None:
        sql = "SELECT 1;\n# Select 2;\nSELECT 3;"
        result = split_statements(sql)
        assert result == ["SELECT 1;", "# Select 2;\nSELECT 3;"]

    def test_line_comment_double_slash(self) -> None:
        sql = "SELECT 1;\n// Select 2;\nSELECT 3;"
        result = split_statements(sql)
        assert result == ["SELECT 1;", "// Select 2;\nSELECT 3;"]

    def test_dollar_quoted_block(self) -> None:
        sql = "SELECT 1;\nexecute immediate $$\n  SELECT 2;\n  SELECT 3;\n$$;"
        result = split_statements(sql)
        assert result == [
            "SELECT 1;",
            "execute immediate $$\n  SELECT 2;\n  SELECT 3;\n$$;",
        ]

    def test_single_quoted_string_with_semicolon(self) -> None:
        sql = "SELECT 'hello; world';\nSELECT 2;"
        result = split_statements(sql)
        assert result == ["SELECT 'hello; world';", "SELECT 2;"]

    def test_double_quoted_identifier_with_semicolon(self) -> None:
        sql = 'SELECT "col;name" FROM t;\nSELECT 2;'
        result = split_statements(sql)
        assert result == ['SELECT "col;name" FROM t;', "SELECT 2;"]

    def test_escaped_quote_in_string(self) -> None:
        sql = "SELECT 'it\\'s a test; yes';\nSELECT 2;"
        result = split_statements(sql)
        assert result == ["SELECT 'it\\'s a test; yes';", "SELECT 2;"]

    def test_no_trailing_semicolon(self) -> None:
        sql = "SELECT 1;\nSELECT 2"
        result = split_statements(sql)
        assert result == ["SELECT 1;", "SELECT 2"]

    def test_multiline_create_table(self) -> None:
        sql = "CREATE TABLE foo AS\n    SELECT col1\n    FROM bar;\n\nINSERT INTO foo VALUES (1);"
        result = split_statements(sql)
        assert result == [
            "CREATE TABLE foo AS\n    SELECT col1\n    FROM bar;",
            "INSERT INTO foo VALUES (1);",
        ]


class TestJoinStatements:
    """Tests for join_statements."""

    def test_empty(self) -> None:
        assert join_statements([]) == ""

    def test_single(self) -> None:
        assert join_statements(["SELECT 1;"]) == "SELECT 1;"

    def test_multiple(self) -> None:
        result = join_statements(["SELECT 1;", "SELECT 2;", "SELECT 3;"])
        assert result == "SELECT 1;\n\nSELECT 2;\n\nSELECT 3;"

    def test_strips_trailing_whitespace(self) -> None:
        result = join_statements(["SELECT 1;  ", "SELECT 2;\n"])
        assert result == "SELECT 1;\n\nSELECT 2;"


class TestRoundTrip:
    """Test that split -> join -> split is idempotent."""

    @pytest.mark.parametrize(
        "statements",
        [
            ["SELECT 1;", "SELECT 2;", "SELECT 3;"],
            ["CREATE TABLE foo AS\n    SELECT col1\n    FROM bar;", "INSERT INTO foo VALUES (1);"],
            ["SELECT 'semicolon; inside';", "SELECT 2;"],
        ],
    )
    def test_split_join_roundtrip(self, statements: list[str]) -> None:
        joined = join_statements(statements)
        split_back = split_statements(joined)
        assert split_back == statements
