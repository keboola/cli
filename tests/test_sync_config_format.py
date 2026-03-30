"""Tests for sync config_format module -- API JSON <-> local YAML conversion."""

import pytest

from keboola_agent_cli.sync.config_format import (
    _normalize_scripts,
    api_config_to_local,
    api_row_to_local,
    classify_component_type,
    local_config_to_api,
    local_row_to_api,
)

SAMPLE_API_CONFIG = {
    "id": "cfg-123",
    "name": "My Extractor",
    "description": "Extracts data from API",
    "configuration": {
        "parameters": {
            "api_url": "https://example.com",
            "#token": "KBC::ProjectSecure::abc",
        },
        "storage": {
            "input": {
                "tables": [{"source": "in.c-main.users", "destination": "users"}],
            },
            "output": {
                "tables": [{"source": "result", "destination": "out.c-main.result"}],
            },
        },
        "processors": {
            "after": [{"definition": {"component": "keboola.processor-move-files"}}],
        },
    },
}

SAMPLE_COMPONENT_ID = "keboola.ex-http"
SAMPLE_CONFIG_ID = "cfg-123"


class TestClassifyComponentType:
    """Tests for classify_component_type()."""

    @pytest.mark.parametrize(
        "api_type,expected",
        [
            ("extractor", "extractor"),
            ("writer", "writer"),
            ("transformation", "transformation"),
            ("application", "application"),
            ("other", "other"),
        ],
    )
    def test_classify_component_type_known(self, api_type: str, expected: str) -> None:
        """Known component types map to themselves."""
        assert classify_component_type(api_type) == expected

    @pytest.mark.parametrize("api_type", ["unknown", "custom", "orchestrator", ""])
    def test_classify_component_type_fallback(self, api_type: str) -> None:
        """Unknown component types fall back to 'other'."""
        assert classify_component_type(api_type) == "other"


class TestApiConfigToLocal:
    """Tests for api_config_to_local()."""

    def test_api_config_to_local_basic(self) -> None:
        """Converted local config has version=2, name, description, and _keboola block."""
        local = api_config_to_local(SAMPLE_COMPONENT_ID, SAMPLE_API_CONFIG, SAMPLE_CONFIG_ID)

        assert local["version"] == 2
        assert local["name"] == "My Extractor"
        assert local["description"] == "Extracts data from API"
        assert local["_keboola"] == {
            "component_id": SAMPLE_COMPONENT_ID,
            "config_id": SAMPLE_CONFIG_ID,
        }

    def test_api_config_to_local_parameters(self) -> None:
        """Parameters are promoted from configuration.parameters to top level."""
        local = api_config_to_local(SAMPLE_COMPONENT_ID, SAMPLE_API_CONFIG, SAMPLE_CONFIG_ID)

        assert "parameters" in local
        assert local["parameters"]["api_url"] == "https://example.com"
        assert local["parameters"]["#token"] == "KBC::ProjectSecure::abc"

    def test_api_config_to_local_storage(self) -> None:
        """Input and output are promoted from configuration.storage."""
        local = api_config_to_local(SAMPLE_COMPONENT_ID, SAMPLE_API_CONFIG, SAMPLE_CONFIG_ID)

        assert "input" in local
        assert local["input"]["tables"][0]["source"] == "in.c-main.users"

        assert "output" in local
        assert local["output"]["tables"][0]["destination"] == "out.c-main.result"

    def test_api_config_to_local_processors(self) -> None:
        """Processors are promoted from configuration.processors."""
        local = api_config_to_local(SAMPLE_COMPONENT_ID, SAMPLE_API_CONFIG, SAMPLE_CONFIG_ID)

        assert "processors" in local
        assert local["processors"]["after"][0]["definition"]["component"] == (
            "keboola.processor-move-files"
        )

    def test_api_config_to_local_extras_preserved(self) -> None:
        """Unknown keys in configuration are preserved under _configuration_extra."""
        api_config = {
            "id": "cfg-1",
            "name": "Test",
            "description": "",
            "configuration": {
                "parameters": {"key": "val"},
                "runtime": {"imageTag": "latest"},
                "authorization": {"oauth_api": {"id": "abc"}},
            },
        }
        local = api_config_to_local("comp", api_config, "cfg-1")

        assert "_configuration_extra" in local
        assert local["_configuration_extra"]["runtime"] == {"imageTag": "latest"}
        assert local["_configuration_extra"]["authorization"] == {"oauth_api": {"id": "abc"}}
        # Promoted keys must not appear in extras
        assert "parameters" not in local["_configuration_extra"]

    def test_api_config_to_local_no_configuration(self) -> None:
        """Config with no configuration block produces minimal local structure."""
        api_config = {"id": "cfg-0", "name": "Empty", "description": ""}
        local = api_config_to_local("comp", api_config, "cfg-0")

        assert local["name"] == "Empty"
        assert "parameters" not in local
        assert "input" not in local
        assert "output" not in local
        assert "processors" not in local
        assert "_configuration_extra" not in local


class TestLocalConfigToApiRoundTrip:
    """Tests for local_config_to_api() and round-trip conversion."""

    def test_local_config_to_api_round_trip(self) -> None:
        """Convert API->local->API and verify the configuration dict matches."""
        local = api_config_to_local(SAMPLE_COMPONENT_ID, SAMPLE_API_CONFIG, SAMPLE_CONFIG_ID)
        name, description, configuration = local_config_to_api(local)

        original_config = SAMPLE_API_CONFIG["configuration"]

        assert name == "My Extractor"
        assert description == "Extracts data from API"
        assert configuration["parameters"] == original_config["parameters"]
        assert configuration["storage"] == original_config["storage"]
        assert configuration["processors"] == original_config["processors"]

    def test_local_config_to_api_extras_merged_back(self) -> None:
        """Extras from _configuration_extra are merged back into API configuration."""
        local = {
            "version": 2,
            "name": "Test",
            "description": "",
            "parameters": {"key": "val"},
            "_configuration_extra": {"runtime": {"imageTag": "latest"}},
            "_keboola": {"component_id": "comp", "config_id": "cfg-1"},
        }
        _, _, configuration = local_config_to_api(local)

        assert configuration["runtime"] == {"imageTag": "latest"}
        assert configuration["parameters"] == {"key": "val"}


class TestNormalizeScripts:
    """Tests for _normalize_scripts() -- script array normalization."""

    def test_per_line_array_joined_to_single_string(self) -> None:
        """Per-line script array is joined into a single string."""
        params = {
            "blocks": [
                {"codes": [{"script": ["CREATE TABLE foo AS", "    SELECT col1", "    FROM bar;"]}]}
            ]
        }
        result = _normalize_scripts(params)
        script = result["blocks"][0]["codes"][0]["script"]
        assert len(script) == 1
        assert script[0] == "CREATE TABLE foo AS\n    SELECT col1\n    FROM bar;"

    def test_single_multiline_string_preserved(self) -> None:
        """A script that is already a single multiline string stays as one element."""
        params = {
            "blocks": [
                {"codes": [{"script": ["CREATE TABLE foo AS\n    SELECT col1\n    FROM bar;"]}]}
            ]
        }
        result = _normalize_scripts(params)
        script = result["blocks"][0]["codes"][0]["script"]
        assert len(script) == 1
        assert script[0] == "CREATE TABLE foo AS\n    SELECT col1\n    FROM bar;"

    def test_trailing_whitespace_stripped(self) -> None:
        """Trailing whitespace per line is stripped during normalization."""
        params = {"blocks": [{"codes": [{"script": ["SELECT 1  ", "FROM bar   "]}]}]}
        result = _normalize_scripts(params)
        script = result["blocks"][0]["codes"][0]["script"]
        assert len(script) == 1
        assert script[0] == "SELECT 1\nFROM bar"

    def test_empty_script_preserved(self) -> None:
        """Empty script array stays empty."""
        params = {"blocks": [{"codes": [{"script": []}]}]}
        result = _normalize_scripts(params)
        assert result["blocks"][0]["codes"][0]["script"] == []

    def test_no_blocks_passthrough(self) -> None:
        """Parameters without blocks are returned unchanged."""
        params = {"key": "value"}
        result = _normalize_scripts(params)
        assert result == {"key": "value"}

    def test_non_dict_passthrough(self) -> None:
        """Non-dict input is returned as-is."""
        assert _normalize_scripts("not a dict") == "not a dict"
        assert _normalize_scripts(42) == 42

    def test_does_not_mutate_input(self) -> None:
        """Original parameters are not mutated."""
        params = {"blocks": [{"codes": [{"script": ["line1", "line2"]}]}]}
        import copy

        original = copy.deepcopy(params)
        _normalize_scripts(params)
        assert params == original


class TestRowConversion:
    """Tests for api_row_to_local() and local_row_to_api()."""

    @pytest.fixture()
    def sample_row(self) -> dict:
        return {
            "id": "row-42",
            "name": "First Row",
            "description": "A test row",
            "configuration": {
                "parameters": {"query": "SELECT 1"},
                "storage": {
                    "output": {"tables": [{"source": "result", "destination": "out.c-main.data"}]},
                },
            },
        }

    def test_api_row_to_local(self, sample_row: dict) -> None:
        """Row conversion includes _keboola.row_id and promotes parameters/storage."""
        local = api_row_to_local(sample_row, "keboola.ex-db-snowflake")

        assert local["version"] == 2
        assert local["name"] == "First Row"
        assert local["description"] == "A test row"
        assert local["parameters"]["query"] == "SELECT 1"
        assert "output" in local
        assert local["output"]["tables"][0]["destination"] == "out.c-main.data"
        assert local["_keboola"]["component_id"] == "keboola.ex-db-snowflake"
        assert local["_keboola"]["row_id"] == "row-42"

    def test_local_row_to_api(self, sample_row: dict) -> None:
        """Row reverse conversion produces (name, description, configuration) tuple."""
        local = api_row_to_local(sample_row, "keboola.ex-db-snowflake")
        name, description, configuration = local_row_to_api(local)

        assert name == "First Row"
        assert description == "A test row"
        assert configuration["parameters"]["query"] == "SELECT 1"
        assert configuration["storage"]["output"]["tables"][0]["destination"] == "out.c-main.data"
