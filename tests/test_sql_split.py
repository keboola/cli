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


_ISSUE_954_SCRIPT = """DECLARE d DATE DEFAULT CURRENT_DATE();
IF d > '2026-01-01' THEN
  CREATE TEMP TABLE t AS SELECT 1 AS x;
  SELECT * FROM t;
END IF;
BEGIN
  INSERT INTO `out` SELECT 1;
EXCEPTION WHEN ERROR THEN
  SELECT @@error.message;
END;"""


class TestProceduralBlocks:
    """A procedural block is one statement: its inner semicolons do not split it (#954)."""

    def test_issue_954_script_keeps_its_blocks(self) -> None:
        assert split_statements(_ISSUE_954_SCRIPT) == [
            "DECLARE d DATE DEFAULT CURRENT_DATE();",
            (
                "IF d > '2026-01-01' THEN\n  CREATE TEMP TABLE t AS SELECT 1 AS x;\n"
                "  SELECT * FROM t;\nEND IF;"
            ),
            (
                "BEGIN\n  INSERT INTO `out` SELECT 1;\nEXCEPTION WHEN ERROR THEN\n"
                "  SELECT @@error.message;\nEND;"
            ),
        ]

    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            (
                (
                    "IF a THEN IF b THEN SELECT 1; ELSE SELECT 2; END IF; "
                    "ELSEIF c THEN SELECT 3; END IF; SELECT 4;"
                ),
                [
                    (
                        "IF a THEN IF b THEN SELECT 1; ELSE SELECT 2; END IF; "
                        "ELSEIF c THEN SELECT 3; END IF;"
                    ),
                    "SELECT 4;",
                ],
            ),
            (
                "WHILE i < 3 DO SET i = i + 1; END WHILE; SELECT 1;",
                ["WHILE i < 3 DO SET i = i + 1; END WHILE;", "SELECT 1;"],
            ),
            (
                "LOOP SET i = i - 1; IF i = 0 THEN LEAVE; END IF; END LOOP; SELECT 1;",
                ["LOOP SET i = i - 1; IF i = 0 THEN LEAVE; END IF; END LOOP;", "SELECT 1;"],
            ),
            (
                "REPEAT SET i = i + 1; UNTIL i > 3 END REPEAT; SELECT 1;",
                ["REPEAT SET i = i + 1; UNTIL i > 3 END REPEAT;", "SELECT 1;"],
            ),
            (
                "FOR r IN (SELECT 1 AS n) DO SELECT r.n; END FOR; SELECT 9;",
                ["FOR r IN (SELECT 1 AS n) DO SELECT r.n; END FOR;", "SELECT 9;"],
            ),
            (
                "CASE x WHEN 1 THEN SELECT 1; ELSE SELECT 2; END CASE; SELECT 3;",
                ["CASE x WHEN 1 THEN SELECT 1; ELSE SELECT 2; END CASE;", "SELECT 3;"],
            ),
            (
                "lbl: BEGIN SELECT 1; SELECT 2; END lbl; SELECT 3;",
                ["lbl: BEGIN SELECT 1; SELECT 2; END lbl;", "SELECT 3;"],
            ),
            (
                "BEGIN\n  CREATE TABLE t (a INT);\n  INSERT INTO t VALUES (1);\nEND;\nSELECT 1;",
                [
                    "BEGIN\n  CREATE TABLE t (a INT);\n  INSERT INTO t VALUES (1);\nEND;",
                    "SELECT 1;",
                ],
            ),
        ],
        ids=["nested-if", "while", "loop", "repeat", "for", "case-statement", "label", "snowflake"],
    )
    def test_block_is_one_statement(self, sql: str, expected: list[str]) -> None:
        assert split_statements(sql) == expected

    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            (
                "WHILE (i < 3) LOOP\n  SET i = i + 1;\nEND LOOP;\nSELECT 1;",
                ["WHILE (i < 3) LOOP\n  SET i = i + 1;\nEND LOOP;", "SELECT 1;"],
            ),
            (
                "FOR i IN 1 TO 3 LOOP\n  SELECT i;\nEND LOOP;\nSELECT 1;",
                ["FOR i IN 1 TO 3 LOOP\n  SELECT i;\nEND LOOP;", "SELECT 1;"],
            ),
            (
                "CREATE PROCEDURE d.p()\nBEGIN\n  SELECT 1;\n  SELECT 2;\nEND;\nCALL d.p();",
                ["CREATE PROCEDURE d.p()\nBEGIN\n  SELECT 1;\n  SELECT 2;\nEND;", "CALL d.p();"],
            ),
            (
                (
                    "CREATE OR REPLACE PROCEDURE p() RETURNS INT LANGUAGE SQL AS BEGIN SELECT 1; "
                    "RETURN 1; END; SELECT 2;"
                ),
                [
                    (
                        "CREATE OR REPLACE PROCEDURE p() RETURNS INT LANGUAGE SQL AS BEGIN SELECT 1; "
                        "RETURN 1; END;"
                    ),
                    "SELECT 2;",
                ],
            ),
        ],
        ids=[
            "snowflake-while-loop",
            "snowflake-for-loop",
            "bigquery-procedure",
            "snowflake-procedure",
        ],
    )
    def test_routine_and_loop_forms_are_one_statement(self, sql: str, expected: list[str]) -> None:
        assert split_statements(sql) == expected

    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            (
                "BEGIN READ WRITE; INSERT INTO t VALUES (1); END; SELECT 2;",
                ["BEGIN READ WRITE;", "INSERT INTO t VALUES (1);", "END;", "SELECT 2;"],
            ),
            (
                "BEGIN IMMEDIATE; SELECT 1; END; SELECT 2;",
                ["BEGIN IMMEDIATE;", "SELECT 1;", "END;", "SELECT 2;"],
            ),
            (
                "CREATE TEMP FUNCTION f(x INT64) AS (x + 1); SELECT f(1);",
                ["CREATE TEMP FUNCTION f(x INT64) AS (x + 1);", "SELECT f(1);"],
            ),
            (
                "BEGIN TRANSACTION; INSERT INTO t VALUES (1); COMMIT TRANSACTION; BEGIN; SELECT 1;",
                [
                    "BEGIN TRANSACTION;",
                    "INSERT INTO t VALUES (1);",
                    "COMMIT TRANSACTION;",
                    "BEGIN;",
                    "SELECT 1;",
                ],
            ),
            (
                "SELECT CASE WHEN a THEN 1 END; SELECT IF(a, 1, 2); SELECT 3;",
                ["SELECT CASE WHEN a THEN 1 END;", "SELECT IF(a, 1, 2);", "SELECT 3;"],
            ),
            (
                "IF @x = 1 SELECT CASE WHEN a THEN b END; SELECT 2;",
                ["IF @x = 1 SELECT CASE WHEN a THEN b END;", "SELECT 2;"],
            ),
            (
                "WHILE @i < 10 BEGIN SET @i = @i + 1; END; SELECT 1;",
                ["WHILE @i < 10 BEGIN SET @i = @i + 1;", "END;", "SELECT 1;"],
            ),
            (
                "SELECT 'END IF; BEGIN'; /* IF x THEN */ SELECT 2; -- BEGIN\nSELECT 3;",
                ["SELECT 'END IF; BEGIN';", "/* IF x THEN */ SELECT 2;", "-- BEGIN\nSELECT 3;"],
            ),
        ],
        ids=[
            "begin-read-write",
            "begin-immediate",
            "function-without-begin",
            "transactions",
            "expressions",
            "tsql-if",
            "tsql-while",
            "keywords-in-text",
        ],
    )
    def test_statements_that_are_not_blocks_split_as_before(
        self, sql: str, expected: list[str]
    ) -> None:
        assert split_statements(sql) == expected

    def test_a_block_after_an_opener_that_never_closes_is_split_too(self) -> None:
        """The pass gives up on the rest of the script, as before it existed."""
        assert split_statements("IF a THEN SELECT 1; BEGIN SELECT 2; SELECT 3; END;") == [
            "IF a THEN SELECT 1;",
            "BEGIN SELECT 2;",
            "SELECT 3;",
            "END;",
        ]

    def test_block_that_never_closes_splits_on_every_semicolon(self) -> None:
        """A false opener must not swallow the rest of the script."""
        assert split_statements("IF a THEN SELECT 1; SELECT 2;") == [
            "IF a THEN SELECT 1;",
            "SELECT 2;",
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
