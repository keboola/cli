"""Tests for metastore scope/target-project/elevation support (PSGO-140).

Covers ``services._semantic_layer_scope`` (pure alias-resolution + merge
logic) and ``SemanticLayerService.scope_*`` (orchestration), plus the
edit-preserves-scope regression: a DELETE+POST edit of an
organization/targeted-scope item must never silently reset it to
``project`` scope.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services import _semantic_layer_scope as scope_helpers
from keboola_agent_cli.services.semantic_layer_service import SemanticLayerService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"


def _make_store(tmp_path: Path) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name="prod",
            project_id=5725,
        ),
    )
    store.add_project(
        "analytics",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name="analytics",
            project_id=1234,
        ),
    )
    return store


def _make_service(store: ConfigStore, *, metastore_mock: MagicMock | None = None):
    mock = metastore_mock or MagicMock()
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    service = SemanticLayerService(
        config_store=store,
        metastore_client_factory=lambda url, token: mock,
    )
    return service, mock


def _model_item(uuid: str = "u-model", name: str = "default") -> dict[str, Any]:
    return {"type": "semantic-model", "id": uuid, "attributes": {"name": name}}


def _child_item(
    item_type: str, item_id: str, attrs: dict[str, Any], meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    item: dict[str, Any] = {"type": item_type, "id": item_id, "attributes": dict(attrs)}
    if meta is not None:
        item["meta"] = meta
    return item


# ---------------------------------------------------------------------------
# services._semantic_layer_scope -- pure helpers
# ---------------------------------------------------------------------------


class TestResolveTargetProjectIds:
    def test_resolves_known_aliases(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        assert scope_helpers.resolve_target_project_ids(store, ["prod", "analytics"]) == [
            5725,
            1234,
        ]

    def test_unknown_alias_raises_not_found(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with pytest.raises(KeboolaApiError) as excinfo:
            scope_helpers.resolve_target_project_ids(store, ["ghost"])
        assert excinfo.value.error_code == ErrorCode.NOT_FOUND

    def test_missing_project_id_raises_config_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.add_project(
            "no-id",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token=TEST_TOKEN,
                project_name="no-id",
                project_id=None,
            ),
        )
        with pytest.raises(KeboolaApiError) as excinfo:
            scope_helpers.resolve_target_project_ids(store, ["no-id"])
        assert excinfo.value.error_code == ErrorCode.CONFIG_ERROR


class TestItemStatus:
    def test_extracts_scope_fields_from_meta(self) -> None:
        item = _child_item(
            "semantic-dataset",
            "d1",
            {"name": "fact_pnl"},
            meta={
                "scope": "targeted",
                "targetProjectIds": [1, 2],
                "scopeElevationRequestedAt": "2026-08-28T00:00:00Z",
                "projectId": 5725,
            },
        )
        status = scope_helpers.item_status(item)
        assert status == {
            "id": "d1",
            "type": "semantic-dataset",
            "name": "fact_pnl",
            "scope": "targeted",
            "target_project_ids": [1, 2],
            "scope_elevation_requested_at": "2026-08-28T00:00:00Z",
            "project_id": 5725,
        }

    def test_defaults_to_project_scope_without_meta(self) -> None:
        item = _child_item("semantic-metric", "m1", {"name": "rev"})
        status = scope_helpers.item_status(item)
        assert status["scope"] == "project"
        assert status["target_project_ids"] is None

    def test_glossary_falls_back_to_term(self) -> None:
        item = _child_item("semantic-glossary", "g1", {"term": "GMV"})
        assert scope_helpers.item_status(item)["name"] == "GMV"


class TestGrantTargetProjects:
    def test_replace_sends_exact_set_without_reading_current(self) -> None:
        client = MagicMock()
        client.get_item.return_value = _child_item(
            "semantic-dataset", "d1", {"name": "x"}, meta={"scope": "targeted"}
        )
        scope_helpers.grant_target_projects(client, "semantic-dataset", "d1", replace=[2, 1, 2])
        client.put_target_projects.assert_called_once_with("semantic-dataset", "d1", [1, 2])
        # replace mode never reads the current grant set before writing.
        assert client.get_item.call_count == 1  # only the post-write status re-fetch

    def test_add_merges_with_current_grants(self) -> None:
        client = MagicMock()
        client.get_item.return_value = _child_item(
            "semantic-dataset", "d1", {"name": "x"}, meta={"targetProjectIds": [1, 2]}
        )
        scope_helpers.grant_target_projects(client, "semantic-dataset", "d1", add=[3])
        client.put_target_projects.assert_called_once_with("semantic-dataset", "d1", [1, 2, 3])

    def test_remove_merges_with_current_grants(self) -> None:
        client = MagicMock()
        client.get_item.return_value = _child_item(
            "semantic-dataset", "d1", {"name": "x"}, meta={"targetProjectIds": [1, 2, 3]}
        )
        scope_helpers.grant_target_projects(client, "semantic-dataset", "d1", remove=[2])
        client.put_target_projects.assert_called_once_with("semantic-dataset", "d1", [1, 3])

    def test_clear_via_empty_replace(self) -> None:
        client = MagicMock()
        client.get_item.return_value = _child_item("semantic-dataset", "d1", {"name": "x"})
        scope_helpers.grant_target_projects(client, "semantic-dataset", "d1", replace=[])
        client.put_target_projects.assert_called_once_with("semantic-dataset", "d1", [])


# ---------------------------------------------------------------------------
# SemanticLayerService.scope_* -- orchestration
# ---------------------------------------------------------------------------


class TestServiceScopeMethods:
    def test_resolve_target_project_ids_public_wrapper(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, _mock = _make_service(store)
        assert service.resolve_target_project_ids(["analytics"]) == [1234]

    def test_resolve_scope_type_rejects_unknown_kind(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, _mock = _make_service(store)
        with pytest.raises(KeboolaApiError) as excinfo:
            service.scope_status("prod", "bogus-kind", "id-1")
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_scope_status_delegates_to_client_get_item(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.get_item.return_value = _child_item(
            "semantic-dataset", "d1", {"name": "x"}, meta={"scope": "organization"}
        )
        result = service.scope_status("prod", "dataset", "d1")
        mock.get_item.assert_called_once_with("semantic-dataset", "d1")
        assert result["scope"] == "organization"

    def test_scope_request_elevation_delegates(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.request_scope_elevation.return_value = _child_item(
            "semantic-metric", "m1", {"name": "x"}, meta={"scopeElevationRequestedAt": "t"}
        )
        result = service.scope_request_elevation("prod", "metric", "m1")
        mock.request_scope_elevation.assert_called_once_with("semantic-metric", "m1")
        assert result["scope_elevation_requested_at"] == "t"

    def test_scope_withdraw_elevation_delegates(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.withdraw_scope_elevation.return_value = _child_item(
            "semantic-metric", "m1", {"name": "x"}
        )
        service.scope_withdraw_elevation("prod", "metric", "m1")
        mock.withdraw_scope_elevation.assert_called_once_with("semantic-metric", "m1")

    def test_scope_elevate_delegates(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.elevate_to_organization.return_value = _child_item(
            "semantic-metric", "m1", {"name": "x"}, meta={"scope": "organization"}
        )
        result = service.scope_elevate("prod", "metric", "m1")
        mock.elevate_to_organization.assert_called_once_with("semantic-metric", "m1")
        assert result["scope"] == "organization"

    def test_scope_pending_delegates_with_pagination(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.list_organization_items.return_value = [
            _child_item("semantic-dataset", "d1", {"name": "x"}, meta={})
        ]
        result = service.scope_pending("prod", "dataset", limit=5, offset=1)
        mock.list_organization_items.assert_called_once_with(
            "semantic-dataset", pending_elevation_only=True, limit=5, offset=1
        )
        assert len(result) == 1

    def test_scope_grant_resolves_aliases_before_calling_client(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.get_item.return_value = _child_item(
            "semantic-dataset", "d1", {"name": "x"}, meta={"targetProjectIds": []}
        )
        service.scope_grant("prod", "dataset", "d1", add=["analytics"])
        mock.put_target_projects.assert_called_once_with("semantic-dataset", "d1", [1234])


# ---------------------------------------------------------------------------
# Regression: edit (DELETE+POST) must preserve the original item's scope
# ---------------------------------------------------------------------------


class TestEditPreservesScope:
    def test_edit_metric_preserves_targeted_scope_and_grants(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        original = _child_item(
            "semantic-metric",
            "m1",
            {"name": "rev", "sql": "1"},
            meta={"scope": "targeted", "targetProjectIds": [1234]},
        )

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item()]
            if item_type == "semantic-metric":
                return [original]
            return []

        mock.list_items.side_effect = _list
        mock.post_item.return_value = {"id": "m_new", "attributes": {"name": "rev"}}

        service.edit_metric("prod", None, current_name="rev", new_description="updated")

        mock.post_item.assert_called_once()
        _args, kwargs = mock.post_item.call_args
        assert kwargs["scope"] == "targeted"
        assert kwargs["target_project_ids"] == [1234]

    def test_edit_dataset_preserves_organization_scope(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        original = _child_item(
            "semantic-dataset",
            "d1",
            {"name": "fact_pnl", "tableId": "out.c-fin.fact_pnl"},
            meta={"scope": "organization"},
        )

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item()]
            if item_type == "semantic-dataset":
                return [original]
            return []

        mock.list_items.side_effect = _list
        mock.post_item.return_value = {"id": "d_new", "attributes": {"name": "fact_pnl"}}

        service.edit_dataset("prod", None, current_name="fact_pnl", new_grain="daily")

        _args, kwargs = mock.post_item.call_args
        assert kwargs["scope"] == "organization"
        assert kwargs["target_project_ids"] is None

    def test_edit_metric_defaults_to_project_scope_when_no_meta(self, tmp_path: Path) -> None:
        """Pre-PSGO-140 items (no meta.scope) edit as plain project scope, unchanged."""
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        original = _child_item("semantic-metric", "m1", {"name": "rev", "sql": "1"})

        def _list(item_type: str, model_uuid: str | None = None) -> list[dict[str, Any]]:
            if item_type == "semantic-model":
                return [_model_item()]
            if item_type == "semantic-metric":
                return [original]
            return []

        mock.list_items.side_effect = _list
        mock.post_item.return_value = {"id": "m_new", "attributes": {"name": "rev"}}

        service.edit_metric("prod", None, current_name="rev", new_sql="2")

        _args, kwargs = mock.post_item.call_args
        assert kwargs["scope"] == "project"
        assert kwargs["target_project_ids"] is None
