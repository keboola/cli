"""Serve-side usage telemetry: the route->CLI-command map and the middleware.

Two properties: the map covers every serve route (so CLI and serve never silently
diverge), and a real request through serve posts an event whose ``command`` is the
mapped CLI command, with the project taken from the path or the query.
"""

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from keboola_agent_cli import telemetry
from keboola_agent_cli.server._serve_command_map import SERVE_COMMAND_MAP
from keboola_agent_cli.server.app import create_app


def test_command_map_matches_every_route_exactly(tmp_config_dir: Path) -> None:
    """The map has exactly one entry per serve route -- none missing, none orphaned.

    Any route added, removed, or renamed fails here, so serve and CLI telemetry can
    never silently diverge (a stale entry from a renamed route is caught too).
    """
    app = create_app(config_dir=str(tmp_config_dir))
    routes = {
        (method.upper(), path)
        for path, methods in app.openapi()["paths"].items()
        for method in methods
        if method.upper() not in ("HEAD", "OPTIONS")
    }
    mapped = set(SERVE_COMMAND_MAP)
    assert not routes - mapped, f"routes missing from SERVE_COMMAND_MAP: {sorted(routes - mapped)}"
    assert not mapped - routes, f"orphaned SERVE_COMMAND_MAP entries: {sorted(mapped - routes)}"


def test_serve_logs_a_mutation_with_the_mapped_command(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mutating serve request posts an event whose command is the CLI equivalent.

    So serve and CLI telemetry share one ``command`` vocabulary. Reads are covered
    by ``test_serve_skips_read_requests`` -- serve logs only mutations.
    """
    captured: list[tuple[str, str | None]] = []

    def fake_send(config_store, *, method, path, operation, status_code, duration_s, project_alias):
        captured.append((operation, project_alias))

    monkeypatch.setattr(telemetry, "send_serve_event", fake_send)

    app = create_app(config_dir=str(tmp_config_dir), auth_token="tok")
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/jobs/prod/run", headers={"Authorization": "Bearer tok"}, json={})

    assert ("job run", "prod") in set(captured)  # mutation, project from the path template


def test_unhandled_route_error_is_logged_as_a_failure(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A route that raises posts status 500, not the middleware's default 200.

    The 500 is synthesized above the telemetry middleware, so the middleware never
    sees the response start. Without recording the failure it would log the crash as a
    success -- the one outcome this signal most needs to catch.
    """
    captured: list[int] = []

    def fake_send(config_store, *, method, path, operation, status_code, duration_s, project_alias):
        captured.append(status_code)

    monkeypatch.setattr(telemetry, "send_serve_event", fake_send)

    app = create_app(config_dir=str(tmp_config_dir), auth_token="tok")

    def _boom(project: str) -> None:
        raise RuntimeError("boom")

    app.add_api_route("/telemetry-test-boom/{project}", _boom, methods=["POST"], name="boom_route")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/telemetry-test-boom/prod", headers={"Authorization": "Bearer tok"})
        assert response.status_code == 500

    # the event runs detached and is drained on shutdown
    assert captured == [500]


def test_serve_maps_a_path_converter_route(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A route declared with a ``{...:path}`` converter still logs the mapped command.

    The map is keyed by the OpenAPI path (converter stripped), so the middleware must
    look the route up by the same shape. Reading ``route.path`` (which keeps ``:path``)
    misses the map and falls back to the endpoint function name, so serve and CLI
    telemetry for that operation land under different names.
    """
    captured: list[str] = []

    def fake_send(config_store, *, method, path, operation, status_code, duration_s, project_alias):
        captured.append(operation)

    monkeypatch.setattr(telemetry, "send_serve_event", fake_send)
    app = create_app(config_dir=str(tmp_config_dir), auth_token="tok")
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post(
            "/storage/buckets/prod/in.c-mybucket/describe",
            headers={"Authorization": "Bearer tok"},
            json={"description": "x"},
        )

    expected = SERVE_COMMAND_MAP[("POST", "/storage/buckets/{project}/{bucket_id}/describe")]
    assert captured == [expected]  # the mapped command, not the endpoint-name fallback


def test_serve_skips_read_requests(tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """serve posts a usage event only for mutating requests, not reads.

    One event per request floods the customer's event log from UI polling (the Jobs
    page refetches every few seconds). Reads are skipped. The CLI still logs reads,
    where volume is one event per human/agent invocation.
    """
    captured: list[tuple[str, str]] = []

    def fake_send(config_store, *, method, path, operation, status_code, duration_s, project_alias):
        captured.append((method, operation))

    monkeypatch.setattr(telemetry, "send_serve_event", fake_send)
    app = create_app(config_dir=str(tmp_config_dir), auth_token="tok")
    with TestClient(app, raise_server_exceptions=False) as client:
        headers = {"Authorization": "Bearer tok"}
        client.get("/jobs?project=prod", headers=headers)
        client.get("/configs?project=prod", headers=headers)
        client.post("/jobs/prod/run", headers=headers, json={})

    methods = {method for method, _ in captured}
    assert methods == {"POST"}  # only the mutation is logged
    assert ("POST", "job run") in captured


def test_serve_does_not_wait_for_the_telemetry_post(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A serve request returns without waiting for its own telemetry POST.

    Awaiting the POST in the middleware's ``finally`` held the request's ASGI task
    until the events endpoint answered, so a slow or blocked endpoint added its full
    timeout to every serve request. The event now runs as a detached task.
    """
    posted = threading.Event()

    def slow_send(config_store, *, method, path, operation, status_code, duration_s, project_alias):
        time.sleep(0.3)
        posted.set()

    monkeypatch.setattr(telemetry, "send_serve_event", slow_send)
    app = create_app(config_dir=str(tmp_config_dir), auth_token="tok")
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/jobs/prod/run", headers={"Authorization": "Bearer tok"}, json={})
        # The response returned; the 0.3s telemetry POST must not have blocked it.
        assert not posted.is_set()
        # ...but the event still fires, off the request path.
        assert posted.wait(2.0)
