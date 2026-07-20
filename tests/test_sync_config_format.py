"""Tests for sync config_format module -- API JSON <-> local YAML conversion."""

from typing import Any

import pytest

from keboola_agent_cli.sync.config_format import (
    _normalize_scripts,
    api_config_to_local,
    api_row_to_local,
    classify_component_type,
    local_config_to_api,
    local_row_to_api,
)

SAMPLE_API_CONFIG: dict[str, Any] = {
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


class TestVariablesRowRoundTrip:
    """Round-trip for keboola.variables / keboola.shared-code rows.

    These components use non-standard top-level configuration keys
    (``values`` for variables, ``code`` for shared-code) that the API accepts
    as the row ``configuration`` body verbatim. The local YAML MUST preserve
    those keys at the top level -- wrapping them under ``_configuration_extra``
    breaks the FIIA convention of direct edits (``values: [...]`` at top level)
    and diverges from ``kbc push`` behaviour.
    """

    def test_variables_row_api_to_local_hoists_values_to_top_level(self) -> None:
        """keboola.variables values row: ``values`` key appears at top level of YAML.

        When a user edits a variables row locally, they expect to see and write
        ``values:`` directly -- not nested under ``_configuration_extra``.
        """
        api_row = {
            "id": "row-main",
            "name": "Main",
            "description": "",
            "configuration": {
                "values": [
                    {"name": "year_start", "value": "2016", "type": "string"},
                    {"name": "region", "value": "eu", "type": "string"},
                ]
            },
        }

        local = api_row_to_local(api_row, "keboola.variables")

        assert local["values"] == api_row["configuration"]["values"]
        assert "_configuration_extra" not in local

    def test_variables_row_local_to_api_reads_top_level_values(self) -> None:
        """User-authored top-level ``values:`` in local YAML flows to API configuration.

        FIIA writes row files with ``values:`` at the top level and expects
        ``sync push`` to PUT that verbatim to
        ``/components/keboola.variables/configs/{id}/rows/{rowId}``.
        """
        local = {
            "version": 2,
            "name": "Main",
            "description": "",
            "values": [{"name": "year_start", "value": "2016", "type": "string"}],
            "_keboola": {"component_id": "keboola.variables", "row_id": "row-1"},
        }

        name, description, configuration = local_row_to_api(local)

        assert name == "Main"
        assert description == ""
        assert configuration == {
            "values": [{"name": "year_start", "value": "2016", "type": "string"}]
        }

    def test_variables_row_byte_for_byte_round_trip(self) -> None:
        """api→local→api returns the identical configuration dict for a variables row.

        This is the FIIA contract: the row body the user sees in Keboola after push
        must equal the row body they wrote locally, byte-for-byte (deep equality).
        """
        api_row = {
            "id": "row-main",
            "name": "Main",
            "description": "default values",
            "configuration": {
                "values": [
                    {"name": "year_start", "value": "2016", "type": "string"},
                    {"name": "flag", "value": "true", "type": "string"},
                ]
            },
        }

        local = api_row_to_local(api_row, "keboola.variables")
        _, _, configuration = local_row_to_api(local)

        assert configuration == api_row["configuration"]

    def test_shared_code_row_hoists_code_to_top_level(self) -> None:
        """keboola.shared-code rows: ``code_content`` / ``componentId`` keys hoisted.

        Shared-code rows use ``componentId`` + ``code_content`` at top level;
        they must round-trip the same way as variables ``values`` rows.
        """
        api_row = {
            "id": "row-1",
            "name": "Reusable snippet",
            "description": "",
            "configuration": {
                "componentId": "keboola.snowflake-transformation",
                "code_content": ["SELECT * FROM my_table;"],
            },
        }

        local = api_row_to_local(api_row, "keboola.shared-code")

        assert local["componentId"] == "keboola.snowflake-transformation"
        assert local["code_content"] == ["SELECT * FROM my_table;"]
        assert "_configuration_extra" not in local

        _, _, configuration = local_row_to_api(local)
        assert configuration == api_row["configuration"]

    def test_non_hoisted_component_still_uses_configuration_extra(self) -> None:
        """Non-variables/shared-code rows keep ``_configuration_extra`` wrapping.

        The hoist-to-top-level behaviour is opt-in per component. Generic
        extractor/writer rows with unusual top-level keys (e.g. a component
        that stores ``foo`` at the configuration root) must not regress.
        """
        api_row = {
            "id": "row-1",
            "name": "Test",
            "description": "",
            "configuration": {"parameters": {"q": "SELECT 1"}, "foo": {"bar": 1}},
        }

        local = api_row_to_local(api_row, "keboola.ex-db-snowflake")

        assert "_configuration_extra" in local
        assert local["_configuration_extra"] == {"foo": {"bar": 1}}
        assert "foo" not in local


class TestLocalRowToApiComponentIdParam:
    """KFR-04: ``local_row_to_api`` accepts an explicit ``component_id``.

    A fresh-CREATE scaffold row may not carry a ``_keboola`` block yet, so the
    hoist decision must be driveable by the caller's known component id. When
    ``component_id`` is omitted the legacy behaviour (read from the file) holds.
    """

    def test_explicit_component_id_hoists_values_without_keboola_block(self) -> None:
        """``component_id="keboola.variables"`` hoists ``values`` even when the
        row file has no ``_keboola`` metadata (the KFR-04 fresh-CREATE case)."""
        local = {
            "version": 2,
            "name": "Main",
            "description": "default values",
            "values": [{"name": "year_start", "value": "2016", "type": "string"}],
        }

        name, _description, configuration = local_row_to_api(local, "keboola.variables")

        assert name == "Main"
        assert configuration == {
            "values": [{"name": "year_start", "value": "2016", "type": "string"}]
        }

    def test_no_component_id_falls_back_to_keboola_block(self) -> None:
        """``component_id=None`` reads the id from ``_keboola`` (back-compat)."""
        local = {
            "version": 2,
            "name": "Main",
            "description": "",
            "values": [{"name": "region", "value": "eu", "type": "string"}],
            "_keboola": {"component_id": "keboola.variables", "row_id": "row-1"},
        }

        _, _, configuration = local_row_to_api(local)

        assert configuration == {"values": [{"name": "region", "value": "eu", "type": "string"}]}

    def test_no_component_id_and_no_keboola_block_does_not_hoist(self) -> None:
        """Without an id from either source, the row is not treated as a hoist
        component: ``values`` stays out of the API body (legacy behaviour)."""
        local = {
            "version": 2,
            "name": "Main",
            "description": "",
            "values": [{"name": "region", "value": "eu", "type": "string"}],
        }

        _, _, configuration = local_row_to_api(local)

        assert configuration == {}

    def test_explicit_component_id_overrides_stale_keboola_block(self) -> None:
        """The explicit arg wins over a (possibly stale) ``_keboola`` block."""
        local = {
            "version": 2,
            "name": "Main",
            "description": "",
            "values": [{"name": "region", "value": "eu", "type": "string"}],
            "_keboola": {"component_id": "keboola.ex-db-snowflake", "row_id": "r"},
        }

        _, _, configuration = local_row_to_api(local, "keboola.variables")

        assert configuration == {"values": [{"name": "region", "value": "eu", "type": "string"}]}


class TestIsDisabledSerialization:
    """Sparse ``is_disabled`` emission and round-trip hygiene (issue #467).

    The key is emitted ONLY when the API reports ``isDisabled`` truthy --
    absence means enabled, so trees pulled before the field existed do not
    show a spurious diff on every config. On the way back, ``is_disabled``
    is local metadata: it must never leak into the API ``configuration``
    body (it travels as the top-level ``isDisabled`` form field instead).
    """

    @staticmethod
    def _api_config(**extra: Any) -> dict[str, Any]:
        return {
            "id": "cfg-1",
            "name": "Cfg",
            "description": "",
            "configuration": {"parameters": {"a": 1}},
            **extra,
        }

    @staticmethod
    def _api_row(**extra: Any) -> dict[str, Any]:
        return {
            "id": "row-1",
            "name": "Row",
            "description": "",
            "configuration": {"parameters": {"a": 1}},
            **extra,
        }

    # -- api_config_to_local ------------------------------------------------

    def test_config_is_disabled_true_emitted(self) -> None:
        """isDisabled=True lands as ``is_disabled: True`` in the local YAML."""
        local = api_config_to_local("comp", self._api_config(isDisabled=True), "cfg-1")
        assert local["is_disabled"] is True

    def test_config_is_disabled_false_absent(self) -> None:
        """isDisabled=False is sparse -- the key is NOT emitted."""
        local = api_config_to_local("comp", self._api_config(isDisabled=False), "cfg-1")
        assert "is_disabled" not in local

    def test_config_is_disabled_missing_absent(self) -> None:
        """No isDisabled key in the API response -> no local key either."""
        local = api_config_to_local("comp", self._api_config(), "cfg-1")
        assert "is_disabled" not in local

    # -- api_row_to_local ---------------------------------------------------

    def test_row_is_disabled_true_emitted(self) -> None:
        """Row isDisabled=True lands as ``is_disabled: True``."""
        local = api_row_to_local(self._api_row(isDisabled=True), "keboola.ex-db-snowflake")
        assert local["is_disabled"] is True

    def test_row_is_disabled_false_absent(self) -> None:
        """Row isDisabled=False is sparse -- the key is NOT emitted."""
        local = api_row_to_local(self._api_row(isDisabled=False), "keboola.ex-db-snowflake")
        assert "is_disabled" not in local

    def test_row_is_disabled_missing_absent(self) -> None:
        """Row without isDisabled -> no local key."""
        local = api_row_to_local(self._api_row(), "keboola.ex-db-snowflake")
        assert "is_disabled" not in local

    # -- local -> API body hygiene -------------------------------------------

    def test_variables_row_is_disabled_not_hoisted_into_configuration(self) -> None:
        """keboola.variables hoist keeps ``is_disabled`` OUT of the API body.

        ``is_disabled`` is in ``_ROW_LOCAL_RESERVED_KEYS`` -- only real payload
        keys (``values``) are hoisted back into ``configuration``.
        """
        local = {
            "version": 2,
            "name": "Main",
            "description": "",
            "is_disabled": True,
            "values": [{"name": "year_start", "value": "2016", "type": "string"}],
        }

        _, _, configuration = local_row_to_api(local, "keboola.variables")

        assert "is_disabled" not in configuration
        assert configuration == {
            "values": [{"name": "year_start", "value": "2016", "type": "string"}]
        }

    def test_local_config_to_api_never_includes_is_disabled(self) -> None:
        """``is_disabled`` never lands inside the config's API configuration."""
        local = {
            "version": 2,
            "name": "Cfg",
            "description": "",
            "is_disabled": True,
            "parameters": {"a": 1},
            "_keboola": {"component_id": "comp", "config_id": "cfg-1"},
        }

        _, _, configuration = local_config_to_api(local)

        assert "is_disabled" not in configuration
        assert configuration == {"parameters": {"a": 1}}
