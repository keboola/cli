"""Tests for metastore scope/target-project/elevation support (PSGO-140).

Covers ``services._semantic_layer_scope`` (target resolution, grant merge,
elevation) and the service orchestration on top of it: ``scope_*``, scope
inheritance on child creates, and the overwrite paths (``import --overwrite``,
``promote``) keeping an item's scope.
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
    store.add_project(
        "eu-project",
        ProjectConfig(
            stack_url="https://connection.europe-west3.gcp.keboola.com",
            token=TEST_TOKEN,
            project_name="eu",
            project_id=77,
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
# services._semantic_layer_scope -- helpers
# ---------------------------------------------------------------------------


class TestResolveTargetProjectIds:
    def test_aliases_ids_and_comma_lists_resolve_and_dedupe(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        resolved = scope_helpers.resolve_target_project_ids(
            store, "prod", ["analytics,5678", "analytics", "9"]
        )
        assert resolved == [1234, 5678, 9]

    @pytest.mark.parametrize(
        ("target", "needle"),
        [
            ("ghost", "neither a registered project alias nor a numeric project ID"),
            ("eu-project", "different stack"),
        ],
    )
    def test_bad_target_is_invalid_argument_naming_the_option(
        self, tmp_path: Path, target: str, needle: str
    ) -> None:
        store = _make_store(tmp_path)
        with pytest.raises(KeboolaApiError) as excinfo:
            scope_helpers.resolve_target_project_ids(store, "prod", [target])
        assert excinfo.value.error_code == ErrorCode.INVALID_ARGUMENT
        assert "--target-project" in excinfo.value.message
        assert needle in excinfo.value.message

    def test_registered_project_without_id_is_invalid_argument(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.add_project(
            "no-id",
            ProjectConfig(
                stack_url="https://connection.keboola.com", token=TEST_TOKEN, project_id=None
            ),
        )
        with pytest.raises(KeboolaApiError) as excinfo:
            scope_helpers.resolve_target_project_ids(store, "prod", ["no-id"])
        assert excinfo.value.error_code == ErrorCode.INVALID_ARGUMENT


def _item(meta: dict[str, Any]) -> dict[str, Any]:
    return {"type": "semantic-dataset", "id": "d1", "attributes": {"name": "x"}, "meta": meta}


OWNED = {"scope": "targeted", "projectId": 5725, "targetProjectIds": [1, 2]}


class TestSetTargetProjects:
    def test_replace_sends_exact_set_without_reading_current(self) -> None:
        client = MagicMock()
        client.get_item.return_value = _item(OWNED)
        scope_helpers.set_target_projects(client, "semantic-dataset", "d1", replace=[9, 3, 9])
        client.put_target_projects.assert_called_once_with("semantic-dataset", "d1", [3, 9])
        assert client.get_item.call_count == 1  # only the report re-read

    def test_clear_via_empty_replace(self) -> None:
        client = MagicMock()
        client.get_item.return_value = _item(OWNED)
        scope_helpers.set_target_projects(client, "semantic-dataset", "d1", replace=[])
        client.put_target_projects.assert_called_once_with("semantic-dataset", "d1", [])

    @pytest.mark.parametrize(
        ("add", "remove", "expected"),
        [([3], [], [1, 2, 3]), ([], [1], [2]), ([3], [1], [2, 3])],
    )
    def test_owner_merge_applies_the_delta(self, add, remove, expected) -> None:
        client = MagicMock()
        client.get_item.return_value = _item(OWNED)
        scope_helpers.set_target_projects(
            client, "semantic-dataset", "d1", add=add, remove=remove, caller_project_id=5725
        )
        client.put_target_projects.assert_called_once_with("semantic-dataset", "d1", expected)

    def test_owner_with_no_grants_can_still_add(self) -> None:
        """The server omits an empty list; that is the owner's state after `scope set --clear`."""
        client = MagicMock()
        client.get_item.return_value = _item({"scope": "targeted", "projectId": 5725})
        scope_helpers.set_target_projects(
            client, "semantic-dataset", "d1", add=[3], caller_project_id=5725
        )
        client.put_target_projects.assert_called_once_with("semantic-dataset", "d1", [3])

    def test_unknown_own_project_id_is_refused_with_a_refresh_hint(self) -> None:
        client = MagicMock()
        client.get_item.return_value = _item(OWNED)
        with pytest.raises(KeboolaApiError) as excinfo:
            scope_helpers.set_target_projects(
                client, "semantic-dataset", "d1", add=[3], caller_project_id=None
            )
        assert "kbagent project refresh" in excinfo.value.message
        client.put_target_projects.assert_not_called()

    @pytest.mark.parametrize(
        "meta",
        [
            # org admin from another project: the server hides the grants from a non-owner
            {"scope": "targeted", "projectId": 5725},
            {"scope": "targeted", "projectId": 5725, "targetProjectIds": [1]},
        ],
    )
    def test_non_owner_merge_is_refused_instead_of_wiping_grants(self, meta) -> None:
        client = MagicMock()
        client.get_item.return_value = _item(meta)
        with pytest.raises(KeboolaApiError) as excinfo:
            scope_helpers.set_target_projects(
                client, "semantic-dataset", "d1", add=[3], caller_project_id=999
            )
        assert excinfo.value.error_code == ErrorCode.INVALID_ARGUMENT
        assert "scope set --target-project" in excinfo.value.message
        client.put_target_projects.assert_not_called()


class TestElevation:
    @pytest.mark.parametrize(
        ("helper", "client_method"),
        [
            (scope_helpers.request_elevation, "request_scope_elevation"),
            (scope_helpers.withdraw_elevation, "withdraw_scope_elevation"),
            (scope_helpers.elevate_to_organization, "elevate_to_organization"),
        ],
    )
    def test_reports_grants_from_a_reread_not_the_endpoint_response(
        self, helper, client_method
    ) -> None:
        client = MagicMock()
        getattr(client, client_method).return_value = {"meta": {"scope": "targeted"}}  # no grants
        client.get_item.return_value = _item(OWNED)
        assert helper(client, "semantic-dataset", "d1")["target_project_ids"] == [1, 2]

    def test_dry_run_elevation_never_patches(self) -> None:
        client = MagicMock()
        client.get_item.return_value = _item(OWNED)
        result = scope_helpers.elevate_to_organization(
            client, "semantic-dataset", "d1", dry_run=True
        )
        client.elevate_to_organization.assert_not_called()
        assert result["dry_run"] is True
        assert result["would_set_scope"] == "organization"

    @pytest.mark.parametrize(("returned", "has_more"), [(3, False), (4, True)])
    def test_request_list_pages_by_fetching_one_extra_row(self, returned, has_more) -> None:
        client = MagicMock()
        client.list_organization_items.return_value = [_item({}) for _ in range(returned)]
        page = scope_helpers.list_pending_elevations(client, "semantic-dataset", limit=3, offset=6)
        client.list_organization_items.assert_called_once_with(
            "semantic-dataset", pending_elevation_only=True, limit=4, offset=6
        )
        assert (page["limit"], page["offset"], page["has_more"]) == (3, 6, has_more)
        assert len(page["items"]) == 3


# ---------------------------------------------------------------------------
# SemanticLayerService.scope_*
# ---------------------------------------------------------------------------


class TestServiceScopeMethods:
    def test_unknown_type_is_invalid_argument(self, tmp_path: Path) -> None:
        service, _ = _make_service(_make_store(tmp_path))
        with pytest.raises(KeboolaApiError) as excinfo:
            service.scope_get("prod", "table", "x")
        assert excinfo.value.error_code == ErrorCode.INVALID_ARGUMENT

    def test_update_targets_resolves_targets_and_passes_the_callers_project(
        self, tmp_path: Path
    ) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_item.return_value = _item(OWNED)
        service.scope_update_targets("prod", "dataset", "d1", add=["analytics,5678"])
        mock.put_target_projects.assert_called_once_with(
            "semantic-dataset", "d1", [1, 2, 1234, 5678]
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},  # nothing
            {"scope": "organization", "target_projects": ["analytics"]},
            {"scope": "organization", "clear": True},
            {"target_projects": ["analytics"], "clear": True},
            {"scope": "project"},  # scope only ever moves to organization
        ],
    )
    def test_set_requires_exactly_one_mode(self, tmp_path: Path, kwargs) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        with pytest.raises(KeboolaApiError) as excinfo:
            service.scope_set("prod", "dataset", "d1", **kwargs)
        assert excinfo.value.error_code == ErrorCode.INVALID_ARGUMENT
        mock.put_target_projects.assert_not_called()
        mock.elevate_to_organization.assert_not_called()

    def test_set_modes_reach_the_right_metastore_call(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_item.return_value = _item(OWNED)
        service.scope_set("prod", "dataset", "d1", target_projects=["analytics"])
        mock.put_target_projects.assert_called_with("semantic-dataset", "d1", [1234])
        service.scope_set("prod", "dataset", "d1", clear=True)
        mock.put_target_projects.assert_called_with("semantic-dataset", "d1", [])
        service.scope_set("prod", "dataset", "d1", scope="organization")
        mock.elevate_to_organization.assert_called_once_with("semantic-dataset", "d1")

    def test_set_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_item.return_value = _item(OWNED)
        result = service.scope_set(
            "prod", "dataset", "d1", target_projects=["analytics"], dry_run=True
        )
        assert result["would_set_target_project_ids"] == [1234]
        mock.put_target_projects.assert_not_called()

    def test_request_list_defaults(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.list_organization_items.return_value = []
        page = service.scope_request_list("prod", "dataset")
        assert page == {"items": [], "limit": 50, "offset": 0, "has_more": False}


# ---------------------------------------------------------------------------
# Creating with a scope / inheriting the model's scope
# ---------------------------------------------------------------------------


def _service_with_model(tmp_path: Path, model_meta: dict[str, Any] | None):
    service, mock = _make_service(_make_store(tmp_path))
    model = _model_item("U", "m")
    if model_meta is not None:
        model["meta"] = model_meta
    mock.list_items.return_value = [model]
    mock.get_item.return_value = model
    mock.post_item.return_value = {"id": "new", "attributes": {}}
    return service, mock


class TestChildScopeInheritance:
    @pytest.mark.parametrize(
        ("model_meta", "scope", "targets", "expected_scope", "expected_ids"),
        [
            (None, None, None, "project", None),
            ({"scope": "organization"}, None, None, "organization", None),
            ({"scope": "targeted", "targetProjectIds": [5, 6]}, None, None, "targeted", [5, 6]),
            ({"scope": "organization"}, "project", None, "project", None),  # explicit wins
            (None, "targeted", ["analytics,5678"], "targeted", [1234, 5678]),
            (None, "organization", None, "organization", None),
        ],
    )
    def test_add_glossary_scope(
        self, tmp_path: Path, model_meta, scope, targets, expected_scope, expected_ids
    ) -> None:
        service, mock = _service_with_model(tmp_path, model_meta)
        service.add_glossary(
            "prod", None, term="t", definition="d", scope=scope, target_projects=targets
        )
        kwargs = mock.post_item.call_args.kwargs
        assert kwargs["scope"] == expected_scope
        assert kwargs["target_project_ids"] == expected_ids

    def test_inherited_org_scope_denied_gets_a_hint(self, tmp_path: Path) -> None:
        service, mock = _service_with_model(tmp_path, {"scope": "organization"})
        mock.post_item.side_effect = KeboolaApiError(
            message="Insufficient permissions", status_code=403, error_code=ErrorCode.ACCESS_DENIED
        )
        with pytest.raises(KeboolaApiError) as excinfo:
            service.add_glossary("prod", None, term="t", definition="d")
        assert "--scope project" in excinfo.value.message
        assert "organization" in excinfo.value.message

    def test_inherited_targeted_scope_denied_gets_no_org_admin_hint(self, tmp_path: Path) -> None:
        """A project admin may create `targeted`, so a 403 there has another cause."""
        service, mock = _service_with_model(
            tmp_path, {"scope": "targeted", "targetProjectIds": [5]}
        )
        mock.post_item.side_effect = KeboolaApiError(
            message="Insufficient permissions", status_code=403, error_code=ErrorCode.ACCESS_DENIED
        )
        with pytest.raises(KeboolaApiError) as excinfo:
            service.add_glossary("prod", None, term="t", definition="d")
        assert excinfo.value.message == "Insufficient permissions"

    def test_explicit_scope_denied_is_not_rewritten(self, tmp_path: Path) -> None:
        service, mock = _service_with_model(tmp_path, {"scope": "organization"})
        mock.post_item.side_effect = KeboolaApiError(
            message="Insufficient permissions", status_code=403, error_code=ErrorCode.ACCESS_DENIED
        )
        with pytest.raises(KeboolaApiError) as excinfo:
            service.add_glossary("prod", None, term="t", scope="organization")
        assert excinfo.value.message == "Insufficient permissions"

    @pytest.mark.parametrize(
        ("meta", "expected"),
        [
            (None, "project"),
            ({"scope": "organization"}, "organization"),
            ({"scope": "targeted"}, "targeted"),
        ],
    )
    def test_child_scope_reports_the_models_scope(self, tmp_path: Path, meta, expected) -> None:
        service, _ = _service_with_model(tmp_path, meta)
        assert service.child_scope("prod", None) == expected

    def test_model_create_defaults_to_project_and_resolves_targets(self, tmp_path: Path) -> None:
        service, mock = _service_with_model(tmp_path, None)
        service.create_model("prod", "m")
        assert mock.post_item.call_args.kwargs["scope"] == "project"
        service.create_model("prod", "m2", scope="targeted", target_projects=["analytics"])
        assert mock.post_item.call_args.kwargs["target_project_ids"] == [1234]

    def test_unknown_target_alias_fails_before_any_write(self, tmp_path: Path) -> None:
        service, mock = _service_with_model(tmp_path, None)
        with pytest.raises(KeboolaApiError) as excinfo:
            service.create_model("prod", "m", scope="targeted", target_projects=["ghost"])
        assert excinfo.value.error_code == ErrorCode.INVALID_ARGUMENT
        mock.post_item.assert_not_called()


# ---------------------------------------------------------------------------
# Overwrite paths keep an item's scope (PUT in place, never DELETE+POST)
# ---------------------------------------------------------------------------


class TestEditAndOverwriteKeepScope:
    def test_edit_dataset_puts_in_place_with_the_items_scope(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        dataset = _child_item(
            "semantic-dataset",
            "d1",
            {"name": "x", "tableId": "a.b.c"},
            {"scope": "targeted", "targetProjectIds": [1, 2]},
        )
        mock.list_items.side_effect = lambda t, m=None: (
            [_model_item("U", "m")] if t == "semantic-model" else [dataset]
        )
        mock.put_item.return_value = {"id": "d1", "attributes": {"name": "x"}}
        service.edit_dataset("prod", None, current_name="x", new_description="d")
        args, kwargs = mock.put_item.call_args
        assert args[:2] == ("semantic-dataset", "d1")
        assert kwargs == {}
        mock.delete_item.assert_not_called()
        mock.post_item.assert_not_called()

    def test_promote_overwrite_keeps_the_target_items_scope(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        src, tgt = MagicMock(), MagicMock()
        for m in (src, tgt):
            m.__enter__ = MagicMock(return_value=m)
            m.__exit__ = MagicMock(return_value=False)
        clients = iter([src, tgt])
        service = SemanticLayerService(
            config_store=store, metastore_client_factory=lambda url, token: next(clients)
        )
        src.list_items.side_effect = lambda t, m=None: (
            [_model_item("U_S", "src")]
            if t == "semantic-model"
            else [_child_item("semantic-metric", "m1", {"name": "b", "sql": "NEW"})]
            if t == "semantic-metric"
            else []
        )
        existing = _child_item(
            "semantic-metric", "tm1", {"name": "b", "sql": "OLD"}, {"scope": "organization"}
        )
        tgt.list_items.side_effect = lambda t, m=None: (
            [_model_item("U_T", "tgt")]
            if t == "semantic-model"
            else [existing]
            if t == "semantic-metric"
            else []
        )
        service.promote_model(from_project="prod", to_project="analytics")
        args, kwargs = tgt.put_item.call_args
        assert args[:3] == ("semantic-metric", "tm1", "b")
        assert kwargs == {}  # a PUT cannot carry scope, so it is untouched
        tgt.delete_item.assert_not_called()
        tgt.post_item.assert_not_called()
