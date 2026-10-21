"""Tests for `server/routers/rls_bindings.py`: router -> service parity, route order, permissions."""

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
from keboola_agent_cli.models import PermissionPolicy
from keboola_agent_cli.server import create_app
from keboola_agent_cli.server.dependencies import ServiceRegistry, get_registry

AUTH = {"Authorization": "Bearer test-token"}
STACK = "dev-keboola-aws-eu-west-1"
RLS_ID = "11111111-1111-4111-8111-111111111111"


def _client(tmp_path: Path, service: MagicMock, rls: MagicMock | None = None) -> TestClient:
    registry = ServiceRegistry.__new__(ServiceRegistry)
    registry.policy_binding = service
    registry.rls = rls or MagicMock()
    app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    app.dependency_overrides[get_registry] = lambda: registry
    return TestClient(app)


def _deny(tmp_path: Path, deny: str) -> None:
    store = ConfigStore(config_dir=tmp_path)
    config = store.load()
    config.permissions = PermissionPolicy(mode="allow", deny=[deny])
    store.save(config)


def test_list_is_not_captured_by_the_policy_detail_route(tmp_path: Path) -> None:
    service, rls = MagicMock(), MagicMock()
    service.list_bindings.return_value = {"project": "prod", "bindings": []}

    with _client(tmp_path, service, rls) as client:
        res = client.get("/rls/prod/bindings?workspace_id=42", headers=AUTH)

    assert res.status_code == 200, res.text
    service.list_bindings.assert_called_once_with("prod", 42)
    rls.get_policy.assert_not_called()


def test_bind_calls_service(tmp_path: Path) -> None:
    service = MagicMock()
    service.bind.return_value = {"kid": "k", "action": "created"}

    with _client(tmp_path, service) as client:
        res = client.put(
            "/rls/prod/bindings/42",
            headers=AUTH,
            json={"stack_id": STACK, "rls_policies": [RLS_ID], "dry_run": True},
        )

    assert res.status_code == 200, res.text
    service.bind.assert_called_once_with(
        "prod", 42, stack_id=STACK, rls_policies=[RLS_ID], cls_policies=[], dry_run=True
    )


def test_bind_requires_stack_id(tmp_path: Path) -> None:
    service = MagicMock()

    with _client(tmp_path, service) as client:
        res = client.put("/rls/prod/bindings/42", headers=AUTH, json={"rls_policies": [RLS_ID]})

    assert res.status_code in (400, 422), res.text
    service.bind.assert_not_called()


def test_unbind_calls_service(tmp_path: Path) -> None:
    service = MagicMock()
    service.unbind.return_value = {"deleted": True}

    with _client(tmp_path, service) as client:
        res = client.delete(f"/rls/prod/bindings/42?stack_id={STACK}", headers=AUTH)

    assert res.status_code == 200, res.text
    service.unbind.assert_called_once_with("prod", 42, stack_id=STACK, dry_run=False)


@pytest.mark.parametrize(
    ("body", "delete"), [({}, False), ({"dry_run": False, "older_than_days": 7}, True)]
)
def test_sweep_calls_service(tmp_path: Path, body: dict[str, Any], delete: bool) -> None:
    service = MagicMock()
    service.sweep.return_value = {"bindings": []}

    with _client(tmp_path, service) as client:
        res = client.post("/rls/prod/bindings/sweep", headers=AUTH, json=body)

    assert res.status_code == 200, res.text
    service.sweep.assert_called_once_with(
        "prod", older_than_days=body.get("older_than_days", 30), delete=delete
    )


@pytest.mark.parametrize(("dry_run", "status"), [(True, 200), (False, 403)])
def test_denying_destructive_blocks_only_a_deleting_sweep(
    tmp_path: Path, dry_run: bool, status: int
) -> None:
    _deny(tmp_path, "cli:destructive")
    service = MagicMock()
    service.sweep.return_value = {"bindings": []}

    with _client(tmp_path, service) as client:
        res = client.post("/rls/prod/bindings/sweep", headers=AUTH, json={"dry_run": dry_run})

    assert res.status_code == status, res.text
    assert service.sweep.called is (status == 200)


@pytest.mark.parametrize(("deny", "status"), [("cli:destructive", 403), ("cli:admin", 200)])
def test_unbind_is_destructive(tmp_path: Path, deny: str, status: int) -> None:
    _deny(tmp_path, deny)
    service = MagicMock()
    service.unbind.return_value = {"deleted": True}

    with _client(tmp_path, service) as client:
        res = client.delete(f"/rls/prod/bindings/42?stack_id={STACK}", headers=AUTH)

    assert res.status_code == status, res.text


def test_bind_is_admin(tmp_path: Path) -> None:
    _deny(tmp_path, "cli:admin")
    service = MagicMock()

    with _client(tmp_path, service) as client:
        res = client.put(
            "/rls/prod/bindings/42",
            headers=AUTH,
            json={"stack_id": STACK, "rls_policies": [RLS_ID]},
        )

    assert res.status_code == 403, res.text
    service.bind.assert_not_called()
