"""Tests for `kbagent flow examples` and the bundled flow resources (issue #397).

Covers the L2 loaders (``get_flow_examples`` / ``get_bundled_flow_schema``),
the new ``flow examples`` CLI command, and the drift guard that pins the
hand-written ``flow schema`` YAML template to the authoritative bundled
conditional-flow JSON Schema.
"""

from __future__ import annotations

import json

import jsonschema
import pytest
import yaml
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.commands.flow import _FLOW_SCHEMA
from keboola_agent_cli.services.flow_service import (
    FLOW_COMPONENT_ID,
    LEGACY_FLOW_COMPONENT_ID,
    get_bundled_flow_schema,
    get_flow_examples,
)

runner = CliRunner()


def _validator(schema: dict) -> jsonschema.protocols.Validator:
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    return validator_cls(schema)


# ---------------------------------------------------------------------------
# L2: get_flow_examples
# ---------------------------------------------------------------------------


class TestGetFlowExamples:
    def test_conditional_examples_parse(self) -> None:
        examples = get_flow_examples(FLOW_COMPONENT_ID)
        assert len(examples) >= 1
        for example in examples:
            assert isinstance(example, dict)
            assert isinstance(example.get("phases"), list)
            assert isinstance(example.get("tasks"), list)

    def test_legacy_examples_parse(self) -> None:
        examples = get_flow_examples(LEGACY_FLOW_COMPONENT_ID)
        assert len(examples) >= 1
        for example in examples:
            assert isinstance(example, dict)
            assert isinstance(example.get("phases"), list)
            assert isinstance(example.get("tasks"), list)

    def test_default_component_is_conditional(self) -> None:
        assert get_flow_examples() == get_flow_examples(FLOW_COMPONENT_ID)

    def test_unknown_component_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"keboola\.flow"):
            get_flow_examples("keboola.does-not-exist")


# ---------------------------------------------------------------------------
# L2: get_bundled_flow_schema
# ---------------------------------------------------------------------------


class TestGetBundledFlowSchema:
    def test_conditional_schema_is_valid_json_schema(self) -> None:
        schema = get_bundled_flow_schema(FLOW_COMPONENT_ID)
        _validator(schema)  # check_schema raises on an invalid schema
        assert "phases" in schema["properties"]
        assert "tasks" in schema["properties"]
        assert "retryConfiguration" in schema["definitions"]

    def test_legacy_schema_is_valid_json_schema(self) -> None:
        schema = get_bundled_flow_schema(LEGACY_FLOW_COMPONENT_ID)
        _validator(schema)
        assert "tasks" in schema["properties"]

    def test_default_component_is_conditional(self) -> None:
        assert get_bundled_flow_schema() == get_bundled_flow_schema(FLOW_COMPONENT_ID)

    def test_unknown_component_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"keboola\.orchestrator"):
            get_bundled_flow_schema("keboola.does-not-exist")

    def test_yaml_template_validates_against_bundled_schema(self) -> None:
        # Drift guard (issue #397): the hand-written `flow schema` authoring
        # template must satisfy the authoritative bundled schema. Before the
        # fix it drifted: retryOn used bare strings (schema requires
        # {type, value} objects) and strategyParams used delaySeconds
        # (schema key is delay, plus maxRetries).
        schema = get_bundled_flow_schema(FLOW_COMPONENT_ID)
        template_doc = yaml.safe_load(_FLOW_SCHEMA)
        errors = [e.message for e in _validator(schema).iter_errors(template_doc)]
        assert errors == []

    def test_legacy_examples_validate_against_legacy_schema(self) -> None:
        schema = get_bundled_flow_schema(LEGACY_FLOW_COMPONENT_ID)
        validator = _validator(schema)
        for index, example in enumerate(get_flow_examples(LEGACY_FLOW_COMPONENT_ID)):
            errors = [e.message for e in validator.iter_errors(example)]
            assert errors == [], f"legacy example {index} does not match the bundled schema"


# ---------------------------------------------------------------------------
# CLI: kbagent flow examples
# ---------------------------------------------------------------------------


class TestFlowExamplesCommand:
    def test_examples_default_json_is_list_of_conditional_examples(self) -> None:
        result = runner.invoke(app, ["--json", "flow", "examples"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert isinstance(payload["data"], list)
        assert payload["data"] == get_flow_examples(FLOW_COMPONENT_ID)

    def test_examples_explicit_conditional_json(self) -> None:
        result = runner.invoke(
            app, ["--json", "flow", "examples", "--component-id", FLOW_COMPONENT_ID]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"] == get_flow_examples(FLOW_COMPONENT_ID)

    def test_examples_orchestrator_json(self) -> None:
        result = runner.invoke(
            app, ["--json", "flow", "examples", "--component-id", LEGACY_FLOW_COMPONENT_ID]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"] == get_flow_examples(LEGACY_FLOW_COMPONENT_ID)

    def test_examples_human_output_numbered_blocks(self) -> None:
        result = runner.invoke(app, ["flow", "examples"])
        assert result.exit_code == 0, result.output
        assert "keboola.flow" in result.output
        assert "1. Flow Configuration:" in result.output
        # No legacy note on the conditional (default) path.
        assert "cannot create or edit" not in result.output

    def test_examples_orchestrator_prints_informational_note(self) -> None:
        result = runner.invoke(
            app, ["flow", "examples", "--component-id", LEGACY_FLOW_COMPONENT_ID]
        )
        assert result.exit_code == 0, result.output
        assert "cannot create or edit" in result.output
        assert "0.57.0" in result.output

    def test_examples_unknown_component_exits_2(self) -> None:
        result = runner.invoke(app, ["flow", "examples", "--component-id", "keboola.wrong"])
        assert result.exit_code == 2
        assert "keboola.wrong" in result.output
