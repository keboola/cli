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

# A trivial JSON schema that requires a top-level "parameters" object with
# a required "table" string field. Used to drive the validation branch.
TABLE_SCHEMA = {
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

VALID_BODY = {"parameters": {"table": "orders"}}
INVALID_BODY = {"parameters": {"limit": 100}}  # missing required "table"


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
