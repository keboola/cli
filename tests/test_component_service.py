"""Tests for ComponentService - component discovery and scaffold generation."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from helpers import setup_single_project
from keboola_agent_cli.constants import SECRET_PLACEHOLDER
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.services.component_service import (
    ComponentService,
    _detect_component_category,
    _generate_from_schema,
    _mask_secrets,
)

# ---------------------------------------------------------------------------
# Sample API responses
# ---------------------------------------------------------------------------

EXTRACTOR_RESPONSE: dict[str, Any] = {
    "componentId": "keboola.ex-http",
    "componentName": "HTTP",
    "componentType": "extractor",
    "componentCategories": ["API"],
    "componentFlags": [],
    "description": "Download CSV files",
    "longDescription": "",
    "documentationUrl": "https://help.keboola.com/components/extractors/other/http/",
    "documentation": "",
    "configurationSchema": {
        "type": "object",
        "required": ["baseUrl"],
        "properties": {"baseUrl": {"type": "string", "default": ""}},
    },
    "configurationRowSchema": {},
    "rootConfigurationExamples": [{"parameters": {"baseUrl": "https://example.com"}}],
    "rowConfigurationExamples": [],
}

SQL_TRANSFORM_RESPONSE: dict[str, Any] = {
    "componentId": "keboola.snowflake-transformation",
    "componentName": "Snowflake SQL",
    "componentType": "transformation",
    "componentCategories": [],
    "componentFlags": ["genericDockerUI-tableInput", "genericDockerUI-tableOutput"],
    "description": "Snowflake SQL transformation",
    "longDescription": "",
    "documentationUrl": "",
    "documentation": "",
    "configurationSchema": {},
    "configurationRowSchema": {},
    "rootConfigurationExamples": [],
    "rowConfigurationExamples": [],
}

PYTHON_TRANSFORM_RESPONSE: dict[str, Any] = {
    "componentId": "keboola.python-transformation-v2",
    "componentName": "Python",
    "componentType": "transformation",
    "componentCategories": [],
    "componentFlags": ["genericDockerUI-tableInput", "genericDockerUI-tableOutput"],
    "description": "Python transformation",
    "longDescription": "",
    "documentationUrl": "",
    "documentation": "",
    "configurationSchema": {},
    "configurationRowSchema": {},
    "rootConfigurationExamples": [],
    "rowConfigurationExamples": [],
}

CUSTOM_PYTHON_APP_RESPONSE: dict[str, Any] = {
    "componentId": "kds-team.app-custom-python",
    "componentName": "Custom Python App",
    "componentType": "application",
    "componentCategories": [],
    "componentFlags": [],
    "description": "Run custom Python code",
    "longDescription": "",
    "documentationUrl": "",
    "documentation": "",
    "configurationSchema": {},
    "configurationRowSchema": {},
    "rootConfigurationExamples": [],
    "rowConfigurationExamples": [],
}

FLOW_RESPONSE: dict[str, Any] = {
    "componentId": "keboola.flow",
    "componentName": "Conditional Flow",
    "componentType": "other",
    "componentCategories": [],
    "componentFlags": [],
    "description": "Orchestrate your pipelines",
    "longDescription": "",
    "documentationUrl": "",
    "documentation": "",
    "configurationSchema": {},
    "configurationRowSchema": {},
    "rootConfigurationExamples": [],
    "rowConfigurationExamples": [],
}

DB_EXTRACTOR_RESPONSE: dict[str, Any] = {
    "componentId": "keboola.ex-db-snowflake",
    "componentName": "Snowflake",
    "componentType": "extractor",
    "componentCategories": ["Database"],
    "componentFlags": [],
    "description": "Extract from Snowflake",
    "longDescription": "",
    "documentationUrl": "",
    "documentation": "",
    "configurationSchema": {},
    "configurationRowSchema": {},
    "rootConfigurationExamples": [
        {
            "parameters": {
                "db": {
                    "host": "example.com",
                    "port": 443,
                    "#password": "<secret>",
                    "user": "admin",
                }
            }
        }
    ],
    "rowConfigurationExamples": [{"parameters": {"outputTable": "out.c-main.table"}}],
}

SCHEMA_ONLY_RESPONSE: dict[str, Any] = {
    "componentId": "keboola.ex-generic",
    "componentName": "Generic",
    "componentType": "extractor",
    "componentCategories": [],
    "componentFlags": [],
    "description": "Generic extractor",
    "longDescription": "",
    "documentationUrl": "",
    "documentation": "",
    "configurationSchema": {
        "type": "object",
        "properties": {
            "baseUrl": {"type": "string", "default": "https://api.example.com"},
            "#token": {"type": "string"},
            "retries": {"type": "integer", "default": 5},
        },
    },
    "configurationRowSchema": {},
    "rootConfigurationExamples": [],
    "rowConfigurationExamples": [],
}

EMPTY_SCHEMA_RESPONSE: dict[str, Any] = {
    "componentId": "keboola.ex-empty",
    "componentName": "Empty",
    "componentType": "extractor",
    "componentCategories": [],
    "componentFlags": [],
    "description": "No schema",
    "longDescription": "",
    "documentationUrl": "",
    "documentation": "",
    "configurationSchema": {},
    "configurationRowSchema": {},
    "rootConfigurationExamples": [],
    "rowConfigurationExamples": [],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ai_client(
    detail_response: dict[str, Any] | None = None,
    suggest_response: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """Create a mock AiServiceClient."""
    mock = MagicMock()
    if detail_response is not None:
        mock.get_component_detail.return_value = detail_response
    if suggest_response is not None:
        mock.suggest_components.return_value = suggest_response
    return mock


def _make_storage_client(
    components: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """Create a mock KeboolaClient that returns list_components results."""
    mock = MagicMock()
    mock.list_components.return_value = components or []
    return mock


def _make_service(
    tmp_config_dir: Path,
    ai_client: MagicMock | None = None,
    storage_client: MagicMock | None = None,
    alias: str = "prod",
) -> ComponentService:
    """Create a ComponentService with pre-configured mocks."""
    store = setup_single_project(tmp_config_dir, alias=alias)
    return ComponentService(
        config_store=store,
        client_factory=(lambda url, token: storage_client) if storage_client else None,
        ai_client_factory=(lambda url, token: ai_client) if ai_client else None,
    )


# ===========================================================================
# list_components
# ===========================================================================


class TestListComponentsViaStorage:
    """Tests for list_components without query (Storage API path)."""

    def test_list_components_via_storage(self, tmp_config_dir: Path) -> None:
        """Without query, list_components uses Storage API and deduplicates."""
        raw_components = [
            {
                "id": "keboola.ex-http",
                "name": "HTTP",
                "type": "extractor",
                "categories": ["API"],
                "description": "Download CSV files",
            },
            {
                "id": "keboola.ex-http",
                "name": "HTTP",
                "type": "extractor",
                "categories": ["API"],
                "description": "Download CSV files",
            },
            {
                "id": "keboola.snowflake-transformation",
                "name": "Snowflake SQL",
                "type": "transformation",
                "categories": [],
                "description": "Snowflake SQL transformation",
            },
        ]
        mock_client = _make_storage_client(raw_components)
        service = _make_service(tmp_config_dir, storage_client=mock_client)

        result = service.list_components()

        assert len(result["errors"]) == 0, "Expected no errors"
        components = result["components"]
        assert len(components) == 2, "Expected 2 unique components after dedup"

        component_ids = [c["component_id"] for c in components]
        assert "keboola.ex-http" in component_ids
        assert "keboola.snowflake-transformation" in component_ids
        mock_client.close.assert_called_once()

    def test_list_components_with_type_filter(self, tmp_config_dir: Path) -> None:
        """With component_type filter, only matching components are returned."""
        raw_components = [
            {
                "id": "keboola.ex-http",
                "name": "HTTP",
                "type": "extractor",
                "categories": [],
                "description": "Extractor",
            },
        ]
        mock_client = _make_storage_client(raw_components)
        service = _make_service(tmp_config_dir, storage_client=mock_client)

        result = service.list_components(component_type="extractor")

        assert len(result["errors"]) == 0
        mock_client.list_components.assert_called_once_with(component_type="extractor")


class TestListComponentsViaAi:
    """Tests for list_components with query (AI Service path)."""

    def test_list_components_via_ai_query(self, tmp_config_dir: Path) -> None:
        """With query, list_components uses AI suggest then enriches with detail."""
        suggestions = [
            {"componentId": "keboola.ex-http", "score": 0.95, "source": "name"},
            {"componentId": "keboola.ex-db-snowflake", "score": 0.80, "source": "desc"},
        ]
        mock_ai = _make_ai_client(suggest_response=suggestions)
        # Return different details per call
        mock_ai.get_component_detail.side_effect = [
            EXTRACTOR_RESPONSE,
            DB_EXTRACTOR_RESPONSE,
        ]

        service = _make_service(tmp_config_dir, ai_client=mock_ai)
        result = service.list_components(query="download data from http")

        assert len(result["errors"]) == 0, "Expected no errors"
        components = result["components"]
        assert len(components) == 2, "Expected 2 components from AI suggestions"
        assert components[0]["component_id"] == "keboola.ex-http"
        assert components[0]["score"] == 0.95
        assert components[1]["component_id"] == "keboola.ex-db-snowflake"
        mock_ai.suggest_components.assert_called_once_with("download data from http")
        mock_ai.close.assert_called_once()


# ===========================================================================
# get_component_detail
# ===========================================================================


class TestGetComponentDetail:
    """Tests for get_component_detail."""

    def test_get_component_detail_success(self, tmp_config_dir: Path) -> None:
        """Returns parsed detail with schema_summary for a valid component."""
        mock_ai = _make_ai_client(detail_response=EXTRACTOR_RESPONSE)
        service = _make_service(tmp_config_dir, ai_client=mock_ai)

        result = service.get_component_detail(alias="prod", component_id="keboola.ex-http")

        assert result["component_id"] == "keboola.ex-http"
        assert result["component_name"] == "HTTP"
        assert result["component_type"] == "extractor"
        assert result["categories"] == ["API"]
        assert result["description"] == "Download CSV files"
        assert (
            result["documentation_url"]
            == "https://help.keboola.com/components/extractors/other/http/"
        )
        assert result["project_alias"] == "prod"

        # Schema summary
        schema_summary = result["schema_summary"]
        assert schema_summary["property_count"] == 1, "Expected 1 property (baseUrl)"
        assert schema_summary["required_count"] == 1, "Expected 1 required field"
        assert schema_summary["has_row_schema"] is False

        assert result["examples_count"] == 1
        assert result["row_examples_count"] == 0

        mock_ai.get_component_detail.assert_called_once_with("keboola.ex-http")
        mock_ai.close.assert_called_once()

    def test_get_component_detail_not_found(self, tmp_config_dir: Path) -> None:
        """Raises KeboolaApiError when AI service returns 404."""
        mock_ai = MagicMock()
        mock_ai.get_component_detail.side_effect = KeboolaApiError(
            message="Component not found",
            status_code=404,
            error_code="NOT_FOUND",
            retryable=False,
        )
        service = _make_service(tmp_config_dir, ai_client=mock_ai)

        with pytest.raises(KeboolaApiError) as exc_info:
            service.get_component_detail(alias="prod", component_id="nonexistent.component")

        assert exc_info.value.error_code == "NOT_FOUND"
        assert exc_info.value.status_code == 404
        mock_ai.close.assert_called_once()


# ===========================================================================
# generate_scaffold
# ===========================================================================


class TestGenerateScaffold:
    """Tests for generate_scaffold."""

    def test_scaffold_extractor(self, tmp_config_dir: Path) -> None:
        """Generic extractor generates only _config.yml."""
        mock_ai = _make_ai_client(detail_response=EXTRACTOR_RESPONSE)
        service = _make_service(tmp_config_dir, ai_client=mock_ai)

        result = service.generate_scaffold(alias="prod", component_id="keboola.ex-http")

        assert result["component_id"] == "keboola.ex-http"
        assert result["component_type"] == "extractor"
        assert result["config_name"] == "HTTP Configuration"

        files = result["files"]
        assert len(files) == 1, "Generic extractor should produce exactly 1 file"
        assert files[0]["path"] == "_config.yml"

        # Verify generated YAML is parseable
        parsed = yaml.safe_load(files[0]["content"])
        assert parsed is not None, "Generated _config.yml must be valid YAML"
        assert parsed["version"] == 2
        assert parsed["name"] == "HTTP Configuration"

        mock_ai.close.assert_called_once()

    def test_scaffold_sql_transformation(self, tmp_config_dir: Path) -> None:
        """SQL transformation generates _config.yml and transform.sql."""
        mock_ai = _make_ai_client(detail_response=SQL_TRANSFORM_RESPONSE)
        service = _make_service(tmp_config_dir, ai_client=mock_ai)

        result = service.generate_scaffold(
            alias="prod", component_id="keboola.snowflake-transformation"
        )

        file_paths = [f["path"] for f in result["files"]]
        assert "_config.yml" in file_paths, "Must include _config.yml"
        assert "transform.sql" in file_paths, "Must include transform.sql"
        assert len(result["files"]) == 2

        # Verify SQL file has expected boilerplate
        sql_file = next(f for f in result["files"] if f["path"] == "transform.sql")
        assert "SELECT 1;" in sql_file["content"]
        assert "BLOCK: 001-main" in sql_file["content"]

    def test_scaffold_python_transformation(self, tmp_config_dir: Path) -> None:
        """Python transformation generates _config.yml and transform.py (no pyproject.toml)."""
        mock_ai = _make_ai_client(detail_response=PYTHON_TRANSFORM_RESPONSE)
        service = _make_service(tmp_config_dir, ai_client=mock_ai)

        result = service.generate_scaffold(
            alias="prod", component_id="keboola.python-transformation-v2"
        )

        file_paths = [f["path"] for f in result["files"]]
        assert "_config.yml" in file_paths, "Must include _config.yml"
        assert "transform.py" in file_paths, "Must include transform.py"
        assert "pyproject.toml" not in file_paths, (
            "Python transformation should NOT include pyproject.toml"
        )
        assert len(result["files"]) == 2

        # Verify Python file has expected boilerplate
        py_file = next(f for f in result["files"] if f["path"] == "transform.py")
        assert "CommonInterface" in py_file["content"]
        assert "BLOCK: 001-main" in py_file["content"]

    def test_scaffold_custom_python_app(self, tmp_config_dir: Path) -> None:
        """Custom Python app generates _config.yml, code.py, and pyproject.toml."""
        mock_ai = _make_ai_client(detail_response=CUSTOM_PYTHON_APP_RESPONSE)
        service = _make_service(tmp_config_dir, ai_client=mock_ai)

        result = service.generate_scaffold(alias="prod", component_id="kds-team.app-custom-python")

        file_paths = [f["path"] for f in result["files"]]
        assert "_config.yml" in file_paths, "Must include _config.yml"
        assert "code.py" in file_paths, "Must include code.py"
        assert "pyproject.toml" in file_paths, "Must include pyproject.toml"
        assert len(result["files"]) == 3

        # Verify code.py boilerplate
        code_file = next(f for f in result["files"] if f["path"] == "code.py")
        assert "CommonInterface" in code_file["content"]
        assert "logging" in code_file["content"]

        # Verify pyproject.toml
        toml_file = next(f for f in result["files"] if f["path"] == "pyproject.toml")
        assert "[project]" in toml_file["content"]
        assert 'requires-python = ">=3.11"' in toml_file["content"]

    def test_scaffold_flow(self, tmp_config_dir: Path) -> None:
        """keboola.flow generates a conditional-flow _config.yml with phases + tasks."""
        mock_ai = _make_ai_client(detail_response=FLOW_RESPONSE)
        service = _make_service(tmp_config_dir, ai_client=mock_ai)

        result = service.generate_scaffold(alias="prod", component_id="keboola.flow")

        files = result["files"]
        assert len(files) == 1, "Flow should produce exactly 1 file"
        assert files[0]["path"] == "_config.yml"

        content = files[0]["content"]
        assert "phases:" in content, "Flow config must contain phases section"
        assert "tasks:" in content, "Flow config must contain tasks section"
        assert "goto:" in content, "Flow config must use goto transitions"
        assert "dependsOn" not in content, "Conditional flows do not use dependsOn"

        # Verify it's valid YAML with string ids
        parsed = yaml.safe_load(content)
        assert parsed["phases"][0]["id"] == "phase-1"
        assert parsed["tasks"][0]["phase"] == "phase-1"
        assert parsed["tasks"][0]["task"]["type"] == "job"

    def test_scaffold_with_secrets(self, tmp_config_dir: Path) -> None:
        """Parameters with #password are masked to SECRET_PLACEHOLDER."""
        mock_ai = _make_ai_client(detail_response=DB_EXTRACTOR_RESPONSE)
        service = _make_service(tmp_config_dir, ai_client=mock_ai)

        result = service.generate_scaffold(alias="prod", component_id="keboola.ex-db-snowflake")

        config_file = next(f for f in result["files"] if f["path"] == "_config.yml")
        content = config_file["content"]

        # Secret should be masked
        assert SECRET_PLACEHOLDER in content, (
            "Secret values must be replaced with SECRET_PLACEHOLDER"
        )
        assert "<secret>" not in content, "Raw secret markers must not appear in output"

        # Non-secret values should be preserved
        assert "example.com" in content
        assert "admin" in content

    def test_scaffold_from_examples(self, tmp_config_dir: Path) -> None:
        """Uses rootConfigurationExamples when available for parameters."""
        mock_ai = _make_ai_client(detail_response=EXTRACTOR_RESPONSE)
        service = _make_service(tmp_config_dir, ai_client=mock_ai)

        result = service.generate_scaffold(alias="prod", component_id="keboola.ex-http")

        config_file = next(f for f in result["files"] if f["path"] == "_config.yml")
        parsed = yaml.safe_load(config_file["content"])

        assert "parameters" in parsed, "Config must have parameters section"
        assert parsed["parameters"]["baseUrl"] == "https://example.com", (
            "Parameters should come from rootConfigurationExamples"
        )

    def test_scaffold_from_schema(self, tmp_config_dir: Path) -> None:
        """Uses configurationSchema when no examples are available."""
        mock_ai = _make_ai_client(detail_response=SCHEMA_ONLY_RESPONSE)
        service = _make_service(tmp_config_dir, ai_client=mock_ai)

        result = service.generate_scaffold(alias="prod", component_id="keboola.ex-generic")

        config_file = next(f for f in result["files"] if f["path"] == "_config.yml")
        parsed = yaml.safe_load(config_file["content"])

        assert "parameters" in parsed, "Config must have parameters section"
        params = parsed["parameters"]
        assert params["baseUrl"] == "https://api.example.com", (
            "String default should come from schema"
        )
        assert params["#token"] == SECRET_PLACEHOLDER, "Secret properties should be masked"
        assert params["retries"] == 5, "Integer default should come from schema"

    def test_scaffold_empty(self, tmp_config_dir: Path) -> None:
        """No schema and no examples produces empty parameters dict."""
        mock_ai = _make_ai_client(detail_response=EMPTY_SCHEMA_RESPONSE)
        service = _make_service(tmp_config_dir, ai_client=mock_ai)

        result = service.generate_scaffold(alias="prod", component_id="keboola.ex-empty")

        config_file = next(f for f in result["files"] if f["path"] == "_config.yml")
        content = config_file["content"]
        assert "parameters: {}" in content, "Empty schema/examples should produce 'parameters: {}'"

    def test_scaffold_custom_name(self, tmp_config_dir: Path) -> None:
        """Custom name is used in config and directory path."""
        mock_ai = _make_ai_client(detail_response=EXTRACTOR_RESPONSE)
        service = _make_service(tmp_config_dir, ai_client=mock_ai)

        result = service.generate_scaffold(
            alias="prod",
            component_id="keboola.ex-http",
            name="My Custom HTTP Extractor",
        )

        assert result["config_name"] == "My Custom HTTP Extractor"
        assert "my-custom-http-extractor" in result["directory"]

        config_file = next(f for f in result["files"] if f["path"] == "_config.yml")
        parsed = yaml.safe_load(config_file["content"])
        assert parsed["name"] == "My Custom HTTP Extractor"

    def test_scaffold_directory_path(self, tmp_config_dir: Path) -> None:
        """Generated directory path follows convention: type/component_id/slugified-name."""
        mock_ai = _make_ai_client(detail_response=EXTRACTOR_RESPONSE)
        service = _make_service(tmp_config_dir, ai_client=mock_ai)

        result = service.generate_scaffold(alias="prod", component_id="keboola.ex-http")

        assert result["directory"].startswith("extractor/keboola.ex-http/")

    def test_scaffold_storage_mappings(self, tmp_config_dir: Path) -> None:
        """Components with tableInput/tableOutput flags get storage mappings."""
        mock_ai = _make_ai_client(detail_response=SQL_TRANSFORM_RESPONSE)
        service = _make_service(tmp_config_dir, ai_client=mock_ai)

        result = service.generate_scaffold(
            alias="prod", component_id="keboola.snowflake-transformation"
        )

        config_file = next(f for f in result["files"] if f["path"] == "_config.yml")
        content = config_file["content"]
        assert "storage:" in content, "Components with table flags must have storage section"
        assert "input:" in content
        assert "output:" in content


# ===========================================================================
# Helper functions (unit tests for private functions)
# ===========================================================================


class TestDetectComponentCategory:
    """Tests for _detect_component_category."""

    @pytest.mark.parametrize(
        "component_id, expected",
        [
            ("keboola.snowflake-transformation", "sql_transformation"),
            ("keboola.synapse-transformation", "sql_transformation"),
            ("keboola.redshift-transformation", "sql_transformation"),
            ("keboola.bigquery-transformation", "sql_transformation"),
            ("keboola.python-transformation-v2", "python_transformation"),
            ("kds-team.app-custom-python", "custom_python"),
            ("keboola.orchestrator", "generic"),
            ("keboola.flow", "flow"),
            ("keboola.ex-http", "generic"),
            ("keboola.ex-db-snowflake", "generic"),
            ("keboola.wr-google-sheets", "generic"),
        ],
    )
    def test_detect_component_category(self, component_id: str, expected: str) -> None:
        """_detect_component_category returns correct category for various IDs."""
        assert _detect_component_category(component_id) == expected, (
            f"Expected '{expected}' for component_id='{component_id}'"
        )


class TestMaskSecrets:
    """Tests for _mask_secrets."""

    def test_mask_secrets_flat(self) -> None:
        """Top-level keys starting with # are masked."""
        data = {"host": "example.com", "#password": "secret123", "user": "admin"}
        result = _mask_secrets(data)
        assert result["host"] == "example.com"
        assert result["#password"] == SECRET_PLACEHOLDER
        assert result["user"] == "admin"

    def test_mask_secrets_nested(self) -> None:
        """Nested dicts with # keys and <secret> values are recursively masked."""
        data = {
            "db": {
                "host": "example.com",
                "#password": "mypass",
                "nested": {"#token": "tok123", "name": "test"},
            }
        }
        result = _mask_secrets(data)
        assert result["db"]["host"] == "example.com"
        assert result["db"]["#password"] == SECRET_PLACEHOLDER
        assert result["db"]["nested"]["#token"] == SECRET_PLACEHOLDER
        assert result["db"]["nested"]["name"] == "test"

    def test_mask_secrets_value_placeholder(self) -> None:
        """String values equal to '<secret>' are replaced regardless of key name."""
        data = {"password": "<secret>", "other": "normal"}
        result = _mask_secrets(data)
        assert result["password"] == SECRET_PLACEHOLDER
        assert result["other"] == "normal"

    def test_mask_secrets_list(self) -> None:
        """Lists of dicts are recursively processed."""
        data = {"items": [{"#key": "secret"}, {"value": "ok"}]}
        result = _mask_secrets(data)
        assert result["items"][0]["#key"] == SECRET_PLACEHOLDER
        assert result["items"][1]["value"] == "ok"

    def test_mask_secrets_empty(self) -> None:
        """Empty dict returns empty dict."""
        assert _mask_secrets({}) == {}

    def test_mask_secrets_scalar(self) -> None:
        """Scalar values pass through unchanged."""
        assert _mask_secrets("hello") == "hello"
        assert _mask_secrets(42) == 42
        assert _mask_secrets(None) is None


class TestGenerateFromSchema:
    """Tests for _generate_from_schema."""

    def test_string_with_default(self) -> None:
        """String property with default uses the default value."""
        schema = {"properties": {"url": {"type": "string", "default": "https://api.example.com"}}}
        result = _generate_from_schema(schema)
        assert result["url"] == "https://api.example.com"

    def test_string_without_default(self) -> None:
        """String property without default uses empty string."""
        schema = {"properties": {"url": {"type": "string"}}}
        result = _generate_from_schema(schema)
        assert result["url"] == ""

    def test_integer_with_default(self) -> None:
        """Integer property with default uses the default value."""
        schema = {"properties": {"retries": {"type": "integer", "default": 3}}}
        result = _generate_from_schema(schema)
        assert result["retries"] == 3

    def test_integer_without_default(self) -> None:
        """Integer property without default uses 0."""
        schema = {"properties": {"retries": {"type": "integer"}}}
        result = _generate_from_schema(schema)
        assert result["retries"] == 0

    def test_boolean_with_default(self) -> None:
        """Boolean property with default uses the default value."""
        schema = {"properties": {"enabled": {"type": "boolean", "default": True}}}
        result = _generate_from_schema(schema)
        assert result["enabled"] is True

    def test_boolean_without_default(self) -> None:
        """Boolean property without default uses False."""
        schema = {"properties": {"enabled": {"type": "boolean"}}}
        result = _generate_from_schema(schema)
        assert result["enabled"] is False

    def test_array_with_default(self) -> None:
        """Array property with default uses the default value."""
        schema = {"properties": {"tags": {"type": "array", "default": ["a", "b"]}}}
        result = _generate_from_schema(schema)
        assert result["tags"] == ["a", "b"]

    def test_array_without_default(self) -> None:
        """Array property without default uses empty list."""
        schema = {"properties": {"tags": {"type": "array"}}}
        result = _generate_from_schema(schema)
        assert result["tags"] == []

    def test_nested_object(self) -> None:
        """Object property with nested properties recurses."""
        schema = {
            "properties": {
                "db": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string", "default": "localhost"},
                        "port": {"type": "integer", "default": 5432},
                    },
                }
            }
        }
        result = _generate_from_schema(schema)
        assert result["db"]["host"] == "localhost"
        assert result["db"]["port"] == 5432

    def test_object_without_properties(self) -> None:
        """Object property without nested properties uses default or empty dict."""
        schema = {"properties": {"extra": {"type": "object"}}}
        result = _generate_from_schema(schema)
        assert result["extra"] == {}

    def test_secret_property(self) -> None:
        """Properties starting with # are masked."""
        schema = {"properties": {"#apiKey": {"type": "string"}}}
        result = _generate_from_schema(schema)
        assert result["#apiKey"] == SECRET_PLACEHOLDER

    def test_empty_schema(self) -> None:
        """Schema with no properties returns empty dict."""
        result = _generate_from_schema({})
        assert result == {}

    def test_number_type(self) -> None:
        """Number type (float) uses default or 0."""
        schema = {"properties": {"threshold": {"type": "number", "default": 0.5}}}
        result = _generate_from_schema(schema)
        assert result["threshold"] == 0.5


# ===========================================================================
# stamp_scaffold_config_id + build_pushed_config_files (issue #644)
# ===========================================================================


_SCAFFOLD_YML = (
    "# Component: HTTP (keboola.ex-http)\n"
    "# Type: extractor\n"
    "#\n"
    "# NOTE: config_id will be assigned by Keboola on first push\n"
    "version: 2\n"
    'name: "test-config"\n'
    "description: |\n"
    "  TODO: describe this configuration\n"
    "\n"
    "parameters: {}\n"
    "\n"
    "_keboola:\n"
    "  component_id: keboola.ex-http\n"
    "\n"
)


class TestStampScaffoldConfigId:
    """The --push path must record the created config's ID in the scaffold.

    Without the ID the next ``sync push`` treats the directory as a brand-new
    configuration and creates a duplicate (issue #644).
    """

    def _scaffold(self) -> dict[str, Any]:
        return {
            "component_id": "keboola.ex-http",
            "component_name": "HTTP",
            "component_type": "extractor",
            "directory": "extractor/keboola.ex-http/test-config",
            "files": [
                {"path": "_config.yml", "content": _SCAFFOLD_YML},
                {"path": "transform.sql", "content": "SELECT 1;\n"},
            ],
        }

    def test_stamps_id_into_existing_keboola_block(self) -> None:
        from keboola_agent_cli.services.component_service import stamp_scaffold_config_id

        result = stamp_scaffold_config_id(self._scaffold(), "01m0njbrqwpyqbx0yqfqq9pyen")
        content = result["files"][0]["content"]
        parsed = yaml.safe_load(content)
        assert parsed["_keboola"]["component_id"] == "keboola.ex-http"
        assert parsed["_keboola"]["config_id"] == "01m0njbrqwpyqbx0yqfqq9pyen"
        # The misleading "first push" note must be gone on this path.
        assert "assigned by Keboola on first push" not in content

    def test_numeric_id_stays_a_string(self) -> None:
        """Legacy numeric config IDs must round-trip as YAML strings.

        An unquoted ``config_id: 12345`` parses as int and then never matches
        the string-keyed remote lookup in the sync diff adopt-by-id guard.
        """
        from keboola_agent_cli.services.component_service import stamp_scaffold_config_id

        result = stamp_scaffold_config_id(self._scaffold(), "12345")
        parsed = yaml.safe_load(result["files"][0]["content"])
        assert parsed["_keboola"]["config_id"] == "12345"
        assert isinstance(parsed["_keboola"]["config_id"], str)

    def test_appends_block_when_keboola_missing(self) -> None:
        """Flow scaffolds have no _keboola block (issue #650); stamping must
        create one so the pushed flow is adoptable too."""
        from keboola_agent_cli.services.component_service import stamp_scaffold_config_id

        scaffold = self._scaffold()
        scaffold["files"][0]["content"] = 'name: "my flow"\nphases: []\n'
        result = stamp_scaffold_config_id(scaffold, "999")
        parsed = yaml.safe_load(result["files"][0]["content"])
        assert parsed["_keboola"]["component_id"] == "keboola.ex-http"
        assert parsed["_keboola"]["config_id"] == "999"

    def test_stamps_real_generator_output_with_single_keboola_block(
        self, tmp_config_dir: Path
    ) -> None:
        """Guard against builder-format drift: feed REAL _build_config_yml
        output through the stamper (PR #653 review) -- a renamed or
        reordered _keboola block would silently fall into the append path
        and produce a duplicate block."""
        from keboola_agent_cli.services.component_service import stamp_scaffold_config_id

        mock_ai = _make_ai_client(detail_response=EXTRACTOR_RESPONSE)
        service = _make_service(tmp_config_dir, ai_client=mock_ai)
        scaffold = service.generate_scaffold(alias="prod", component_id="keboola.ex-http")

        stamped = stamp_scaffold_config_id(scaffold, "01m0njbrqwpyqbx0yqfqq9pyen")

        content = stamped["files"][0]["content"]
        assert content.count("_keboola:") == 1, "stamping must not append a second block"
        parsed = yaml.safe_load(content)
        assert parsed["_keboola"]["component_id"] == "keboola.ex-http"
        assert parsed["_keboola"]["config_id"] == "01m0njbrqwpyqbx0yqfqq9pyen"
        assert "assigned by Keboola on first push" not in content

    def test_companion_files_untouched_and_input_not_mutated(self) -> None:
        from keboola_agent_cli.services.component_service import stamp_scaffold_config_id

        scaffold = self._scaffold()
        original_yml = scaffold["files"][0]["content"]
        result = stamp_scaffold_config_id(scaffold, "12345")
        assert result["files"][1]["content"] == "SELECT 1;\n"
        # Pure function: the input scaffold must not be mutated.
        assert scaffold["files"][0]["content"] == original_yml


class TestMaterializePushedConfig:
    """When --configuration was pushed, the local dir must mirror the pushed
    body exactly the way ``sync pull`` would materialize it -- placeholder
    scaffolding would make the next ``sync push`` overwrite the real remote
    config with TODO templates, and inline-only YAML would drop the code
    files pull-based trees carry."""

    def test_mirrors_pushed_body(self, tmp_path: Path) -> None:
        from keboola_agent_cli.services.component_service import materialize_pushed_config

        written = materialize_pushed_config(
            component_id="keboola.ex-http",
            config_id="12345",
            name="test-config",
            description="desc",
            configuration={
                "parameters": {
                    "baseUrl": "https://example.com",
                    "#token": "KBC::ProjectSecure::abc",
                },
                "storage": {"input": {"tables": [{"source": "in.c-b.t"}]}},
            },
            config_dir=tmp_path,
        )
        # _description.md is pull-parity: sync pull extracts a non-empty
        # description into a companion file the same way.
        assert written == ["_config.yml", "_description.md"]
        parsed = yaml.safe_load((tmp_path / "_config.yml").read_text(encoding="utf-8"))
        assert parsed["name"] == "test-config"
        assert parsed["parameters"]["baseUrl"] == "https://example.com"
        # Encrypted value travels verbatim -- never decrypted, never a TODO.
        assert parsed["parameters"]["#token"] == "KBC::ProjectSecure::abc"
        assert parsed["input"] == {"tables": [{"source": "in.c-b.t"}]}
        assert parsed["_keboola"] == {
            "component_id": "keboola.ex-http",
            "config_id": "12345",
        }

    def test_transformation_body_extracts_real_code(self, tmp_path: Path) -> None:
        """A pushed SQL body must yield a real transform.sql (like sync pull),
        NOT a placeholder -- and not disappear entirely (PR #653 review)."""
        from keboola_agent_cli.services.component_service import materialize_pushed_config

        written = materialize_pushed_config(
            component_id="keboola.snowflake-transformation",
            config_id="67890",
            name="tf",
            description="",
            configuration={
                "parameters": {
                    "blocks": [
                        {
                            "name": "Blocks",
                            "codes": [{"name": "Code", "script": ["SELECT 1;"]}],
                        }
                    ]
                }
            },
            config_dir=tmp_path,
        )
        assert "transform.sql" in written and "_config.yml" in written
        sql = (tmp_path / "transform.sql").read_text(encoding="utf-8")
        assert "SELECT 1;" in sql
        assert "TODO" not in sql

    def test_null_parameters_body_does_not_crash(self, tmp_path: Path) -> None:
        """'{"parameters": null}' is accepted by the Storage API; the local
        materialization must not crash after the remote create succeeded
        (PR #653 review sweep)."""
        from keboola_agent_cli.services.component_service import materialize_pushed_config

        written = materialize_pushed_config(
            component_id="keboola.snowflake-transformation",
            config_id="123",
            name="tf",
            description="",
            configuration={"parameters": None},
            config_dir=tmp_path,
        )
        assert "_config.yml" in written

    def test_stale_files_not_reported_and_stale_description_removed(self, tmp_path: Path) -> None:
        """The slugified dir can pre-exist: stray files must not be reported
        as written, and a stale _description.md must not misattribute to the
        new config when the pushed description is empty."""
        from keboola_agent_cli.services.component_service import materialize_pushed_config

        (tmp_path / "_description.md").write_text("old description", encoding="utf-8")
        (tmp_path / "leftover_notes.txt").write_text("stray", encoding="utf-8")

        written = materialize_pushed_config(
            component_id="keboola.ex-http",
            config_id="123",
            name="cfg",
            description="",
            configuration={"parameters": {"a": 1}},
            config_dir=tmp_path,
        )
        assert written == ["_config.yml"]
        assert not (tmp_path / "_description.md").exists()
        # Stray unrelated files are left alone -- just not claimed as ours.
        assert (tmp_path / "leftover_notes.txt").exists()
