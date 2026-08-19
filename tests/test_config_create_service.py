"""Tests for ConfigService.create_config (the `config new --push` lifecycle).

Covers:
- Happy paths: minimal create, explicit body, description forwarding,
  branch_id resolution.
- Dry-run: planned envelope without API call.
- Schema validation: ok / failed / skipped (no-schema, AI error, malformed
  schema, validate=False).
- Empty-shell short-circuit: validation auto-skips when no body is provided.
- Cleanup: client.close() and ai_client.close() always called.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from helpers import setup_single_project
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.services.config_service import ConfigService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_CREATED = {
    "id": "12345",
    "name": "My Config",
    "description": "",
    "configuration": {},
    "version": 1,
    "created": "2026-05-11T10:00:00+00:00",
}

# A trivial JSON schema in the shape the AI Service actually returns: a
# component's ``configurationSchema`` describes the CONTENTS of the
# ``parameters`` key, NOT the whole configuration object (issue #587). A real
# writer schema says ``required: ["db"]`` while ``db`` lives at
# ``configuration.parameters.db`` -- so the body must be unwrapped before it is
# validated. This fixture previously wrapped everything in a ``parameters``
# property, a shape no component ever ships, which is why the off-by-one-level
# validation bug stayed green in CI.
TABLE_SCHEMA = {
    "type": "object",
    "properties": {"table": {"type": "string"}},
    "required": ["table"],
}

VALID_BODY = {"parameters": {"table": "orders"}}
INVALID_BODY = {"parameters": {"limit": 100}}  # missing required "table"

# A keboola.flow-style schema + body: conditional flows carry no ``parameters``
# key at all -- ``phases`` / ``tasks`` sit at the configuration root and the
# schema describes that root (see resources/flow/conditional-flow-schema.json).
FLOW_SCHEMA = {
    "type": "object",
    "properties": {"phases": {"type": "array"}, "tasks": {"type": "array"}},
    "required": ["phases", "tasks"],
}

# A schema that declares a top-level ``parameters`` property describes the
# WHOLE configuration object, so it must NOT be unwrapped. Before the #587 fix
# every body was validated whole, so such a component worked; keying the unwrap
# on the body alone would have regressed it.
WHOLE_BODY_SCHEMA = {
    "type": "object",
    "properties": {
        "parameters": {
            "type": "object",
            "properties": {"table": {"type": "string"}},
            "required": ["table"],
        }
    },
    "required": ["parameters"],
}


def _make_service(
    tmp_config_dir: Path,
    *,
    schema: dict | None = None,
    ai_raises: Exception | None = None,
) -> tuple[ConfigService, MagicMock, MagicMock]:
    """Build a ConfigService wired to a mock Storage client + mock AI client.

    Args:
        tmp_config_dir: pytest fixture providing a temp directory for ConfigStore.
        schema: when set, the mock AI client returns a component detail with
            this ``configurationSchema``. When None, the AI client returns a
            detail with no schema (the "no-schema available" branch).
        ai_raises: when set, the mock AI client raises this exception on
            ``get_component_detail`` to exercise the "AI Service error" branch.

    Returns:
        (service, mock_storage_client, mock_ai_client)
    """
    store = setup_single_project(tmp_config_dir)

    mock_storage = MagicMock()
    mock_storage.create_config.return_value = dict(SAMPLE_CREATED)

    mock_ai = MagicMock()
    if ai_raises is not None:
        mock_ai.get_component_detail.side_effect = ai_raises
    else:
        # Mirror the raw AI Service response shape (camelCase). ComponentDetail
        # parses this via its field aliases (componentId, componentName, ...).
        mock_ai.get_component_detail.return_value = {
            "componentId": "keboola.ex-db-snowflake",
            "componentName": "Snowflake Extractor",
            "componentType": "extractor",
            "configurationSchema": schema or {},
        }

    service = ConfigService(
        config_store=store,
        client_factory=lambda url, token: mock_storage,
        ai_client_factory=lambda url, token: mock_ai,
    )
    return service, mock_storage, mock_ai


# ---------------------------------------------------------------------------
# Core happy-path tests
# ---------------------------------------------------------------------------


class TestCreateConfigCore:
    def test_minimal_create_empty_shell(self, tmp_config_dir: Path) -> None:
        """No body provided => POST {} and skip validation (empty-shell mode)."""
        service, storage, ai = _make_service(tmp_config_dir, schema=TABLE_SCHEMA)

        result = service.create_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            name="My Config",
        )

        storage.create_config.assert_called_once_with(
            component_id="keboola.ex-db-snowflake",
            name="My Config",
            configuration={},
            description="",
            branch_id=None,
        )
        # AI Service must NOT be consulted for the empty-shell case -- it
        # would always fail and that's not useful for FIIA's pattern.
        ai.get_component_detail.assert_not_called()

        assert result["id"] == "12345"
        assert result["project_alias"] == "prod"
        assert result["branch_id"] is None
        assert result["validation_status"] == "skipped"
        # validation_errors is always present (symmetric with dry-run envelope).
        assert result["validation_errors"] == []

    def test_create_with_explicit_body_validates_ok(self, tmp_config_dir: Path) -> None:
        """Explicit body + valid against schema => validation_status='ok'."""
        service, storage, ai = _make_service(tmp_config_dir, schema=TABLE_SCHEMA)

        result = service.create_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            name="My Config",
            configuration=VALID_BODY,
        )

        ai.get_component_detail.assert_called_once_with("keboola.ex-db-snowflake")
        storage.create_config.assert_called_once()
        assert result["validation_status"] == "ok"

    def test_create_with_description(self, tmp_config_dir: Path) -> None:
        """Description forwards to the client call."""
        service, storage, _ = _make_service(tmp_config_dir)

        service.create_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            name="My Config",
            description="A test config",
        )

        call_kwargs = storage.create_config.call_args.kwargs
        assert call_kwargs["description"] == "A test config"

    def test_branch_id_override(self, tmp_config_dir: Path) -> None:
        """Explicit branch_id beats the project's active_branch_id."""
        service, storage, _ = _make_service(tmp_config_dir)

        result = service.create_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            name="My Config",
            branch_id=42,
        )

        assert storage.create_config.call_args.kwargs["branch_id"] == 42
        assert result["branch_id"] == 42

    def test_branch_id_falls_back_to_active(self, tmp_config_dir: Path) -> None:
        """When branch_id is None, the project's active_branch_id is used."""
        store = setup_single_project(tmp_config_dir)
        # Mutate the registered project to have an active branch.
        config = store.load()
        project = config.projects["prod"]
        project.active_branch_id = 99
        store.save(config)

        mock_storage = MagicMock()
        mock_storage.create_config.return_value = dict(SAMPLE_CREATED)
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_storage,
        )

        result = service.create_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            name="My Config",
        )

        assert mock_storage.create_config.call_args.kwargs["branch_id"] == 99
        assert result["branch_id"] == 99

    def test_unknown_project_raises_config_error(self, tmp_config_dir: Path) -> None:
        """Alias not in the config store => ConfigError, no API call."""
        service, storage, _ = _make_service(tmp_config_dir)

        with pytest.raises(ConfigError, match="not found"):
            service.create_config(
                alias="unknown",
                component_id="keboola.ex-db-snowflake",
                name="My Config",
            )
        storage.create_config.assert_not_called()

    def test_api_error_propagates_and_closes_client(self, tmp_config_dir: Path) -> None:
        """Storage API failure propagates KeboolaApiError; client.close() still called."""
        service, storage, _ = _make_service(tmp_config_dir)
        storage.create_config.side_effect = KeboolaApiError(
            message="500 boom",
            error_code="STORAGE_ERROR",
            status_code=500,
        )

        with pytest.raises(KeboolaApiError, match="500 boom"):
            service.create_config(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                name="My Config",
            )
        storage.close.assert_called_once()


# ---------------------------------------------------------------------------
# Dry-run tests
# ---------------------------------------------------------------------------


class TestCreateConfigDryRun:
    def test_dry_run_returns_planned_envelope_no_api_call(self, tmp_config_dir: Path) -> None:
        """Dry-run returns the planned POST body; no API call is made."""
        service, storage, _ = _make_service(tmp_config_dir, schema=TABLE_SCHEMA)

        result = service.create_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            name="My Config",
            configuration=VALID_BODY,
            dry_run=True,
        )

        storage.create_config.assert_not_called()
        assert result["dry_run"] is True
        assert result["project_alias"] == "prod"
        assert result["component_id"] == "keboola.ex-db-snowflake"
        assert result["name"] == "My Config"
        assert result["configuration"] == VALID_BODY
        assert result["validation_status"] == "ok"

    def test_dry_run_with_validation_failure_does_not_raise(self, tmp_config_dir: Path) -> None:
        """Dry-run + invalid body: envelope reports failure but doesn't raise."""
        service, storage, _ = _make_service(tmp_config_dir, schema=TABLE_SCHEMA)

        result = service.create_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            name="My Config",
            configuration=INVALID_BODY,
            dry_run=True,
        )

        storage.create_config.assert_not_called()
        assert result["dry_run"] is True
        assert result["validation_status"] == "failed"
        assert result["validation_errors"], "Expected non-empty validation_errors"


# ---------------------------------------------------------------------------
# Schema validation tests
# ---------------------------------------------------------------------------


class TestCreateConfigSchemaValidation:
    def test_validation_failure_aborts_real_create(self, tmp_config_dir: Path) -> None:
        """Real (non-dry-run) create with invalid body => ConfigError; no POST."""
        service, storage, _ = _make_service(tmp_config_dir, schema=TABLE_SCHEMA)

        with pytest.raises(ConfigError, match="failed schema validation"):
            service.create_config(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                name="My Config",
                configuration=INVALID_BODY,
            )
        storage.create_config.assert_not_called()

    def test_no_schema_available_skips_validation(self, tmp_config_dir: Path) -> None:
        """No configurationSchema on the component => skip, proceed with POST."""
        service, storage, _ = _make_service(tmp_config_dir, schema=None)

        result = service.create_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            name="My Config",
            configuration=VALID_BODY,
        )

        storage.create_config.assert_called_once()
        assert result["validation_status"] == "skipped"

    def test_ai_service_error_skips_validation_silently(self, tmp_config_dir: Path) -> None:
        """AI Service raises => skip validation, do NOT block the create."""
        service, storage, ai = _make_service(
            tmp_config_dir,
            ai_raises=KeboolaApiError(
                message="503 unavailable", error_code="UNAVAILABLE", status_code=503
            ),
        )

        result = service.create_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            name="My Config",
            configuration=VALID_BODY,
        )

        ai.get_component_detail.assert_called_once()
        ai.close.assert_called_once()
        storage.create_config.assert_called_once()
        assert result["validation_status"] == "skipped"

    def test_validate_false_skips_ai_client_call_entirely(self, tmp_config_dir: Path) -> None:
        """validate=False => AI Service is never consulted."""
        service, storage, ai = _make_service(tmp_config_dir, schema=TABLE_SCHEMA)

        result = service.create_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            name="My Config",
            configuration=INVALID_BODY,  # would fail validation, but skipped
            validate=False,
        )

        ai.get_component_detail.assert_not_called()
        storage.create_config.assert_called_once()
        assert result["validation_status"] == "skipped"

    def test_malformed_schema_skips_validation(self, tmp_config_dir: Path) -> None:
        """A broken JSON schema => skip validation rather than block the create."""
        broken_schema = {"type": "not-a-real-type"}  # invalid Draft7 schema
        service, storage, _ = _make_service(tmp_config_dir, schema=broken_schema)

        result = service.create_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            name="My Config",
            configuration=VALID_BODY,
        )

        storage.create_config.assert_called_once()
        assert result["validation_status"] == "skipped"

    def test_ai_client_closed_even_when_detail_call_succeeds(self, tmp_config_dir: Path) -> None:
        """AI client.close() is called after a successful detail fetch."""
        service, _, ai = _make_service(tmp_config_dir, schema=TABLE_SCHEMA)

        service.create_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            name="My Config",
            configuration=VALID_BODY,
        )

        ai.close.assert_called_once()


# ---------------------------------------------------------------------------
# Parameters-level schema validation (issue #587)
# ---------------------------------------------------------------------------


class TestParametersLevelSchemaValidation:
    """A component's ``configurationSchema`` describes the contents of the
    ``parameters`` key, so the body must be unwrapped before validating.

    Validating the whole configuration object against it inverted the outcome:
    a CORRECT Keboola configuration (``{"parameters": {"db": ...}}``) was
    rejected with ``<root>: 'db' is a required property``, while a MALFORMED
    one (``{"db": ...}``, missing the wrapper) validated clean. Reporters hit
    this on ``config new --push``, worked around it with ``--no-validate``, and
    thereby lost the one check that would have caught an unrelated mistake.
    """

    def test_sibling_keys_next_to_parameters_do_not_fail_validation(
        self, tmp_config_dir: Path
    ) -> None:
        """runtime / storage / authorization are siblings of parameters and are
        not described by the parameters schema -- their presence must not fail.
        """
        service, storage, _ = _make_service(tmp_config_dir, schema=TABLE_SCHEMA)

        result = service.create_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            name="My Config",
            configuration={
                "parameters": {"table": "orders"},
                "runtime": {"parallelism": "20"},
                "storage": {"input": {"tables": []}},
            },
        )

        storage.create_config.assert_called_once()
        assert result["validation_status"] == "ok"
        assert result["validation_errors"] == []

    def test_whole_body_is_posted_even_though_only_parameters_is_validated(
        self, tmp_config_dir: Path
    ) -> None:
        """Unwrapping happens for validation ONLY -- the POSTed configuration
        keeps every sibling key, otherwise the fix would drop the very data
        (``runtime.parallelism``) whose loss prompted issue #587.
        """
        service, storage, _ = _make_service(tmp_config_dir, schema=TABLE_SCHEMA)
        body = {"parameters": {"table": "orders"}, "runtime": {"parallelism": "20"}}

        service.create_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            name="My Config",
            configuration=body,
        )

        posted = storage.create_config.call_args.kwargs["configuration"]
        assert posted == body

    def test_validation_error_path_points_inside_parameters(self, tmp_config_dir: Path) -> None:
        """The reported path must name the parameters section the user has to
        fix, not ``<root>`` -- the misleading path was the reporter's whole
        symptom in issue #587.
        """
        service, _, _ = _make_service(tmp_config_dir, schema=TABLE_SCHEMA)

        result = service.create_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            name="My Config",
            configuration=INVALID_BODY,
            dry_run=True,
        )

        assert result["validation_status"] == "failed"
        assert result["validation_errors"] == ["parameters: 'table' is a required property"]

    def test_empty_parameters_still_fails_a_schema_that_requires_fields(
        self, tmp_config_dir: Path
    ) -> None:
        """Unwrapping must not turn validation into a no-op: a body whose
        ``parameters`` is empty still has to fail a schema with required fields.
        """
        service, storage, _ = _make_service(tmp_config_dir, schema=TABLE_SCHEMA)

        with pytest.raises(ConfigError, match="failed schema validation"):
            service.create_config(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                name="My Config",
                configuration={"parameters": {}},
            )
        storage.create_config.assert_not_called()

    def test_config_without_parameters_key_is_validated_whole(self, tmp_config_dir: Path) -> None:
        """keboola.flow-style configs have no ``parameters`` key -- phases and
        tasks ARE the configuration root, so the whole body is the thing the
        schema describes and must still be validated.
        """
        service, storage, _ = _make_service(tmp_config_dir, schema=FLOW_SCHEMA)

        result = service.create_config(
            alias="prod",
            component_id="keboola.flow",
            name="My Flow",
            configuration={"phases": [], "tasks": []},
        )

        storage.create_config.assert_called_once()
        assert result["validation_status"] == "ok"

    def test_schema_declaring_a_parameters_property_is_validated_whole(
        self, tmp_config_dir: Path
    ) -> None:
        """A schema with a top-level ``parameters`` property describes the whole
        configuration object, so unwrapping would validate the wrong level.

        Keying the unwrap on the body alone would have regressed such a
        component: before #587 every body was validated whole, so it worked.
        The parameters-level read must not be applied to a schema that is
        self-evidently whole-body.
        """
        service, storage, _ = _make_service(tmp_config_dir, schema=WHOLE_BODY_SCHEMA)

        result = service.create_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            name="My Config",
            configuration=VALID_BODY,
        )

        storage.create_config.assert_called_once()
        assert result["validation_status"] == "ok", result

    def test_whole_body_schema_still_reports_errors_at_root(self, tmp_config_dir: Path) -> None:
        """Whole-body schemas keep whole-body error paths -- no `parameters.`
        prefix is invented for a level that was never unwrapped.
        """
        service, _, _ = _make_service(tmp_config_dir, schema=WHOLE_BODY_SCHEMA)

        result = service.create_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            name="My Config",
            configuration=INVALID_BODY,
            dry_run=True,
        )

        assert result["validation_status"] == "failed"
        assert result["validation_errors"] == ["parameters: 'table' is a required property"]

    def test_config_without_parameters_key_still_reports_its_errors(
        self, tmp_config_dir: Path
    ) -> None:
        """The no-parameters fallback validates for real -- it is not a bypass."""
        service, storage, _ = _make_service(tmp_config_dir, schema=FLOW_SCHEMA)

        with pytest.raises(ConfigError, match="failed schema validation"):
            service.create_config(
                alias="prod",
                component_id="keboola.flow",
                name="My Flow",
                configuration={"phases": []},  # 'tasks' missing
            )
        storage.create_config.assert_not_called()


# ---------------------------------------------------------------------------
# Missing `parameters` wrapper (issue #605)
# ---------------------------------------------------------------------------

# A parameters-level schema with NO required fields: an empty ``parameters``
# section is legitimate for such a component, so a body that carries only
# sibling keys must not be rejected.
OPTIONAL_TABLE_SCHEMA = {
    "type": "object",
    "properties": {"table": {"type": "string"}},
}


class TestMissingParametersWrapper:
    """A body missing the ``parameters`` wrapper must fail, not create silently.

    Issue #587 fixed one half of the mismatch (a correctly nested body was
    rejected). The other half survived: a FLATTENED body -- the component's
    parameters sitting at the configuration root -- was validated whole against
    the parameters-level schema, matched it, and was POSTed verbatim. The
    result was a live configuration with no ``parameters`` key at all, which
    the UI and the component runtime both read as empty, while ``--push``
    reported success (issue #605).
    """

    def test_flattened_body_fails_instead_of_creating_a_broken_config(
        self, tmp_config_dir: Path
    ) -> None:
        """The parameters section of a flattened body is empty, so a schema with
        required fields must reject it -- nothing reaches the Storage API.
        """
        service, storage, _ = _make_service(tmp_config_dir, schema=TABLE_SCHEMA)

        with pytest.raises(ConfigError, match="failed schema validation"):
            service.create_config(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                name="My Config",
                configuration={"table": "orders"},  # missing the parameters wrapper
            )
        storage.create_config.assert_not_called()

    def test_flattened_body_error_names_the_missing_wrapper(self, tmp_config_dir: Path) -> None:
        """ "'table' is a required property" alone is baffling when the caller DID
        supply ``table`` -- at the wrong level. The errors must say so.
        """
        service, _, _ = _make_service(tmp_config_dir, schema=TABLE_SCHEMA)

        result = service.create_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            name="My Config",
            configuration={"table": "orders"},
            dry_run=True,
        )

        assert result["validation_status"] == "failed"
        assert result["validation_errors"][0] == "parameters: 'table' is a required property"
        assert any("no 'parameters' key" in err for err in result["validation_errors"]), result[
            "validation_errors"
        ]

    def test_body_without_parameters_passes_when_the_schema_requires_nothing(
        self, tmp_config_dir: Path
    ) -> None:
        """Treating a missing wrapper as an empty ``parameters`` section must not
        invent a failure: a config that is legitimately storage-only still creates.
        """
        service, storage, _ = _make_service(tmp_config_dir, schema=OPTIONAL_TABLE_SCHEMA)

        result = service.create_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            name="My Config",
            configuration={"storage": {"input": {"tables": []}}},
        )

        storage.create_config.assert_called_once()
        assert result["validation_status"] == "ok", result

    def test_whole_body_carve_out_is_keyed_on_the_component_not_the_body_shape(
        self, tmp_config_dir: Path
    ) -> None:
        """``keboola.flow`` keeps whole-body validation because ITS configuration
        root is what the schema describes -- an ordinary component with a
        flow-shaped body does not inherit that exemption.
        """
        service, storage, _ = _make_service(tmp_config_dir, schema=FLOW_SCHEMA)

        with pytest.raises(ConfigError, match="failed schema validation"):
            service.create_config(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                name="My Config",
                configuration={"phases": [], "tasks": []},
            )
        storage.create_config.assert_not_called()
