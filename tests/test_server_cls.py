"""Tests for `server/routers/cls.py` -- router -> service call parity + permission gating.

Mirrors ``test_server_rls.py``: kwarg parity via a mocked ``ServiceRegistry``
and persisted-policy -> 403. CLS writes are ``admin``, like RLS.
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
RULES = [{"principal": "a@x.com", "visible_columns": ["id"]}]


def _app(tmp_path: Path, cls_svc: MagicMock) -> Any:
    app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    registry = ServiceRegistry.__new__(ServiceRegistry)
    registry.cls = cls_svc
    app.dependency_overrides[get_registry] = lambda: registry
    return app


def _persist_policy(config_dir: Path, policy: PermissionPolicy) -> None:
    store = ConfigStore(config_dir=config_dir)
    config = store.load()
    config.permissions = policy
    store.save(config)


def test_list_and_detail_call_service(tmp_path: Path) -> None:
    svc = MagicMock()
    svc.list_policies.return_value = {"project": PROJECT, "policies": []}
    svc.get_policy.return_value = {"id": POLICY_ID}

    with TestClient(_app(tmp_path, svc)) as client:
        assert client.get(f"/cls/{PROJECT}", headers=AUTH).status_code == 200
        assert client.get(f"/cls/{PROJECT}/{POLICY_ID}", headers=AUTH).status_code == 200

    svc.list_policies.assert_called_once_with(PROJECT)
    svc.get_policy.assert_called_once_with(PROJECT, POLICY_ID)


def test_schema_success_and_not_shadowed_by_policy_id_route(tmp_path: Path) -> None:
    svc = MagicMock()
    svc.fetch_schema.return_value = MagicMock(schema={"type": "object"}, reason=None)

    with TestClient(_app(tmp_path, svc)) as client:
        res = client.get(f"/cls/{PROJECT}/schema", headers=AUTH)

    assert res.status_code == 200, res.text
    assert res.json()["schema"] == {"type": "object"}
    svc.fetch_schema.assert_called_once_with(PROJECT)
    svc.get_policy.assert_not_called()


def test_schema_fetch_failure_is_404(tmp_path: Path) -> None:
    svc = MagicMock()
    svc.fetch_schema.return_value = MagicMock(schema=None, reason="not registered")

    with TestClient(_app(tmp_path, svc)) as client:
        res = client.get(f"/cls/{PROJECT}/schema", headers=AUTH)

    assert res.status_code == 404, res.text
    assert res.json()["error"]["code"] == "NOT_FOUND"
    assert "cls-policy" in res.json()["error"]["message"]


def test_create_and_update_pass_kwargs(tmp_path: Path) -> None:
    svc = MagicMock()
    svc.create_policy.return_value = {"id": POLICY_ID}
    svc.update_policy.return_value = {"id": POLICY_ID}
    body = {
        "table": "in.c-crm.invoices",
        "dialect": "snowflake",
        "rules": RULES,
        "target_project_ids": [999],
    }

    with TestClient(_app(tmp_path, svc)) as client:
        assert client.post(f"/cls/{PROJECT}", headers=AUTH, json=body).status_code == 200
        res = client.put(f"/cls/{PROJECT}/{POLICY_ID}", headers=AUTH, json={"rules": RULES})
        assert res.status_code == 200, res.text

    create_kwargs = svc.create_policy.call_args.kwargs
    assert create_kwargs["rules"] == RULES
    assert create_kwargs["target_project_ids"] == [999]
    assert create_kwargs["dry_run"] is False
    args, update_kwargs = svc.update_policy.call_args
    assert args == (PROJECT, POLICY_ID)
    assert update_kwargs["rules"] == RULES
    assert update_kwargs["table"] is None


def test_delete_calls_service(tmp_path: Path) -> None:
    svc = MagicMock()
    svc.delete_policy.return_value = {"deleted": True}

    with TestClient(_app(tmp_path, svc)) as client:
        res = client.delete(f"/cls/{PROJECT}/{POLICY_ID}", headers=AUTH)

    assert res.status_code == 200, res.text
    svc.delete_policy.assert_called_once_with(PROJECT, POLICY_ID)


def test_service_error_maps_to_http_status(tmp_path: Path) -> None:
    svc = MagicMock()
    svc.get_policy.side_effect = KeboolaApiError(
        message="not found", status_code=404, error_code=ErrorCode.NOT_FOUND
    )

    with TestClient(_app(tmp_path, svc)) as client:
        res = client.get(f"/cls/{PROJECT}/{POLICY_ID}", headers=AUTH)

    assert res.status_code == 404, res.text


class TestPermissionGating:
    @pytest.mark.parametrize("deny", ["cli:admin", "cli:write"])
    def test_denied_policy_blocks_every_write(self, tmp_path: Path, deny: str) -> None:
        _persist_policy(tmp_path, PermissionPolicy(mode="allow", deny=[deny]))
        svc = MagicMock()
        body = {"table": "t", "dialect": "snowflake", "rules": RULES}

        with TestClient(_app(tmp_path, svc)) as client:
            responses = [
                client.post(f"/cls/{PROJECT}", headers=AUTH, json=body),
                client.put(f"/cls/{PROJECT}/{POLICY_ID}", headers=AUTH, json={"table": "t"}),
                client.delete(f"/cls/{PROJECT}/{POLICY_ID}", headers=AUTH),
            ]

        for res in responses:
            assert res.status_code == 403, res.text
            assert res.json()["error"]["code"] == "PERMISSION_DENIED"
        svc.create_policy.assert_not_called()
        svc.update_policy.assert_not_called()
        svc.delete_policy.assert_not_called()

    def test_denying_cli_admin_still_allows_reads(self, tmp_path: Path) -> None:
        _persist_policy(tmp_path, PermissionPolicy(mode="allow", deny=["cli:admin"]))
        svc = MagicMock()
        svc.list_policies.return_value = {"project": PROJECT, "policies": []}
        svc.get_policy.return_value = {"id": POLICY_ID}
        svc.fetch_schema.return_value = MagicMock(schema={"type": "object"}, reason=None)

        with TestClient(_app(tmp_path, svc)) as client:
            statuses = [
                client.get(f"/cls/{PROJECT}", headers=AUTH).status_code,
                client.get(f"/cls/{PROJECT}/{POLICY_ID}", headers=AUTH).status_code,
                client.get(f"/cls/{PROJECT}/schema", headers=AUTH).status_code,
            ]

        assert statuses == [200, 200, 200]
