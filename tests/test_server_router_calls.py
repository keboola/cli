"""Per-endpoint regression tests for kbagent serve REST router -> service call parity.

Each test verifies that the router passes the CORRECT keyword argument names to
the underlying service method.  The pattern that failed in the past was router code
using the wrong kwarg name (e.g. ``new_name=`` instead of ``name=``, ``regex=``
instead of ``use_regex=``, etc.), which caused a TypeError on first real use while
having zero test coverage to catch the drift.

Pattern
-------
1. Build the FastAPI app via ``create_app``.
2. Replace the ServiceRegistry dependency with a hand-rolled MagicMock registry.
3. Call the endpoint via ``TestClient``.
4. Assert ``mock.<method>.assert_called_once_with(...)`` or inspect
   ``call_args.kwargs`` for the specific kwarg under test.

The tests do NOT exercise real Keboola API calls.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

if importlib.util.find_spec("fastapi") is None:  # pragma: no cover
    pytest.skip(
        "FastAPI not installed; run `uv pip install -e '.[server]'`", allow_module_level=True
    )

from fastapi.testclient import TestClient

from keboola_agent_cli.server import create_app
from keboola_agent_cli.server.dependencies import ServiceRegistry, get_manage_token, get_registry

AUTH = {"Authorization": "Bearer test-token"}
PROJECT = "my-proj"
COMPONENT = "keboola.ex-http"
CONFIG_ID = "42"
ROW_ID = "7"
APP_ID = "1234"
TABLE_ID = "in.c-main.mytable"
FILE_ID = 999


def _make_app_with_registry(tmp_path: Path, registry: ServiceRegistry) -> Any:
    """Create the FastAPI app and override the registry dependency."""
    app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    app.dependency_overrides[get_registry] = lambda: registry
    return app


def _mock_registry(**services: Any) -> ServiceRegistry:
    """Return a bare ServiceRegistry with the given service mocks attached."""
    registry = ServiceRegistry.__new__(ServiceRegistry)
    for name, mock in services.items():
        setattr(registry, name, mock)
    return registry


# ---------------------------------------------------------------------------
# configs.py  POST /{p}/{c}/{cfg}/rename
# Service: config.rename_config(name=...)   (was new_name= in broken version)
# ---------------------------------------------------------------------------


def test_config_rename_passes_name_kwarg(tmp_path: Path) -> None:
    """Router must pass ``name=`` not ``new_name=`` to ConfigService.rename_config."""
    config_svc = MagicMock()
    config_svc.rename_config.return_value = {"old_name": "old", "new_name": "fresh"}
    registry = _mock_registry(config=config_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            f"/configs/{PROJECT}/{COMPONENT}/{CONFIG_ID}/rename",
            headers=AUTH,
            json={"name": "fresh"},
        )

    assert res.status_code == 200, res.text
    kwargs = config_svc.rename_config.call_args.kwargs
    assert kwargs["name"] == "fresh", f"Expected name='fresh', got kwargs={kwargs}"
    assert "new_name" not in kwargs, "Router must not pass new_name= to rename_config"


# ---------------------------------------------------------------------------
# configs.py  POST /{p}/{c}/{cfg}/folder
# Service: config.set_config_folder(folder_name=...)   (was folder= in broken version)
# ---------------------------------------------------------------------------


def test_config_set_folder_passes_folder_name_kwarg(tmp_path: Path) -> None:
    """Router must pass ``folder_name=`` not ``folder=`` to ConfigService.set_config_folder."""
    config_svc = MagicMock()
    config_svc.set_config_folder.return_value = {"folder": "MyFolder"}
    registry = _mock_registry(config=config_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            f"/configs/{PROJECT}/{COMPONENT}/{CONFIG_ID}/folder",
            headers=AUTH,
            json={"folder": "MyFolder"},
        )

    assert res.status_code == 200, res.text
    kwargs = config_svc.set_config_folder.call_args.kwargs
    assert kwargs["folder_name"] == "MyFolder", (
        f"Expected folder_name='MyFolder', got kwargs={kwargs}"
    )
    assert "folder" not in kwargs, "Router must not pass folder= to set_config_folder"


# ---------------------------------------------------------------------------
# configs.py  GET /configs/search
# Service: config.search_configs(use_regex=...)   (was regex= in broken version)
# ---------------------------------------------------------------------------


def test_config_search_passes_use_regex_kwarg(tmp_path: Path) -> None:
    """Router must pass ``use_regex=`` not ``regex=`` to ConfigService.search_configs."""
    config_svc = MagicMock()
    config_svc.search_configs.return_value = {"matches": [], "errors": [], "stats": {}}
    registry = _mock_registry(config=config_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get(
            "/configs/search",
            headers=AUTH,
            params={"query": "hello", "regex": True},
        )

    assert res.status_code == 200, res.text
    kwargs = config_svc.search_configs.call_args.kwargs
    assert "use_regex" in kwargs, f"Expected use_regex in kwargs, got {kwargs}"
    assert kwargs["use_regex"] is True
    assert "regex" not in kwargs, "Router must not pass regex= to search_configs"


# ---------------------------------------------------------------------------
# configs.py  POST /{p}/{c}  (create config)
# Service: config.create_config(description="" when body.description is None)
# ---------------------------------------------------------------------------


def test_config_create_defaults_description_to_empty_string(tmp_path: Path) -> None:
    """When description is omitted from the request, router must pass description='' (not None)."""
    config_svc = MagicMock()
    config_svc.create_config.return_value = {"id": "99", "name": "New Config"}
    registry = _mock_registry(config=config_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            f"/configs/{PROJECT}/{COMPONENT}",
            headers=AUTH,
            json={"name": "New Config"},  # description omitted
        )

    assert res.status_code == 200, res.text
    kwargs = config_svc.create_config.call_args.kwargs
    assert kwargs.get("description") == "", (
        f"Expected description='', got description={kwargs.get('description')!r}"
    )


# ---------------------------------------------------------------------------
# configs.py  POST /{p}/{c}/{cfg}/rows  (create row)
# Service: config.create_config_row(description="" when body.description is None)
# ---------------------------------------------------------------------------


def test_config_create_row_defaults_description_to_empty_string(tmp_path: Path) -> None:
    """When description is omitted, router must pass description='' (not None) to create_config_row."""
    config_svc = MagicMock()
    config_svc.create_config_row.return_value = {"id": "55", "name": "Row One"}
    registry = _mock_registry(config=config_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            f"/configs/{PROJECT}/{COMPONENT}/{CONFIG_ID}/rows",
            headers=AUTH,
            json={"name": "Row One"},  # description omitted
        )

    assert res.status_code == 200, res.text
    kwargs = config_svc.create_config_row.call_args.kwargs
    assert kwargs.get("description") == "", (
        f"Expected description='', got description={kwargs.get('description')!r}"
    )


# ---------------------------------------------------------------------------
# storage.py  POST /{p}/{tbl}/describe-columns
# Service: storage.describe_columns(columns=...)  (was column_descriptions= in broken version)
# ---------------------------------------------------------------------------


def test_storage_describe_columns_passes_columns_kwarg(tmp_path: Path) -> None:
    """Router must pass ``columns=`` not ``column_descriptions=`` to StorageService.describe_columns."""
    storage_svc = MagicMock()
    storage_svc.describe_columns.return_value = {"updated": 2}
    registry = _mock_registry(storage=storage_svc)
    app = _make_app_with_registry(tmp_path, registry)

    col_map = {"col_a": "First column", "col_b": "Second column"}
    with TestClient(app) as client:
        res = client.post(
            f"/storage/columns/{PROJECT}/{TABLE_ID}/describe",
            headers=AUTH,
            json={"columns": col_map},
        )

    assert res.status_code == 200, res.text
    kwargs = storage_svc.describe_columns.call_args.kwargs
    assert "columns" in kwargs, f"Expected columns in kwargs, got {kwargs}"
    assert kwargs["columns"] == col_map
    assert "column_descriptions" not in kwargs, (
        "Router must not pass column_descriptions= to describe_columns"
    )


# ---------------------------------------------------------------------------
# storage.py  GET /{p}/{fid}/file-download
# Service: storage.download_file(output_path=...)  (was output_dir= in broken version)
# ---------------------------------------------------------------------------


def test_storage_file_download_passes_output_path_kwarg(tmp_path: Path) -> None:
    """Router must pass ``output_path=`` not ``output_dir=`` to StorageService.download_file."""
    storage_svc = MagicMock()
    # Return a dict with a local_path pointing to a real file so FileResponse works
    fake_file = tmp_path / "downloaded.csv"
    fake_file.write_text("a,b\n1,2\n")
    storage_svc.download_file.return_value = {"local_path": str(fake_file)}
    registry = _mock_registry(storage=storage_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get(
            f"/storage/files/{PROJECT}/{FILE_ID}/download",
            headers=AUTH,
        )

    assert res.status_code == 200, res.text
    kwargs = storage_svc.download_file.call_args.kwargs
    assert "output_path" in kwargs, f"Expected output_path in kwargs, got {kwargs}"
    assert "output_dir" not in kwargs, "Router must not pass output_dir= to download_file"


# ---------------------------------------------------------------------------
# data_apps.py  GET /{p}/{app}/password
# Service: data_app.get_data_app_password(manage_token=...)
# Also: omitting X-Manage-Token header returns 401.
# ---------------------------------------------------------------------------


def test_data_app_password_passes_manage_token_kwarg(tmp_path: Path) -> None:
    """Router must pass ``manage_token=`` to DataAppService.get_data_app_password."""
    data_app_svc = MagicMock()
    data_app_svc.get_data_app_password.return_value = {"password": "s3cr3t"}
    registry = _mock_registry(data_app=data_app_svc)
    app = _make_app_with_registry(tmp_path, registry)
    # Override get_manage_token to provide a token
    app.dependency_overrides[get_manage_token] = lambda: "mgmt-tok"

    with TestClient(app) as client:
        res = client.get(
            f"/data-apps/{PROJECT}/{APP_ID}/password",
            headers=AUTH,
        )

    assert res.status_code == 200, res.text
    kwargs = data_app_svc.get_data_app_password.call_args.kwargs
    assert kwargs.get("manage_token") == "mgmt-tok", (
        f"Expected manage_token='mgmt-tok', got kwargs={kwargs}"
    )


def test_data_app_password_missing_manage_token_returns_401(tmp_path: Path) -> None:
    """GET /{p}/{app}/password without X-Manage-Token must return 401."""
    data_app_svc = MagicMock()
    registry = _mock_registry(data_app=data_app_svc)
    app = _make_app_with_registry(tmp_path, registry)
    # Explicitly provide None (no token) -- this mirrors the real behaviour when
    # the header is absent; no dependency override so the real get_manage_token runs.

    with TestClient(app) as client:
        res = client.get(
            f"/data-apps/{PROJECT}/{APP_ID}/password",
            headers=AUTH,  # Bearer auth present but NO X-Manage-Token
        )

    assert res.status_code == 401, f"Expected 401, got {res.status_code}: {res.text}"
    body = res.json()
    # The app wraps HTTPException via a global handler into
    # {"status": "error", "error": {"code": ..., "message": ...}}.
    msg = body.get("detail") or body.get("error", {}).get("message", "")
    assert "X-Manage-Token" in msg, f"Expected message mentioning X-Manage-Token, got: {body}"
    data_app_svc.get_data_app_password.assert_not_called()


# ---------------------------------------------------------------------------
# configs.py  PUT /{p}/{c}/{cfg}/variables
# Service: variables.set_variables(...)  -- must NOT receive dry_run kwarg
# ---------------------------------------------------------------------------


def test_config_variables_set_no_dry_run_kwarg(tmp_path: Path) -> None:
    """PUT /variables router must NOT pass dry_run= to VariablesService.set_variables."""
    variables_svc = MagicMock()
    variables_svc.set_variables.return_value = {"variables": []}
    registry = _mock_registry(variables=variables_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.put(
            f"/configs/{PROJECT}/{COMPONENT}/{CONFIG_ID}/variables",
            headers=AUTH,
            json={"variables": {"KEY": "val"}},
        )

    assert res.status_code == 200, res.text
    kwargs = variables_svc.set_variables.call_args.kwargs
    assert "dry_run" not in kwargs, (
        f"Router must not pass dry_run= to set_variables, but got kwargs={kwargs}"
    )
    assert kwargs.get("variables") == {"KEY": "val"}


# ---------------------------------------------------------------------------
# feature.py  -- all 7 endpoints require X-Manage-Token and pass it through.
# ---------------------------------------------------------------------------


def test_feature_list_passes_manage_token(tmp_path: Path) -> None:
    """GET /feature/{p}/list must forward manage_token to list_stack_features."""
    feature_svc = MagicMock()
    feature_svc.list_stack_features.return_value = {"features": []}
    registry = _mock_registry(feature=feature_svc)
    app = _make_app_with_registry(tmp_path, registry)
    app.dependency_overrides[get_manage_token] = lambda: "mgmt-tok"

    with TestClient(app) as client:
        res = client.get(f"/feature/{PROJECT}/list", headers=AUTH)

    assert res.status_code == 200, res.text
    kwargs = feature_svc.list_stack_features.call_args.kwargs
    assert kwargs == {"manage_token": "mgmt-tok", "alias": PROJECT}


def test_feature_project_show_passes_manage_token(tmp_path: Path) -> None:
    feature_svc = MagicMock()
    feature_svc.list_project_features.return_value = {"features": []}
    registry = _mock_registry(feature=feature_svc)
    app = _make_app_with_registry(tmp_path, registry)
    app.dependency_overrides[get_manage_token] = lambda: "mgmt-tok"

    with TestClient(app) as client:
        res = client.get(f"/feature/{PROJECT}/project-show", headers=AUTH)

    assert res.status_code == 200, res.text
    assert feature_svc.list_project_features.call_args.kwargs == {
        "manage_token": "mgmt-tok",
        "alias": PROJECT,
    }


def test_feature_project_add_passes_body_and_token(tmp_path: Path) -> None:
    feature_svc = MagicMock()
    feature_svc.add_project_feature.return_value = {"status": "added"}
    registry = _mock_registry(feature=feature_svc)
    app = _make_app_with_registry(tmp_path, registry)
    app.dependency_overrides[get_manage_token] = lambda: "mgmt-tok"

    with TestClient(app) as client:
        res = client.post(
            f"/feature/{PROJECT}/project-add",
            headers=AUTH,
            json={"feature": "data-streams", "dry_run": True},
        )

    assert res.status_code == 200, res.text
    assert feature_svc.add_project_feature.call_args.kwargs == {
        "manage_token": "mgmt-tok",
        "alias": PROJECT,
        "feature": "data-streams",
        "dry_run": True,
    }


def test_feature_project_remove_passes_body_and_token(tmp_path: Path) -> None:
    feature_svc = MagicMock()
    feature_svc.remove_project_feature.return_value = {"status": "removed"}
    registry = _mock_registry(feature=feature_svc)
    app = _make_app_with_registry(tmp_path, registry)
    app.dependency_overrides[get_manage_token] = lambda: "mgmt-tok"

    with TestClient(app) as client:
        res = client.post(
            f"/feature/{PROJECT}/project-remove",
            headers=AUTH,
            json={"feature": "data-streams"},
        )

    assert res.status_code == 200, res.text
    kwargs = feature_svc.remove_project_feature.call_args.kwargs
    assert kwargs["manage_token"] == "mgmt-tok"
    assert kwargs["feature"] == "data-streams"
    assert kwargs["dry_run"] is False


def test_feature_user_show_passes_email_and_token(tmp_path: Path) -> None:
    feature_svc = MagicMock()
    feature_svc.list_user_features.return_value = {"features": []}
    registry = _mock_registry(feature=feature_svc)
    app = _make_app_with_registry(tmp_path, registry)
    app.dependency_overrides[get_manage_token] = lambda: "mgmt-tok"

    with TestClient(app) as client:
        res = client.get(
            f"/feature/{PROJECT}/user-show",
            headers=AUTH,
            params={"email": "user@example.com"},
        )

    assert res.status_code == 200, res.text
    assert feature_svc.list_user_features.call_args.kwargs == {
        "manage_token": "mgmt-tok",
        "alias": PROJECT,
        "email": "user@example.com",
    }


def test_feature_user_add_passes_body_and_token(tmp_path: Path) -> None:
    feature_svc = MagicMock()
    feature_svc.add_user_feature.return_value = {"status": "added"}
    registry = _mock_registry(feature=feature_svc)
    app = _make_app_with_registry(tmp_path, registry)
    app.dependency_overrides[get_manage_token] = lambda: "mgmt-tok"

    with TestClient(app) as client:
        res = client.post(
            f"/feature/{PROJECT}/user-add",
            headers=AUTH,
            json={"email": "user@example.com", "feature": "early-adopter-preview"},
        )

    assert res.status_code == 200, res.text
    assert feature_svc.add_user_feature.call_args.kwargs == {
        "manage_token": "mgmt-tok",
        "alias": PROJECT,
        "email": "user@example.com",
        "feature": "early-adopter-preview",
        "dry_run": False,
    }


def test_feature_list_missing_manage_token_returns_401(tmp_path: Path) -> None:
    """No X-Manage-Token header -> 401 and the service is never called."""
    feature_svc = MagicMock()
    registry = _mock_registry(feature=feature_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get(f"/feature/{PROJECT}/list", headers=AUTH)

    assert res.status_code == 401, res.text
    body = res.json()
    msg = body.get("detail") or body.get("error", {}).get("message", "")
    assert "X-Manage-Token" in msg, f"Expected X-Manage-Token mention, got: {body}"
    feature_svc.list_stack_features.assert_not_called()


# ---------------------------------------------------------------------------
# dev_portal.py  GET /dev-portal/apps  and  GET /dev-portal/apps/{app}
# Service: dev_portal.list_apps(alias, vendor) / get_app(alias, vendor, app_id)
# ---------------------------------------------------------------------------


def test_dev_portal_list_apps_passes_alias_and_vendor(tmp_path: Path) -> None:
    """GET /dev-portal/apps must call list_apps with the resolved alias + vendor."""
    dp_svc = MagicMock()
    dp_svc.list_apps.return_value = [{"id": "keboola.ex-a"}]
    registry = _mock_registry(dev_portal=dp_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get("/dev-portal/apps?vendor=keboola&identity=alpha", headers=AUTH)

    assert res.status_code == 200, res.text
    dp_svc.list_apps.assert_called_once_with("alpha", "keboola")


def test_dev_portal_get_app_splits_vendor_from_app_id(tmp_path: Path) -> None:
    """GET /dev-portal/apps/{app} must split VENDOR.APP_ID and pass both."""
    dp_svc = MagicMock()
    dp_svc.get_app.return_value = {"id": "keboola.ex-a", "name": "Hello"}
    registry = _mock_registry(dev_portal=dp_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get("/dev-portal/apps/keboola.ex-a?identity=alpha", headers=AUTH)

    assert res.status_code == 200, res.text
    dp_svc.get_app.assert_called_once_with("alpha", "keboola", "keboola.ex-a")


def test_dev_portal_get_app_rejects_app_without_vendor(tmp_path: Path) -> None:
    """An app id missing the VENDOR. prefix is a 400, not a service call."""
    dp_svc = MagicMock()
    registry = _mock_registry(dev_portal=dp_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get("/dev-portal/apps/no-dot?identity=alpha", headers=AUTH)

    assert res.status_code == 400, res.text
    dp_svc.get_app.assert_not_called()


def test_dev_portal_list_falls_back_to_default_identity(tmp_path: Path) -> None:
    """Without ?identity=, the router resolves the configured default identity."""
    dp_svc = MagicMock()
    dp_svc.current_identity.return_value = "default-alias"
    dp_svc.list_apps.return_value = []
    registry = _mock_registry(dev_portal=dp_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get("/dev-portal/apps?vendor=keboola", headers=AUTH)

    assert res.status_code == 200, res.text
    dp_svc.list_apps.assert_called_once_with("default-alias", "keboola")


def test_dev_portal_list_no_identity_no_default_is_400(tmp_path: Path) -> None:
    """No explicit identity and no default configured -> 400, no service call."""
    dp_svc = MagicMock()
    dp_svc.current_identity.return_value = ""
    registry = _mock_registry(dev_portal=dp_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get("/dev-portal/apps?vendor=keboola", headers=AUTH)

    assert res.status_code == 400, res.text
    dp_svc.list_apps.assert_not_called()


# ---------------------------------------------------------------------------
# semantic_layer.py  reference-data routes -> SemanticLayerService parity
# ---------------------------------------------------------------------------


def test_reference_data_list_route(tmp_path: Path) -> None:
    """GET /semantic-layer/reference-data -> list_reference_data(alias=, model_name_or_uuid=)."""
    sl = MagicMock()
    sl.list_reference_data.return_value = {"project": PROJECT, "reference_data": []}
    app = _make_app_with_registry(tmp_path, _mock_registry(semantic_layer=sl))
    resp = TestClient(app).get(
        "/semantic-layer/reference-data",
        params={"project": PROJECT, "model": "m"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    sl.list_reference_data.assert_called_once_with(alias=PROJECT, model_name_or_uuid="m")


def test_reference_data_get_route(tmp_path: Path) -> None:
    """GET /semantic-layer/reference-data/{id} -> get_reference_data(alias=, record_id=)."""
    sl = MagicMock()
    sl.get_reference_data.return_value = {"id": "r1", "members": []}
    app = _make_app_with_registry(tmp_path, _mock_registry(semantic_layer=sl))
    resp = TestClient(app).get(
        "/semantic-layer/reference-data/r1", params={"project": PROJECT}, headers=AUTH
    )
    assert resp.status_code == 200, resp.text
    sl.get_reference_data.assert_called_once_with(alias=PROJECT, record_id="r1")


def test_reference_data_set_route(tmp_path: Path) -> None:
    """PUT /semantic-layer/reference-data -> set_reference_data(...) with all kwargs."""
    sl = MagicMock()
    sl.set_reference_data.return_value = {"id": "r1", "action": "created"}
    app = _make_app_with_registry(tmp_path, _mock_registry(semantic_layer=sl))
    resp = TestClient(app).put(
        "/semantic-layer/reference-data",
        json={
            "project": PROJECT,
            "dimension": "chart_of_accounts",
            "members": [{"account_code": "4011"}],
            "dataset_id": "in.c-f.DIM_COA",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    sl.set_reference_data.assert_called_once_with(
        alias=PROJECT,
        model_name_or_uuid=None,
        dimension="chart_of_accounts",
        members=[{"account_code": "4011"}],
        dataset_id="in.c-f.DIM_COA",
        description=None,
    )


def test_reference_data_delete_route(tmp_path: Path) -> None:
    """DELETE /semantic-layer/reference-data/{id} -> delete_reference_data(alias=, record_id=)."""
    sl = MagicMock()
    sl.delete_reference_data.return_value = {"removed": {"id": "r1"}}
    app = _make_app_with_registry(tmp_path, _mock_registry(semantic_layer=sl))
    resp = TestClient(app).delete(
        "/semantic-layer/reference-data/r1", params={"project": PROJECT}, headers=AUTH
    )
    assert resp.status_code == 200, resp.text
    sl.delete_reference_data.assert_called_once_with(alias=PROJECT, record_id="r1")


# ---------------------------------------------------------------------------
# flows.py  POST /flows/validate  +  GET /flows/{project}/schema
# New in 0.57.0 -- mirror `flow validate` / `flow schema --full`.
# ---------------------------------------------------------------------------

_CF_PHASES = [{"id": "p1", "name": "Extract", "next": [{"id": "n1", "goto": None}]}]
_CF_TASKS = [
    {
        "id": "t1",
        "name": "Run extractor",
        "phase": "p1",
        "enabled": True,
        "task": {"type": "job", "componentId": "keboola.ex-http", "configId": "1", "mode": "run"},
    }
]


def test_flows_validate_without_project_is_semantic_only(tmp_path: Path) -> None:
    """No `project` in body -> no schema fetch, semantic-only note, valid payload passes.

    Also guards FastAPI route ordering: /flows/validate must NOT be captured by
    POST /flows/{project} (which would call create_flow).
    """
    flow_svc = MagicMock()
    registry = _mock_registry(flow=flow_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            "/flows/validate",
            headers=AUTH,
            json={"phases": _CF_PHASES, "tasks": _CF_TASKS},
        )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["valid"] is True, body
    assert any("no schema source" in n for n in body["notes"]), body
    flow_svc.fetch_flow_schema.assert_not_called()
    flow_svc.create_flow.assert_not_called()


def test_flows_validate_with_project_fetches_live_schema(tmp_path: Path) -> None:
    """`project` in body -> fetch_flow_schema(alias) is called and schema is applied."""
    flow_svc = MagicMock()
    flow_svc.fetch_flow_schema.return_value = (
        {"type": "object", "required": ["phases", "tasks"]},
        None,
    )
    registry = _mock_registry(flow=flow_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            "/flows/validate",
            headers=AUTH,
            json={"phases": _CF_PHASES, "tasks": _CF_TASKS, "project": PROJECT},
        )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["valid"] is True, body
    assert body["notes"] == [], body
    flow_svc.fetch_flow_schema.assert_called_once_with(PROJECT)


def test_flows_validate_schema_fetch_failure_degrades(tmp_path: Path) -> None:
    """Fetch failure -> semantic-only validation + skip reason in notes, never 5xx."""
    flow_svc = MagicMock()
    flow_svc.fetch_flow_schema.return_value = (None, "AI Service unreachable")
    registry = _mock_registry(flow=flow_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            "/flows/validate",
            headers=AUTH,
            json={"phases": _CF_PHASES, "tasks": _CF_TASKS, "project": PROJECT},
        )

    assert res.status_code == 200, res.text
    body = res.json()
    assert any("AI Service unreachable" in n for n in body["notes"]), body


def test_flows_validate_reports_semantic_errors(tmp_path: Path) -> None:
    """Semantic violation (task references missing phase) -> valid=false with errors."""
    registry = _mock_registry(flow=MagicMock())
    app = _make_app_with_registry(tmp_path, registry)

    bad_tasks = [dict(_CF_TASKS[0], phase="missing-phase")]
    with TestClient(app) as client:
        res = client.post(
            "/flows/validate",
            headers=AUTH,
            json={"phases": _CF_PHASES, "tasks": bad_tasks},
        )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["valid"] is False, body
    assert body["errors"], body


def test_flows_get_schema_returns_live_schema(tmp_path: Path) -> None:
    """GET /flows/{project}/schema -> fetch_flow_schema(alias), json-schema envelope.

    Also guards route ordering: /{project}/schema must NOT be captured by
    GET /flows/{project}/{config_id} (which would call get_flow_detail).
    """
    flow_svc = MagicMock()
    flow_svc.fetch_flow_schema.return_value = ({"type": "object"}, None)
    registry = _mock_registry(flow=flow_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get(f"/flows/{PROJECT}/schema", headers=AUTH)

    assert res.status_code == 200, res.text
    assert res.json() == {"format": "json-schema", "schema": {"type": "object"}}
    flow_svc.fetch_flow_schema.assert_called_once_with(PROJECT)
    flow_svc.get_flow_detail.assert_not_called()


def test_flows_get_schema_fetch_failure_is_502(tmp_path: Path) -> None:
    """Schema unavailable -> 502 with the reason (REST has no degrade path to offer)."""
    flow_svc = MagicMock()
    flow_svc.fetch_flow_schema.return_value = (None, "no configurationSchema")
    registry = _mock_registry(flow=flow_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get(f"/flows/{PROJECT}/schema", headers=AUTH)

    assert res.status_code == 502, res.text
    assert "no configurationSchema" in res.text
