"""Tests for sync code_extraction module -- SQL/Python code extraction and merging."""

import copy
from pathlib import Path

import pytest

from keboola_agent_cli.sync.code_extraction import (
    extract_code_files,
    merge_code_files,
)

# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------

SAMPLE_SQL_CONFIG = {
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

SAMPLE_PYTHON_TRANSFORM_CONFIG = {
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

SAMPLE_PYTHON_APP_CONFIG = {
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
        assert "SELECT 1;" in blocks[0]["codes"][0]["script"][0]


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
                assert orig_code["script"][0].strip() == rest_code["script"][0].strip()


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
