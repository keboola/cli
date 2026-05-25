"""Service-layer tests for ``SemanticLayerService``.

Covers every business operation: model resolution, list/show/validate/export/diff
reads, create/delete/add/edit/remove writes, import/promote/build orchestration,
and the encrypt-token helper.

Each test injects a ``unittest.mock.MagicMock`` as the metastore client factory
so we verify orchestration (envelope shape, call order, error propagation)
without touching HTTP. The pattern mirrors ``test_data_app_service.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.semantic_layer_service import (
    CONSTRAINT_NAME_RE,
    CONSTRAINT_SEVERITIES,
    CONSTRAINT_TYPES,
    SemanticLayerService,
    _classify_field_role,
    _derive_fqn,
)

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path, alias: str = "prod") -> ConfigStore:
    """Build a ConfigStore with a single project registered."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        alias,
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name=alias,
            project_id=5725,
        ),
    )
    return store


def _make_store_two(tmp_path: Path) -> ConfigStore:
    """Build a ConfigStore with two projects (for promote tests)."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    store = ConfigStore(config_dir=config_dir)
    for alias in ("source", "target"):
        store.add_project(
            alias,
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token=TEST_TOKEN,
                project_name=alias,
                project_id=1000 if alias == "source" else 2000,
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
    service = SemanticLayerService(
        config_store=store,
        metastore_client_factory=lambda url, token: mock,
    )
    return service, mock


def _model_item(
    uuid: str = "u-model",
    name: str = "default",
    description: str = "",
    sql_dialect: str = "Snowflake",
) -> dict[str, Any]:
    return {
        "type": "semantic-model",
        "id": uuid,
        "attributes": {
            "name": name,
            "description": description,
            "sql_dialect": sql_dialect,
        },
    }


def _child_item(
    item_type: str,
    item_id: str,
    attrs: dict[str, Any],
) -> dict[str, Any]:
    return {"type": item_type, "id": item_id, "attributes": dict(attrs)}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestDeriveFqn:
    def test_simple_two_segment_table_id(self) -> None:
        assert _derive_fqn("out.c-gold.FACT_X") == '"KEBOOLA"."out.c-gold"."FACT_X"'

    def test_three_segment_table_id(self) -> None:
        assert _derive_fqn("in.c-raw.users") == '"KEBOOLA"."in.c-raw"."users"'

    def test_invalid_single_segment_raises(self) -> None:
        with pytest.raises(KeboolaApiError) as excinfo:
            _derive_fqn("nodots")
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR


class TestClassifyFieldRole:
    """Role heuristic for `add dataset --deep-fields`."""

    def test_pk_prefix_is_key(self) -> None:
        assert _classify_field_role("PK_USER_ID", "NUMBER") == "key"

    def test_fk_prefix_is_key(self) -> None:
        assert _classify_field_role("FK_ORDER_ID", "NUMBER") == "key"

    def test_date_suffix_is_timestamp(self) -> None:
        assert _classify_field_role("ORDER_DATE", "TIMESTAMP_TZ") == "timestamp"

    def test_date_prefix_is_timestamp(self) -> None:
        assert _classify_field_role("DATE_ORDER", "TIMESTAMP_TZ") == "timestamp"

    def test_numeric_with_measure_token_is_measure(self) -> None:
        assert _classify_field_role("AMOUNT_USD", "NUMBER") == "measure"

    def test_numeric_without_measure_token_is_dimension(self) -> None:
        assert _classify_field_role("USER_ID", "NUMBER") == "dimension"

    def test_string_with_measure_token_is_dimension(self) -> None:
        # measure tokens require a numeric basetype
        assert _classify_field_role("AMOUNT_LABEL", "STRING") == "dimension"

    def test_plain_dimension_default(self) -> None:
        assert _classify_field_role("USER_NAME", "STRING") == "dimension"


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


class TestResolveModel:
    def test_single_model_no_selector_returns_it(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.list_items.return_value = [_model_item("u1", "only")]
        result = service.list_models("prod")
        assert len(result["models"]) == 1
        assert result["models"][0]["name"] == "only"

    def test_ambiguous_models_raises_config_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.list_items.return_value = [
            _model_item("u1", "a"),
            _model_item("u2", "b"),
        ]
        with pytest.raises(ConfigError) as excinfo:
            service.show_model("prod", model_name_or_uuid=None)
        assert "specify --model" in excinfo.value.message
        assert "a" in excinfo.value.message and "b" in excinfo.value.message

    def test_no_models_raises_config_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.list_items.return_value = []
        with pytest.raises(ConfigError) as excinfo:
            service.show_model("prod")
        assert "no semantic-layer models" in excinfo.value.message.lower()

    def test_resolve_by_exact_uuid(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("u1", "a"), _model_item("u2", "b")]
            return []

        mock.list_items.side_effect = _list
        result = service.show_model("prod", model_name_or_uuid="u2")
        assert result["model"]["id"] == "u2"

    def test_resolve_by_name(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("u1", "alpha"), _model_item("u2", "beta")]
            return []

        mock.list_items.side_effect = _list
        result = service.show_model("prod", model_name_or_uuid="beta")
        assert result["model"]["id"] == "u2"

    def test_not_found_raises_with_available_names(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("u1", "alpha"), _model_item("u2", "beta")]
            return []

        mock.list_items.side_effect = _list
        with pytest.raises(ConfigError) as excinfo:
            service.show_model("prod", model_name_or_uuid="ghost")
        assert "alpha" in excinfo.value.message
        assert "beta" in excinfo.value.message


# ---------------------------------------------------------------------------
# list_models / create_model / delete_model
# ---------------------------------------------------------------------------


class TestListModels:
    def test_returns_shape(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.list_items.return_value = [
            _model_item("u1", "a", "first", "Snowflake"),
            _model_item("u2", "b", "second", "Snowflake"),
        ]
        result = service.list_models("prod")
        assert result["project"] == "prod"
        assert result["models"][0] == {
            "id": "u1",
            "name": "a",
            "description": "first",
            "sql_dialect": "Snowflake",
        }
        mock.close.assert_called_once()

    def test_empty_project(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.list_items.return_value = []
        result = service.list_models("prod")
        assert result["models"] == []


class TestCreateModel:
    def test_happy_path(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.post_item.return_value = {
            "type": "semantic-model",
            "id": "new-uuid",
            "attributes": {"name": "m", "sql_dialect": "Snowflake"},
        }
        result = service.create_model("prod", name="m", description="d", sql_dialect="Snowflake")
        assert result["model"]["id"] == "new-uuid"
        mock.post_item.assert_called_once()
        _, kwargs = mock.post_item.call_args
        # data should include description (truthy)
        assert kwargs["data"]["description"] == "d"
        assert kwargs["data"]["sql_dialect"] == "Snowflake"

    def test_omits_empty_description(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.post_item.return_value = _model_item("x", "m")
        service.create_model("prod", name="m")
        _, kwargs = mock.post_item.call_args
        assert "description" not in kwargs["data"]


class TestDeleteModel:
    def test_cascade_deletes_children_then_parent(self, tmp_path: Path) -> None:
        """Children deleted in reverse PUSH_ORDER, then the parent."""
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("u1", "doomed")]
            if item_type == "semantic-dataset":
                return [_child_item("semantic-dataset", "d1", {"name": "addresses"})]
            if item_type == "semantic-metric":
                return [_child_item("semantic-metric", "m1", {"name": "revenue"})]
            if item_type == "semantic-constraint":
                return [_child_item("semantic-constraint", "c1", {"name": "rev_critical"})]
            return []

        mock.list_items.side_effect = _list
        result = service.delete_model("prod", model_name_or_uuid="doomed")

        # Reverse PUSH_ORDER: constraints → glossary → relationships → metrics → datasets → model.
        # Glossary and relationships are empty here, so the visible order is:
        # constraint → metric → dataset → semantic-model.
        actual_calls = [args for args, _ in mock.delete_item.call_args_list]
        assert actual_calls == [
            ("semantic-constraint", "c1"),
            ("semantic-metric", "m1"),
            ("semantic-dataset", "d1"),
            ("semantic-model", "u1"),
        ]

        assert result["deleted"] == {"id": "u1", "name": "doomed"}
        assert result["cascade"]["parent_deleted"] is True
        assert result["cascade"]["failures"] == []
        assert result["cascade"]["deleted"] == {
            "datasets": 1,
            "metrics": 1,
            "relationships": 0,
            "glossary": 0,
            "constraints": 1,
        }
        # orphaned_children kept as alias for the deleted counts (back-compat).
        assert result["orphaned_children"] == result["cascade"]["deleted"]

    def test_cascade_partial_failure_preserves_parent(self, tmp_path: Path) -> None:
        """Mid-cascade child failure leaves the parent intact; surface failures."""
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("u1", "doomed")]
            if item_type == "semantic-dataset":
                return [_child_item("semantic-dataset", "d1", {"name": "addresses"})]
            if item_type == "semantic-metric":
                return [
                    _child_item("semantic-metric", "m1", {"name": "revenue"}),
                    _child_item("semantic-metric", "m2", {"name": "orders"}),
                ]
            return []

        mock.list_items.side_effect = _list

        # Fail the first metric DELETE; everything else succeeds.
        def _delete(item_type: str, item_id: str) -> None:
            if item_type == "semantic-metric" and item_id == "m1":
                raise KeboolaApiError(
                    message="metric still referenced by something",
                    error_code=ErrorCode.VALIDATION_ERROR,
                    status_code=409,
                )

        mock.delete_item.side_effect = _delete

        with pytest.raises(KeboolaApiError) as excinfo:
            service.delete_model("prod", model_name_or_uuid="doomed")

        # Parent delete must NOT have been attempted.
        attempted_types = [args[0] for args, _ in mock.delete_item.call_args_list]
        assert "semantic-model" not in attempted_types

        # Cascade kept going past the failure (m2 and d1 still attempted).
        assert ("semantic-metric", "m2") in [args for args, _ in mock.delete_item.call_args_list]
        assert ("semantic-dataset", "d1") in [args for args, _ in mock.delete_item.call_args_list]

        details = excinfo.value.details or {}
        cascade = details.get("cascade") or {}
        assert cascade["parent_deleted"] is False
        assert cascade["model_uuid"] == "u1"
        assert len(cascade["failures"]) == 1
        assert cascade["failures"][0]["type"] == "semantic-metric"
        assert cascade["failures"][0]["id"] == "m1"
        assert cascade["failures"][0]["name"] == "revenue"
        # The successful siblings are counted in deleted.
        assert cascade["deleted"]["metrics"] == 1
        assert cascade["deleted"]["datasets"] == 1

    def test_empty_model_deletes_parent_only(self, tmp_path: Path) -> None:
        """A childless model deletes the parent and reports zero cascades."""
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("u1", "doomed")]
            return []

        mock.list_items.side_effect = _list
        result = service.delete_model("prod", model_name_or_uuid="doomed")

        mock.delete_item.assert_called_once_with("semantic-model", "u1")
        assert result["cascade"]["parent_deleted"] is True
        assert all(v == 0 for v in result["cascade"]["deleted"].values())
        assert result["cascade"]["failures"] == []


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


class TestShowModel:
    def _setup(self, tmp_path: Path) -> tuple[SemanticLayerService, MagicMock]:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U1", "default")]
            if item_type == "semantic-dataset":
                return [_child_item("semantic-dataset", "d1", {"name": "ds_a"})]
            if item_type == "semantic-metric":
                return [_child_item("semantic-metric", "m1", {"name": "rev"})]
            if item_type == "semantic-relationship":
                return [_child_item("semantic-relationship", "r1", {"name": "r"})]
            if item_type == "semantic-constraint":
                return [_child_item("semantic-constraint", "c1", {"name": "c"})]
            if item_type == "semantic-glossary":
                return [_child_item("semantic-glossary", "g1", {"term": "GMV"})]
            return []

        mock.list_items.side_effect = _list
        return service, mock

    def test_returns_envelope_with_all_types(self, tmp_path: Path) -> None:
        service, _ = self._setup(tmp_path)
        result = service.show_model("prod")
        assert result["project"] == "prod"
        assert result["model"] == {"id": "U1", "name": "default"}
        assert len(result["datasets"]) == 1
        assert result["datasets"][0]["id"] == "d1"
        assert "metrics" in result
        assert "relationships" in result
        assert "constraints" in result
        assert "glossary" in result

    def test_filters_to_one_type(self, tmp_path: Path) -> None:
        service, _ = self._setup(tmp_path)
        result = service.show_model("prod", type_filter="metric")
        assert "metrics" in result
        # other types filtered out
        for k in ("datasets", "relationships", "constraints", "glossary"):
            assert k not in result

    def test_rejects_unknown_type_filter(self, tmp_path: Path) -> None:
        service, _ = self._setup(tmp_path)
        with pytest.raises(KeboolaApiError) as excinfo:
            service.show_model("prod", type_filter="notathing")
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_empty_model(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "lone")]
            return []

        mock.list_items.side_effect = _list
        result = service.show_model("prod")
        for k in ("datasets", "metrics", "relationships", "constraints", "glossary"):
            assert result[k] == []

    def test_meta_keys_not_leaked(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "lone")]
            if item_type == "semantic-dataset":
                return [
                    {
                        "type": "semantic-dataset",
                        "id": "d1",
                        "attributes": {"name": "x"},
                        "meta": {"createdAt": "2020"},
                    }
                ]
            return []

        mock.list_items.side_effect = _list
        result = service.show_model("prod")
        # 'meta' key from server shape must not appear on the per-item dict
        assert "meta" not in result["datasets"][0]


# ---------------------------------------------------------------------------
# validate (basic + deep)
# ---------------------------------------------------------------------------


class TestValidateBasic:
    """Basic validation checks (no API calls beyond the show fetch)."""

    def _service_with(
        self, tmp_path: Path, by_type: dict[str, list[dict[str, Any]]]
    ) -> SemanticLayerService:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            return list(by_type.get(item_type, []))

        mock.list_items.side_effect = _list
        return service

    def test_clean_model_is_valid(self, tmp_path: Path) -> None:
        service = self._service_with(
            tmp_path,
            {
                "semantic-dataset": [
                    _child_item("semantic-dataset", "d1", {"name": "ds", "tableId": "out.c.t"})
                ],
                "semantic-metric": [
                    _child_item(
                        "semantic-metric",
                        "m1",
                        {"name": "rev", "sql": "COUNT(*)", "dataset": "out.c.t"},
                    )
                ],
                "semantic-constraint": [
                    _child_item(
                        "semantic-constraint",
                        "c1",
                        {"name": "rev_warning", "metrics": ["rev"], "constraintType": "inequality"},
                    )
                ],
            },
        )
        result = service.validate_model("prod")
        assert result["valid"] is True
        assert result["errors"] == []

    def test_duplicate_names(self, tmp_path: Path) -> None:
        service = self._service_with(
            tmp_path,
            {
                "semantic-metric": [
                    _child_item("semantic-metric", "m1", {"name": "rev"}),
                    _child_item("semantic-metric", "m2", {"name": "rev"}),
                ],
            },
        )
        result = service.validate_model("prod")
        errors = [e for e in result["errors"] if e["type"] == "DUPLICATE"]
        assert errors and "rev" in errors[0]["item"]

    def test_dangling_relationship(self, tmp_path: Path) -> None:
        service = self._service_with(
            tmp_path,
            {
                "semantic-dataset": [
                    _child_item("semantic-dataset", "d1", {"name": "a", "tableId": "out.c.a"})
                ],
                "semantic-relationship": [
                    _child_item(
                        "semantic-relationship",
                        "r1",
                        {"name": "r", "from": "out.c.a", "to": "out.c.MISSING"},
                    )
                ],
            },
        )
        result = service.validate_model("prod")
        errors = [e for e in result["errors"] if e["type"] == "DANGLING_RELATIONSHIP"]
        assert errors

    def test_dangling_metric(self, tmp_path: Path) -> None:
        service = self._service_with(
            tmp_path,
            {
                "semantic-dataset": [
                    _child_item("semantic-dataset", "d1", {"name": "a", "tableId": "out.c.a"})
                ],
                "semantic-metric": [
                    _child_item(
                        "semantic-metric",
                        "m1",
                        {"name": "rev", "sql": "x", "dataset": "out.c.GONE"},
                    )
                ],
            },
        )
        result = service.validate_model("prod")
        errors = [e for e in result["errors"] if e["type"] == "DANGLING_METRIC"]
        assert errors

    def test_sum_on_pct_warning(self, tmp_path: Path) -> None:
        service = self._service_with(
            tmp_path,
            {
                "semantic-metric": [
                    _child_item(
                        "semantic-metric",
                        "m1",
                        {"name": "bad", "sql": 'SUM("t"."PCT")', "dataset": "x"},
                    )
                ],
            },
        )
        result = service.validate_model("prod")
        warns = [w for w in result["warnings"] if w["type"] == "SUM_ON_PCT"]
        assert warns

    def test_constraint_orphan(self, tmp_path: Path) -> None:
        service = self._service_with(
            tmp_path,
            {
                "semantic-metric": [_child_item("semantic-metric", "m1", {"name": "rev"})],
                "semantic-constraint": [
                    _child_item(
                        "semantic-constraint",
                        "c1",
                        {"name": "orph_warning", "metrics": ["rev", "MISSING"]},
                    )
                ],
            },
        )
        result = service.validate_model("prod")
        errors = [e for e in result["errors"] if e["type"] == "CONSTRAINT_ORPHAN"]
        assert errors

    def test_severity_suffix_warning(self, tmp_path: Path) -> None:
        service = self._service_with(
            tmp_path,
            {
                "semantic-metric": [_child_item("semantic-metric", "m1", {"name": "rev"})],
                "semantic-constraint": [
                    _child_item(
                        "semantic-constraint",
                        "c1",
                        {"name": "no_suffix", "metrics": ["rev"]},
                    )
                ],
            },
        )
        result = service.validate_model("prod")
        warns = [w for w in result["warnings"] if w["type"] == "SEVERITY_SUFFIX"]
        assert warns


class TestValidateDeep:
    """Deep validation -- fetches Snowflake schemas via StorageService."""

    def test_phantom_field(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        ds_attrs = {
            "name": "ds",
            "tableId": "out.c.t",
            "fields": [{"name": "REAL"}, {"name": "GHOST"}],
        }

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            if item_type == "semantic-dataset":
                return [_child_item("semantic-dataset", "d1", ds_attrs)]
            return []

        mock.list_items.side_effect = _list

        with patch(
            "keboola_agent_cli.services.semantic_layer_service.StorageService"
        ) as MockStorageCls:
            inst = MockStorageCls.return_value
            inst.get_table_detail.return_value = {
                "columns": ["REAL"],
                "column_details": [{"name": "REAL", "type": "STRING"}],
            }
            result = service.validate_model("prod", deep=True)
        errors = [e for e in result["errors"] if e["type"] == "PHANTOM_FIELD"]
        assert any("GHOST" in e["item"] for e in errors)

    def test_metric_phantom_column_ref(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            if item_type == "semantic-dataset":
                return [_child_item("semantic-dataset", "d1", {"name": "ds", "tableId": "out.c.t"})]
            if item_type == "semantic-metric":
                return [
                    _child_item(
                        "semantic-metric",
                        "m1",
                        {"name": "rev", "sql": 'SUM("schema"."GHOST_COL")', "dataset": "out.c.t"},
                    )
                ]
            return []

        mock.list_items.side_effect = _list
        with patch(
            "keboola_agent_cli.services.semantic_layer_service.StorageService"
        ) as MockStorageCls:
            inst = MockStorageCls.return_value
            inst.get_table_detail.return_value = {
                "columns": ["REAL"],
                "column_details": [{"name": "REAL", "type": "NUMBER"}],
            }
            result = service.validate_model("prod", deep=True)
        errors = [e for e in result["errors"] if e["type"] == "METRIC_PHANTOM"]
        assert errors

    def test_agg_on_string(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            if item_type == "semantic-dataset":
                return [_child_item("semantic-dataset", "d1", {"name": "ds", "tableId": "out.c.t"})]
            if item_type == "semantic-metric":
                return [
                    _child_item(
                        "semantic-metric",
                        "m1",
                        {
                            "name": "agg_bad",
                            "sql": 'SUM("schema"."NAME")',
                            "dataset": "out.c.t",
                        },
                    )
                ]
            return []

        mock.list_items.side_effect = _list
        with patch(
            "keboola_agent_cli.services.semantic_layer_service.StorageService"
        ) as MockStorageCls:
            inst = MockStorageCls.return_value
            inst.get_table_detail.return_value = {
                "columns": ["NAME"],
                "column_details": [{"name": "NAME", "type": "STRING"}],
            }
            result = service.validate_model("prod", deep=True)
        errors = [e for e in result["errors"] if e["type"] == "AGG_ON_STRING"]
        assert errors


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


class TestExportModel:
    def _setup(self, tmp_path: Path) -> tuple[SemanticLayerService, MagicMock]:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "exp_model")]
            if item_type == "semantic-dataset":
                return [
                    _child_item("semantic-dataset", "d1", {"name": "ds_a", "tableId": "out.c.t"})
                ]
            if item_type == "semantic-metric":
                return [_child_item("semantic-metric", "m1", {"name": "rev"})]
            if item_type == "semantic-relationship":
                return [_child_item("semantic-relationship", "r1", {"name": "r"})]
            if item_type == "semantic-constraint":
                return [_child_item("semantic-constraint", "c1", {"name": "c_warning"})]
            if item_type == "semantic-glossary":
                return [_child_item("semantic-glossary", "g1", {"term": "GMV"})]
            return []

        mock.list_items.side_effect = _list
        return service, mock

    def test_export_envelope_shape(self, tmp_path: Path) -> None:
        service, _ = self._setup(tmp_path)
        out_path = tmp_path / "export.json"
        result = service.export_model("prod", output_path=out_path)
        assert result["project"] == "prod"
        assert "exported_at" in result
        for k in ("datasets", "metrics", "relationships", "constraints", "glossary"):
            assert k in result
            assert result["counts"][k] == 1
        assert result["path"] == str(out_path)

    def test_export_writes_file_with_correct_permissions(self, tmp_path: Path) -> None:
        service, _ = self._setup(tmp_path)
        out_path = tmp_path / "snap.json"
        service.export_model("prod", output_path=out_path)
        assert out_path.is_file()
        # Permissions: world-readable per spec
        mode = out_path.stat().st_mode & 0o777
        assert mode == 0o644
        # Content is valid JSON with expected keys
        payload = json.loads(out_path.read_text())
        assert payload["datasets"][0]["id"] == "d1"
        assert payload["model"]["id"] == "U"

    def test_export_default_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        service, _ = self._setup(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = service.export_model("prod")
        path = Path(result["path"])
        assert path.parent == tmp_path
        assert path.name.startswith("sl_export_exp_model_")
        assert path.suffix == ".json"


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


class TestDiff:
    def _service_two_sides(
        self,
        tmp_path: Path,
        *,
        a_metrics: list[dict[str, Any]],
        b_metrics: list[dict[str, Any]],
    ) -> SemanticLayerService:
        """Build a service whose mocked client returns different shapes per project alias.

        Since the factory builds one client per project resolution, we keep a
        single MagicMock but rotate its `side_effect` between calls by inspecting
        which alias's stack URL was used.  Simpler: just inject the SAME
        responses both times, then drive diff via project-vs-file.
        """
        store = _make_store_two(tmp_path)
        service, mock = _make_service(store)

        # Default list returns the A side; we'll diff project A vs a snapshot
        # file holding B.
        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U_A", "ma")]
            if item_type == "semantic-metric":
                return list(a_metrics)
            return []

        mock.list_items.side_effect = _list

        # Write the B side as a snapshot file.
        snap = tmp_path / "b.json"
        snap.write_text(
            json.dumps(
                {
                    "exported_at": "2020-01-01T00:00:00Z",
                    "project": "target",
                    "model": {"id": "U_B", "name": "mb"},
                    "datasets": [],
                    "metrics": b_metrics,
                    "relationships": [],
                    "constraints": [],
                    "glossary": [],
                }
            )
        )
        self._snap = snap
        return service

    def test_added_removed_changed(self, tmp_path: Path) -> None:
        a_metrics = [
            _child_item("semantic-metric", "m1", {"name": "a", "sql": "1"}),
            _child_item("semantic-metric", "m2", {"name": "b", "sql": "1"}),  # changed
        ]
        b_metrics = [
            _child_item("semantic-metric", "m2", {"name": "b", "sql": "2"}),  # changed
            _child_item("semantic-metric", "m3", {"name": "c", "sql": "1"}),  # new in B
        ]
        service = self._service_two_sides(tmp_path, a_metrics=a_metrics, b_metrics=b_metrics)
        result = service.diff(project_a="source", file_b=self._snap)
        metrics = result["metrics"]
        # A had a, b; B has b, c -> added=[c], removed=[a], changed=[b]
        assert metrics["added"] == ["c"]
        assert metrics["removed"] == ["a"]
        assert metrics["changed"] == [{"name": "b", "diff_keys": ["sql"]}]

    def test_identical_after_strip(self, tmp_path: Path) -> None:
        """modelUUID + timestamps differ; structural content matches → no change."""
        a_metrics = [
            _child_item(
                "semantic-metric",
                "m1",
                {"name": "a", "sql": "1", "modelUUID": "U_A", "createdAt": "2020"},
            )
        ]
        b_metrics = [
            _child_item(
                "semantic-metric",
                "m1",
                {"name": "a", "sql": "1", "modelUUID": "U_B", "createdAt": "2025"},
            )
        ]
        service = self._service_two_sides(tmp_path, a_metrics=a_metrics, b_metrics=b_metrics)
        result = service.diff(project_a="source", file_b=self._snap)
        assert result["metrics"]["changed"] == []

    def test_glossary_uses_term_key(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "x")]
            if item_type == "semantic-glossary":
                return [_child_item("semantic-glossary", "g1", {"term": "GMV", "definition": "v1"})]
            return []

        mock.list_items.side_effect = _list

        # Right side: a snapshot with a different definition for the same term.
        snap = tmp_path / "right.json"
        snap.write_text(
            json.dumps(
                {
                    "model": {"id": "U", "name": "x"},
                    "datasets": [],
                    "metrics": [],
                    "relationships": [],
                    "constraints": [],
                    "glossary": [
                        _child_item("semantic-glossary", "g1", {"term": "GMV", "definition": "v2"})
                    ],
                }
            )
        )
        result = service.diff(project_a="prod", file_b=snap)
        assert result["glossary"]["changed"][0]["term"] == "GMV"

    def test_file_vs_file(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, _ = _make_service(store)
        f_left = tmp_path / "L.json"
        f_right = tmp_path / "R.json"
        f_left.write_text(
            json.dumps(
                {
                    "model": {"id": "U", "name": "x"},
                    "datasets": [],
                    "metrics": [_child_item("semantic-metric", "m", {"name": "rev", "sql": "1"})],
                    "relationships": [],
                    "constraints": [],
                    "glossary": [],
                }
            )
        )
        f_right.write_text(
            json.dumps(
                {
                    "model": {"id": "U", "name": "x"},
                    "datasets": [],
                    "metrics": [_child_item("semantic-metric", "m", {"name": "rev", "sql": "2"})],
                    "relationships": [],
                    "constraints": [],
                    "glossary": [],
                }
            )
        )
        result = service.diff(file_a=f_left, file_b=f_right)
        assert result["metrics"]["changed"] == [{"name": "rev", "diff_keys": ["sql"]}]


# ---------------------------------------------------------------------------
# add_* operations
# ---------------------------------------------------------------------------


class TestAddDataset:
    def test_fqn_derivation(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "default")]
            return []

        mock.list_items.side_effect = _list
        mock.post_item.return_value = {"id": "d1", "attributes": {"name": "fact_x"}}
        service.add_dataset(
            "prod",
            None,
            name="fact_x",
            table_id="out.c-gold.FACT_X",
        )
        _, kwargs = mock.post_item.call_args
        assert kwargs["data"]["fqn"] == '"KEBOOLA"."out.c-gold"."FACT_X"'
        assert kwargs["data"]["modelUUID"] == "U"

    def test_deep_fields_role_heuristics(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "default")]
            return []

        mock.list_items.side_effect = _list
        mock.post_item.return_value = {"id": "d1", "attributes": {"name": "x"}}
        with patch(
            "keboola_agent_cli.services.semantic_layer_service.StorageService"
        ) as MockStorageCls:
            inst = MockStorageCls.return_value
            inst.get_table_detail.return_value = {
                "column_details": [
                    {"name": "PK_USER_ID", "type": "NUMBER"},
                    {"name": "ORDER_DATE", "type": "TIMESTAMP_TZ"},
                    {"name": "AMOUNT_USD", "type": "NUMBER"},
                    {"name": "USER_NAME", "type": "STRING"},
                ]
            }
            service.add_dataset(
                "prod",
                None,
                name="x",
                table_id="out.c-g.X",
                deep_fields=True,
            )
        _, kwargs = mock.post_item.call_args
        fields = {f["name"]: f["role"] for f in kwargs["data"]["fields"]}
        assert fields["PK_USER_ID"] == "key"
        assert fields["ORDER_DATE"] == "timestamp"
        assert fields["AMOUNT_USD"] == "measure"
        assert fields["USER_NAME"] == "dimension"


class TestAddMetric:
    def _ds_list_factory(self, dataset_tids: list[str]):
        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "default")]
            if item_type == "semantic-dataset":
                return [
                    _child_item("semantic-dataset", f"d{i}", {"name": f"d{i}", "tableId": tid})
                    for i, tid in enumerate(dataset_tids)
                ]
            return []

        return _list

    def test_happy_path_dataset_in_model(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.list_items.side_effect = self._ds_list_factory(["out.c.t"])
        mock.post_item.return_value = {"id": "m1", "attributes": {"name": "rev"}}
        result = service.add_metric(
            "prod",
            None,
            name="rev",
            sql="COUNT(*)",
            dataset="out.c.t",
            assume_yes=False,
        )
        assert result["id"] == "m1"
        _, kwargs = mock.post_item.call_args
        assert kwargs["item_type"] == "semantic-metric" if "item_type" in kwargs else True
        assert kwargs["data"]["modelUUID"] == "U"
        assert kwargs["data"]["dataset"] == "out.c.t"

    def test_dataset_not_in_model_non_tty_requires_yes(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.list_items.side_effect = self._ds_list_factory(["out.c.OTHER"])
        with pytest.raises(KeboolaApiError) as excinfo:
            service.add_metric(
                "prod",
                None,
                name="rev",
                sql="x",
                dataset="out.c.MISSING",
                assume_yes=False,
                is_tty=False,
            )
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_dataset_not_in_model_yes_bypasses(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.list_items.side_effect = self._ds_list_factory(["out.c.OTHER"])
        mock.post_item.return_value = {"id": "m1", "attributes": {"name": "rev"}}
        result = service.add_metric(
            "prod",
            None,
            name="rev",
            sql="x",
            dataset="out.c.MISSING",
            assume_yes=True,
        )
        assert result["id"] == "m1"


class TestAddRelationship:
    def test_rejects_invalid_type(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, _ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            service.add_relationship(
                "prod",
                None,
                name="r",
                from_="a",
                to="b",
                on="a.id=b.id",
                type_="full_outer",
            )
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_happy_path(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            return []

        mock.list_items.side_effect = _list
        mock.post_item.return_value = {"id": "r1"}
        service.add_relationship(
            "prod",
            None,
            name="users_to_orders",
            from_="out.c.users",
            to="out.c.orders",
            on="users.id = orders.user_id",
            type_="left",
        )
        _, kwargs = mock.post_item.call_args
        assert kwargs["data"]["type"] == "left"
        assert kwargs["data"]["modelUUID"] == "U"


class TestAddConstraint:
    def _setup(
        self, tmp_path: Path, metric_names: list[str]
    ) -> tuple[SemanticLayerService, MagicMock]:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            if item_type == "semantic-metric":
                return [
                    _child_item("semantic-metric", f"m{i}", {"name": n})
                    for i, n in enumerate(metric_names)
                ]
            return []

        mock.list_items.side_effect = _list
        mock.post_item.return_value = {"id": "c1"}
        return service, mock

    def test_rejects_bad_name_uppercase(self, tmp_path: Path) -> None:
        service, _ = self._setup(tmp_path, ["rev"])
        with pytest.raises(KeboolaApiError) as excinfo:
            service.add_constraint(
                "prod",
                None,
                name="BadName",
                constraint_type="inequality",
                rule="x > 0",
                metrics=["rev"],
            )
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_rejects_bad_name_dashes(self, tmp_path: Path) -> None:
        service, _ = self._setup(tmp_path, ["rev"])
        with pytest.raises(KeboolaApiError):
            service.add_constraint(
                "prod",
                None,
                name="bad-name",
                constraint_type="inequality",
                rule="x",
                metrics=["rev"],
            )

    def test_rejects_leading_digit(self, tmp_path: Path) -> None:
        service, _ = self._setup(tmp_path, ["rev"])
        with pytest.raises(KeboolaApiError):
            service.add_constraint(
                "prod",
                None,
                name="1bad",
                constraint_type="inequality",
                rule="x",
                metrics=["rev"],
            )

    def test_accepts_valid_name(self, tmp_path: Path) -> None:
        service, _ = self._setup(tmp_path, ["rev"])
        # Must not raise
        service.add_constraint(
            "prod",
            None,
            name="good_name_warning",
            constraint_type="inequality",
            rule="x",
            metrics=["rev"],
        )

    def test_rejects_unknown_constraint_type(self, tmp_path: Path) -> None:
        service, _ = self._setup(tmp_path, ["rev"])
        with pytest.raises(KeboolaApiError):
            service.add_constraint(
                "prod",
                None,
                name="ok_warning",
                constraint_type="fancy",
                rule="x",
                metrics=["rev"],
            )

    def test_rejects_unknown_severity(self, tmp_path: Path) -> None:
        service, _ = self._setup(tmp_path, ["rev"])
        with pytest.raises(KeboolaApiError):
            service.add_constraint(
                "prod",
                None,
                name="ok_warning",
                constraint_type="inequality",
                rule="x",
                metrics=["rev"],
                severity="extreme",
            )

    def test_rejects_missing_metric_reference(self, tmp_path: Path) -> None:
        service, _ = self._setup(tmp_path, ["rev"])
        with pytest.raises(KeboolaApiError) as excinfo:
            service.add_constraint(
                "prod",
                None,
                name="ok_warning",
                constraint_type="inequality",
                rule="x",
                metrics=["NONEXISTENT"],
            )
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR


class TestAddGlossary:
    def test_happy_path(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            return []

        mock.list_items.side_effect = _list
        mock.post_item.return_value = {"id": "g1", "attributes": {"term": "GMV"}}
        service.add_glossary("prod", None, term="GMV", definition="gross merchandise value")
        _, kwargs = mock.post_item.call_args
        # name parameter (envelope key) must equal the term
        assert kwargs["name"] == "GMV"
        assert kwargs["data"]["term"] == "GMV"
        assert kwargs["data"]["modelUUID"] == "U"


# ---------------------------------------------------------------------------
# edit_* operations (DELETE+POST + rollback + cascade)
# ---------------------------------------------------------------------------


class TestEditMetric:
    def test_rename_cascades_constraints(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        original = _child_item("semantic-metric", "m1", {"name": "rev", "sql": "1"})
        c_attrs = {"name": "rev_warning", "metrics": ["rev"]}
        constraint = _child_item("semantic-constraint", "c1", c_attrs)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            if item_type == "semantic-metric":
                return [original]
            if item_type == "semantic-constraint":
                return [constraint]
            return []

        mock.list_items.side_effect = _list
        mock.post_item.side_effect = [
            {"id": "m_new", "attributes": {"name": "revenue"}},
            {"id": "c_new", "attributes": dict(c_attrs, name="rev_warning")},
        ]

        result = service.edit_metric(
            "prod",
            None,
            current_name="rev",
            new_name="revenue",
            assume_yes=True,
        )
        assert result["updated"]["id"] == "m_new"
        # delete called twice: once for the old metric, once for the cascade
        assert mock.delete_item.call_count == 2
        # cascade list populated
        assert result["cascaded_constraints"][0]["status"] == "updated"

    def test_description_only_change_no_cascade(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        original = _child_item("semantic-metric", "m1", {"name": "rev", "sql": "1"})

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            if item_type == "semantic-metric":
                return [original]
            return []

        mock.list_items.side_effect = _list
        mock.post_item.return_value = {"id": "m_new", "attributes": {"name": "rev"}}

        result = service.edit_metric(
            "prod",
            None,
            current_name="rev",
            new_description="updated desc",
        )
        assert result["updated"]["id"] == "m_new"
        # Only one delete: the metric itself.
        assert mock.delete_item.call_count == 1
        assert result["cascaded_constraints"] == []

    def test_metric_not_found(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            return []

        mock.list_items.side_effect = _list
        with pytest.raises(KeboolaApiError) as excinfo:
            service.edit_metric("prod", None, current_name="ghost", new_sql="1")
        assert excinfo.value.error_code == ErrorCode.NOT_FOUND

    def test_rollback_on_post_failure(self, tmp_path: Path) -> None:
        """When POST after DELETE fails, the original item is re-POSTed."""
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        original = _child_item("semantic-metric", "m1", {"name": "rev", "sql": "1"})

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            if item_type == "semantic-metric":
                return [original]
            return []

        mock.list_items.side_effect = _list
        # First POST (the actual edit) fails; second POST (rollback) succeeds.
        mock.post_item.side_effect = [
            KeboolaApiError(message="boom", status_code=500, error_code=ErrorCode.API_ERROR),
            {"id": "restored", "attributes": {"name": "rev"}},
        ]

        with pytest.raises(KeboolaApiError) as excinfo:
            service.edit_metric("prod", None, current_name="rev", new_sql="2")
        details = excinfo.value.details or {}
        rollback = details.get("rollback") or {}
        assert rollback.get("status") == "succeeded"

    def test_partial_state_false_when_cascade_succeeds(self, tmp_path: Path) -> None:
        """Envelope carries partial_state=False + recovery_hint=None on full success (issue #294)."""
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        original = _child_item("semantic-metric", "m1", {"name": "rev", "sql": "1"})
        c_attrs = {"name": "rev_warning", "metrics": ["rev"]}
        constraint = _child_item("semantic-constraint", "c1", c_attrs)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            if item_type == "semantic-metric":
                return [original]
            if item_type == "semantic-constraint":
                return [constraint]
            return []

        mock.list_items.side_effect = _list
        mock.post_item.side_effect = [
            {"id": "m_new", "attributes": {"name": "revenue"}},
            {"id": "c_new", "attributes": dict(c_attrs, name="rev_warning")},
        ]
        result = service.edit_metric(
            "prod", None, current_name="rev", new_name="revenue", assume_yes=True
        )
        assert result["partial_state"] is False
        assert result["recovery_hint"] is None

    def test_partial_state_true_when_cascade_constraint_fails(self, tmp_path: Path) -> None:
        """Envelope flags partial_state + recovery_hint when M of N cascades fail (issue #294)."""
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        original = _child_item("semantic-metric", "m1", {"name": "rev", "sql": "1"})
        # Two dependent constraints; only the second's POST will fail.
        c1 = _child_item("semantic-constraint", "c1", {"name": "rev_ok", "metrics": ["rev"]})
        c2 = _child_item("semantic-constraint", "c2", {"name": "rev_fail", "metrics": ["rev"]})

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            if item_type == "semantic-metric":
                return [original]
            if item_type == "semantic-constraint":
                return [c1, c2]
            return []

        mock.list_items.side_effect = _list
        # POST sequence:
        #  1. metric rename POST -> succeeds
        #  2. c1 cascade POST  -> succeeds
        #  3. c2 cascade POST  -> fails
        #  4. c2 rollback POST -> succeeds (per-item rollback)
        mock.post_item.side_effect = [
            {"id": "m_new", "attributes": {"name": "revenue"}},
            {"id": "c1_new", "attributes": {"name": "rev_ok", "metrics": ["revenue"]}},
            KeboolaApiError(message="boom", status_code=500, error_code=ErrorCode.API_ERROR),
            {"id": "c2_restored", "attributes": {"name": "rev_fail", "metrics": ["rev"]}},
        ]
        result = service.edit_metric(
            "prod", None, current_name="rev", new_name="revenue", assume_yes=True
        )
        assert result["partial_state"] is True
        assert result["recovery_hint"] is not None
        assert "validate" in result["recovery_hint"]
        assert "edit constraint" in result["recovery_hint"]
        # Per-entry record stays as before for diagnostics.
        statuses = [entry["status"] for entry in result["cascaded_constraints"]]
        assert statuses == ["updated", "failed"]


class TestEditDataset:
    def test_not_found(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            return []

        mock.list_items.side_effect = _list
        with pytest.raises(KeboolaApiError) as excinfo:
            service.edit_dataset("prod", None, current_name="ghost", new_description="x")
        assert excinfo.value.error_code == ErrorCode.NOT_FOUND


class TestEditConstraint:
    def test_rejects_bad_new_name(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, _ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            service.edit_constraint(
                "prod",
                None,
                current_name="ok_warning",
                new_name="UPPER",
            )
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_rejects_bad_new_type(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, _ = _make_service(store)
        with pytest.raises(KeboolaApiError):
            service.edit_constraint(
                "prod",
                None,
                current_name="ok_warning",
                new_constraint_type="bogus",
            )


class TestEditRelationship:
    def test_updates_endpoint(self, tmp_path: Path) -> None:
        """Happy path: --new-from rewrites the source tableId via DELETE+POST."""
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            if item_type == "semantic-relationship":
                return [
                    _child_item(
                        "semantic-relationship",
                        "r1",
                        {
                            "name": "fact_to_dim",
                            "from": "out.c.fact",
                            "to": "out.c.dim",
                            "on": "fact.id = dim.id",
                            "type": "left",
                        },
                    )
                ]
            return []

        mock.list_items.side_effect = _list
        mock.post_item.return_value = {
            "id": "r2",
            "attributes": {"name": "fact_to_dim", "from": "out.c.fact_v2"},
        }
        result = service.edit_relationship(
            "prod",
            None,
            current_name="fact_to_dim",
            new_from="out.c.fact_v2",
        )
        mock.delete_item.assert_called_once_with("semantic-relationship", "r1")
        # The POST payload retains the unchanged endpoints but rewrites `from`.
        post_kwargs = mock.post_item.call_args.kwargs
        assert post_kwargs["data"]["from"] == "out.c.fact_v2"
        assert post_kwargs["data"]["to"] == "out.c.dim"
        assert result["rollback"] is None
        assert result["cascaded_constraints"] == []

    def test_rejects_bad_new_type(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, _ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            service.edit_relationship(
                "prod",
                None,
                current_name="x",
                new_type="bogus",
            )
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_not_found(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            return []

        mock.list_items.side_effect = _list
        with pytest.raises(KeboolaApiError) as excinfo:
            service.edit_relationship("prod", None, current_name="ghost", new_to="x")
        assert excinfo.value.error_code == ErrorCode.NOT_FOUND


class TestEditGlossary:
    def test_updates_definition(self, tmp_path: Path) -> None:
        """Happy path: --new-definition rewrites the definition; term unchanged."""
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            if item_type == "semantic-glossary":
                return [
                    _child_item(
                        "semantic-glossary",
                        "g1",
                        {"term": "MRR", "definition": "Monthly Recurring Revenue"},
                    )
                ]
            return []

        mock.list_items.side_effect = _list
        mock.post_item.return_value = {
            "id": "g2",
            "attributes": {"term": "MRR", "definition": "Updated def"},
        }
        result = service.edit_glossary(
            "prod",
            None,
            current_term="MRR",
            new_definition="Updated def",
        )
        mock.delete_item.assert_called_once_with("semantic-glossary", "g1")
        post_kwargs = mock.post_item.call_args.kwargs
        assert post_kwargs["data"]["term"] == "MRR"
        assert post_kwargs["data"]["definition"] == "Updated def"
        assert result["rollback"] is None

    def test_rename_term(self, tmp_path: Path) -> None:
        """--new-term rewrites the term identity."""
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            if item_type == "semantic-glossary":
                return [_child_item("semantic-glossary", "g1", {"term": "MRR"})]
            return []

        mock.list_items.side_effect = _list
        mock.post_item.return_value = {"id": "g2", "attributes": {"term": "RECURRING_REVENUE"}}
        service.edit_glossary(
            "prod",
            None,
            current_term="MRR",
            new_term="RECURRING_REVENUE",
        )
        post_kwargs = mock.post_item.call_args.kwargs
        assert post_kwargs["data"]["term"] == "RECURRING_REVENUE"
        assert post_kwargs["name"] == "RECURRING_REVENUE"

    def test_not_found(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            return []

        mock.list_items.side_effect = _list
        with pytest.raises(KeboolaApiError) as excinfo:
            service.edit_glossary("prod", None, current_term="ghost", new_definition="x")
        assert excinfo.value.error_code == ErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# remove (preview + delete)
# ---------------------------------------------------------------------------


class TestRemove:
    def test_preview_lists_orphans(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            if item_type == "semantic-metric":
                return [_child_item("semantic-metric", "m1", {"name": "rev"})]
            if item_type == "semantic-constraint":
                return [
                    _child_item(
                        "semantic-constraint",
                        "c1",
                        {"name": "rev_warning", "metrics": ["rev"]},
                    )
                ]
            return []

        mock.list_items.side_effect = _list
        preview = service.preview_remove("prod", None, kind="metric", name="rev")
        assert len(preview["orphaned_constraints"]) == 1
        assert preview["orphaned_constraints"][0]["name"] == "rev_warning"

    def test_remove_invokes_delete(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            if item_type == "semantic-metric":
                return [_child_item("semantic-metric", "m1", {"name": "rev"})]
            return []

        mock.list_items.side_effect = _list
        result = service.remove_item("prod", None, kind="metric", name="rev")
        mock.delete_item.assert_called_once_with("semantic-metric", "m1")
        assert result["removed"]["name"] == "rev"

    def test_remove_unknown_kind(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, _ = _make_service(store)
        with pytest.raises(KeboolaApiError):
            service.remove_item("prod", None, kind="bogus", name="x")

    def test_remove_not_found(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            return []

        mock.list_items.side_effect = _list
        with pytest.raises(KeboolaApiError) as excinfo:
            service.remove_item("prod", None, kind="metric", name="ghost")
        assert excinfo.value.error_code == ErrorCode.NOT_FOUND

    def test_remove_relationship(self, tmp_path: Path) -> None:
        """Removing a relationship: DELETE only, no orphan check (leaf entity)."""
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            if item_type == "semantic-relationship":
                return [_child_item("semantic-relationship", "r1", {"name": "fact_to_dim"})]
            return []

        mock.list_items.side_effect = _list
        result = service.remove_item("prod", None, kind="relationship", name="fact_to_dim")
        mock.delete_item.assert_called_once_with("semantic-relationship", "r1")
        assert result["removed"]["name"] == "fact_to_dim"
        assert result["orphaned_constraints"] == []

    def test_remove_glossary_uses_term_identity(self, tmp_path: Path) -> None:
        """Removing glossary: identity key is `term`, not `name`."""
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            if item_type == "semantic-glossary":
                return [_child_item("semantic-glossary", "g1", {"term": "MRR"})]
            return []

        mock.list_items.side_effect = _list
        result = service.remove_item("prod", None, kind="glossary", name="MRR")
        mock.delete_item.assert_called_once_with("semantic-glossary", "g1")
        assert result["removed"]["name"] == "MRR"
        assert result["orphaned_constraints"] == []

    def test_preview_remove_glossary_not_found_uses_term(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "m")]
            if item_type == "semantic-glossary":
                # Only term `MRR` exists -- lookup by `OTHER` must miss.
                return [_child_item("semantic-glossary", "g1", {"term": "MRR"})]
            return []

        mock.list_items.side_effect = _list
        with pytest.raises(KeboolaApiError) as excinfo:
            service.preview_remove("prod", None, kind="glossary", name="OTHER")
        assert excinfo.value.error_code == ErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# import_snapshot
# ---------------------------------------------------------------------------


def _write_snapshot(path: Path, *, datasets=None, metrics=None, constraints=None) -> None:
    payload = {
        "model": {"id": "src-uuid", "name": "src"},
        "datasets": datasets or [],
        "metrics": metrics or [],
        "relationships": [],
        "constraints": constraints or [],
        "glossary": [],
    }
    path.write_text(json.dumps(payload))


class TestImportSnapshot:
    def test_skip_on_conflict_default(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "target")]
            if item_type == "semantic-dataset":
                return [_child_item("semantic-dataset", "d1", {"name": "fact_x"})]
            return []

        mock.list_items.side_effect = _list
        snap = tmp_path / "s.json"
        _write_snapshot(
            snap,
            datasets=[
                _child_item("semantic-dataset", "src-d1", {"name": "fact_x", "tableId": "out.c.t"})
            ],
        )
        result = service.import_snapshot("prod", snap)
        assert result["imported"]["datasets"]["skipped"] == 1
        assert result["imported"]["datasets"]["created"] == 0
        mock.post_item.assert_not_called()

    def test_overwrite_deletes_then_posts(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "target")]
            if item_type == "semantic-dataset":
                return [_child_item("semantic-dataset", "d1", {"name": "fact_x"})]
            return []

        mock.list_items.side_effect = _list
        mock.post_item.return_value = {"id": "new"}
        snap = tmp_path / "s.json"
        _write_snapshot(
            snap,
            datasets=[
                _child_item("semantic-dataset", "src-d1", {"name": "fact_x", "tableId": "out.c.t"})
            ],
        )
        result = service.import_snapshot("prod", snap, overwrite=True)
        assert result["imported"]["datasets"]["overwritten"] == 1
        mock.delete_item.assert_called_with("semantic-dataset", "d1")
        mock.post_item.assert_called()

    def test_dry_run_no_writes(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "target")]
            return []

        mock.list_items.side_effect = _list
        snap = tmp_path / "s.json"
        _write_snapshot(
            snap,
            datasets=[
                _child_item("semantic-dataset", "x", {"name": "new_ds", "tableId": "out.c.t"})
            ],
        )
        result = service.import_snapshot("prod", snap, dry_run=True)
        assert result["imported"]["datasets"]["created"] == 1
        mock.post_item.assert_not_called()
        mock.delete_item.assert_not_called()

    def test_dependency_order(self, tmp_path: Path) -> None:
        """Push order: datasets -> metrics -> relationships -> glossary -> constraints."""
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U", "target")]
            return []

        mock.list_items.side_effect = _list
        mock.post_item.return_value = {"id": "x"}
        snap = tmp_path / "s.json"
        snap.write_text(
            json.dumps(
                {
                    "model": {"id": "src", "name": "src"},
                    "datasets": [
                        _child_item("semantic-dataset", "d", {"name": "ds", "tableId": "out.c.t"})
                    ],
                    "metrics": [_child_item("semantic-metric", "m", {"name": "rev"})],
                    "relationships": [],
                    "constraints": [_child_item("semantic-constraint", "c", {"name": "c_w"})],
                    "glossary": [_child_item("semantic-glossary", "g", {"term": "GMV"})],
                }
            )
        )
        service.import_snapshot("prod", snap)
        type_call_order = [
            c.kwargs["item_type"] if "item_type" in c.kwargs else c.args[0]
            for c in mock.post_item.call_args_list
        ]
        # Order: dataset first, constraint last
        assert type_call_order[0] == "semantic-dataset"
        assert type_call_order[-1] == "semantic-constraint"

    def test_invalid_json_file(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, _ = _make_service(store)
        snap = tmp_path / "broken.json"
        snap.write_text("{not valid json")
        with pytest.raises(KeboolaApiError) as excinfo:
            service.import_snapshot("prod", snap)
        assert excinfo.value.error_code == ErrorCode.INVALID_FORMAT

    def test_import_rejects_unknown_types_filter(self, tmp_path: Path) -> None:
        """`--types BOGUS` raises VALIDATION_ERROR instead of silently no-opping.

        Iter-3: closes the silent-filter bug where typo'd type names
        (e.g. ``--types metric`` instead of ``metrics``) filtered every type
        out and emitted zero imports without an error.
        """
        from keboola_agent_cli.services.semantic_layer_service import _validate_types_filter

        with pytest.raises(KeboolaApiError) as excinfo:
            _validate_types_filter(["BOGUS"])
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR
        assert "BOGUS" in excinfo.value.message

    def test_validate_types_filter_accepts_valid_subset(self) -> None:
        from keboola_agent_cli.services.semantic_layer_service import _validate_types_filter

        assert _validate_types_filter(["datasets", "metrics"]) == {"datasets", "metrics"}
        assert _validate_types_filter(None) is None
        assert _validate_types_filter([]) is None


# ---------------------------------------------------------------------------
# promote_model
# ---------------------------------------------------------------------------


class TestPromoteModel:
    def test_classification_new_changed_identical(self, tmp_path: Path) -> None:
        store = _make_store_two(tmp_path)

        src_mock = MagicMock()
        tgt_mock = MagicMock()
        clients = {0: src_mock, 1: tgt_mock}
        call_idx = {"i": 0}

        def _factory(url: str, token: str) -> MagicMock:
            c = clients[call_idx["i"]]
            call_idx["i"] += 1
            return c

        service = SemanticLayerService(
            config_store=store,
            metastore_client_factory=_factory,
        )

        def _src_list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U_S", "src")]
            if item_type == "semantic-metric":
                return [
                    _child_item(
                        "semantic-metric", "m1", {"name": "a", "sql": "1", "modelUUID": "U_S"}
                    ),
                    _child_item(
                        "semantic-metric", "m2", {"name": "b", "sql": "2", "modelUUID": "U_S"}
                    ),  # changed
                    _child_item(
                        "semantic-metric", "m3", {"name": "c", "sql": "3", "modelUUID": "U_S"}
                    ),  # new
                ]
            return []

        def _tgt_list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item("U_T", "tgt")]
            if item_type == "semantic-metric":
                return [
                    _child_item(
                        "semantic-metric", "tm1", {"name": "a", "sql": "1", "modelUUID": "U_T"}
                    ),  # identical
                    _child_item(
                        "semantic-metric", "tm2", {"name": "b", "sql": "OLD", "modelUUID": "U_T"}
                    ),  # will change
                    _child_item(
                        "semantic-metric", "tm9", {"name": "z", "sql": "9", "modelUUID": "U_T"}
                    ),  # target-only
                ]
            return []

        src_mock.list_items.side_effect = _src_list
        tgt_mock.list_items.side_effect = _tgt_list

        result = service.promote_model(from_project="source", to_project="target", dry_run=True)
        metrics = result["metrics"]
        assert metrics["new"] == 1  # c
        assert metrics["overwritten"] == 1  # b
        assert metrics["identical"] == 1  # a
        # Target-only items not touched
        tgt_mock.delete_item.assert_not_called()

    def test_both_clients_closed_even_on_error(self, tmp_path: Path) -> None:
        store = _make_store_two(tmp_path)

        src_mock = MagicMock()
        tgt_mock = MagicMock()
        clients = {0: src_mock, 1: tgt_mock}
        call_idx = {"i": 0}

        def _factory(url: str, token: str) -> MagicMock:
            c = clients[call_idx["i"]]
            call_idx["i"] += 1
            return c

        service = SemanticLayerService(
            config_store=store,
            metastore_client_factory=_factory,
        )

        # src_client raises during resolve
        src_mock.list_items.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError):
            service.promote_model(from_project="source", to_project="target")
        src_mock.close.assert_called_once()
        tgt_mock.close.assert_called_once()


# ---------------------------------------------------------------------------
# build_model
# ---------------------------------------------------------------------------


class TestNormalizeFieldType:
    """Pin the warehouse-native → metastore-vocabulary mapping.

    The metastore's `fields[*].type` accepts only a closed lowercase set
    (`string`, `integer`, `decimal`, `boolean`, `date`, `datetime`, `json`).
    Storage hands us warehouse-native uppercase types — Snowflake `NUMBER`,
    `VARCHAR(255)`, BigQuery `STRING`, etc. Forgetting to normalize causes
    HTTP 422 on every legacy untyped table. These cases pin the mapping so
    extending `_FIELD_TYPE_MAP` can't accidentally drop one.
    """

    @pytest.mark.parametrize(
        "basetype,expected",
        [
            # The case the bug report was filed against — empty string from
            # an untyped Storage column.
            ("", "string"),
            (None, "string"),
            # Snowflake / BigQuery uppercase types with parameter brackets.
            ("VARCHAR(255)", "string"),
            ("NUMBER(38,2)", "decimal"),
            ("DECIMAL(18, 9)", "decimal"),
            # Plain types in different cases.
            ("STRING", "string"),
            ("integer", "integer"),
            ("BIGINT", "integer"),
            ("Boolean", "boolean"),
            ("DATE", "date"),
            ("TIMESTAMP_NTZ", "datetime"),
            ("VARIANT", "json"),
            # Unknown types fall through to `"string"` — safest default.
            ("CUSTOM_UDT", "string"),
            ("geography", "string"),
        ],
    )
    def test_normalize_field_type(self, basetype: str, expected: str) -> None:
        from keboola_agent_cli.services._semantic_layer_internals import (
            _normalize_field_type,
        )

        assert _normalize_field_type(basetype) == expected


class TestBuildModel:
    def test_heuristic_fallback(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.list_items.return_value = []
        mock.post_item.side_effect = [
            {"id": "new-model"},  # the model
            {"id": "d1"},  # dataset
            {"id": "m1"},  # metric (count(*))
            {"id": "g1"},  # glossary
        ]
        with patch(
            "keboola_agent_cli.services.semantic_layer_service.StorageService"
        ) as MockStorageCls:
            inst = MockStorageCls.return_value
            inst.get_table_detail.return_value = {
                "display_name": "fact_orders",
                "column_details": [{"name": "AMOUNT", "type": "NUMBER"}],
            }
            result = service.build_model("prod", table_ids=["out.c.t"])
        assert result["fallback_used"] == "heuristic"
        assert len(result["generated"]["datasets"]) == 1
        assert len(result["generated"]["metrics"]) == 1
        assert len(result["generated"]["glossary"]) == 1
        assert result["validated"] is True
        # Pin the warehouse → metastore type normalization: Storage hands us
        # `"NUMBER"`, the metastore only accepts lowercase `"decimal"`. The
        # heuristic builder must perform the mapping before the model is
        # posted, otherwise we regress to the HTTP 422 this PR fixes.
        ds_fields = result["generated"]["datasets"][0]["fields"]
        assert ds_fields[0]["type"] == "decimal"

    def test_dry_run_skips_post(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        with patch(
            "keboola_agent_cli.services.semantic_layer_service.StorageService"
        ) as MockStorageCls:
            inst = MockStorageCls.return_value
            inst.get_table_detail.return_value = {
                "column_details": [{"name": "X", "type": "NUMBER"}],
            }
            result = service.build_model("prod", table_ids=["out.c.t"], dry_run=True)
        assert result["dry_run"] is True
        mock.post_item.assert_not_called()

    def test_empty_table_ids_rejected(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, _ = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            service.build_model("prod", table_ids=[])
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_fqn_derived_for_each_dataset(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, _ = _make_service(store)
        with patch(
            "keboola_agent_cli.services.semantic_layer_service.StorageService"
        ) as MockStorageCls:
            inst = MockStorageCls.return_value
            inst.get_table_detail.return_value = {
                "column_details": [{"name": "X", "type": "NUMBER"}],
            }
            result = service.build_model(
                "prod",
                table_ids=["out.c-bk.tab"],
                dry_run=True,
            )
        ds = result["generated"]["datasets"][0]
        assert ds["fqn"] == '"KEBOOLA"."out.c-bk"."tab"'


class TestBuildModelRollback:
    """build_model push-loop rollback semantics (issue #295)."""

    @staticmethod
    def _patch_storage(
        column_details: list[dict[str, Any]] | None = None,
    ) -> Any:
        return patch("keboola_agent_cli.services.semantic_layer_service.StorageService")

    def test_rollback_deletes_posted_children_in_reverse_when_child_fails(
        self, tmp_path: Path
    ) -> None:
        """On child POST failure, every successfully-POSTed child is DELETEd in reverse + model is deleted."""
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.list_items.return_value = []
        # POST sequence:
        #  1. model (created here, model_created_here=True)
        #  2. dataset (succeeds, id=d1)
        #  3. metric (succeeds, id=m1)
        #  4. glossary (fails)
        mock.post_item.side_effect = [
            {"id": "new-model"},
            {"id": "d1"},
            {"id": "m1"},
            KeboolaApiError(message="boom", status_code=500, error_code=ErrorCode.API_ERROR),
        ]
        with self._patch_storage() as MockStorageCls:
            inst = MockStorageCls.return_value
            inst.get_table_detail.return_value = {
                "display_name": "fact_orders",
                "column_details": [{"name": "AMOUNT", "type": "NUMBER"}],
            }
            with pytest.raises(KeboolaApiError) as excinfo:
                service.build_model("prod", table_ids=["out.c.t"])
        # Cleanup DELETEs: m1 first (reverse of POST order), then d1, then the model itself last.
        delete_args = [call.args for call in mock.delete_item.call_args_list]
        assert delete_args == [
            ("semantic-metric", "m1"),
            ("semantic-dataset", "d1"),
            ("semantic-model", "new-model"),
        ]
        rollback = (excinfo.value.details or {}).get("rollback") or {}
        assert rollback["attempted"] is True
        assert rollback["model_created_here"] is True
        assert rollback["model_deleted"] is True
        assert rollback["deleted"] == 2  # two children deleted

    def test_keep_on_failure_preserves_state(self, tmp_path: Path) -> None:
        """With --keep-on-failure, no cleanup runs; rollback envelope flags reason."""
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.list_items.return_value = []
        mock.post_item.side_effect = [
            {"id": "new-model"},
            {"id": "d1"},
            {"id": "m1"},
            KeboolaApiError(message="boom", status_code=500, error_code=ErrorCode.API_ERROR),
        ]
        with self._patch_storage() as MockStorageCls:
            inst = MockStorageCls.return_value
            inst.get_table_detail.return_value = {
                "column_details": [{"name": "X", "type": "NUMBER"}],
            }
            with pytest.raises(KeboolaApiError) as excinfo:
                service.build_model("prod", table_ids=["out.c.t"], keep_on_failure=True)
        mock.delete_item.assert_not_called()
        rollback = (excinfo.value.details or {}).get("rollback") or {}
        assert rollback["attempted"] is False
        assert rollback["reason"] == "keep_on_failure"
        assert rollback["posted_children"] == 2
        assert rollback["model_created_here"] is True

    def test_rollback_does_not_delete_caller_supplied_model(self, tmp_path: Path) -> None:
        """When caller passes --model EXISTING, the model itself is never DELETEd on rollback."""
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        existing_model = _model_item("existing-uuid", "existing_model")

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [existing_model]
            return []

        mock.list_items.side_effect = _list
        # POST sequence (caller passed --model so no model POST):
        #  1. dataset (succeeds, id=d1)
        #  2. metric (fails)
        mock.post_item.side_effect = [
            {"id": "d1"},
            KeboolaApiError(message="boom", status_code=500, error_code=ErrorCode.API_ERROR),
        ]
        with self._patch_storage() as MockStorageCls:
            inst = MockStorageCls.return_value
            inst.get_table_detail.return_value = {
                "column_details": [{"name": "X", "type": "NUMBER"}],
            }
            with pytest.raises(KeboolaApiError) as excinfo:
                service.build_model(
                    "prod",
                    table_ids=["out.c.t"],
                    model_name_or_uuid="existing_model",
                )
        # Only the child was deleted, NOT the model.
        delete_args = [call.args for call in mock.delete_item.call_args_list]
        assert delete_args == [("semantic-dataset", "d1")]
        rollback = (excinfo.value.details or {}).get("rollback") or {}
        assert rollback["model_created_here"] is False
        assert rollback["model_deleted"] is False

    def test_rollback_triggers_on_non_api_exception(self, tmp_path: Path) -> None:
        """Rollback must also fire when client.post_item raises a non-KeboolaApiError
        (e.g. an httpx network exception); otherwise the partial state this PR exists
        to clean up would still leak (review iter-2 NON-BLOCKING finding)."""
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.list_items.return_value = []
        mock.post_item.side_effect = [
            {"id": "new-model"},
            {"id": "d1"},
            RuntimeError("simulated network glitch"),  # NOT a KeboolaApiError
        ]
        with self._patch_storage() as MockStorageCls:
            inst = MockStorageCls.return_value
            inst.get_table_detail.return_value = {
                "column_details": [{"name": "X", "type": "NUMBER"}],
            }
            with pytest.raises(KeboolaApiError) as excinfo:
                service.build_model("prod", table_ids=["out.c.t"])
        # Rollback still ran (deleted dataset + model).
        delete_args = [call.args for call in mock.delete_item.call_args_list]
        assert delete_args == [
            ("semantic-dataset", "d1"),
            ("semantic-model", "new-model"),
        ]
        rollback = (excinfo.value.details or {}).get("rollback") or {}
        assert rollback["attempted"] is True
        assert rollback["model_deleted"] is True
        # The original exception is chained; the wrapped error code falls back to
        # INTERNAL_ERROR since RuntimeError carries no api-layer code.
        assert excinfo.value.error_code == ErrorCode.INTERNAL_ERROR

    def test_continues_when_post_returns_no_id(self, tmp_path: Path) -> None:
        """Degenerate POST response (missing id) is logged + skipped from rollback tracking
        rather than silently incrementing counts (review iter-2 NON-BLOCKING finding)."""
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.list_items.return_value = []
        # POST returns {} (no id) for the dataset; subsequent POSTs succeed.
        mock.post_item.side_effect = [
            {"id": "new-model"},
            {},  # dataset POST with no id
            {"id": "m1"},
            {"id": "g1"},
        ]
        with self._patch_storage() as MockStorageCls:
            inst = MockStorageCls.return_value
            inst.get_table_detail.return_value = {
                "column_details": [{"name": "X", "type": "NUMBER"}],
            }
            result = service.build_model("prod", table_ids=["out.c.t"])
        # The id-less dataset must NOT be counted as created.
        assert result["created"]["datasets"] == 0
        assert result["created"]["metrics"] == 1
        assert result["created"]["glossary"] == 1
        # AND the id-less dataset must NOT be tracked for rollback -- on
        # successful overall build there are zero DELETE calls regardless,
        # but this makes the exclusion explicit (per padak's NIT-2 review).
        mock.delete_item.assert_not_called()

    def test_rollback_continues_when_individual_delete_fails(self, tmp_path: Path) -> None:
        """A failed cleanup DELETE never masks the original error; remaining cleanup proceeds."""
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.list_items.return_value = []
        mock.post_item.side_effect = [
            {"id": "new-model"},
            {"id": "d1"},
            {"id": "m1"},
            KeboolaApiError(message="boom", status_code=500, error_code=ErrorCode.API_ERROR),
        ]
        # First cleanup DELETE (the metric) raises; rest must still run.
        mock.delete_item.side_effect = [
            KeboolaApiError(
                message="metric delete failed",
                status_code=500,
                error_code=ErrorCode.API_ERROR,
            ),
            None,  # dataset DELETE succeeds
            None,  # model DELETE succeeds
        ]
        with self._patch_storage() as MockStorageCls:
            inst = MockStorageCls.return_value
            inst.get_table_detail.return_value = {
                "column_details": [{"name": "X", "type": "NUMBER"}],
            }
            with pytest.raises(KeboolaApiError) as excinfo:
                service.build_model("prod", table_ids=["out.c.t"])
        rollback = (excinfo.value.details or {}).get("rollback") or {}
        assert rollback["attempted"] is True
        assert rollback["deleted"] == 1  # only the dataset DELETE succeeded
        assert len(rollback["failed_deletes"]) == 1
        assert rollback["failed_deletes"][0]["type"] == "semantic-metric"
        assert rollback["model_deleted"] is True


# ---------------------------------------------------------------------------
# encrypt_token
# ---------------------------------------------------------------------------


class TestEncryptToken:
    def test_uses_project_token_and_returns_envelope(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, _ = _make_service(store)
        # Patch the symbol where it's BOUND (semantic_layer_service imports
        # EncryptService at module load now, so the patch must target the
        # rebound name in the consumer module, not the source module).
        with patch(
            "keboola_agent_cli.services.semantic_layer_service.EncryptService"
        ) as MockEncryptCls:
            instance = MockEncryptCls.return_value
            instance.encrypt.return_value = {"#metastore_token": "KBC::ProjectSecureGKMS::cipher"}
            result = service.encrypt_token("prod", "keboola.ex-db-snowflake")
        instance.encrypt.assert_called_once()
        _, kwargs = instance.encrypt.call_args
        # Validate the payload key + the actual stored token are passed.
        assert kwargs["component_id"] == "keboola.ex-db-snowflake"
        assert kwargs["input_data"] == {"#metastore_token": TEST_TOKEN}
        assert kwargs["alias"] == "prod"
        # Returned envelope contains expected fields.
        assert result["component_id"] == "keboola.ex-db-snowflake"
        assert result["project"] == "prod"
        assert result["encrypted"]["#metastore_token"].startswith("KBC::")


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_constraint_name_regex(self) -> None:
        assert CONSTRAINT_NAME_RE.match("rev_critical")
        assert CONSTRAINT_NAME_RE.match("a")
        assert not CONSTRAINT_NAME_RE.match("BadName")
        assert not CONSTRAINT_NAME_RE.match("1bad")
        assert not CONSTRAINT_NAME_RE.match("bad-name")

    def test_constraint_types_complete(self) -> None:
        assert set(CONSTRAINT_TYPES) == {
            "inequality",
            "equality",
            "range",
            "composition",
            "exclusion",
            "temporal",
            "conditional",
        }

    def test_constraint_severities(self) -> None:
        assert set(CONSTRAINT_SEVERITIES) == {"error", "warning", "info"}


# ---------------------------------------------------------------------------
# search-context / get-context (v0.47.0)
# ---------------------------------------------------------------------------


class TestSearchContext:
    """Project-wide name-pattern search across semantic-layer entities."""

    def _setup(
        self, tmp_path: Path, items_by_type: dict[str, list[dict[str, Any]]]
    ) -> tuple[SemanticLayerService, MagicMock]:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            return items_by_type.get(item_type, [])

        mock.list_items.side_effect = _list
        return service, mock

    def test_default_pattern_matches_everything(self, tmp_path: Path) -> None:
        items = {
            "semantic-dataset": [_child_item("semantic-dataset", "d1", {"name": "users"})],
            "semantic-metric": [_child_item("semantic-metric", "m1", {"name": "revenue"})],
        }
        service, _ = self._setup(tmp_path, items)

        result = service.search_context("prod")

        assert result["project"] == "prod"
        assert result["total_count"] == 2
        names = sorted(c["name"] for c in result["contexts"])
        assert names == ["revenue", "users"]
        # Types are CLI-friendly singular (no "semantic-" prefix).
        types = {c["type"] for c in result["contexts"]}
        assert types == {"dataset", "metric"}

    def test_pattern_glob_narrows_results(self, tmp_path: Path) -> None:
        items = {
            "semantic-dataset": [
                _child_item("semantic-dataset", "d1", {"name": "DIM_users"}),
                _child_item("semantic-dataset", "d2", {"name": "FACT_orders"}),
                _child_item("semantic-dataset", "d3", {"name": "DIM_products"}),
            ],
        }
        service, _ = self._setup(tmp_path, items)

        result = service.search_context("prod", patterns=["DIM_*"])

        assert result["total_count"] == 2
        names = sorted(c["name"] for c in result["contexts"])
        assert names == ["DIM_products", "DIM_users"]

    def test_pattern_is_case_sensitive(self, tmp_path: Path) -> None:
        items = {
            "semantic-dataset": [
                _child_item("semantic-dataset", "d1", {"name": "DIM_x"}),
                _child_item("semantic-dataset", "d2", {"name": "dim_y"}),
            ],
        }
        service, _ = self._setup(tmp_path, items)

        upper = service.search_context("prod", patterns=["DIM_*"])
        lower = service.search_context("prod", patterns=["dim_*"])

        assert [c["name"] for c in upper["contexts"]] == ["DIM_x"]
        assert [c["name"] for c in lower["contexts"]] == ["dim_y"]

    def test_multiple_patterns_take_union(self, tmp_path: Path) -> None:
        items = {
            "semantic-dataset": [
                _child_item("semantic-dataset", "d1", {"name": "DIM_a"}),
                _child_item("semantic-dataset", "d2", {"name": "FACT_b"}),
                _child_item("semantic-dataset", "d3", {"name": "AGG_c"}),
            ],
        }
        service, _ = self._setup(tmp_path, items)

        result = service.search_context("prod", patterns=["DIM_*", "FACT_*"])

        assert result["total_count"] == 2
        assert {c["name"] for c in result["contexts"]} == {"DIM_a", "FACT_b"}

    def test_type_filter_narrows_to_one_kind(self, tmp_path: Path) -> None:
        items = {
            "semantic-dataset": [_child_item("semantic-dataset", "d1", {"name": "users"})],
            "semantic-metric": [_child_item("semantic-metric", "m1", {"name": "revenue"})],
            "semantic-relationship": [_child_item("semantic-relationship", "r1", {"name": "r"})],
            "semantic-constraint": [_child_item("semantic-constraint", "c1", {"name": "c"})],
            "semantic-glossary": [_child_item("semantic-glossary", "g1", {"name": "term"})],
        }
        service, mock = self._setup(tmp_path, items)

        result = service.search_context("prod", type_filter="metric")

        assert result["total_count"] == 1
        assert result["contexts"][0]["type"] == "metric"
        called_types = {call.args[0] for call in mock.list_items.call_args_list}
        assert called_types == {"semantic-metric"}, (
            "type_filter must short-circuit the per-type loop"
        )

    def test_type_filter_model(self, tmp_path: Path) -> None:
        items = {"semantic-model": [_model_item("u1", "default")]}
        service, mock = self._setup(tmp_path, items)

        result = service.search_context("prod", type_filter="model")

        assert result["total_count"] == 1
        assert result["contexts"][0]["type"] == "model"
        assert mock.list_items.call_args_list[0].args[0] == "semantic-model"

    def test_type_filter_all_iterates_every_child(self, tmp_path: Path) -> None:
        items = {
            t: [_child_item(t, "x", {"name": "n"})]
            for t in (
                "semantic-dataset",
                "semantic-metric",
                "semantic-relationship",
                "semantic-constraint",
                "semantic-glossary",
            )
        }
        service, mock = self._setup(tmp_path, items)

        result = service.search_context("prod", type_filter="all")

        assert result["total_count"] == 5
        called_types = {call.args[0] for call in mock.list_items.call_args_list}
        assert called_types == set(items)
        assert "semantic-model" not in called_types, (
            "type=all means every CHILD type, not the model itself"
        )

    def test_limit_caps_results_and_short_circuits(self, tmp_path: Path) -> None:
        items = {
            "semantic-dataset": [
                _child_item("semantic-dataset", f"d{i}", {"name": f"n{i}"}) for i in range(10)
            ],
            "semantic-metric": [
                _child_item("semantic-metric", "m1", {"name": "rev"}),
            ],
        }
        service, mock = self._setup(tmp_path, items)

        result = service.search_context("prod", limit=3)

        assert result["total_count"] == 3
        # short-circuit: never reached semantic-metric
        called_types = [call.args[0] for call in mock.list_items.call_args_list]
        assert "semantic-metric" not in called_types

    def test_invalid_type_filter_rejected(self, tmp_path: Path) -> None:
        service, _ = self._setup(tmp_path, {})

        with pytest.raises(KeboolaApiError) as excinfo:
            service.search_context("prod", type_filter="bogus")

        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_empty_pattern_rejected(self, tmp_path: Path) -> None:
        service, _ = self._setup(tmp_path, {})

        with pytest.raises(KeboolaApiError) as excinfo:
            service.search_context("prod", patterns=["valid", ""])

        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_zero_limit_rejected(self, tmp_path: Path) -> None:
        service, _ = self._setup(tmp_path, {})

        with pytest.raises(KeboolaApiError) as excinfo:
            service.search_context("prod", limit=0)

        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_client_closed_even_on_api_error(self, tmp_path: Path) -> None:
        """try/finally must close the client even when list_items raises."""
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.list_items.side_effect = KeboolaApiError(
            message="boom", status_code=500, error_code=ErrorCode.API_ERROR
        )

        with pytest.raises(KeboolaApiError):
            service.search_context("prod")

        mock.close.assert_called_once()


class TestGetContext:
    """Single-id lookup across every semantic type."""

    def _setup(self, tmp_path: Path) -> tuple[SemanticLayerService, MagicMock]:
        return _make_service(_make_store(tmp_path))

    def test_finds_dataset(self, tmp_path: Path) -> None:
        service, mock = self._setup(tmp_path)

        def _get(item_type: str, item_id: str) -> dict[str, Any]:
            if item_type == "semantic-dataset" and item_id == "d1":
                return _child_item("semantic-dataset", "d1", {"name": "users"})
            raise KeboolaApiError(message="404", status_code=404, error_code=ErrorCode.NOT_FOUND)

        mock.get_item.side_effect = _get
        result = service.get_context("prod", "d1")

        assert result["id"] == "d1"
        assert result["type"] == "dataset"
        assert result["name"] == "users"

    def test_finds_model(self, tmp_path: Path) -> None:
        """Lookup probes model first; a model hit short-circuits the scan."""
        service, mock = self._setup(tmp_path)
        mock.get_item.return_value = _model_item("u-model", "default")

        result = service.get_context("prod", "u-model")

        assert result["type"] == "model"
        # Only one client call needed when model is the first probe.
        assert mock.get_item.call_count == 1
        assert mock.get_item.call_args.args[0] == "semantic-model"

    def test_missing_id_raises_not_found(self, tmp_path: Path) -> None:
        service, mock = self._setup(tmp_path)
        mock.get_item.side_effect = KeboolaApiError(
            message="404", status_code=404, error_code=ErrorCode.NOT_FOUND
        )

        with pytest.raises(KeboolaApiError) as excinfo:
            service.get_context("prod", "missing")

        assert excinfo.value.error_code == ErrorCode.NOT_FOUND
        # Probed every type before giving up: model + 5 child types = 6 calls.
        assert mock.get_item.call_count == 6

    def test_non_404_error_propagates_immediately(self, tmp_path: Path) -> None:
        """A 500 on one type must surface as-is, not be swallowed."""
        service, mock = self._setup(tmp_path)
        mock.get_item.side_effect = KeboolaApiError(
            message="boom", status_code=500, error_code=ErrorCode.API_ERROR
        )

        with pytest.raises(KeboolaApiError) as excinfo:
            service.get_context("prod", "x")

        assert excinfo.value.error_code == ErrorCode.API_ERROR

    def test_empty_id_rejected(self, tmp_path: Path) -> None:
        service, _ = self._setup(tmp_path)

        with pytest.raises(KeboolaApiError) as excinfo:
            service.get_context("prod", "")

        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_client_closed_even_on_api_error(self, tmp_path: Path) -> None:
        service, mock = self._setup(tmp_path)
        mock.get_item.side_effect = KeboolaApiError(
            message="500", status_code=500, error_code=ErrorCode.API_ERROR
        )

        with pytest.raises(KeboolaApiError):
            service.get_context("prod", "x")

        mock.close.assert_called_once()
