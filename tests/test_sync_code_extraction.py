"""Tests for sync code_extraction module -- SQL/Python code extraction and merging."""

import copy
from pathlib import Path
from typing import Any

import pytest

from keboola_agent_cli.sync.code_extraction import (
    SQL_STATEMENT_MARKER,
    canonical_sql_script,
    extract_code_files,
    merge_code_files,
)

# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------

SAMPLE_SQL_CONFIG: dict[str, Any] = {
    "version": 2,
    "name": "Clean Data",
    "description": "Cleans raw data",
    "parameters": {
        "blocks": [
            {
                "name": "Preparation",
                "codes": [
                    {
                        "name": "Create staging",
                        "script": ["CREATE TABLE staging AS SELECT * FROM raw;"],
                    },
                    {
                        "name": "Clean nulls",
                        "script": ["DELETE FROM staging WHERE id IS NULL;"],
                    },
                ],
            },
            {
                "name": "Output",
                "codes": [
                    {
                        "name": "Final select",
                        "script": ["SELECT * FROM staging;"],
                    },
                ],
            },
        ],
    },
    "_keboola": {
        "component_id": "keboola.snowflake-transformation",
        "config_id": "cfg-100",
    },
}

SAMPLE_PYTHON_TRANSFORM_CONFIG: dict[str, Any] = {
    "version": 2,
    "name": "Python Analysis",
    "description": "Runs Python analysis",
    "parameters": {
        "blocks": [
            {
                "name": "Analysis",
                "codes": [
                    {
                        "name": "Load data",
                        "script": ["import pandas as pd\ndf = pd.read_csv('in/tables/data.csv')"],
                    },
                    {
                        "name": "Transform",
                        "script": ["df['total'] = df['price'] * df['qty']"],
                    },
                ],
            },
        ],
        "packages": ["pandas==2.1.0", "numpy>=1.24"],
    },
    "_keboola": {
        "component_id": "keboola.python-transformation-v2",
        "config_id": "cfg-200",
    },
}

SAMPLE_PYTHON_APP_CONFIG: dict[str, Any] = {
    "version": 2,
    "name": "Custom Script",
    "description": "Custom Python app",
    "parameters": {
        "code": "import json\nresult = {'status': 'ok'}\nprint(json.dumps(result))\n",
        "packages": ["requests>=2.31", "beautifulsoup4"],
    },
    "_keboola": {
        "component_id": "kds-team.app-custom-python",
        "config_id": "cfg-300",
    },
}


# ===================================================================
# SQL Transformation Tests
# ===================================================================


class TestSqlExtraction:
    """Tests for SQL transformation code extraction and merging."""

    def test_extract_sql_blocks(self, tmp_path: Path) -> None:
        """Config with blocks produces transform.sql with markers and removes blocks from params."""
        config_data = copy.deepcopy(SAMPLE_SQL_CONFIG)
        config_dir = tmp_path / "sql-config"

        result = extract_code_files("keboola.snowflake-transformation", config_data, config_dir)

        # transform.sql should exist
        sql_file = config_dir / "transform.sql"
        assert sql_file.exists()

        content = sql_file.read_text(encoding="utf-8")
        assert "/* ===== BLOCK: Preparation ===== */" in content
        assert "/* ===== CODE: Create staging ===== */" in content
        assert "CREATE TABLE staging AS SELECT * FROM raw;" in content
        assert "/* ===== CODE: Clean nulls ===== */" in content
        assert "DELETE FROM staging WHERE id IS NULL;" in content
        assert "/* ===== BLOCK: Output ===== */" in content
        assert "/* ===== CODE: Final select ===== */" in content
        assert "SELECT * FROM staging;" in content

        # Blocks should be removed from parameters
        assert "blocks" not in result["parameters"]

    def test_merge_sql_blocks(self, tmp_path: Path) -> None:
        """transform.sql with markers is parsed back into blocks structure."""
        config_dir = tmp_path / "sql-config"
        config_dir.mkdir(parents=True)

        sql_content = (
            "/* ===== BLOCK: Preparation ===== */\n"
            "\n"
            "/* ===== CODE: Create staging ===== */\n"
            "CREATE TABLE staging AS SELECT * FROM raw;\n"
            "\n"
            "/* ===== BLOCK: Output ===== */\n"
            "\n"
            "/* ===== CODE: Final select ===== */\n"
            "SELECT * FROM staging;\n"
        )
        (config_dir / "transform.sql").write_text(sql_content, encoding="utf-8")

        config_data: dict = {"parameters": {}}
        result = merge_code_files("keboola.snowflake-transformation", config_data, config_dir)

        blocks = result["parameters"]["blocks"]
        assert len(blocks) == 2
        assert blocks[0]["name"] == "Preparation"
        assert len(blocks[0]["codes"]) == 1
        assert blocks[0]["codes"][0]["name"] == "Create staging"
        assert "CREATE TABLE staging" in blocks[0]["codes"][0]["script"][0]

        assert blocks[1]["name"] == "Output"
        assert blocks[1]["codes"][0]["name"] == "Final select"
        assert "SELECT * FROM staging" in blocks[1]["codes"][0]["script"][0]

    def test_sql_round_trip(self, tmp_path: Path) -> None:
        """Extract then merge produces equivalent blocks structure."""
        config_data = copy.deepcopy(SAMPLE_SQL_CONFIG)
        original_blocks = copy.deepcopy(config_data["parameters"]["blocks"])
        config_dir = tmp_path / "sql-roundtrip"

        # Extract: writes transform.sql, removes blocks
        extract_code_files("keboola.snowflake-transformation", config_data, config_dir)
        assert "blocks" not in config_data["parameters"]

        # Merge: reads transform.sql, restores blocks
        merge_code_files("keboola.snowflake-transformation", config_data, config_dir)
        restored_blocks = config_data["parameters"]["blocks"]

        # Compare block/code names and script content
        assert len(restored_blocks) == len(original_blocks)
        for orig_block, rest_block in zip(original_blocks, restored_blocks, strict=True):
            assert orig_block["name"] == rest_block["name"]
            assert len(orig_block["codes"]) == len(rest_block["codes"])
            for orig_code, rest_code in zip(orig_block["codes"], rest_block["codes"], strict=True):
                assert orig_code["name"] == rest_code["name"]
                # Script content should match (whitespace-stripped)
                orig_script = orig_code["script"][0].strip()
                rest_script = rest_code["script"][0].strip()
                assert orig_script == rest_script

    def test_no_blocks_no_file(self, tmp_path: Path) -> None:
        """Config without blocks produces no transform.sql file."""
        config_data = {
            "version": 2,
            "name": "Empty Transform",
            "parameters": {},
            "_keboola": {
                "component_id": "keboola.snowflake-transformation",
                "config_id": "cfg-empty",
            },
        }
        config_dir = tmp_path / "sql-empty"

        extract_code_files("keboola.snowflake-transformation", config_data, config_dir)

        assert not (config_dir / "transform.sql").exists()

    def test_sql_merge_without_file(self, tmp_path: Path) -> None:
        """Merging when transform.sql does not exist leaves config unchanged."""
        config_dir = tmp_path / "sql-nofile"
        config_dir.mkdir(parents=True)

        config_data: dict = {"parameters": {"other_key": "value"}}
        result = merge_code_files("keboola.snowflake-transformation", config_data, config_dir)

        assert "blocks" not in result["parameters"]
        assert result["parameters"]["other_key"] == "value"

    def test_sql_merge_no_markers(self, tmp_path: Path) -> None:
        """Plain SQL without markers is treated as a single block/code."""
        config_dir = tmp_path / "sql-plain"
        config_dir.mkdir(parents=True)

        (config_dir / "transform.sql").write_text("SELECT 1;\nSELECT 2;\n", encoding="utf-8")

        config_data: dict = {"parameters": {}}
        result = merge_code_files("keboola.snowflake-transformation", config_data, config_dir)

        blocks = result["parameters"]["blocks"]
        assert len(blocks) == 1
        assert blocks[0]["name"] == "Block 1"
        assert blocks[0]["codes"][0]["name"] == "Code 1"
        script = blocks[0]["codes"][0]["script"]
        # SQL splitter splits on semicolons -> 2 statements
        assert len(script) == 2
        assert script[0] == "SELECT 1;"
        assert script[1] == "SELECT 2;"

    def test_multiline_sql_produces_single_string(self, tmp_path: Path) -> None:
        """Multi-line SQL statement is joined into a single script element."""
        config_dir = tmp_path / "sql-multiline"
        config_dir.mkdir(parents=True)

        sql_content = (
            "/* ===== BLOCK: ETL ===== */\n"
            "\n"
            "/* ===== CODE: Create table ===== */\n"
            "CREATE TABLE foo AS\n"
            "    SELECT col1,\n"
            "           col2\n"
            "    FROM bar\n"
            "    WHERE active = true;\n"
        )
        (config_dir / "transform.sql").write_text(sql_content, encoding="utf-8")

        config_data: dict = {"parameters": {}}
        result = merge_code_files("keboola.snowflake-transformation", config_data, config_dir)

        blocks = result["parameters"]["blocks"]
        script = blocks[0]["codes"][0]["script"]
        # Must be exactly one element (not one per line)
        assert len(script) == 1
        # Must contain newlines (multi-line preserved)
        assert "\n" in script[0]
        # Must contain the full statement
        assert "CREATE TABLE foo AS" in script[0]
        assert "FROM bar" in script[0]
        assert "WHERE active = true;" in script[0]

    def test_multiline_sql_round_trip(self, tmp_path: Path) -> None:
        """Round-trip with multi-line statements preserves SQL integrity."""
        config_data = {
            "parameters": {
                "blocks": [
                    {
                        "name": "ETL",
                        "codes": [
                            {
                                "name": "Create",
                                "script": ["CREATE TABLE foo AS\n    SELECT col1\n    FROM bar;"],
                            },
                        ],
                    },
                ],
            },
        }
        config_dir = tmp_path / "sql-multiline-rt"

        extract_code_files("keboola.snowflake-transformation", config_data, config_dir)
        assert "blocks" not in config_data["parameters"]

        merge_code_files("keboola.snowflake-transformation", config_data, config_dir)

        script = config_data["parameters"]["blocks"][0]["codes"][0]["script"]
        assert len(script) == 1
        assert script[0] == "CREATE TABLE foo AS\n    SELECT col1\n    FROM bar;"

    def test_empty_code_block(self, tmp_path: Path) -> None:
        """Empty CODE block produces an empty script list."""
        config_dir = tmp_path / "sql-empty-code"
        config_dir.mkdir(parents=True)

        sql_content = (
            "/* ===== BLOCK: ETL ===== */\n"
            "\n"
            "/* ===== CODE: Empty ===== */\n"
            "\n"
            "/* ===== CODE: Has content ===== */\n"
            "SELECT 1;\n"
        )
        (config_dir / "transform.sql").write_text(sql_content, encoding="utf-8")

        config_data: dict = {"parameters": {}}
        result = merge_code_files("keboola.snowflake-transformation", config_data, config_dir)

        codes = result["parameters"]["blocks"][0]["codes"]
        assert len(codes) == 2
        assert codes[0]["name"] == "Empty"
        assert codes[0]["script"] == []
        assert codes[1]["name"] == "Has content"
        assert codes[1]["script"] == ["SELECT 1;"]

    def test_whitespace_only_code_block(self, tmp_path: Path) -> None:
        """Whitespace-only CODE block produces an empty script list."""
        config_dir = tmp_path / "sql-ws-code"
        config_dir.mkdir(parents=True)

        sql_content = "/* ===== BLOCK: ETL ===== */\n\n/* ===== CODE: Spaces ===== */\n   \n  \n\n"
        (config_dir / "transform.sql").write_text(sql_content, encoding="utf-8")

        config_data: dict = {"parameters": {}}
        result = merge_code_files("keboola.snowflake-transformation", config_data, config_dir)

        codes = result["parameters"]["blocks"][0]["codes"]
        assert codes[0]["name"] == "Spaces"
        assert codes[0]["script"] == []

    def test_sql_roundtrip_multi_script(self, tmp_path: Path) -> None:
        """Round-trip preserves multi-element script[] arrays (issue #119)."""
        config_data = {
            "parameters": {
                "blocks": [
                    {
                        "name": "Block 1",
                        "codes": [
                            {
                                "name": "multi-stmt",
                                "script": [
                                    "CREATE OR REPLACE TABLE a AS SELECT 1;",
                                    "INSERT INTO a VALUES (2);",
                                    "UPDATE a SET x = 3 WHERE x = 2;",
                                ],
                            }
                        ],
                    }
                ],
            },
        }
        original_scripts = copy.deepcopy(
            config_data["parameters"]["blocks"][0]["codes"][0]["script"]
        )
        config_dir = tmp_path / "sql-multi-script"

        extract_code_files("keboola.snowflake-transformation", config_data, config_dir)

        # File should be clean SQL with no artificial markers
        content = (config_dir / "transform.sql").read_text(encoding="utf-8")
        assert "STATEMENT" not in content

        merge_code_files("keboola.snowflake-transformation", config_data, config_dir)

        scripts = config_data["parameters"]["blocks"][0]["codes"][0]["script"]
        assert scripts == original_scripts

    def test_sql_roundtrip_multiline_multi_script(self, tmp_path: Path) -> None:
        """Round-trip with multi-line statements in multi-element script[]."""
        config_data = {
            "parameters": {
                "blocks": [
                    {
                        "name": "Block 1",
                        "codes": [
                            {
                                "name": "complex",
                                "script": [
                                    "CREATE TABLE foo AS\n    SELECT col1\n    FROM bar;",
                                    "INSERT INTO foo\n    SELECT col2\n    FROM baz;",
                                ],
                            }
                        ],
                    }
                ],
            },
        }
        original_scripts = copy.deepcopy(
            config_data["parameters"]["blocks"][0]["codes"][0]["script"]
        )
        config_dir = tmp_path / "sql-multi-multiline"

        extract_code_files("keboola.snowflake-transformation", config_data, config_dir)
        merge_code_files("keboola.snowflake-transformation", config_data, config_dir)

        scripts = config_data["parameters"]["blocks"][0]["codes"][0]["script"]
        assert scripts == original_scripts


# ===================================================================
# Python Transformation Tests
# ===================================================================


class TestPythonTransformExtraction:
    """Tests for Python transformation code extraction and merging."""

    def test_extract_python_blocks(self, tmp_path: Path) -> None:
        """Blocks are extracted to transform.py with Python markers."""
        config_data = copy.deepcopy(SAMPLE_PYTHON_TRANSFORM_CONFIG)
        config_dir = tmp_path / "py-transform"

        result = extract_code_files("keboola.python-transformation-v2", config_data, config_dir)

        py_file = config_dir / "transform.py"
        assert py_file.exists()

        content = py_file.read_text(encoding="utf-8")
        assert "# ===== BLOCK: Analysis =====" in content
        assert "# ===== CODE: Load data =====" in content
        assert "import pandas as pd" in content
        assert "# ===== CODE: Transform =====" in content
        assert "df['total'] = df['price'] * df['qty']" in content

        # Blocks should be removed from parameters
        assert "blocks" not in result["parameters"]

    def test_extract_python_packages(self, tmp_path: Path) -> None:
        """Packages are extracted to pyproject.toml."""
        config_data = copy.deepcopy(SAMPLE_PYTHON_TRANSFORM_CONFIG)
        config_dir = tmp_path / "py-packages"

        result = extract_code_files("keboola.python-transformation-v2", config_data, config_dir)

        toml_file = config_dir / "pyproject.toml"
        assert toml_file.exists()

        content = toml_file.read_text(encoding="utf-8")
        assert '"pandas==2.1.0"' in content
        assert '"numpy>=1.24"' in content
        assert 'name = "python-analysis"' in content

        # Packages should be removed from parameters
        assert "packages" not in result["parameters"]

    def test_merge_python_blocks(self, tmp_path: Path) -> None:
        """transform.py with markers is parsed back into blocks."""
        config_dir = tmp_path / "py-merge"
        config_dir.mkdir(parents=True)

        py_content = (
            "# ===== BLOCK: Analysis =====\n"
            "\n"
            "# ===== CODE: Load data =====\n"
            "import pandas as pd\n"
            "df = pd.read_csv('data.csv')\n"
            "\n"
        )
        (config_dir / "transform.py").write_text(py_content, encoding="utf-8")

        config_data: dict = {"parameters": {}}
        result = merge_code_files("keboola.python-transformation-v2", config_data, config_dir)

        blocks = result["parameters"]["blocks"]
        assert len(blocks) == 1
        assert blocks[0]["name"] == "Analysis"
        assert blocks[0]["codes"][0]["name"] == "Load data"
        assert "import pandas as pd" in blocks[0]["codes"][0]["script"][0]

    def test_merge_python_packages(self, tmp_path: Path) -> None:
        """pyproject.toml dependencies are merged back into packages list."""
        config_dir = tmp_path / "py-merge-pkg"
        config_dir.mkdir(parents=True)

        toml_content = (
            "[project]\n"
            'name = "my-transform"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "pandas==2.1.0",\n'
            '    "numpy>=1.24",\n'
            "]\n"
        )
        (config_dir / "pyproject.toml").write_text(toml_content, encoding="utf-8")

        config_data: dict = {"parameters": {}}
        result = merge_code_files("keboola.python-transformation-v2", config_data, config_dir)

        assert result["parameters"]["packages"] == ["pandas==2.1.0", "numpy>=1.24"]

    def test_multiline_python_produces_single_string(self, tmp_path: Path) -> None:
        """Multi-line Python code is joined into a single script element."""
        config_dir = tmp_path / "py-multiline"
        config_dir.mkdir(parents=True)

        py_content = (
            "# ===== BLOCK: Analysis =====\n"
            "\n"
            "# ===== CODE: Process =====\n"
            "import pandas as pd\n"
            "\n"
            "df = pd.read_csv('data.csv')\n"
            "result = df.groupby('category').sum()\n"
        )
        (config_dir / "transform.py").write_text(py_content, encoding="utf-8")

        config_data: dict = {"parameters": {}}
        result = merge_code_files("keboola.python-transformation-v2", config_data, config_dir)

        blocks = result["parameters"]["blocks"]
        script = blocks[0]["codes"][0]["script"]
        assert len(script) == 1
        assert "import pandas as pd" in script[0]
        assert "result = df.groupby" in script[0]
        assert "\n" in script[0]

    def test_python_transform_round_trip(self, tmp_path: Path) -> None:
        """Extract then merge produces equivalent blocks and packages."""
        config_data = copy.deepcopy(SAMPLE_PYTHON_TRANSFORM_CONFIG)
        original_blocks = copy.deepcopy(config_data["parameters"]["blocks"])
        original_packages = copy.deepcopy(config_data["parameters"]["packages"])
        config_dir = tmp_path / "py-roundtrip"

        extract_code_files("keboola.python-transformation-v2", config_data, config_dir)
        merge_code_files("keboola.python-transformation-v2", config_data, config_dir)

        # Verify packages round-trip
        assert config_data["parameters"]["packages"] == original_packages

        # Verify block/code names and content
        restored_blocks = config_data["parameters"]["blocks"]
        assert len(restored_blocks) == len(original_blocks)
        for orig_block, rest_block in zip(original_blocks, restored_blocks, strict=True):
            assert orig_block["name"] == rest_block["name"]
            for orig_code, rest_code in zip(orig_block["codes"], rest_block["codes"], strict=True):
                assert orig_code["name"] == rest_code["name"]
                orig_text = "\n".join(orig_code["script"]).strip()
                rest_text = "\n".join(rest_code["script"]).strip()
                assert orig_text == rest_text


# ===================================================================
# Python App Tests
# ===================================================================


class TestPythonAppExtraction:
    """Tests for Python custom app code extraction and merging."""

    def test_extract_app_code(self, tmp_path: Path) -> None:
        """parameters.code is extracted to code.py."""
        config_data = copy.deepcopy(SAMPLE_PYTHON_APP_CONFIG)
        config_dir = tmp_path / "app-code"

        result = extract_code_files("kds-team.app-custom-python", config_data, config_dir)

        code_file = config_dir / "code.py"
        assert code_file.exists()

        content = code_file.read_text(encoding="utf-8")
        assert "import json" in content
        assert "result = {'status': 'ok'}" in content

        # code should be removed from parameters
        assert "code" not in result["parameters"]

    def test_extract_app_packages(self, tmp_path: Path) -> None:
        """Packages are extracted to pyproject.toml with keboola metadata."""
        config_data = copy.deepcopy(SAMPLE_PYTHON_APP_CONFIG)
        config_dir = tmp_path / "app-packages"

        result = extract_code_files("kds-team.app-custom-python", config_data, config_dir)

        toml_file = config_dir / "pyproject.toml"
        assert toml_file.exists()

        content = toml_file.read_text(encoding="utf-8")
        assert '"requests>=2.31"' in content
        assert '"beautifulsoup4"' in content
        assert "[tool.keboola]" in content
        assert 'component_id = "kds-team.app-custom-python"' in content
        assert 'config_id = "cfg-300"' in content

        # packages should be removed from parameters
        assert "packages" not in result["parameters"]

    def test_merge_app_code(self, tmp_path: Path) -> None:
        """code.py is merged back into parameters.code."""
        config_dir = tmp_path / "app-merge"
        config_dir.mkdir(parents=True)

        code_content = "print('hello world')\n"
        (config_dir / "code.py").write_text(code_content, encoding="utf-8")

        config_data: dict = {"parameters": {}}
        result = merge_code_files("kds-team.app-custom-python", config_data, config_dir)

        assert result["parameters"]["code"] == code_content

    def test_merge_app_packages(self, tmp_path: Path) -> None:
        """pyproject.toml is merged back into parameters.packages."""
        config_dir = tmp_path / "app-merge-pkg"
        config_dir.mkdir(parents=True)

        toml_content = (
            "[project]\n"
            'name = "my-app"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            '    "requests>=2.31",\n'
            "]\n"
            "\n"
            "[tool.keboola]\n"
            'component_id = "kds-team.app-custom-python"\n'
        )
        (config_dir / "pyproject.toml").write_text(toml_content, encoding="utf-8")

        config_data: dict = {"parameters": {}}
        result = merge_code_files("kds-team.app-custom-python", config_data, config_dir)

        assert result["parameters"]["packages"] == ["requests>=2.31"]

    def test_app_round_trip(self, tmp_path: Path) -> None:
        """Extract then merge produces equivalent code and packages."""
        config_data = copy.deepcopy(SAMPLE_PYTHON_APP_CONFIG)
        original_code = config_data["parameters"]["code"]
        original_packages = copy.deepcopy(config_data["parameters"]["packages"])
        config_dir = tmp_path / "app-roundtrip"

        extract_code_files("kds-team.app-custom-python", config_data, config_dir)
        merge_code_files("kds-team.app-custom-python", config_data, config_dir)

        assert config_data["parameters"]["code"] == original_code
        assert config_data["parameters"]["packages"] == original_packages


# ===================================================================
# Non-extractable Components
# ===================================================================


class TestNonExtractableComponent:
    """Tests for components that should not trigger code extraction."""

    def test_non_extractable_component(self, tmp_path: Path) -> None:
        """Generic components are returned unchanged with no files created."""
        config_data = {
            "version": 2,
            "name": "My Extractor",
            "parameters": {"key": "value"},
            "_keboola": {
                "component_id": "keboola.ex-http",
                "config_id": "cfg-generic",
            },
        }
        config_dir = tmp_path / "generic"

        result = extract_code_files("keboola.ex-http", config_data, config_dir)

        # No files should be created
        assert not config_dir.exists()

        # Config should be unchanged
        assert result["parameters"]["key"] == "value"

    def test_merge_non_extractable_component(self, tmp_path: Path) -> None:
        """Merge on generic component is a no-op."""
        config_dir = tmp_path / "generic-merge"
        config_dir.mkdir(parents=True)

        config_data: dict = {"parameters": {"key": "value"}}
        result = merge_code_files("keboola.ex-http", config_data, config_dir)

        assert result["parameters"]["key"] == "value"

    @pytest.mark.parametrize(
        "component_id",
        [
            "keboola.snowflake-transformation",
            "keboola.synapse-transformation",
            "keboola.oracle-transformation",
            "keboola.redshift-sql-transformation",
        ],
    )
    def test_all_sql_components_recognized(self, component_id: str, tmp_path: Path) -> None:
        """All SQL transformation component IDs trigger extraction."""
        config_data = copy.deepcopy(SAMPLE_SQL_CONFIG)
        config_dir = tmp_path / component_id.replace(".", "-")

        extract_code_files(component_id, config_data, config_dir)

        assert (config_dir / "transform.sql").exists()


# ===================================================================
# Statement-boundary markers (issue #686, part 3)
# ===================================================================


SQL_COMPONENT = "keboola.snowflake-transformation"


def _sql_config(script: list[str]) -> dict[str, Any]:
    """Build a minimal SQL-transformation config carrying one code block."""
    return {
        "parameters": {
            "blocks": [{"name": "Block 1", "codes": [{"name": "Code 1", "script": list(script)}]}]
        }
    }


def _script_of(config_data: dict[str, Any]) -> list[str]:
    """Return the first code's script array."""
    return config_data["parameters"]["blocks"][0]["codes"][0]["script"]


class TestStatementMarkers:
    """``transform.sql`` carries explicit statement boundaries when needed."""

    def test_marker_written_when_semicolons_cannot_recover_boundaries(self, tmp_path: Path) -> None:
        """No trailing semicolons -> an explicit marker separates the statements."""
        config_data = _sql_config(["SELECT 1", "SELECT 2"])
        config_dir = tmp_path / "no-semicolons"

        extract_code_files(SQL_COMPONENT, config_data, config_dir)

        content = (config_dir / "transform.sql").read_text(encoding="utf-8")
        assert SQL_STATEMENT_MARKER in content

    def test_marker_round_trip_preserves_statement_count(self, tmp_path: Path) -> None:
        """The marked file merges back to the exact original array."""
        original = ["SELECT 1", "SELECT 2", "SELECT 3"]
        config_data = _sql_config(original)
        config_dir = tmp_path / "no-semicolons-rt"

        extract_code_files(SQL_COMPONENT, config_data, config_dir)
        merge_code_files(SQL_COMPONENT, config_data, config_dir)

        assert _script_of(config_data) == original

    def test_no_marker_for_semicolon_terminated_scripts(self, tmp_path: Path) -> None:
        """The common ``;``-terminated case stays byte-identical to before."""
        config_data = _sql_config(["SELECT 1;", "SELECT 2;"])
        config_dir = tmp_path / "semicolons"

        extract_code_files(SQL_COMPONENT, config_data, config_dir)

        content = (config_dir / "transform.sql").read_text(encoding="utf-8")
        assert SQL_STATEMENT_MARKER not in content
        assert "STATEMENT" not in content

    def test_marker_segment_is_still_split_on_semicolons(self, tmp_path: Path) -> None:
        """A user adding ``; SELECT ...`` inside a marked segment gets split (R3)."""
        config_data = _sql_config(["SELECT 1", "SELECT 2"])
        config_dir = tmp_path / "resplit-segment"
        extract_code_files(SQL_COMPONENT, config_data, config_dir)

        sql_file = config_dir / "transform.sql"
        content = sql_file.read_text(encoding="utf-8")
        sql_file.write_text(content.replace("SELECT 1", "SELECT 1; SELECT 9;"), encoding="utf-8")

        merged: dict[str, Any] = {"parameters": {}}
        merge_code_files(SQL_COMPONENT, merged, config_dir)

        assert _script_of(merged) == ["SELECT 1;", "SELECT 9;", "SELECT 2"]

    def test_marker_collision_falls_back_to_no_markers(self, tmp_path: Path) -> None:
        """A statement whose own text holds a marker line disables marker emission."""
        config_data = _sql_config([f"SELECT 1\n{SQL_STATEMENT_MARKER}", "SELECT 2"])
        config_dir = tmp_path / "collision"

        extract_code_files(SQL_COMPONENT, config_data, config_dir)

        content = (config_dir / "transform.sql").read_text(encoding="utf-8")
        # The marker text appears only as part of the statement itself -- exactly
        # once -- never as an emitted boundary.
        assert content.count(SQL_STATEMENT_MARKER) == 1

    def test_canonical_sql_script_splits_each_element(self) -> None:
        """Each element is split independently and the results flattened."""
        assert canonical_sql_script(["SELECT 1; SELECT 2;", "SELECT 3"]) == [
            "SELECT 1;",
            "SELECT 2;",
            "SELECT 3",
        ]

    def test_canonical_sql_script_drops_blank_elements(self) -> None:
        """Whitespace-only elements vanish, matching the file round-trip."""
        assert canonical_sql_script(["SELECT 1;", "   ", ""]) == ["SELECT 1;"]


class TestPullPushDiffShapeParity:
    """The issue #686 repro table, at the hash level.

    Drives the real pull -> push -> diff producers over one code block and
    asserts the two sides agree (no phantom drift) and that the statement
    array survives the file round-trip.
    """

    @staticmethod
    def _api(script: list[str]) -> dict[str, Any]:
        return {
            "id": "1",
            "name": "c",
            "description": "",
            "configuration": {
                "parameters": {
                    "blocks": [{"name": "B", "codes": [{"name": "C", "script": list(script)}]}]
                }
            },
        }

    @pytest.mark.parametrize(
        ("api_script", "expected_sent"),
        [
            # ;-terminated, several elements: unchanged, was PHANTOM before.
            (["SELECT 1;", "SELECT 2;"], ["SELECT 1;", "SELECT 2;"]),
            # No semicolons: boundaries survive via the marker. Before the fix
            # push silently sent ONE element (MULTI_STATEMENT_COUNT=1).
            (["SELECT 1", "SELECT 2"], ["SELECT 1", "SELECT 2"]),
            # One element packing two statements is NOT a canonical array: the
            # runtime wants one statement per element, so it is split. That is
            # the #274 normalization, deliberate -- and now no longer phantom.
            (["SELECT 1;\nSELECT 2;"], ["SELECT 1;", "SELECT 2;"]),
            (["SELECT 1"], ["SELECT 1"]),
        ],
    )
    def test_push_matches_remote_and_diff_is_in_sync(
        self, api_script: list[str], expected_sent: list[str], tmp_path: Path
    ) -> None:
        from keboola_agent_cli.sync.config_format import api_config_to_local
        from keboola_agent_cli.sync.diff_engine import config_hash

        component = "keboola.snowflake-transformation"
        config_dir = tmp_path / "cfg"

        local = api_config_to_local(component, self._api(api_script), "1")  # pull
        extract_code_files(component, local, config_dir)
        pushed = copy.deepcopy(local)
        merge_code_files(component, pushed, config_dir)  # push
        sent = pushed["parameters"]["blocks"][0]["codes"][0]["script"]

        assert sent == expected_sent
        # diff: remote side of what push just wrote vs the pushed baseline
        remote_hash = config_hash(api_config_to_local(component, self._api(sent), "1"))
        assert remote_hash == config_hash(pushed)


class TestBroadPredicateSqlComponent:
    """SQL backends matched only by fragment keep both sides consistent (R1).

    ``keboola.exasol-transformation`` is SQL for normalization purposes but is
    not in the exact extraction set, so its blocks stay inside ``_config.yml``
    and the merge is an identity. The split shape must therefore survive
    untouched -- the local data IS the normalized data.
    """

    COMPONENT = "keboola.exasol-transformation"

    def test_yaml_identity_round_trip_preserves_split_shape(self, tmp_path: Path) -> None:
        from keboola_agent_cli.sync.config_format import api_config_to_local
        from keboola_agent_cli.sync.diff_engine import config_hash

        api_config = {
            "id": "1",
            "name": "c",
            "description": "",
            "configuration": {
                "parameters": {
                    "blocks": [
                        {
                            "name": "B",
                            "codes": [{"name": "C", "script": ["SELECT 1;", "SELECT 2;"]}],
                        }
                    ]
                }
            },
        }
        config_dir = tmp_path / "exasol"

        local = api_config_to_local(self.COMPONENT, api_config, "1")
        extract_code_files(self.COMPONENT, local, config_dir)
        # No code file is written -- the blocks stay in the YAML body.
        assert not (config_dir / "transform.sql").exists()
        assert local["parameters"]["blocks"][0]["codes"][0]["script"] == [
            "SELECT 1;",
            "SELECT 2;",
        ]

        pushed = copy.deepcopy(local)
        merge_code_files(self.COMPONENT, pushed, config_dir)
        assert config_hash(pushed) == config_hash(local)
