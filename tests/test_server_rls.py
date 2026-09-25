"""Tests for `server/routers/rls.py` -- router -> service call parity + permission gating.

Two families, mirroring ``test_server_router_calls.py`` (kwarg parity via a
mocked ``ServiceRegistry``) and ``test_server_permissions.py`` (persisted
policy -> 403). RLS is classified ``admin`` for every write -- these tests
pin that ``cli:admin`` (not merely ``cli:write``) is what has to be denied
to block ``create``/``update``/``delete``, and that ``list``/``detail``/
``schema`` stay reachable under that same denial.
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

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import PermissionPolicy
from keboola_agent_cli.server import create_app
from keboola_agent_cli.server.dependencies import ServiceRegistry, get_registry

AUTH = {"Authorization": "Bearer test-token"}
PROJECT = "prod"
POLICY_ID = "p-1"


def _make_app_with_registry(tmp_path: Path, registry: ServiceRegistry) -> Any:
    app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    app.dependency_overrides[get_registry] = lambda: registry
    return app


def _mock_registry(**services: Any) -> ServiceRegistry:
    registry = ServiceRegistry.__new__(ServiceRegistry)
    for name, mock in services.items():
        setattr(registry, name, mock)
    return registry


def _persist_policy(config_dir: Path, policy: PermissionPolicy) -> None:
    store = ConfigStore(config_dir=config_dir)
    config = store.load()
    config.permissions = policy
    store.save(config)


# ---------------------------------------------------------------------------
# Router -> service call parity
# ---------------------------------------------------------------------------


def test_list_policies_calls_service(tmp_path: Path) -> None:
    rls_svc = MagicMock()
    rls_svc.list_policies.return_value = {"project": PROJECT, "policies": []}
    app = _make_app_with_registry(tmp_path, _mock_registry(rls=rls_svc))

    with TestClient(app) as client:
        res = client.get(f"/rls/{PROJECT}", headers=AUTH)

    assert res.status_code == 200, res.text
    rls_svc.list_policies.assert_called_once_with(PROJECT)


def test_get_policy_calls_service(tmp_path: Path) -> None:
    rls_svc = MagicMock()
    rls_svc.get_policy.return_value = {"id": POLICY_ID}
    app = _make_app_with_registry(tmp_path, _mock_registry(rls=rls_svc))

    with TestClient(app) as client:
        res = client.get(f"/rls/{PROJECT}/{POLICY_ID}", headers=AUTH)

    assert res.status_code == 200, res.text
    rls_svc.get_policy.assert_called_once_with(PROJECT, POLICY_ID)


def test_schema_success(tmp_path: Path) -> None:
    rls_svc = MagicMock()
    rls_svc.fetch_schema.return_value = MagicMock(schema={"type": "object"}, reason=None)
    app = _make_app_with_registry(tmp_path, _mock_registry(rls=rls_svc))

    with TestClient(app) as client:
        res = client.get(f"/rls/{PROJECT}/schema", headers=AUTH)

    assert res.status_code == 200, res.text
    assert res.json()["schema"] == {"type": "object"}


def test_schema_fetch_failure_is_404_not_a_crash(tmp_path: Path) -> None:
    rls_svc = MagicMock()
    rls_svc.fetch_schema.return_value = MagicMock(schema=None, reason="not registered yet")
    app = _make_app_with_registry(tmp_path, _mock_registry(rls=rls_svc))

    with TestClient(app) as client:
        res = client.get(f"/rls/{PROJECT}/schema", headers=AUTH)

    assert res.status_code == 404, res.text
    assert res.json()["error"]["code"] == "NOT_FOUND"


def test_schema_route_not_shadowed_by_policy_id_route(tmp_path: Path) -> None:
    """`/rls/{project}/schema` must resolve to the schema route, never `detail`

    with policy_id='schema' -- the registration-order guard the router's
    docstring promises."""
    rls_svc = MagicMock()
    rls_svc.fetch_schema.return_value = MagicMock(schema={"type": "object"}, reason=None)
    app = _make_app_with_registry(tmp_path, _mock_registry(rls=rls_svc))

    with TestClient(app) as client:
        client.get(f"/rls/{PROJECT}/schema", headers=AUTH)

    rls_svc.fetch_schema.assert_called_once_with(PROJECT)
    rls_svc.get_policy.assert_not_called()


def test_create_policy_passes_kwargs(tmp_path: Path) -> None:
    rls_svc = MagicMock()
    rls_svc.create_policy.return_value = {"id": POLICY_ID}
    app = _make_app_with_registry(tmp_path, _mock_registry(rls=rls_svc))
    body = {
        "table": "in.c-crm.invoices",
        "dialect": "snowflake",
        "rules": [{"principal": "a@x.com", "condition": {"true": True}}],
        "target_project_ids": ["999"],
        "dry_run": False,
    }

    with TestClient(app) as client:
        res = client.post(f"/rls/{PROJECT}", headers=AUTH, json=body)

    assert res.status_code == 200, res.text
    kwargs = rls_svc.create_policy.call_args.kwargs
    assert kwargs["table"] == "in.c-crm.invoices"
    assert kwargs["dialect"] == "snowflake"
    assert kwargs["rules"] == body["rules"]
    assert kwargs["target_project_ids"] == ["999"]
    assert kwargs["dry_run"] is False


def test_update_policy_passes_kwargs(tmp_path: Path) -> None:
    rls_svc = MagicMock()
    rls_svc.update_policy.return_value = {"id": POLICY_ID}
    app = _make_app_with_registry(tmp_path, _mock_registry(rls=rls_svc))

    with TestClient(app) as client:
        res = client.put(f"/rls/{PROJECT}/{POLICY_ID}", headers=AUTH, json={"table": "new.table"})

    assert res.status_code == 200, res.text
    args, kwargs = rls_svc.update_policy.call_args
    assert args == (PROJECT, POLICY_ID)
    assert kwargs["table"] == "new.table"
    assert kwargs["rules"] is None


def test_delete_policy_calls_service(tmp_path: Path) -> None:
    rls_svc = MagicMock()
    rls_svc.delete_policy.return_value = {"deleted": True}
    app = _make_app_with_registry(tmp_path, _mock_registry(rls=rls_svc))

    with TestClient(app) as client:
        res = client.delete(f"/rls/{PROJECT}/{POLICY_ID}", headers=AUTH)

    assert res.status_code == 200, res.text
    rls_svc.delete_policy.assert_called_once_with(PROJECT, POLICY_ID)


def test_keboola_api_error_from_service_maps_to_http_status(tmp_path: Path) -> None:
    rls_svc = MagicMock()
    rls_svc.get_policy.side_effect = KeboolaApiError(
        message="not found", status_code=404, error_code=ErrorCode.NOT_FOUND
    )
    app = _make_app_with_registry(tmp_path, _mock_registry(rls=rls_svc))

    with TestClient(app) as client:
        res = client.get(f"/rls/{PROJECT}/{POLICY_ID}", headers=AUTH)

    assert res.status_code == 404, res.text


# ---------------------------------------------------------------------------
# Permission gating -- RLS writes are `admin`, not merely `write`
# ---------------------------------------------------------------------------


class TestPermissionGating:
    def test_denying_cli_admin_blocks_create(self, tmp_path: Path) -> None:
        _persist_policy(tmp_path, PermissionPolicy(mode="allow", deny=["cli:admin"]))
        rls_svc = MagicMock()
        app = _make_app_with_registry(tmp_path, _mock_registry(rls=rls_svc))

        with TestClient(app) as client:
            res = client.post(
                f"/rls/{PROJECT}",
                headers=AUTH,
                json={
                    "table": "t",
                    "dialect": "snowflake",
                    "rules": [{"principal": "a@x.com", "condition": {"true": True}}],
                },
            )

        assert res.status_code == 403, res.text
        assert res.json()["error"]["code"] == "PERMISSION_DENIED"
        rls_svc.create_policy.assert_not_called()

    def test_denying_cli_admin_blocks_delete(self, tmp_path: Path) -> None:
        _persist_policy(tmp_path, PermissionPolicy(mode="allow", deny=["cli:admin"]))
        rls_svc = MagicMock()
        app = _make_app_with_registry(tmp_path, _mock_registry(rls=rls_svc))

        with TestClient(app) as client:
            res = client.delete(f"/rls/{PROJECT}/{POLICY_ID}", headers=AUTH)

        assert res.status_code == 403, res.text
        rls_svc.delete_policy.assert_not_called()

    def test_denying_cli_admin_still_allows_reads(self, tmp_path: Path) -> None:
        _persist_policy(tmp_path, PermissionPolicy(mode="allow", deny=["cli:admin"]))
        rls_svc = MagicMock()
        rls_svc.list_policies.return_value = {"project": PROJECT, "policies": []}
        app = _make_app_with_registry(tmp_path, _mock_registry(rls=rls_svc))

        with TestClient(app) as client:
            res = client.get(f"/rls/{PROJECT}", headers=AUTH)

        assert res.status_code == 200, res.text
        rls_svc.list_policies.assert_called_once()

    def test_denying_cli_write_also_blocks_admin_ops(self, tmp_path: Path) -> None:
        """`cli:write` spans write+destructive+admin (see permissions.py) --

        pins that RLS writes are not a hole in that broader firewall."""
        _persist_policy(tmp_path, PermissionPolicy(mode="allow", deny=["cli:write"]))
        rls_svc = MagicMock()
        app = _make_app_with_registry(tmp_path, _mock_registry(rls=rls_svc))

        with TestClient(app) as client:
            res = client.delete(f"/rls/{PROJECT}/{POLICY_ID}", headers=AUTH)

        assert res.status_code == 403, res.text
        rls_svc.delete_policy.assert_not_called()
