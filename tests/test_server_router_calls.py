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
from keboola_agent_cli.services.flow_service import FlowSchemaFetch

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
# search.py  GET /search
# Service: search.search(regex=...)   (DMD-1716 -- must forward the flag)
# ---------------------------------------------------------------------------


def test_global_search_forwards_regex_kwarg(tmp_path: Path) -> None:
    """Router must forward ``regex=`` to SearchService.search (mirrors kbagent search)."""
    search_svc = MagicMock()
    search_svc.search.return_value = {"results": [], "errors": [], "stats": {}}
    registry = _mock_registry(search=search_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get(
            "/search",
            headers=AUTH,
            params={"query": ".*orders.*", "regex": True},
        )

    assert res.status_code == 200, res.text
    kwargs = search_svc.search.call_args.kwargs
    assert kwargs["regex"] is True


def test_global_search_regex_defaults_false(tmp_path: Path) -> None:
    """Without the regex param the router forwards regex=False."""
    search_svc = MagicMock()
    search_svc.search.return_value = {"results": [], "errors": [], "stats": {}}
    registry = _mock_registry(search=search_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get("/search", headers=AUTH, params={"query": "orders"})

    assert res.status_code == 200, res.text
    assert search_svc.search.call_args.kwargs["regex"] is False


def test_global_search_regex_with_config_based_returns_400(tmp_path: Path) -> None:
    """SearchService rejects regex + config-based with ConfigError -> HTTP 400 (not silent 200)."""
    from keboola_agent_cli.errors import ConfigError

    search_svc = MagicMock()
    search_svc.search.side_effect = ConfigError(
        "Regex mode is only supported with textual search "
        "(config-based search does not support regex)."
    )
    registry = _mock_registry(search=search_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get(
            "/search",
            headers=AUTH,
            params={"query": ".*orders.*", "search_type": "config-based", "regex": True},
        )

    assert res.status_code == 400, res.text
    assert "regex" in res.json()["error"]["message"].lower()


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
# storage.py  POST /columns/{p}/describe-migrate
# Service: storage.describe_migrate(...)  -- the 1:1 mirror of the CLI command
# ---------------------------------------------------------------------------


def test_storage_describe_migrate_forwards_scope_and_flags(tmp_path: Path) -> None:
    """Router must forward every scope/flag kwarg to StorageService.describe_migrate."""
    storage_svc = MagicMock()
    storage_svc.describe_migrate.return_value = {"tables_migrated": 1}
    registry = _mock_registry(storage=storage_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            f"/storage/columns/{PROJECT}/describe-migrate",
            headers=AUTH,
            json={"table_ids": [TABLE_ID], "prune_orphans": True, "dry_run": True},
        )

    assert res.status_code == 200, res.text
    kwargs = storage_svc.describe_migrate.call_args.kwargs
    assert kwargs["alias"] == PROJECT
    assert kwargs["table_ids"] == [TABLE_ID]
    assert kwargs["bucket_id"] is None
    assert kwargs["prune_orphans"] is True
    assert kwargs["dry_run"] is True


def test_storage_describe_migrate_defaults_to_whole_project_write(tmp_path: Path) -> None:
    """An empty body means whole-project scope and a real (non-dry-run) write."""
    storage_svc = MagicMock()
    storage_svc.describe_migrate.return_value = {"tables_migrated": 0}
    registry = _mock_registry(storage=storage_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(f"/storage/columns/{PROJECT}/describe-migrate", headers=AUTH, json={})

    assert res.status_code == 200, res.text
    kwargs = storage_svc.describe_migrate.call_args.kwargs
    assert kwargs["table_ids"] is None
    assert kwargs["bucket_id"] is None
    assert kwargs["dry_run"] is False
    assert kwargs["prune_orphans"] is False


# ---------------------------------------------------------------------------
# storage.py  POST /{p}  (create table)
# Service: storage.create_table(source_table_id=..., time_partitioning_*=...,
#          clustering_fields=...) -- the source-copy + BigQuery layout params
#          must be forwarded so the web "repartition" flow reaches the service.
# ---------------------------------------------------------------------------


def test_storage_create_table_forwards_source_and_partition_kwargs(tmp_path: Path) -> None:
    """Router must forward the source-copy + BigQuery partition/clustering body
    fields to StorageService.create_table (the web repartition flow)."""
    storage_svc = MagicMock()
    storage_svc.create_table.return_value = {"table_id": "in.c-main.events_repart"}
    registry = _mock_registry(storage=storage_svc)
    app = _make_app_with_registry(tmp_path, registry)

    body = {
        "bucket_id": "in.c-main",
        "name": "events_repart",
        "source_table_id": "in.c-main.events",
        "branch_id": 123,
        "time_partitioning_type": "DAY",
        "time_partitioning_field": "created_at",
        "clustering_fields": ["tenant_id"],
        "primary_key": ["id"],
    }
    with TestClient(app) as client:
        res = client.post(f"/storage/tables/{PROJECT}", headers=AUTH, json=body)

    assert res.status_code == 200, res.text
    kwargs = storage_svc.create_table.call_args.kwargs
    assert kwargs["source_table_id"] == "in.c-main.events"
    assert kwargs["time_partitioning_type"] == "DAY"
    assert kwargs["time_partitioning_field"] == "created_at"
    assert kwargs["clustering_fields"] == ["tenant_id"]
    # columns is optional now (source mode); router passes None, not a crash.
    assert kwargs["columns"] is None


def test_storage_create_table_columns_optional(tmp_path: Path) -> None:
    """`columns` is no longer required by the request model -- a source-only
    body must validate (HTTP 200), not 422."""
    storage_svc = MagicMock()
    storage_svc.create_table.return_value = {"table_id": "in.c-main.t"}
    registry = _mock_registry(storage=storage_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            f"/storage/tables/{PROJECT}",
            headers=AUTH,
            json={"bucket_id": "in.c-main", "name": "t", "source_table_id": "in.c-main.src"},
        )

    assert res.status_code == 200, res.text


def test_storage_create_table_columns_and_source_is_422(tmp_path: Path) -> None:
    """Both columns and source_table_id given -> clean 422 at the request
    boundary (not a 500 from the service)."""
    storage_svc = MagicMock()
    registry = _mock_registry(storage=storage_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            f"/storage/tables/{PROJECT}",
            headers=AUTH,
            json={
                "bucket_id": "in.c-main",
                "name": "t",
                "columns": ["id:INTEGER"],
                "source_table_id": "in.c-main.src",
            },
        )

    assert res.status_code == 422, res.text
    storage_svc.create_table.assert_not_called()


def test_storage_create_table_neither_columns_nor_source_is_422(tmp_path: Path) -> None:
    """Neither columns nor source_table_id -> clean 422, service untouched."""
    storage_svc = MagicMock()
    registry = _mock_registry(storage=storage_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            f"/storage/tables/{PROJECT}",
            headers=AUTH,
            json={"bucket_id": "in.c-main", "name": "t"},
        )

    assert res.status_code == 422, res.text
    storage_svc.create_table.assert_not_called()


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
    flow_svc.fetch_flow_schema.return_value = FlowSchemaFetch(
        schema={"type": "object", "required": ["phases", "tasks"]}, reason=None
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
    flow_svc.fetch_flow_schema.return_value = FlowSchemaFetch(
        schema=None, reason="AI Service unreachable"
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
    flow_svc.fetch_flow_schema.return_value = FlowSchemaFetch(
        schema={"type": "object"}, reason=None
    )
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
    flow_svc.fetch_flow_schema.return_value = FlowSchemaFetch(
        schema=None, reason="no configurationSchema"
    )
    registry = _mock_registry(flow=flow_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get(f"/flows/{PROJECT}/schema", headers=AUTH)

    assert res.status_code == 502, res.text
    assert "no configurationSchema" in res.text


def test_flows_create_drops_component_id(tmp_path: Path) -> None:
    """POST /flows/{project} calls create_flow WITHOUT component_id.

    Orchestrator support is dropped: FlowCreate has no component_id field, so
    even a client that still sends one has it silently dropped (Pydantic
    extra='ignore') and the service is never asked to target a component.
    """
    flow_svc = MagicMock()
    flow_svc.create_flow.return_value = {"id": "999", "name": "My Flow"}
    registry = _mock_registry(flow=flow_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            f"/flows/{PROJECT}",
            headers=AUTH,
            json={
                "name": "My Flow",
                "phases": _CF_PHASES,
                "tasks": _CF_TASKS,
                "component_id": "keboola.orchestrator",  # legacy field -> dropped
            },
        )

    assert res.status_code == 200, res.text
    flow_svc.create_flow.assert_called_once()
    assert "component_id" not in flow_svc.create_flow.call_args.kwargs


def test_flows_update_drops_component_id(tmp_path: Path) -> None:
    """PATCH /flows/{project}/{config_id} calls update_flow WITHOUT component_id."""
    flow_svc = MagicMock()
    flow_svc.update_flow.return_value = {"id": "999", "name": "Renamed"}
    registry = _mock_registry(flow=flow_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.patch(
            f"/flows/{PROJECT}/999",
            headers=AUTH,
            json={"name": "Renamed", "component_id": "keboola.orchestrator"},
        )

    assert res.status_code == 200, res.text
    flow_svc.update_flow.assert_called_once()
    assert "component_id" not in flow_svc.update_flow.call_args.kwargs


# ---------------------------------------------------------------------------
# data_apps.py  managed-repo REST parity (0.65.0)
# ---------------------------------------------------------------------------


def test_data_app_create_passes_use_managed_git_repo(tmp_path: Path) -> None:
    """POST /data-apps/{p} must forward use_managed_git_repo to the service."""
    data_app_svc = MagicMock()
    data_app_svc.create_data_app.return_value = {"app_id": "9", "use_managed_git_repo": True}
    registry = _mock_registry(data_app=data_app_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            f"/data-apps/{PROJECT}",
            headers=AUTH,
            json={"name": "App", "slug": "app", "use_managed_git_repo": True},
        )

    assert res.status_code == 200, res.text
    kwargs = data_app_svc.create_data_app.call_args.kwargs
    assert kwargs.get("use_managed_git_repo") is True
    assert kwargs.get("git_repo") == ""


def test_data_app_create_workspace_defaults_on_over_rest(tmp_path: Path) -> None:
    """The REST surface must default Storage access ON, like the CLI."""
    data_app_svc = MagicMock()
    data_app_svc.create_data_app.return_value = {"app_id": "9", "workspace": True}
    registry = _mock_registry(data_app=data_app_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            f"/data-apps/{PROJECT}",
            headers=AUTH,
            json={"name": "App", "slug": "app", "git_repo": "https://github.com/o/r"},
        )

    assert res.status_code == 200, res.text
    assert data_app_svc.create_data_app.call_args.kwargs.get("workspace") is True


def test_data_app_create_workspace_can_be_disabled_over_rest(tmp_path: Path) -> None:
    """``"workspace": false`` in the body must reach the service."""
    data_app_svc = MagicMock()
    data_app_svc.create_data_app.return_value = {"app_id": "9", "workspace": False}
    registry = _mock_registry(data_app=data_app_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            f"/data-apps/{PROJECT}",
            headers=AUTH,
            json={
                "name": "App",
                "slug": "app",
                "git_repo": "https://github.com/o/r",
                "workspace": False,
            },
        )

    assert res.status_code == 200, res.text
    assert data_app_svc.create_data_app.call_args.kwargs.get("workspace") is False


def test_data_app_runs_endpoint_calls_service(tmp_path: Path) -> None:
    """GET /data-apps/{p}/{app}/runs must call DataAppService.list_app_runs."""
    data_app_svc = MagicMock()
    data_app_svc.list_app_runs.return_value = {"count": 0, "runs": []}
    registry = _mock_registry(data_app=data_app_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get(f"/data-apps/{PROJECT}/{APP_ID}/runs?limit=3", headers=AUTH)

    assert res.status_code == 200, res.text
    data_app_svc.list_app_runs.assert_called_once_with(PROJECT, APP_ID, limit=3)


# ---------------------------------------------------------------------------
# projects.py  POST /projects/bulk-delete
# Service: project.bulk_remove_projects(aliases=..., dry_run=...)
# ---------------------------------------------------------------------------


def test_bulk_delete_projects_passes_aliases(tmp_path: Path) -> None:
    """POST /projects/bulk-delete must call ProjectService.bulk_remove_projects."""
    project_svc = MagicMock()
    project_svc.bulk_remove_projects.return_value = {
        "removed": ["a", "b"],
        "failed": [],
        "dry_run": False,
    }
    registry = _mock_registry(project=project_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            "/projects/bulk-delete",
            headers=AUTH,
            json={"aliases": ["a", "b"]},
        )

    assert res.status_code == 200, res.text
    assert res.json()["removed"] == ["a", "b"]
    project_svc.bulk_remove_projects.assert_called_once_with(aliases=["a", "b"], dry_run=False)


def test_bulk_delete_projects_forwards_dry_run(tmp_path: Path) -> None:
    """The dry_run flag in the body must reach the service."""
    project_svc = MagicMock()
    project_svc.bulk_remove_projects.return_value = {
        "removed": ["a"],
        "failed": [],
        "dry_run": True,
    }
    registry = _mock_registry(project=project_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            "/projects/bulk-delete",
            headers=AUTH,
            json={"aliases": ["a"], "dry_run": True},
        )

    assert res.status_code == 200, res.text
    project_svc.bulk_remove_projects.assert_called_once_with(aliases=["a"], dry_run=True)


def test_bulk_delete_route_not_shadowed_by_alias_delete(tmp_path: Path) -> None:
    """The literal /projects/bulk-delete POST must not be swallowed by /{alias}.

    A DELETE /{alias} exists; the bulk route is a POST to a literal path, so it
    must resolve to bulk_remove_projects, never remove_project('bulk-delete').
    """
    project_svc = MagicMock()
    project_svc.bulk_remove_projects.return_value = {"removed": [], "failed": [], "dry_run": False}
    registry = _mock_registry(project=project_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post("/projects/bulk-delete", headers=AUTH, json={"aliases": []})

    assert res.status_code == 200, res.text
    project_svc.bulk_remove_projects.assert_called_once()
    project_svc.remove_project.assert_not_called()


# ---------------------------------------------------------------------------
# configs.py  PATCH /{p}/{c}/{cfg}
# Service: config.update_config(change_description=...)
# ---------------------------------------------------------------------------


def test_config_update_passes_change_description_kwarg(tmp_path: Path) -> None:
    """Router forwards change_description= to ConfigService.update_config."""
    config_svc = MagicMock()
    config_svc.update_config.return_value = {"id": CONFIG_ID, "name": "cfg"}
    registry = _mock_registry(config=config_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.patch(
            f"/configs/{PROJECT}/{COMPONENT}/{CONFIG_ID}",
            headers=AUTH,
            json={"name": "cfg", "change_description": "AI-1234: via API"},
        )

    assert res.status_code == 200, res.text
    kwargs = config_svc.update_config.call_args.kwargs
    assert kwargs["change_description"] == "AI-1234: via API"


# ---------------------------------------------------------------------------
# configs.py  PATCH /{p}/{c}/{cfg}/rows/{row}
# Service: config.update_config_row(change_description=...)
# ---------------------------------------------------------------------------


def test_config_row_update_passes_change_description_kwarg(tmp_path: Path) -> None:
    """Router forwards change_description= to ConfigService.update_config_row."""
    config_svc = MagicMock()
    config_svc.update_config_row.return_value = {"id": ROW_ID, "name": "row"}
    registry = _mock_registry(config=config_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.patch(
            f"/configs/{PROJECT}/{COMPONENT}/{CONFIG_ID}/rows/{ROW_ID}",
            headers=AUTH,
            json={"name": "row", "change_description": "AI-1234: via API"},
        )

    assert res.status_code == 200, res.text
    kwargs = config_svc.update_config_row.call_args.kwargs
    assert kwargs["change_description"] == "AI-1234: via API"


# ---------------------------------------------------------------------------
# docs.py  POST /documentation/query
# Service: docs.ask_docs(alias=..., query=...)  (mirrors `kbagent docs query`)
# ---------------------------------------------------------------------------


def test_docs_query_passes_alias_and_query(tmp_path: Path) -> None:
    """POST /documentation/query must call DocsService.ask_docs(alias=, query=)."""
    docs_svc = MagicMock()
    docs_svc.ask_docs.return_value = {"query": "q", "text": "answer", "source_urls": []}
    registry = _mock_registry(docs=docs_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            "/documentation/query",
            headers=AUTH,
            json={"query": "How do incremental loads work?", "project": PROJECT},
        )

    assert res.status_code == 200, res.text
    docs_svc.ask_docs.assert_called_once_with(alias=PROJECT, query="How do incremental loads work?")


def test_docs_query_project_optional_defaults_to_none(tmp_path: Path) -> None:
    """Omitting `project` passes alias=None (service picks the first project)."""
    docs_svc = MagicMock()
    docs_svc.ask_docs.return_value = {"query": "q", "text": "a", "source_urls": []}
    registry = _mock_registry(docs=docs_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post("/documentation/query", headers=AUTH, json={"query": "q"})

    assert res.status_code == 200, res.text
    assert docs_svc.ask_docs.call_args.kwargs["alias"] is None


def test_docs_query_config_error_is_400(tmp_path: Path) -> None:
    """No projects configured -> ConfigError -> HTTP 400 error envelope."""
    from keboola_agent_cli.errors import ConfigError

    docs_svc = MagicMock()
    docs_svc.ask_docs.side_effect = ConfigError("No projects configured.")
    registry = _mock_registry(docs=docs_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post("/documentation/query", headers=AUTH, json={"query": "q"})

    assert res.status_code == 400, res.text
    assert "No projects configured" in res.json()["error"]["message"]


def test_docs_query_requires_bearer_auth(tmp_path: Path) -> None:
    """/documentation must NOT live in the auth-exempt /docs (Swagger) namespace.

    The auth middleware exempts every path starting with /docs; the docs-QA
    router therefore uses /documentation and must reject unauthenticated calls.
    """
    docs_svc = MagicMock()
    registry = _mock_registry(docs=docs_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post("/documentation/query", json={"query": "q"})  # no auth header

    assert res.status_code == 401, res.text
    docs_svc.ask_docs.assert_not_called()


# ---------------------------------------------------------------------------
# billing.py  GET /billing/credits
# Service: billing.get_credits(aliases=...)  (mirrors `kbagent billing credits`)
# ---------------------------------------------------------------------------


def test_billing_credits_passes_none_aliases_when_no_project_given(tmp_path: Path) -> None:
    """GET /billing/credits with no `project` query param must call get_credits(aliases=None)."""
    billing_svc = MagicMock()
    billing_svc.get_credits.return_value = {"credits": [], "errors": []}
    registry = _mock_registry(billing=billing_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get("/billing/credits", headers=AUTH)

    assert res.status_code == 200, res.text
    billing_svc.get_credits.assert_called_once_with(aliases=None)


def test_billing_credits_repeated_project_param_forwards_alias_list(tmp_path: Path) -> None:
    """Repeated `?project=a&project=b` must forward aliases=["a", "b"]."""
    billing_svc = MagicMock()
    billing_svc.get_credits.return_value = {"credits": [], "errors": []}
    registry = _mock_registry(billing=billing_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get("/billing/credits", headers=AUTH, params={"project": ["a", "b"]})

    assert res.status_code == 200, res.text
    billing_svc.get_credits.assert_called_once_with(aliases=["a", "b"])


def test_billing_credits_returns_service_envelope_unchanged(tmp_path: Path) -> None:
    """The router must return the service's {"credits": ..., "errors": ...} dict verbatim."""
    billing_svc = MagicMock()
    envelope = {
        "credits": [
            {
                "project_alias": "prod",
                "project_id": 123,
                "consumed": 100.5,
                "remaining": 25.5,
                "total": 126.0,
                "consumed_minutes": 6030.0,
                "remaining_minutes": 1530.0,
                "component_jobs_consumed": 95.25,
                "workspace_jobs": [
                    {"workspace_type": "sandbox-sql", "warehouse_size": "small", "consumed": 5.0}
                ],
            }
        ],
        "errors": [],
    }
    billing_svc.get_credits.return_value = envelope
    registry = _mock_registry(billing=billing_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get("/billing/credits", headers=AUTH)

    assert res.status_code == 200, res.text
    assert res.json() == envelope


def test_billing_credits_requires_bearer_auth(tmp_path: Path) -> None:
    """GET /billing/credits without an Authorization header must be rejected."""
    billing_svc = MagicMock()
    registry = _mock_registry(billing=billing_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get("/billing/credits")  # no auth header

    assert res.status_code == 401, res.text
    billing_svc.get_credits.assert_not_called()


# ---------------------------------------------------------------------------
# configs.py  GET /configs/examples/{component_id}
# Service: component.get_config_examples(alias=..., component_id=...)
# (method lives on ComponentService; mirrors `kbagent config examples`)
# ---------------------------------------------------------------------------


def test_config_examples_passes_alias_and_component_id(tmp_path: Path) -> None:
    """GET /configs/examples/{c} must call ComponentService.get_config_examples."""
    component_svc = MagicMock()
    component_svc.get_config_examples.return_value = {
        "component_id": COMPONENT,
        "root_examples": [{"parameters": {}}],
        "row_examples": [],
    }
    registry = _mock_registry(component=component_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get(
            f"/configs/examples/{COMPONENT}", headers=AUTH, params={"project": PROJECT}
        )

    assert res.status_code == 200, res.text
    component_svc.get_config_examples.assert_called_once_with(alias=PROJECT, component_id=COMPONENT)


def test_config_examples_project_optional(tmp_path: Path) -> None:
    """Without ?project= the router passes alias=None (first configured project)."""
    component_svc = MagicMock()
    component_svc.get_config_examples.return_value = {
        "component_id": COMPONENT,
        "root_examples": [],
        "row_examples": [],
    }
    registry = _mock_registry(component=component_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get(f"/configs/examples/{COMPONENT}", headers=AUTH)

    assert res.status_code == 200, res.text
    assert component_svc.get_config_examples.call_args.kwargs["alias"] is None


def test_config_examples_not_found_is_404(tmp_path: Path) -> None:
    """An upstream NOT_FOUND is about the resource, so it answers 404, not 502."""
    from keboola_agent_cli.errors import ErrorCode, KeboolaApiError

    component_svc = MagicMock()
    component_svc.get_config_examples.side_effect = KeboolaApiError(
        message="Component not found", status_code=404, error_code=ErrorCode.NOT_FOUND
    )
    registry = _mock_registry(component=component_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get(f"/configs/examples/{COMPONENT}", headers=AUTH)

    assert res.status_code == 404, res.text
    assert res.json()["error"]["code"] == "NOT_FOUND"
    assert "Component not found" in res.json()["error"]["message"]


def test_config_examples_api_error_is_502(tmp_path: Path) -> None:
    """A genuine upstream fault (non-NOT_FOUND KeboolaApiError) keeps its 502."""
    from keboola_agent_cli.errors import ErrorCode, KeboolaApiError

    component_svc = MagicMock()
    component_svc.get_config_examples.side_effect = KeboolaApiError(
        message="AI Service is unavailable", status_code=503, error_code=ErrorCode.API_ERROR
    )
    registry = _mock_registry(component=component_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get(f"/configs/examples/{COMPONENT}", headers=AUTH)

    assert res.status_code == 502, res.text
    assert res.json()["error"]["code"] == "API_ERROR"


# ---------------------------------------------------------------------------
# components.py  GET /components/{component_id}
# Service: component.get_component_detail(alias=..., component_id=...)
# ---------------------------------------------------------------------------


def test_component_detail_not_found_is_404_not_502(tmp_path: Path) -> None:
    """A component neither source knows answers 404 -- it is not a gateway fault.

    Regression: `GET /components/keboola.mcp-server-tool` used to answer 502
    with a NOT_FOUND body, so callers retried a request that can never succeed.
    """
    from keboola_agent_cli.errors import ErrorCode, KeboolaApiError

    component_svc = MagicMock()
    component_svc.get_component_detail.side_effect = KeboolaApiError(
        message='Resource not found: Component "nope.nope" not found',
        status_code=404,
        error_code=ErrorCode.NOT_FOUND,
    )
    registry = _mock_registry(component=component_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get("/components/nope.nope", params={"project": PROJECT}, headers=AUTH)

    assert res.status_code == 404, res.text
    body = res.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "NOT_FOUND"


def test_component_detail_returns_storage_catalog_source(tmp_path: Path) -> None:
    """The catalog fallback reaches the REST caller with its discriminator intact."""
    component_svc = MagicMock()
    component_svc.get_component_detail.return_value = {
        "component_id": "keboola.mcp-server-tool",
        "component_name": "MCP Server Tool",
        "documentation_source": "storage_catalog",
    }
    registry = _mock_registry(component=component_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get(
            "/components/keboola.mcp-server-tool", params={"project": PROJECT}, headers=AUTH
        )

    assert res.status_code == 200, res.text
    assert res.json()["documentation_source"] == "storage_catalog"
    component_svc.get_component_detail.assert_called_once_with(
        alias=PROJECT, component_id="keboola.mcp-server-tool"
    )


# ---------------------------------------------------------------------------
# components.py  POST /components/{component_id}/actions/{action}
# Service: component.run_sync_action(...)  (mirrors `kbagent component sync-action`)
# ---------------------------------------------------------------------------


def test_component_sync_action_forwards_all_kwargs(tmp_path: Path) -> None:
    """POST /components/{c}/actions/{a} must forward every body field by name."""
    component_svc = MagicMock()
    component_svc.run_sync_action.return_value = {
        "component_id": COMPONENT,
        "action": "testConnection",
        "result": {"status": "success"},
    }
    registry = _mock_registry(component=component_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            f"/components/{COMPONENT}/actions/testConnection",
            headers=AUTH,
            json={
                "project": PROJECT,
                "config_id": CONFIG_ID,
                "row_id": ROW_ID,
                "branch_id": 123,
                "timeout": 60,
            },
        )

    assert res.status_code == 200, res.text
    component_svc.run_sync_action.assert_called_once_with(
        alias=PROJECT,
        component_id=COMPONENT,
        action="testConnection",
        config_id=CONFIG_ID,
        row_id=ROW_ID,
        branch_id=123,
        config_data_override=None,
        timeout=60,
    )


def test_component_sync_action_config_data_override(tmp_path: Path) -> None:
    """`config_data` in the body reaches the service as config_data_override=."""
    component_svc = MagicMock()
    component_svc.run_sync_action.return_value = {"result": {}}
    registry = _mock_registry(component=component_svc)
    app = _make_app_with_registry(tmp_path, registry)

    payload = {"parameters": {"db": {"host": "example.com"}}}
    with TestClient(app) as client:
        res = client.post(
            f"/components/{COMPONENT}/actions/testConnection",
            headers=AUTH,
            json={"project": PROJECT, "config_data": payload},
        )

    assert res.status_code == 200, res.text
    kwargs = component_svc.run_sync_action.call_args.kwargs
    assert kwargs["config_data_override"] == payload
    assert kwargs["config_id"] is None


def test_component_sync_action_resolves_pinned_alias(tmp_path: Path) -> None:
    """Without `project` in the body, the pinned alias is resolved (like detail/scaffold)."""
    component_svc = MagicMock()
    component_svc.run_sync_action.return_value = {"result": {}}
    project_svc = MagicMock()
    project_svc.resolve_pinned_alias.return_value = ("pinned-proj", "config")
    registry = _mock_registry(component=component_svc, project=project_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            f"/components/{COMPONENT}/actions/getTables",
            headers=AUTH,
            json={"config_id": CONFIG_ID},
        )

    assert res.status_code == 200, res.text
    project_svc.resolve_pinned_alias.assert_called_once_with(None)
    assert component_svc.run_sync_action.call_args.kwargs["alias"] == "pinned-proj"


def test_component_sync_action_missing_inputs_is_400(tmp_path: Path) -> None:
    """Service-side ConfigError (no config_id, no config_data) -> HTTP 400."""
    from keboola_agent_cli.errors import ConfigError

    component_svc = MagicMock()
    component_svc.run_sync_action.side_effect = ConfigError(
        "Either a configuration ID or explicit config data is required to run a sync action."
    )
    registry = _mock_registry(component=component_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            f"/components/{COMPONENT}/actions/testConnection",
            headers=AUTH,
            json={"project": PROJECT},
        )

    assert res.status_code == 400, res.text
    assert "configuration ID" in res.json()["error"]["message"]


# ---------------------------------------------------------------------------
# semantic_layer.py  GET /semantic-layer/schema
# Service: semantic_layer.get_schema(alias=..., types=[...])
# (mirrors `kbagent semantic-layer schema`)
# ---------------------------------------------------------------------------


def test_semantic_layer_schema_forwards_types(tmp_path: Path) -> None:
    """Repeated ?type= params must reach get_schema as an ordered list."""
    sl = MagicMock()
    sl.get_schema.return_value = {"project": PROJECT, "schemas": []}
    app = _make_app_with_registry(tmp_path, _mock_registry(semantic_layer=sl))

    with TestClient(app) as client:
        res = client.get(
            "/semantic-layer/schema",
            headers=AUTH,
            params=[("project", PROJECT), ("type", "metric"), ("type", "model")],
        )

    assert res.status_code == 200, res.text
    sl.get_schema.assert_called_once_with(alias=PROJECT, types=["metric", "model"])


def test_semantic_layer_schema_defaults_to_all_types(tmp_path: Path) -> None:
    """Omitting ?type= fetches every known semantic type (the CLI's --all)."""
    from keboola_agent_cli.services.semantic_layer_service import SCHEMA_TYPE_ALIAS

    sl = MagicMock()
    sl.get_schema.return_value = {"project": PROJECT, "schemas": []}
    app = _make_app_with_registry(tmp_path, _mock_registry(semantic_layer=sl))

    with TestClient(app) as client:
        res = client.get("/semantic-layer/schema", headers=AUTH, params={"project": PROJECT})

    assert res.status_code == 200, res.text
    sl.get_schema.assert_called_once_with(alias=PROJECT, types=list(SCHEMA_TYPE_ALIAS))


def test_semantic_layer_schema_unknown_type_is_400(tmp_path: Path) -> None:
    """Unknown type name -> service ConfigError -> HTTP 400 (fail fast, no network)."""
    from keboola_agent_cli.errors import ConfigError

    sl = MagicMock()
    sl.get_schema.side_effect = ConfigError("Unknown semantic type(s): bogus.")
    app = _make_app_with_registry(tmp_path, _mock_registry(semantic_layer=sl))

    with TestClient(app) as client:
        res = client.get(
            "/semantic-layer/schema",
            headers=AUTH,
            params={"project": PROJECT, "type": "bogus"},
        )

    assert res.status_code == 400, res.text
    assert "Unknown semantic type" in res.json()["error"]["message"]


# ---------------------------------------------------------------------------
# transformation.py  POST /{p} + GET /{p}/{cfg} + PATCH /{p}/{cfg}
# Service: transformation.create/show/edit  (mirrors `kbagent transformation *`)
# ---------------------------------------------------------------------------


def test_transformation_create_forwards_kwargs(tmp_path: Path) -> None:
    """POST /transformations/{p} must forward every create field by name."""
    tf = MagicMock()
    tf.create.return_value = {"config_id": "77", "name": "Orders"}
    app = _make_app_with_registry(tmp_path, _mock_registry(transformation=tf))

    with TestClient(app) as client:
        res = client.post(
            f"/transformations/{PROJECT}",
            headers=AUTH,
            json={
                "name": "Orders",
                "sql": 'CREATE TABLE "report" AS SELECT 1;',
                "created_tables": ["report"],
                "description": "demo",
                "branch_id": 5,
                "dry_run": True,
            },
        )

    assert res.status_code == 200, res.text
    tf.create.assert_called_once_with(
        PROJECT,
        name="Orders",
        sql='CREATE TABLE "report" AS SELECT 1;',
        created_tables=["report"],
        component_id=None,
        description="demo",
        branch_id=5,
        dry_run=True,
    )


def test_transformation_create_empty_sql_is_400(tmp_path: Path) -> None:
    """Service ValueError (SQL contains no statements) -> HTTP 400, not 500."""
    tf = MagicMock()
    tf.create.side_effect = ValueError("SQL contains no statements (empty input)")
    app = _make_app_with_registry(tmp_path, _mock_registry(transformation=tf))

    with TestClient(app) as client:
        res = client.post(
            f"/transformations/{PROJECT}",
            headers=AUTH,
            json={"name": "Empty", "sql": "   "},
        )

    assert res.status_code == 400, res.text
    assert "no statements" in res.json()["error"]["message"]


def test_transformation_show_forwards_kwargs(tmp_path: Path) -> None:
    """GET /transformations/{p}/{cfg} must pass config_id/component_id/branch_id."""
    tf = MagicMock()
    tf.show.return_value = {"config_id": CONFIG_ID, "blocks": []}
    app = _make_app_with_registry(tmp_path, _mock_registry(transformation=tf))

    with TestClient(app) as client:
        res = client.get(
            f"/transformations/{PROJECT}/{CONFIG_ID}",
            headers=AUTH,
            params={"component_id": "keboola.snowflake-transformation", "branch_id": 9},
        )

    assert res.status_code == 200, res.text
    tf.show.assert_called_once_with(
        PROJECT,
        config_id=CONFIG_ID,
        component_id="keboola.snowflake-transformation",
        branch_id=9,
    )


def test_transformation_show_not_found_is_404(tmp_path: Path) -> None:
    """Config not found under any SQL component -> KeboolaApiError(NOT_FOUND) -> HTTP 404."""
    from keboola_agent_cli.errors import ErrorCode, KeboolaApiError

    tf = MagicMock()
    tf.show.side_effect = KeboolaApiError(
        message="Configuration '42' was not found under any SQL transformation component",
        status_code=404,
        error_code=ErrorCode.NOT_FOUND,
    )
    app = _make_app_with_registry(tmp_path, _mock_registry(transformation=tf))

    with TestClient(app) as client:
        res = client.get(f"/transformations/{PROJECT}/{CONFIG_ID}", headers=AUTH)

    assert res.status_code == 404, res.text
    assert res.json()["error"]["code"] == "NOT_FOUND"
    assert "was not found" in res.json()["error"]["message"]


def test_transformation_edit_forwards_kwargs(tmp_path: Path) -> None:
    """PATCH /transformations/{p}/{cfg} must forward ops + change_description + storage."""
    tf = MagicMock()
    tf.edit.return_value = {"config_id": CONFIG_ID, "operations_applied": [], "blocks": []}
    app = _make_app_with_registry(tmp_path, _mock_registry(transformation=tf))

    ops = [{"op": "str_replace", "search_for": "a", "replace_with": "b"}]
    storage = {"input": {"tables": []}, "output": {"tables": []}}
    with TestClient(app) as client:
        res = client.patch(
            f"/transformations/{PROJECT}/{CONFIG_ID}",
            headers=AUTH,
            json={
                "ops": ops,
                "change_description": "Rename column",
                "storage": storage,
                "dry_run": True,
            },
        )

    assert res.status_code == 200, res.text
    tf.edit.assert_called_once_with(
        PROJECT,
        config_id=CONFIG_ID,
        ops=ops,
        change_description="Rename column",
        component_id=None,
        storage=storage,
        branch_id=None,
        dry_run=True,
    )


def test_transformation_edit_invalid_op_is_400(tmp_path: Path) -> None:
    """Service ValueError (bad op schema) -> HTTP 400, not 500."""
    tf = MagicMock()
    tf.edit.side_effect = ValueError("Operation #0 has unknown op 'explode'")
    app = _make_app_with_registry(tmp_path, _mock_registry(transformation=tf))

    with TestClient(app) as client:
        res = client.patch(
            f"/transformations/{PROJECT}/{CONFIG_ID}",
            headers=AUTH,
            json={"ops": [{"op": "explode"}], "change_description": "boom"},
        )

    assert res.status_code == 400, res.text
    assert "unknown op" in res.json()["error"]["message"]


# ---------------------------------------------------------------------------
# flows.py  GET /flows/examples
# Module-level flow_service.get_flow_examples (offline, bundled resources --
# exercised for real, no mocks; mirrors `kbagent flow examples`).
# ---------------------------------------------------------------------------


def test_flow_examples_returns_bundled_conditional_examples(tmp_path: Path) -> None:
    """Default component id serves the bundled keboola.flow examples."""
    registry = _mock_registry(flow=MagicMock())
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get("/flows/examples", headers=AUTH)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["component_id"] == "keboola.flow"
    assert body["count"] == len(body["examples"])
    assert body["count"] > 0
    assert all(isinstance(example, dict) for example in body["examples"])


def test_flow_examples_unknown_component_is_400(tmp_path: Path) -> None:
    """An unknown component id -> ValueError -> HTTP 400 (mirrors CLI exit 2)."""
    registry = _mock_registry(flow=MagicMock())
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get(
            "/flows/examples", headers=AUTH, params={"component_id": "keboola.nonsense"}
        )

    assert res.status_code == 400, res.text
    assert "No bundled flow examples" in res.json()["error"]["message"]


# ---------------------------------------------------------------------------
# storage.py  snapshot routes (issue #512)
# Service: registry.snapshot (SnapshotService) -- create_snapshot,
# list_snapshots, get_snapshot, delete_snapshots, create_table_from_snapshot.
# ---------------------------------------------------------------------------


def test_snapshot_create_passes_kwargs(tmp_path: Path) -> None:
    """POST /storage/tables/{p}/{table}/snapshots -> snapshot.create_snapshot."""
    snapshot_svc = MagicMock()
    snapshot_svc.create_snapshot.return_value = {"snapshot_id": "954"}
    registry = _mock_registry(snapshot=snapshot_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            f"/storage/tables/{PROJECT}/{TABLE_ID}/snapshots",
            headers=AUTH,
            json={"description": "before migration", "branch_id": 42},
        )

    assert res.status_code == 200, res.text
    snapshot_svc.create_snapshot.assert_called_once_with(
        alias=PROJECT,
        table_id=TABLE_ID,
        description="before migration",
        branch_id=42,
    )


def test_snapshot_list_passes_kwargs(tmp_path: Path) -> None:
    """GET /storage/snapshots/{p}/{table} -> snapshot.list_snapshots."""
    snapshot_svc = MagicMock()
    snapshot_svc.list_snapshots.return_value = {"count": 0, "snapshots": []}
    registry = _mock_registry(snapshot=snapshot_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get(
            f"/storage/snapshots/{PROJECT}/{TABLE_ID}",
            headers=AUTH,
            params={"limit": 5},
        )

    assert res.status_code == 200, res.text
    snapshot_svc.list_snapshots.assert_called_once_with(
        alias=PROJECT,
        table_id=TABLE_ID,
        limit=5,
        branch_id=None,
    )


def test_snapshot_detail_passes_kwargs(tmp_path: Path) -> None:
    """GET /storage/snapshot-detail/{p}/{id} -> snapshot.get_snapshot."""
    snapshot_svc = MagicMock()
    snapshot_svc.get_snapshot.return_value = {"snapshot": {"id": "954"}}
    registry = _mock_registry(snapshot=snapshot_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get(f"/storage/snapshot-detail/{PROJECT}/954", headers=AUTH)

    assert res.status_code == 200, res.text
    snapshot_svc.get_snapshot.assert_called_once_with(alias=PROJECT, snapshot_id="954")


def test_snapshot_delete_passes_kwargs(tmp_path: Path) -> None:
    """DELETE /storage/snapshots/{p}?snapshot_id=... -> snapshot.delete_snapshots."""
    snapshot_svc = MagicMock()
    snapshot_svc.delete_snapshots.return_value = {"deleted": ["954"], "failed": []}
    registry = _mock_registry(snapshot=snapshot_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.delete(
            f"/storage/snapshots/{PROJECT}",
            headers=AUTH,
            params={"snapshot_id": ["954", "955"], "dry_run": True},
        )

    assert res.status_code == 200, res.text
    snapshot_svc.delete_snapshots.assert_called_once_with(
        alias=PROJECT,
        snapshot_ids=["954", "955"],
        dry_run=True,
    )


def test_table_from_snapshot_passes_kwargs(tmp_path: Path) -> None:
    """POST /storage/table-from-snapshot/{p} -> snapshot.create_table_from_snapshot."""
    snapshot_svc = MagicMock()
    snapshot_svc.create_table_from_snapshot.return_value = {"table_id": "in.c-dest.restored"}
    registry = _mock_registry(snapshot=snapshot_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.post(
            f"/storage/table-from-snapshot/{PROJECT}",
            headers=AUTH,
            json={"snapshot_id": "954", "bucket_id": "in.c-dest", "name": "restored"},
        )

    assert res.status_code == 200, res.text
    snapshot_svc.create_table_from_snapshot.assert_called_once_with(
        alias=PROJECT,
        bucket_id="in.c-dest",
        snapshot_id="954",
        name="restored",
        branch_id=None,
        dry_run=False,
    )


# ---------------------------------------------------------------------------
# configs.py  GET/PUT /{p}/{c}/{cfg}/state
# Service: config.get_config_state / config.set_config_state (issue #593)
# ---------------------------------------------------------------------------


def test_config_state_get_passes_kwargs(tmp_path: Path) -> None:
    """GET /configs/{p}/{c}/{cfg}/state -> config.get_config_state (no row_id/branch_id)."""
    config_svc = MagicMock()
    config_svc.get_config_state.return_value = {
        "project_alias": PROJECT,
        "component_id": COMPONENT,
        "config_id": CONFIG_ID,
        "row_id": None,
        "branch_id": None,
        "state": {"lastId": 123},
    }
    registry = _mock_registry(config=config_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get(
            f"/configs/{PROJECT}/{COMPONENT}/{CONFIG_ID}/state",
            headers=AUTH,
        )

    assert res.status_code == 200, res.text
    config_svc.get_config_state.assert_called_once_with(
        alias=PROJECT,
        component_id=COMPONENT,
        config_id=CONFIG_ID,
        row_id=None,
        branch_id=None,
    )


def test_config_state_get_forwards_row_id_and_branch_id(tmp_path: Path) -> None:
    """GET .../state?row_id=&branch_id= forwards both query params to the service."""
    config_svc = MagicMock()
    config_svc.get_config_state.return_value = {
        "project_alias": PROJECT,
        "component_id": COMPONENT,
        "config_id": CONFIG_ID,
        "row_id": ROW_ID,
        "branch_id": 456,
        "state": {},
    }
    registry = _mock_registry(config=config_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.get(
            f"/configs/{PROJECT}/{COMPONENT}/{CONFIG_ID}/state",
            headers=AUTH,
            params={"row_id": ROW_ID, "branch_id": 456},
        )

    assert res.status_code == 200, res.text
    config_svc.get_config_state.assert_called_once_with(
        alias=PROJECT,
        component_id=COMPONENT,
        config_id=CONFIG_ID,
        row_id=ROW_ID,
        branch_id=456,
    )


def test_config_state_set_passes_kwargs(tmp_path: Path) -> None:
    """PUT /configs/{p}/{c}/{cfg}/state -> config.set_config_state with body fields."""
    config_svc = MagicMock()
    config_svc.set_config_state.return_value = {
        "project_alias": PROJECT,
        "component_id": COMPONENT,
        "config_id": CONFIG_ID,
        "row_id": None,
        "branch_id": None,
        "state": {"lastId": 999},
        "changed": True,
    }
    registry = _mock_registry(config=config_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.put(
            f"/configs/{PROJECT}/{COMPONENT}/{CONFIG_ID}/state",
            headers=AUTH,
            json={"state": {"lastId": 999}},
        )

    assert res.status_code == 200, res.text
    config_svc.set_config_state.assert_called_once_with(
        alias=PROJECT,
        component_id=COMPONENT,
        config_id=CONFIG_ID,
        state={"lastId": 999},
        row_id=None,
        branch_id=None,
        dry_run=False,
    )


def test_config_state_set_forwards_row_id_branch_id_and_dry_run(tmp_path: Path) -> None:
    """PUT .../state with row_id/branch_id/dry_run in the body forwards all of them."""
    config_svc = MagicMock()
    config_svc.set_config_state.return_value = {
        "project_alias": PROJECT,
        "component_id": COMPONENT,
        "config_id": CONFIG_ID,
        "row_id": ROW_ID,
        "branch_id": 456,
        "dry_run": True,
        "changes": {},
        "old_state": {},
        "new_state": {"lastId": 1},
    }
    registry = _mock_registry(config=config_svc)
    app = _make_app_with_registry(tmp_path, registry)

    with TestClient(app) as client:
        res = client.put(
            f"/configs/{PROJECT}/{COMPONENT}/{CONFIG_ID}/state",
            headers=AUTH,
            json={
                "state": {"lastId": 1},
                "row_id": ROW_ID,
                "branch_id": 456,
                "dry_run": True,
            },
        )

    assert res.status_code == 200, res.text
    config_svc.set_config_state.assert_called_once_with(
        alias=PROJECT,
        component_id=COMPONENT,
        config_id=CONFIG_ID,
        state={"lastId": 1},
        row_id=ROW_ID,
        branch_id=456,
        dry_run=True,
    )


# ---------------------------------------------------------------------------
# notifications.py  GET /notifications  +  GET /{p}/{subscription_id}
# Service: notification.list_subscriptions(aliases=..., event=...,
#          component_id=..., config_id=...)
#          notification.get_subscription_detail(alias=..., subscription_id=...)
# ---------------------------------------------------------------------------


def test_notifications_list_passes_filters(tmp_path: Path) -> None:
    notification_svc = MagicMock()
    notification_svc.list_subscriptions.return_value = {
        "subscriptions": [],
        "errors": [],
        "project_wide_excluded": 0,
    }
    app = _make_app_with_registry(tmp_path, _mock_registry(notification=notification_svc))

    with TestClient(app) as client:
        res = client.get(
            "/notifications",
            headers=AUTH,
            params={
                "project": [PROJECT],
                "event": "job-failed",
                "component_id": "keboola.flow",
                "config_id": CONFIG_ID,
            },
        )

    assert res.status_code == 200, res.text
    notification_svc.list_subscriptions.assert_called_once_with(
        aliases=[PROJECT],
        event="job-failed",
        component_id="keboola.flow",
        config_id=CONFIG_ID,
    )


def test_notifications_list_defaults_to_every_project(tmp_path: Path) -> None:
    notification_svc = MagicMock()
    notification_svc.list_subscriptions.return_value = {
        "subscriptions": [],
        "errors": [],
        "project_wide_excluded": 0,
    }
    app = _make_app_with_registry(tmp_path, _mock_registry(notification=notification_svc))

    with TestClient(app) as client:
        res = client.get("/notifications", headers=AUTH)

    assert res.status_code == 200, res.text
    notification_svc.list_subscriptions.assert_called_once_with(
        aliases=None, event=None, component_id=None, config_id=None
    )


def test_notifications_detail_passes_alias_and_subscription_id(tmp_path: Path) -> None:
    notification_svc = MagicMock()
    notification_svc.get_subscription_detail.return_value = {"subscription_id": "1234"}
    app = _make_app_with_registry(tmp_path, _mock_registry(notification=notification_svc))

    with TestClient(app) as client:
        res = client.get(f"/notifications/{PROJECT}/1234", headers=AUTH)

    assert res.status_code == 200, res.text
    notification_svc.get_subscription_detail.assert_called_once_with(
        alias=PROJECT, subscription_id="1234"
    )


# ---------------------------------------------------------------------------
# token.py  GET /{p}/list?with_last_used=
# Service: token.list_tokens(alias=..., with_last_used=...)
# The enrichment is one extra Storage call PER TOKEN, so a router that dropped
# the query param would silently make every REST listing pay for it -- or,
# worse, silently never deliver it.
# ---------------------------------------------------------------------------


def test_token_list_forwards_with_last_used_kwarg(tmp_path: Path) -> None:
    """`?with_last_used=true` must reach TokenService.list_tokens."""
    token_svc = MagicMock()
    token_svc.list_tokens.return_value = {"alias": PROJECT, "count": 0, "tokens": [], "errors": []}
    app = _make_app_with_registry(tmp_path, _mock_registry(token=token_svc))

    with TestClient(app) as client:
        res = client.get(f"/token/{PROJECT}/list", params={"with_last_used": "true"}, headers=AUTH)

    assert res.status_code == 200, res.text
    kwargs = token_svc.list_tokens.call_args.kwargs
    assert kwargs.get("with_last_used") is True, f"got kwargs={kwargs}"


def test_token_list_defaults_with_last_used_to_false(tmp_path: Path) -> None:
    """Omitting the param must NOT opt the caller into the N+1 enrichment."""
    token_svc = MagicMock()
    token_svc.list_tokens.return_value = {"alias": PROJECT, "count": 0, "tokens": []}
    app = _make_app_with_registry(tmp_path, _mock_registry(token=token_svc))

    with TestClient(app) as client:
        res = client.get(f"/token/{PROJECT}/list", headers=AUTH)

    assert res.status_code == 200, res.text
    assert token_svc.list_tokens.call_args.kwargs.get("with_last_used") is False


# 0.88.0 MCP-parity flags: every one must be reachable over `kbagent serve`,
# not just from the CLI. Each router docstring claims it "Mirrors" its command,
# so a flag the router cannot express makes that claim false (PR #632 review).
# ---------------------------------------------------------------------------


def test_jobs_list_forwards_offset_and_sort(tmp_path: Path) -> None:
    """GET /jobs must expose the Queue API paging controls."""
    job_svc = MagicMock()
    job_svc.list_jobs.return_value = {"jobs": [], "errors": []}
    app = _make_app_with_registry(tmp_path, _mock_registry(job=job_svc))

    with TestClient(app) as client:
        res = client.get(
            "/jobs?offset=100&sort_by=endTime&sort_order=asc",
            headers=AUTH,
        )

    assert res.status_code == 200, res.text
    kwargs = job_svc.list_jobs.call_args.kwargs
    assert kwargs["offset"] == 100
    assert kwargs["sort_by"] == "endTime"
    assert kwargs["sort_order"] == "asc"


def test_jobs_detail_forwards_log_tail_lines(tmp_path: Path) -> None:
    """GET /jobs/{p}/{id} must be able to ask for the log tail."""
    job_svc = MagicMock()
    job_svc.get_job_detail.return_value = {"id": "1"}
    app = _make_app_with_registry(tmp_path, _mock_registry(job=job_svc))

    with TestClient(app) as client:
        res = client.get(f"/jobs/{PROJECT}/123?log_tail_lines=25", headers=AUTH)

    assert res.status_code == 200, res.text
    assert job_svc.get_job_detail.call_args.kwargs["log_tail_lines"] == 25


def test_jobs_detail_defaults_to_no_log_tail(tmp_path: Path) -> None:
    """The extra events call stays opt-in over REST too."""
    job_svc = MagicMock()
    job_svc.get_job_detail.return_value = {"id": "1"}
    app = _make_app_with_registry(tmp_path, _mock_registry(job=job_svc))

    with TestClient(app) as client:
        res = client.get(f"/jobs/{PROJECT}/123", headers=AUTH)

    assert res.status_code == 200, res.text
    assert job_svc.get_job_detail.call_args.kwargs["log_tail_lines"] == 0


def test_search_forwards_scopes(tmp_path: Path) -> None:
    """GET /search must forward repeated ?scope= as scopes=."""
    search_svc = MagicMock()
    search_svc.search.return_value = {"results": [], "errors": [], "stats": {}}
    app = _make_app_with_registry(tmp_path, _mock_registry(search=search_svc))

    with TestClient(app) as client:
        res = client.get(
            "/search?query=orders&search_type=config-based"
            "&scope=storage.input&scope=storage.output",
            headers=AUTH,
        )

    assert res.status_code == 200, res.text
    kwargs = search_svc.search.call_args.kwargs
    assert kwargs["scopes"] == ["storage.input", "storage.output"]


def test_sharing_link_forwards_stage(tmp_path: Path) -> None:
    """POST /sharing/{p}/link must be able to target the out stage."""
    sharing_svc = MagicMock()
    sharing_svc.link.return_value = {"linked_bucket_id": "out.c-x", "message": "ok"}
    app = _make_app_with_registry(tmp_path, _mock_registry(sharing=sharing_svc))

    with TestClient(app) as client:
        res = client.post(
            f"/sharing/{PROJECT}/link",
            headers=AUTH,
            json={"source_project_id": 9, "bucket_id": "out.c-data", "stage": "out"},
        )

    assert res.status_code == 200, res.text
    assert sharing_svc.link.call_args.kwargs["stage"] == "out"


def test_sharing_link_stage_defaults_to_in(tmp_path: Path) -> None:
    """Omitting stage keeps the CLI's `in` default, not the source bucket's."""
    sharing_svc = MagicMock()
    sharing_svc.link.return_value = {"linked_bucket_id": "in.c-x", "message": "ok"}
    app = _make_app_with_registry(tmp_path, _mock_registry(sharing=sharing_svc))

    with TestClient(app) as client:
        res = client.post(
            f"/sharing/{PROJECT}/link",
            headers=AUTH,
            json={"source_project_id": 9, "bucket_id": "out.c-data"},
        )

    assert res.status_code == 200, res.text
    assert sharing_svc.link.call_args.kwargs["stage"] == "in"


def test_storage_tables_forwards_include_usage(tmp_path: Path) -> None:
    """GET /storage/tables must expose the usage scan."""
    storage_svc = MagicMock()
    storage_svc.list_tables.return_value = {"tables": [], "errors": []}
    app = _make_app_with_registry(tmp_path, _mock_registry(storage=storage_svc))

    with TestClient(app) as client:
        res = client.get("/storage/tables?include_usage=true", headers=AUTH)

    assert res.status_code == 200, res.text
    assert storage_svc.list_tables.call_args.kwargs["include_usage"] is True


# ---------------------------------------------------------------------------
# configs.py  trash safety: DELETE dry_run + restore + trash listing (0.89.0)
# ---------------------------------------------------------------------------


def test_config_delete_forwards_dry_run(tmp_path: Path) -> None:
    """DELETE /configs/... must be able to preview without deleting."""
    cfg_svc = MagicMock()
    cfg_svc.delete_config.return_value = {"status": "would_delete"}
    app = _make_app_with_registry(tmp_path, _mock_registry(config=cfg_svc))

    with TestClient(app) as client:
        res = client.delete(
            f"/configs/{PROJECT}/{COMPONENT}/{CONFIG_ID}?dry_run=true", headers=AUTH
        )

    assert res.status_code == 200, res.text
    assert cfg_svc.delete_config.call_args.kwargs["dry_run"] is True


def test_config_restore_route(tmp_path: Path) -> None:
    """POST .../restore mirrors `kbagent config restore`."""
    cfg_svc = MagicMock()
    cfg_svc.restore_config.return_value = {"status": "restored"}
    app = _make_app_with_registry(tmp_path, _mock_registry(config=cfg_svc))

    with TestClient(app) as client:
        res = client.post(f"/configs/{PROJECT}/{COMPONENT}/{CONFIG_ID}/restore", headers=AUTH)

    assert res.status_code == 200, res.text
    kwargs = cfg_svc.restore_config.call_args.kwargs
    assert kwargs["component_id"] == COMPONENT
    assert kwargs["config_id"] == CONFIG_ID


def test_config_trash_list_route(tmp_path: Path) -> None:
    """GET /configs/trash/{project} mirrors `kbagent config trash-list`."""
    cfg_svc = MagicMock()
    cfg_svc.list_config_trash.return_value = {"trash": []}
    app = _make_app_with_registry(tmp_path, _mock_registry(config=cfg_svc))

    with TestClient(app) as client:
        res = client.get(f"/configs/trash/{PROJECT}?component_id={COMPONENT}", headers=AUTH)

    assert res.status_code == 200, res.text
    assert cfg_svc.list_config_trash.call_args.kwargs["component_id"] == COMPONENT
