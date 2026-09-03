"""Serve-side usage telemetry: the route->CLI-command map and the middleware.

Two properties: the map covers every serve route (so CLI and serve never silently
diverge), and a real request through serve posts an event whose ``command`` is the
mapped CLI command, with the project taken from the path or the query.
"""

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


def test_serve_requests_log_the_mapped_cli_command(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A serve request posts an event whose command is the CLI equivalent.

    Covers a mutation with the project in the path, and reads with the project in
    the query -- so serve and CLI telemetry share one ``command`` vocabulary.
    """
    captured: list[tuple[str, str | None]] = []

    def fake_send(config_store, *, method, path, operation, status_code, duration_s, project_alias):
        captured.append((operation, project_alias))

    monkeypatch.setattr(telemetry, "send_serve_event", fake_send)

    app = create_app(config_dir=str(tmp_config_dir), auth_token="tok")
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": "Bearer tok"}
    client.post("/jobs/prod/run", headers=headers, json={})  # project in path
    client.get("/jobs?project=prod", headers=headers)  # read, project in query
    client.get("/configs?project=prod", headers=headers)  # read, project in query

    got = set(captured)
    assert ("job run", "prod") in got  # mutation, project from path template
    assert ("job list", "prod") in got  # read, project from query
    assert ("config list", "prod") in got


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

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/telemetry-test-boom/prod", headers={"Authorization": "Bearer tok"})

    assert response.status_code == 500
    assert captured == [500]
