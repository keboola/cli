"""Tests for ``kbagent semantic-layer schema`` (issue #394).

Covers all three layers:

- L3 ``MetastoreClient.get_schema`` via pytest-httpx (URL, verbatim body
  passthrough, non-dict response normalization).
- L2 ``SemanticLayerService.get_schema`` with a MagicMock metastore factory
  (single type, multi-type fan-out, dedupe, unknown-type fail-fast, worker
  error propagation).
- L1 CLI via CliRunner with the cli.py service factory patched (JSON
  envelope, ``--all``, mutual exclusion, error mapping, human panels).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
from keboola_agent_cli.metastore_client import MetastoreClient
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.project_service import ProjectService
from keboola_agent_cli.services.semantic_layer_service import (
    SCHEMA_TYPE_ALIAS,
    TYPE_ALIAS,
    SemanticLayerService,
)

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
STACK_URL_US = "https://connection.keboola.com"
METASTORE_URL_US = "https://metastore.keboola.com"

runner = CliRunner()


# ---------------------------------------------------------------------------
# Shared helpers (mirror test_semantic_layer_cli.py / _service.py conventions)
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path, alias: str = "prod") -> ConfigStore:
    """Build a ConfigStore with a single project registered."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        alias,
        ProjectConfig(
            stack_url=STACK_URL_US,
            token=TEST_TOKEN,
            project_name=alias,
            project_id=5725,
        ),
    )
    return store


def _make_service(
    store: ConfigStore,
    *,
    metastore_mock: MagicMock | None = None,
) -> tuple[SemanticLayerService, MagicMock]:
    """Wire a SemanticLayerService with a mocked metastore client factory."""
    mock = metastore_mock or MagicMock()
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    service = SemanticLayerService(
        config_store=store,
        metastore_client_factory=lambda url, token: mock,
    )
    return service, mock


def _invoke(
    args: list[str],
    *,
    store: ConfigStore,
    sl_mock: MagicMock,
):
    """Run the CLI with cli.py services patched to mocks."""
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ProjectService") as MockProj,
        patch("keboola_agent_cli.cli.ConfigService") as MockCfg,
        patch("keboola_agent_cli.cli.JobService") as MockJob,
        patch("keboola_agent_cli.cli.SemanticLayerService") as MockSL,
    ):
        MockStore.return_value = store
        MockProj.return_value = ProjectService(config_store=store)
        MockCfg.return_value = ConfigService(config_store=store)
        MockJob.return_value = JobService(config_store=store)
        MockSL.return_value = sl_mock
        return runner.invoke(app, args)


@pytest.fixture
def store(tmp_path: Path) -> ConfigStore:
    return _make_store(tmp_path)


def _schema_for(wire_type: str) -> dict[str, Any]:
    """Deterministic fake JSON schema keyed by wire type."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": wire_type,
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }


# ---------------------------------------------------------------------------
# L3 -- MetastoreClient.get_schema
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable retry-backoff sleeps so the suite stays fast."""
    import keboola_agent_cli.http_base as http_base_module

    monkeypatch.setattr(http_base_module.time, "sleep", lambda _x: None)


class TestClientGetSchema:
    def test_get_schema_url_and_verbatim_body(self, httpx_mock) -> None:
        schema = _schema_for("semantic-metric")
        httpx_mock.add_response(
            url=f"{METASTORE_URL_US}/api/v1/schema/semantic-metric",
            json=schema,
            status_code=200,
        )
        client = MetastoreClient(stack_url=STACK_URL_US, token=TEST_TOKEN)
        try:
            result = client.get_schema("semantic-metric")
        finally:
            client.close()
        # Verbatim passthrough -- no {"data": ...} unwrapping on this endpoint.
        assert result == schema
        request = httpx_mock.get_requests()[0]
        assert request.headers["X-StorageApi-Token"] == TEST_TOKEN

    def test_get_schema_data_key_not_unwrapped(self, httpx_mock) -> None:
        # A schema that legitimately contains a top-level "data" property
        # must NOT be unwrapped like the repository endpoints.
        schema = {"type": "object", "data": {"nested": True}}
        httpx_mock.add_response(
            url=f"{METASTORE_URL_US}/api/v1/schema/semantic-model",
            json=schema,
            status_code=200,
        )
        client = MetastoreClient(stack_url=STACK_URL_US, token=TEST_TOKEN)
        try:
            result = client.get_schema("semantic-model")
        finally:
            client.close()
        assert result == schema

    def test_get_schema_non_dict_body_raises_api_error(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{METASTORE_URL_US}/api/v1/schema/semantic-dataset",
            json=["not", "a", "dict"],
            status_code=200,
        )
        client = MetastoreClient(stack_url=STACK_URL_US, token=TEST_TOKEN)
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.get_schema("semantic-dataset")
        finally:
            client.close()
        assert exc_info.value.error_code == ErrorCode.API_ERROR
        assert exc_info.value.retryable is False


# ---------------------------------------------------------------------------
# L2 -- SemanticLayerService.get_schema
# ---------------------------------------------------------------------------


class TestServiceGetSchema:
    def test_single_type(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.return_value = _schema_for("semantic-metric")

        result = service.get_schema("prod", types=["metric"])

        assert result == {
            "project": "prod",
            "schemas": [{"type": "metric", "schema": _schema_for("semantic-metric")}],
        }
        mock.get_schema.assert_called_once_with("semantic-metric")

    def test_model_type_maps_to_semantic_model(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.return_value = _schema_for("semantic-model")

        result = service.get_schema("prod", types=["model"])

        mock.get_schema.assert_called_once_with("semantic-model")
        assert result["schemas"][0]["type"] == "model"

    def test_multi_type_fan_out_preserves_requested_order(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = _schema_for

        result = service.get_schema("prod", types=["metric", "dataset", "model"])

        assert [s["type"] for s in result["schemas"]] == ["metric", "dataset", "model"]
        assert result["schemas"][0]["schema"] == _schema_for("semantic-metric")
        assert result["schemas"][1]["schema"] == _schema_for("semantic-dataset")
        assert result["schemas"][2]["schema"] == _schema_for("semantic-model")
        called_wire_types = {c.args[0] for c in mock.get_schema.call_args_list}
        assert called_wire_types == {"semantic-metric", "semantic-dataset", "semantic-model"}

    def test_duplicates_collapsed(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.return_value = _schema_for("semantic-metric")

        result = service.get_schema("prod", types=["metric", "metric"])

        assert [s["type"] for s in result["schemas"]] == ["metric"]
        mock.get_schema.assert_called_once_with("semantic-metric")

    def test_unknown_type_fails_fast_without_network(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))

        with pytest.raises(ConfigError) as exc_info:
            service.get_schema("prod", types=["metric", "bogus"])

        assert "bogus" in exc_info.value.message
        # The message lists every valid type name.
        for valid in SCHEMA_TYPE_ALIAS:
            assert valid in exc_info.value.message
        mock.get_schema.assert_not_called()

    def test_empty_types_fails_fast(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))

        with pytest.raises(ConfigError):
            service.get_schema("prod", types=[])
        mock.get_schema.assert_not_called()

    def test_worker_error_propagates(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))

        def _raise_for_dataset(wire_type: str) -> dict[str, Any]:
            if wire_type == "semantic-dataset":
                raise KeboolaApiError(
                    message="boom",
                    status_code=500,
                    error_code=ErrorCode.API_ERROR,
                )
            return _schema_for(wire_type)

        mock.get_schema.side_effect = _raise_for_dataset

        with pytest.raises(KeboolaApiError) as exc_info:
            service.get_schema("prod", types=["metric", "dataset"])
        assert exc_info.value.error_code == ErrorCode.API_ERROR

    def test_schema_alias_is_superset_of_type_alias_plus_model(self) -> None:
        assert set(SCHEMA_TYPE_ALIAS) == set(TYPE_ALIAS) | {"model"}
        assert SCHEMA_TYPE_ALIAS["model"] == "semantic-model"
        # show --type must NOT accept "model" (no plural payload key).
        assert "model" not in TYPE_ALIAS

    def test_operation_registry_classifies_schema_as_read(self) -> None:
        """Without this entry the fail-closed default ('write') would deny
        `semantic-layer schema` under --deny-writes despite being read-only."""
        from keboola_agent_cli.permissions import OPERATION_REGISTRY

        assert OPERATION_REGISTRY["semantic-layer.schema"] == "read"


# ---------------------------------------------------------------------------
# L1 -- CLI `semantic-layer schema`
# ---------------------------------------------------------------------------


class TestCliSchema:
    def test_single_type_json(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.get_schema.return_value = {
            "project": "prod",
            "schemas": [{"type": "metric", "schema": _schema_for("semantic-metric")}],
        }
        result = _invoke(
            ["--json", "semantic-layer", "schema", "--project", "prod", "--type", "metric"],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["status"] == "ok"
        assert body["data"]["schemas"][0]["type"] == "metric"
        assert body["data"]["schemas"][0]["schema"]["title"] == "semantic-metric"
        mock.get_schema.assert_called_once_with(alias="prod", types=["metric"])

    def test_multi_type_comma_separated(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.get_schema.return_value = {
            "project": "prod",
            "schemas": [
                {"type": "metric", "schema": _schema_for("semantic-metric")},
                {"type": "dataset", "schema": _schema_for("semantic-dataset")},
            ],
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "schema",
                "--project",
                "prod",
                "--type",
                "metric, dataset",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        # Whitespace around commas is stripped.
        mock.get_schema.assert_called_once_with(alias="prod", types=["metric", "dataset"])

    def test_all_fetches_every_known_type(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.get_schema.return_value = {
            "project": "prod",
            "schemas": [{"type": t, "schema": {}} for t in SCHEMA_TYPE_ALIAS],
        }
        result = _invoke(
            ["--json", "semantic-layer", "schema", "--project", "prod", "--all"],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        mock.get_schema.assert_called_once_with(alias="prod", types=list(SCHEMA_TYPE_ALIAS))

    def test_type_and_all_mutually_exclusive_exit_2(self, store: ConfigStore) -> None:
        mock = MagicMock()
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "schema",
                "--project",
                "prod",
                "--type",
                "metric",
                "--all",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 2, result.output
        body = json.loads(result.output)
        assert body["error"]["code"] == "USAGE_ERROR"
        mock.get_schema.assert_not_called()

    def test_neither_type_nor_all_exit_2(self, store: ConfigStore) -> None:
        mock = MagicMock()
        result = _invoke(
            ["--json", "semantic-layer", "schema", "--project", "prod"],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 2, result.output
        body = json.loads(result.output)
        assert body["error"]["code"] == "USAGE_ERROR"
        mock.get_schema.assert_not_called()

    def test_empty_type_list_exit_2(self, store: ConfigStore) -> None:
        mock = MagicMock()
        result = _invoke(
            ["--json", "semantic-layer", "schema", "--project", "prod", "--type", " , "],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 2, result.output
        body = json.loads(result.output)
        assert body["error"]["code"] == "VALIDATION_ERROR"
        mock.get_schema.assert_not_called()

    def test_unknown_type_maps_to_config_error_exit_5(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.get_schema.side_effect = ConfigError(
            "Unknown semantic type(s): bogus. Valid types: model, dataset, metric, "
            "relationship, constraint, glossary."
        )
        result = _invoke(
            ["--json", "semantic-layer", "schema", "--project", "prod", "--type", "bogus"],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 5, result.output
        body = json.loads(result.output)
        assert body["error"]["code"] == "CONFIG_ERROR"
        assert "bogus" in body["error"]["message"]
        assert "glossary" in body["error"]["message"]

    def test_api_error_invalid_token_exits_3(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.get_schema.side_effect = KeboolaApiError(
            message="bad token", status_code=401, error_code="INVALID_TOKEN"
        )
        result = _invoke(
            ["--json", "semantic-layer", "schema", "--project", "prod", "--type", "metric"],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 3, result.output
        body = json.loads(result.output)
        assert body["error"]["code"] == "INVALID_TOKEN"

    def test_human_output_renders_per_type_panels(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.get_schema.return_value = {
            "project": "prod",
            "schemas": [
                {"type": "metric", "schema": _schema_for("semantic-metric")},
                {"type": "dataset", "schema": _schema_for("semantic-dataset")},
            ],
        }
        result = _invoke(
            ["semantic-layer", "schema", "--project", "prod", "--type", "metric,dataset"],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        assert "metric" in result.output
        assert "dataset" in result.output
        assert "schema" in result.output
        # The JSON schema body is rendered inside the panels.
        assert "semantic-metric" in result.output

    def test_missing_project_arg_exit_2(self, store: ConfigStore) -> None:
        mock = MagicMock()
        result = _invoke(
            ["--json", "semantic-layer", "schema", "--type", "metric"],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 2
