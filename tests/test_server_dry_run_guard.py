"""Tests for the app-level refusal of an undeclared ``dry_run`` (CLI-33).

FastAPI ignores undeclared query parameters and body fields, so a mutating
route without ``dry_run`` used to run the real write when a caller asked for a
preview (``DELETE /flows/p/123?dry_run=true``). The walk tests below send that
request to every mutating route on the live app; the remaining tests pin what
must keep working: declared ``dry_run``, the multipart upload, and plain bodies.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Annotated, Any
from unittest.mock import MagicMock

import pytest

if importlib.util.find_spec("fastapi") is None:  # pragma: no cover
    pytest.skip(
        "FastAPI not installed; run `uv pip install -e '.[server]'`", allow_module_level=True
    )

from fastapi import APIRouter, Body
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from keboola_agent_cli.server import create_app
from keboola_agent_cli.server.dependencies import (
    _body_declares_dry_run,
    _is_false,
    get_registry,
)
from test_server_route_permissions import _iter_api_routes

TOKEN = "dry-run-guard-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MUTATING = ("POST", "PUT", "PATCH", "DELETE")
# A request to this URL is matched by `POST /data-apps/{project}`, which is
# registered first, so the walk cannot reach the route itself.
UNREACHABLE = {("POST", "/data-apps/validate-repo")}


def _app(tmp_path: Path, registry: Any) -> Any:
    app = create_app(config_dir=str(tmp_path), auth_token=TOKEN)
    app.dependency_overrides[get_registry] = lambda: registry
    return app


def _url(path: str) -> str:
    """A concrete URL for a route template; every path parameter becomes ``x``."""
    return re.sub(r"\{[^}]+\}", "x", path)


def _mutating_routes(app: Any) -> list[tuple[str, Any]]:
    return sorted(
        (
            (method, route)
            for route in _iter_api_routes(app)
            for method in route.methods
            if method in MUTATING and (method, route.path) not in UNREACHABLE
        ),
        key=lambda item: (item[1].path, item[0]),
    )


def _declares(route: Any, where: str) -> bool:
    """Whether ``route`` takes ``dry_run`` as a query parameter or a body field."""
    flat = get_flat_dependant(route.dependant)
    if where == "query":
        return any(p.alias == "dry_run" for p in flat.query_params)
    return _body_declares_dry_run(flat.body_params)


def _is_form(route: Any) -> bool:
    return any(
        type(p.field_info).__name__ in ("Form", "File")
        for p in get_flat_dependant(route.dependant).body_params
    )


def _assert_refused(response: Any, method: str, route: Any) -> None:
    label = f"{method} {route.path}"
    assert response.status_code == 422, f"{label}: {response.status_code} {response.text}"
    body = response.json()
    assert body["status"] == "error", label
    assert body["error"]["code"] == "HTTP_ERROR", label
    assert "`dry_run`" in body["error"]["message"], label
    assert label in body["error"]["message"]


class TestUndeclaredDryRunIsRefused:
    """Every mutating route without ``dry_run`` answers 422 and calls no service."""

    def test_query_dry_run_on_every_mutating_route(self, tmp_path: Path) -> None:
        registry = MagicMock()
        app = _app(tmp_path, registry)
        client = TestClient(app, raise_server_exceptions=False)
        checked = []
        for method, route in _mutating_routes(app):
            if _declares(route, "query"):
                continue
            response = client.request(method, _url(route.path) + "?dry_run=true", headers=AUTH)
            _assert_refused(response, method, route)
            if not _declares(route, "body"):
                assert "has no dry run" in response.json()["error"]["message"]
            checked.append((method, route.path))
        assert ("DELETE", "/flows/{project}/{config_id}") in checked
        assert ("POST", "/storage/tables/{project}/upload") in checked
        assert registry.mock_calls == []

    def test_body_dry_run_on_every_mutating_json_route(self, tmp_path: Path) -> None:
        registry = MagicMock()
        app = _app(tmp_path, registry)
        client = TestClient(app, raise_server_exceptions=False)
        checked = []
        for method, route in _mutating_routes(app):
            if _declares(route, "body") or _is_form(route):
                continue
            response = client.request(
                method, _url(route.path), headers=AUTH, json={"dry_run": True}
            )
            _assert_refused(response, method, route)
            checked.append((method, route.path))
        # A route with a body model and one with no body parameter at all.
        assert ("PATCH", "/configs/{project}/{component_id}/{config_id}/rows/{row_id}") in checked
        assert ("DELETE", "/flows/{project}/{config_id}") in checked
        assert registry.mock_calls == []

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE", "Off", "f", "n"])
    def test_false_in_the_query_is_ignored(self, tmp_path: Path, value: str) -> None:
        """A false value asks for the real operation, which the route runs anyway."""
        flow = MagicMock()
        flow.delete_flow.return_value = {"deleted": True}
        client = TestClient(_app(tmp_path, MagicMock(flow=flow)))
        response = client.delete(f"/flows/p/123?dry_run={value}", headers=AUTH)
        assert response.status_code == 200, response.text
        flow.delete_flow.assert_called_once()

    @pytest.mark.parametrize("value", [False, 0, "false", "No"])
    def test_false_in_the_body_is_ignored(self, tmp_path: Path, value: Any) -> None:
        config = MagicMock()
        config.update_config_row.return_value = {"id": "7"}
        client = TestClient(_app(tmp_path, MagicMock(config=config)))
        response = client.patch(
            "/configs/p/keboola.ex-http/42/rows/7",
            headers=AUTH,
            json={"name": "renamed", "dry_run": value},
        )
        assert response.status_code == 200, response.text
        config.update_config_row.assert_called_once()

    def test_false_in_the_wrong_place_is_ignored(self, tmp_path: Path) -> None:
        """Config delete takes `dry_run` in the query; a false body value still runs."""
        config = MagicMock()
        config.delete_config.return_value = {"deleted": True}
        client = TestClient(_app(tmp_path, MagicMock(config=config)))
        response = client.request(
            "DELETE", "/configs/p/keboola.ex-http/42", headers=AUTH, json={"dry_run": False}
        )
        assert response.status_code == 200, response.text
        assert config.delete_config.call_args.kwargs["dry_run"] is False

    @pytest.mark.parametrize("value", ["yes", "maybe", "", "dry_run=false&dry_run=true"])
    def test_other_query_values_are_refused(self, tmp_path: Path, value: str) -> None:
        flow = MagicMock()
        client = TestClient(_app(tmp_path, MagicMock(flow=flow)))
        query = value if "=" in value else f"dry_run={value}"
        response = client.delete(f"/flows/p/123?{query}", headers=AUTH)
        assert response.status_code == 422, response.text
        flow.delete_flow.assert_not_called()

    @pytest.mark.parametrize("value", [None, "maybe", 1, 2, 0.5, True, {"x": 1}])
    def test_other_body_values_are_refused(self, tmp_path: Path, value: Any) -> None:
        config = MagicMock()
        client = TestClient(_app(tmp_path, MagicMock(config=config)))
        response = client.patch(
            "/configs/p/keboola.ex-http/42/rows/7", headers=AUTH, json={"dry_run": value}
        )
        assert response.status_code == 422, response.text
        assert "has no dry run" in response.json()["error"]["message"]
        config.update_config_row.assert_not_called()

    def test_get_routes_are_not_checked(self, tmp_path: Path) -> None:
        flow = MagicMock()
        flow.list_flows.return_value = {"flows": []}
        client = TestClient(_app(tmp_path, MagicMock(flow=flow)))
        response = client.get("/flows?project=p&dry_run=true", headers=AUTH)
        assert response.status_code == 200, response.text


class TestDeclaredDryRunStillWorks:
    def test_query_form(self, tmp_path: Path) -> None:
        config = MagicMock()
        config.delete_config.return_value = {"dry_run": True}
        client = TestClient(_app(tmp_path, MagicMock(config=config)))
        response = client.delete("/configs/p/keboola.ex-http/42?dry_run=true", headers=AUTH)
        assert response.status_code == 200, response.text
        assert config.delete_config.call_args.kwargs["dry_run"] is True

    def test_body_form(self, tmp_path: Path) -> None:
        job = MagicMock()
        job.terminate_jobs.return_value = {"dry_run": True}
        client = TestClient(_app(tmp_path, MagicMock(job=job)))
        response = client.post(
            "/jobs/p/terminate", headers=AUTH, json={"job_ids": ["1"], "dry_run": True}
        )
        assert response.status_code == 200, response.text
        assert job.terminate_jobs.call_args.kwargs["dry_run"] is True

    def test_declared_in_the_query_refused_in_the_body(self, tmp_path: Path) -> None:
        config = MagicMock()
        client = TestClient(_app(tmp_path, MagicMock(config=config)))
        response = client.request(
            "DELETE", "/configs/p/keboola.ex-http/42", headers=AUTH, json={"dry_run": True}
        )
        assert response.status_code == 422, response.text
        assert "takes `dry_run` in the query string" in response.json()["error"]["message"]
        config.delete_config.assert_not_called()

    def test_declared_routes_are_not_refused(self, tmp_path: Path) -> None:
        """The other direction of the walk: a declared ``dry_run`` passes the check."""
        app = _app(tmp_path, MagicMock())
        client = TestClient(app, raise_server_exceptions=False)
        for method, route in _mutating_routes(app):
            if _declares(route, "query"):
                response = client.request(
                    method, _url(route.path) + "?dry_run=true", headers=AUTH, json={}
                )
            elif _declares(route, "body"):
                response = client.request(
                    method, _url(route.path), headers=AUTH, json={"dry_run": True}
                )
            else:
                continue
            # A route can still answer 422 for its own reasons (FastAPI body
            # validation of the `x` placeholders), so only a 422 that names
            # `dry_run` is this check's refusal.
            refused = response.status_code == 422 and "`dry_run`" in response.text
            assert not refused, f"{method} {route.path}: {response.text}"


class TestBodiesAndUploads:
    def test_body_without_dry_run_is_unaffected(self, tmp_path: Path) -> None:
        """A nested ``dry_run`` is configuration content, not the control parameter."""
        config = MagicMock()
        config.update_config_row.return_value = {"id": "7"}
        client = TestClient(_app(tmp_path, MagicMock(config=config)))
        response = client.patch(
            "/configs/p/keboola.ex-http/42/rows/7",
            headers=AUTH,
            json={"name": "renamed", "configuration": {"parameters": {"dry_run": True}}},
        )
        assert response.status_code == 200, response.text
        kwargs = config.update_config_row.call_args.kwargs
        assert kwargs["name"] == "renamed"
        assert kwargs["configuration"] == {"parameters": {"dry_run": True}}

    def test_route_without_body_ignores_a_non_json_body(self, tmp_path: Path) -> None:
        flow = MagicMock()
        flow.delete_flow.return_value = {"deleted": True}
        client = TestClient(_app(tmp_path, MagicMock(flow=flow)))
        response = client.request(
            "DELETE",
            "/flows/p/123",
            headers={**AUTH, "Content-Type": "text/plain"},
            content=b'{"dry_run": true}',
        )
        assert response.status_code == 200, response.text
        flow.delete_flow.assert_called_once()

    def test_upload_streams_the_file_and_is_refused_only_on_the_query(self, tmp_path: Path) -> None:
        """The multipart upload: its body is never read by the check.

        A ``dry_run`` form field is not inspected (the route still gets the
        whole file), while ``?dry_run=true`` is refused before the upload runs.
        """
        received: list[bytes] = []

        def _upload_table(**kwargs: Any) -> dict[str, Any]:
            received.append(Path(kwargs["file_path"]).read_bytes())
            return {"imported": True}

        storage = MagicMock()
        storage.upload_table.side_effect = _upload_table
        client = TestClient(_app(tmp_path, MagicMock(storage=storage)))
        payload = b"id,name\n" + b"1,a\n" * 50_000
        files = {"file": ("data.csv", payload, "text/csv")}

        response = client.post(
            "/storage/tables/p/upload",
            headers=AUTH,
            data={"table_id": "in.c-main.t", "dry_run": "true"},
            files=files,
        )
        assert response.status_code == 200, response.text
        assert received == [payload]

        response = client.post(
            "/storage/tables/p/upload?dry_run=true",
            headers=AUTH,
            data={"table_id": "in.c-main.t"},
            files=files,
        )
        assert response.status_code == 422, response.text
        assert storage.upload_table.call_count == 1

    def test_deeply_nested_body_passes_through(self, tmp_path: Path) -> None:
        """JSON too deep for the parser is not this check's business: the route runs."""
        flow = MagicMock()
        flow.delete_flow.return_value = {"deleted": True}
        client = TestClient(_app(tmp_path, MagicMock(flow=flow)))
        response = client.request(
            "DELETE",
            "/flows/p/123",
            headers={**AUTH, "Content-Type": "application/json"},
            content=b"[" * 200_000,
        )
        assert response.status_code == 200, response.text
        flow.delete_flow.assert_called_once()

    def test_large_body_on_a_route_without_body_is_not_read(self, tmp_path: Path) -> None:
        """Above 1 MiB the check does not read the body, so its `dry_run` is not seen."""
        flow = MagicMock()
        flow.delete_flow.return_value = {"deleted": True}
        client = TestClient(_app(tmp_path, MagicMock(flow=flow)))
        body = b'{"dry_run": true, "pad": "' + b"x" * (1024 * 1024) + b'"}'
        response = client.request(
            "DELETE",
            "/flows/p/123",
            headers={**AUTH, "Content-Type": "application/json"},
            content=body,
        )
        assert response.status_code == 200, response.text
        flow.delete_flow.assert_called_once()

    def test_body_without_length_on_a_route_without_body_is_not_read(self, tmp_path: Path) -> None:
        flow = MagicMock()
        flow.delete_flow.return_value = {"deleted": True}
        client = TestClient(_app(tmp_path, MagicMock(flow=flow)))
        response = client.request(
            "DELETE",
            "/flows/p/123",
            headers={**AUTH, "Content-Type": "application/json"},
            content=iter([b'{"dry_run": true}']),
        )
        assert response.status_code == 200, response.text
        flow.delete_flow.assert_called_once()


class _WithDryRun(BaseModel):
    name: str = ""
    dry_run: bool = False


class _AliasedDryRun(BaseModel):
    preview: bool = Field(False, alias="dry_run")


class _WithoutDryRun(BaseModel):
    name: str = ""


def _body_params(endpoint: Any) -> list[Any]:
    router = APIRouter()
    router.post("/probe")(endpoint)
    route = router.routes[0]
    assert isinstance(route, APIRoute)
    return get_flat_dependant(route.dependant).body_params


def _optional_model(body: _WithDryRun | None = None) -> None: ...


def _annotated_model(body: Annotated[_WithDryRun, Body()]) -> None: ...


def _annotated_optional_model(body: Annotated[_WithDryRun | None, Body()] = None) -> None: ...


def _aliased_field(body: _AliasedDryRun) -> None: ...


def _embedded_model(body: Annotated[_WithDryRun, Body(embed=True)]) -> None: ...


def _named_body_param(item: _WithoutDryRun, dry_run: Annotated[bool, Body()] = False) -> None: ...


def _two_params_one_model(item: _WithDryRun, other: _WithoutDryRun) -> None: ...


def _free_dict(body: dict[str, Any]) -> None: ...


def _model_without_field(body: _WithoutDryRun) -> None: ...


class TestBodyDeclaresDryRun:
    @pytest.mark.parametrize(
        ("endpoint", "expected"),
        [
            (_optional_model, True),
            (_annotated_model, True),
            (_annotated_optional_model, True),
            (_aliased_field, True),
            # Embedded: the model sits under "body", so its field is not top level.
            (_embedded_model, False),
            (_named_body_param, True),
            (_two_params_one_model, False),
            (_free_dict, False),
            (_model_without_field, False),
        ],
    )
    def test_body_declares_dry_run(self, endpoint: Any, expected: bool) -> None:
        assert _body_declares_dry_run(_body_params(endpoint)) is expected


class TestIsFalse:
    @pytest.mark.parametrize("value", [False, 0, 0.0, "false", "FALSE", "0", "no", "Off", "f", "n"])
    def test_false_values(self, value: Any) -> None:
        assert _is_false(value) is True

    @pytest.mark.parametrize(
        "value", [True, 1, 2, -1, 0.5, "true", "1", "yes", "", " false", None, {}, []]
    )
    def test_other_values(self, value: Any) -> None:
        assert _is_false(value) is False
