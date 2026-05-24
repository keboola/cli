"""HTTP integration tests for ``/semantic-layer/*`` routes (real metastore).

Bootstraps a throwaway ``kbagent_e2e_<ts>`` model on ``e2e-1143``
(``E2E_URL`` / ``E2E_API_TOKEN``), exercises every one of the 14 routes
declared in :mod:`keboola_agent_cli.server.routers.semantic_layer` against
the real ``SemanticLayerService`` (NOT mocked), and tears down in a
``finally`` block. Residue assertion at session end verifies no
``kbagent_e2e_*`` items remain across any of the 6 semantic types.

Gated behind the same ``E2E_API_TOKEN`` + ``E2E_URL`` env vars and the
``@pytest.mark.e2e`` marker as :mod:`tests.test_e2e`, so unit-only CI
runs skip cleanly.
"""

from __future__ import annotations

import importlib.util
import os
import time
from collections.abc import Iterator
from typing import Any

import pytest

if importlib.util.find_spec("fastapi") is None:  # pragma: no cover
    pytest.skip(
        "FastAPI not installed; run `uv pip install -e '.[server]'`",
        allow_module_level=True,
    )

from fastapi.testclient import TestClient

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.server import create_app

ENV_TOKEN = "E2E_API_TOKEN"
ENV_URL = "E2E_URL"
HAS_CREDENTIALS = os.environ.get(ENV_TOKEN) is not None

pytestmark = pytest.mark.e2e


# Session-scoped tag so every test in the suite operates on the same
# bootstrapped model. Cleanup runs once at session end.
_RUN_TAG = f"kbagent_e2e_{int(time.time())}"
_PROJECT_ALIAS = "e2e-http-sl"


def _metastore_scope_available(url: str, token: str) -> bool:
    """Probe whether the project has a usable metastore scope.

    Semantic-layer is a gated feature; a project that does not have it returns
    HTTP 502 "Failed to create project scope" on every metastore call. That is
    an environment limitation, not a test failure -- so the suite skips cleanly
    instead of reporting a wall of false-positive failures.
    """
    from keboola_agent_cli.errors import KeboolaApiError
    from keboola_agent_cli.metastore_client import SEMANTIC_TYPES, MetastoreClient

    try:
        with MetastoreClient(stack_url=url, token=token) as mc:
            mc.list_items(SEMANTIC_TYPES[0])  # ty: ignore[invalid-argument-type]  # probe call; str vs SemanticType Literal
        return True
    except KeboolaApiError as exc:
        if exc.status_code == 502 or "scope" in (exc.message or "").lower():
            return False
        raise


@pytest.fixture(scope="session")
def http_session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """Bootstrap a semantic-layer model + return a TestClient + tracking dict.

    Mirrors :class:`tests.test_e2e.TestE2ESemanticLayerLifecycle` setup but
    holds artifacts at session scope so every test_* function in this
    module shares one bootstrapped model.
    """
    if not HAS_CREDENTIALS:
        pytest.skip(f"{ENV_TOKEN} not set")

    token = os.environ[ENV_TOKEN]
    raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
    url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"

    if not _metastore_scope_available(url, token):
        pytest.skip("metastore/semantic-layer scope not available for this project")

    config_dir = tmp_path_factory.mktemp("kbagent-sl-http-config")
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        _PROJECT_ALIAS,
        ProjectConfig(stack_url=url, token=token),
    )

    app = create_app(config_dir=str(config_dir), auth_token="test-http-token")
    client = TestClient(app)

    state: dict[str, Any] = {
        "client": client,
        "tag": _RUN_TAG,
        "model_id": None,
        "model_name": _RUN_TAG,
        "url": url,
        "token": token,
        "config_dir": config_dir,
        # Tracked items for guaranteed teardown.
        "created_items": [],  # list[tuple[item_type, item_id]]
    }

    try:
        yield state
    finally:
        # ── teardown ──────────────────────────────────────────────
        _cleanup(state)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-http-token"}


def _direct_delete(state: dict[str, Any], item_type: str, item_id: str) -> None:
    """Direct DELETE via MetastoreClient, bypassing the service."""
    from keboola_agent_cli.metastore_client import MetastoreClient

    with MetastoreClient(stack_url=state["url"], token=state["token"]) as mc:
        mc.delete_item(item_type, item_id)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def _cleanup(state: dict[str, Any]) -> None:
    """Teardown: direct-delete every tracked item + the model, then residue scan."""
    from keboola_agent_cli.errors import KeboolaApiError
    from keboola_agent_cli.metastore_client import SEMANTIC_TYPES, MetastoreClient

    print("\n--- SEMANTIC LAYER HTTP CLEANUP ---")
    for item_type, item_id in reversed(state["created_items"]):
        try:
            _direct_delete(state, item_type, item_id)
            print(f"  Deleted {item_type} {item_id}")
        except Exception as exc:
            print(f"  WARN: failed to delete {item_type} {item_id}: {exc}")

    if state.get("model_id"):
        try:
            _direct_delete(state, "semantic-model", state["model_id"])
            print(f"  Deleted semantic-model {state['model_id']}")
        except Exception as exc:
            print(f"  WARN: failed to delete semantic-model: {exc}")

    # Session-end residue scan
    try:
        with MetastoreClient(stack_url=state["url"], token=state["token"]) as mc:
            residue: list[str] = []
            for stype in SEMANTIC_TYPES:
                for item in mc.list_items(stype):  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
                    attrs = item.get("attributes") or {}
                    name = attrs.get("name") or attrs.get("term", "")
                    if isinstance(name, str) and name.startswith(state["tag"]):
                        residue.append(f"{stype}:{name}:{item.get('id', '')}")
            assert not residue, f"Residue left after HTTP integration suite: {residue}"
    except KeboolaApiError as exc:
        print(f"  WARN: residue scan failed: {exc}")


# ── 14 happy-path tests (one per declared route) ────────────────────


def test_post_models_create(http_session: dict[str, Any]) -> None:
    """POST /semantic-layer/models — create the bootstrap model."""
    client = http_session["client"]
    res = client.post(
        "/semantic-layer/models",
        headers=_auth(),
        json={
            "project": _PROJECT_ALIAS,
            "name": http_session["model_name"],
            "description": "kbagent http integration bootstrap",
            "sql_dialect": "Snowflake",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    http_session["model_id"] = body["model"]["id"]
    assert http_session["model_id"]


def test_get_models_list(http_session: dict[str, Any]) -> None:
    """GET /semantic-layer/models — should include the bootstrapped model."""
    res = http_session["client"].get(
        f"/semantic-layer/models?project={_PROJECT_ALIAS}", headers=_auth()
    )
    assert res.status_code == 200, res.text
    body = res.json()
    names = {m["name"] for m in body["models"]}
    assert http_session["model_name"] in names


def test_post_items_add_dataset(http_session: dict[str, Any]) -> None:
    """POST /semantic-layer/items/dataset — adds the first dataset."""
    res = http_session["client"].post(
        "/semantic-layer/items/dataset",
        headers=_auth(),
        json={
            "project": _PROJECT_ALIAS,
            "model": http_session["model_name"],
            "name": f"{http_session['tag']}_ds_a",
            "table_id": "out.c-syn.fact_a",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    item_id = body["id"]
    http_session["created_items"].append(("semantic-dataset", item_id))
    http_session["dataset_a_id"] = item_id
    # Add a second dataset for later relationship test.
    res2 = http_session["client"].post(
        "/semantic-layer/items/dataset",
        headers=_auth(),
        json={
            "project": _PROJECT_ALIAS,
            "model": http_session["model_name"],
            "name": f"{http_session['tag']}_ds_b",
            "table_id": "out.c-syn.fact_b",
        },
    )
    assert res2.status_code == 200, res2.text
    http_session["created_items"].append(("semantic-dataset", res2.json()["id"]))


def test_post_items_add_metric(http_session: dict[str, Any]) -> None:
    """POST /semantic-layer/items/metric — adds a metric for later edits."""
    res = http_session["client"].post(
        "/semantic-layer/items/metric",
        headers=_auth(),
        json={
            "project": _PROJECT_ALIAS,
            "model": http_session["model_name"],
            "name": f"{http_session['tag']}_m_rev",
            "sql": "COUNT(*)",
            "dataset": "out.c-syn.fact_a",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    item_id = body["id"]
    http_session["created_items"].append(("semantic-metric", item_id))
    http_session["metric_rev_id"] = item_id


def test_post_items_add_relationship(http_session: dict[str, Any]) -> None:
    """POST /semantic-layer/items/relationship — uses `from` alias in body."""
    res = http_session["client"].post(
        "/semantic-layer/items/relationship",
        headers=_auth(),
        json={
            "project": _PROJECT_ALIAS,
            "model": http_session["model_name"],
            "name": f"{http_session['tag']}_rel_a_b",
            "from": "out.c-syn.fact_a",
            "to": "out.c-syn.fact_b",
            "on": "fact_a.id = fact_b.fact_a_id",
            "type": "left",
        },
    )
    assert res.status_code == 200, res.text
    item_id = res.json()["id"]
    http_session["created_items"].append(("semantic-relationship", item_id))


def test_post_items_add_constraint(http_session: dict[str, Any]) -> None:
    """POST /semantic-layer/items/constraint — references the metric just added."""
    res = http_session["client"].post(
        "/semantic-layer/items/constraint",
        headers=_auth(),
        json={
            "project": _PROJECT_ALIAS,
            "model": http_session["model_name"],
            "name": f"{http_session['tag']}_warn",
            "constraint_type": "inequality",
            "rule": "value >= 0",
            "metrics": [f"{http_session['tag']}_m_rev"],
            "severity": "warning",
        },
    )
    assert res.status_code == 200, res.text
    item_id = res.json()["id"]
    http_session["created_items"].append(("semantic-constraint", item_id))


def test_post_items_add_glossary(http_session: dict[str, Any]) -> None:
    """POST /semantic-layer/items/glossary — outer envelope name = term."""
    res = http_session["client"].post(
        "/semantic-layer/items/glossary",
        headers=_auth(),
        json={
            "project": _PROJECT_ALIAS,
            "model": http_session["model_name"],
            "term": f"{http_session['tag']}_term",
            "definition": "an integration test definition",
        },
    )
    assert res.status_code == 200, res.text
    item_id = res.json()["id"]
    http_session["created_items"].append(("semantic-glossary", item_id))


def test_get_show(http_session: dict[str, Any]) -> None:
    """GET /semantic-layer/show — assert child counts."""
    res = http_session["client"].get(
        f"/semantic-layer/show?project={_PROJECT_ALIAS}&model={http_session['model_name']}",
        headers=_auth(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["datasets"]) >= 2
    assert len(body["metrics"]) >= 1
    assert len(body["relationships"]) >= 1
    assert len(body["constraints"]) >= 1
    assert len(body["glossary"]) >= 1


def test_get_validate(http_session: dict[str, Any]) -> None:
    """GET /semantic-layer/validate — basic checks, expect clean."""
    res = http_session["client"].get(
        f"/semantic-layer/validate?project={_PROJECT_ALIAS}&model={http_session['model_name']}",
        headers=_auth(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["valid"] is True, f"Expected clean model, got errors: {body['errors']}"


def test_get_export(http_session: dict[str, Any]) -> None:
    """GET /semantic-layer/export — snapshot returned inline (no `path` field)."""
    res = http_session["client"].get(
        f"/semantic-layer/export?project={_PROJECT_ALIAS}&model={http_session['model_name']}",
        headers=_auth(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "path" not in body, "Export should NOT echo a server-side tmp path"
    assert "datasets" in body
    assert "metrics" in body
    # Stash for the diff + import tests.
    http_session["exported_snapshot"] = body


def test_put_items_edit_metric(http_session: dict[str, Any]) -> None:
    """PUT /semantic-layer/items/metric/{name} — rename + assert cascade."""
    res = http_session["client"].put(
        f"/semantic-layer/items/metric/{http_session['tag']}_m_rev",
        headers=_auth(),
        json={
            "project": _PROJECT_ALIAS,
            "model": http_session["model_name"],
            "new_name": f"{http_session['tag']}_m_revenue",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    new_id = body["updated"]["id"]
    # Rename leaves a fresh metric id and DELETE+POSTs the constraint —
    # refresh tracking so cleanup hits the right rows.
    http_session["created_items"] = [
        (t, i)
        for (t, i) in http_session["created_items"]
        if not (t == "semantic-metric" and i == http_session["metric_rev_id"])
    ]
    http_session["created_items"].append(("semantic-metric", new_id))
    # Refresh constraint id (cascade DELETE+POST).
    show = http_session["client"].get(
        f"/semantic-layer/show?project={_PROJECT_ALIAS}"
        f"&model={http_session['model_name']}&type=constraint",
        headers=_auth(),
    )
    http_session["created_items"] = [
        (t, i) for (t, i) in http_session["created_items"] if t != "semantic-constraint"
    ]
    for c in show.json()["constraints"]:
        http_session["created_items"].append(("semantic-constraint", c["id"]))
    cascaded = body["cascaded_constraints"]
    assert any(c["status"] == "updated" for c in cascaded), f"Expected cascade, got: {cascaded}"


def test_post_diff_project_vs_file(http_session: dict[str, Any]) -> None:
    """POST /semantic-layer/diff — file_b = the prior exported snapshot.

    Diff against the live state after rename — should show the metric
    rename as a change (m_rev removed, m_revenue added). This proves the
    inline-file branch of the body validator works.
    """
    snapshot = http_session.get("exported_snapshot")
    assert snapshot is not None, "exported_snapshot must be set by prior test"
    # Strip the model wrapping that export adds (just keep the diffable
    # bare child lists — diff service expects raw side data).
    snapshot_for_file = {
        "datasets": snapshot["datasets"],
        "metrics": snapshot["metrics"],
        "relationships": snapshot["relationships"],
        "constraints": snapshot["constraints"],
        "glossary": snapshot["glossary"],
        "model": snapshot["model"],
    }
    res = http_session["client"].post(
        "/semantic-layer/diff",
        headers=_auth(),
        json={
            "project_a": _PROJECT_ALIAS,
            "model_a": http_session["model_name"],
            "file_b": snapshot_for_file,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # The metric rename is the only post-export change → diff is non-empty.
    metric_diff = body["metrics"]
    assert metric_diff["added"] or metric_diff["removed"] or metric_diff["changed"], (
        f"Expected metric diff after rename, got: {metric_diff}"
    )


def test_post_import_dry_run(http_session: dict[str, Any]) -> None:
    """POST /semantic-layer/import — dry-run reuses the exported snapshot."""
    snapshot = http_session.get("exported_snapshot")
    res = http_session["client"].post(
        "/semantic-layer/import",
        headers=_auth(),
        json={
            "project": _PROJECT_ALIAS,
            "model": http_session["model_name"],
            "snapshot": snapshot,
            "dry_run": True,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["dry_run"] is True
    assert "imported" in body


def test_post_promote_dry_run(http_session: dict[str, Any]) -> None:
    """POST /semantic-layer/promote — dry-run from the model to itself.

    Self-promotion is a degenerate case the service handles gracefully:
    every item classifies as IDENTICAL (no overwrite, no new). That's
    enough to prove the route + body validation reach the service.
    """
    res = http_session["client"].post(
        "/semantic-layer/promote",
        headers=_auth(),
        json={
            "from_project": _PROJECT_ALIAS,
            "to_project": _PROJECT_ALIAS,
            "from_model": http_session["model_name"],
            "to_model": http_session["model_name"],
            "dry_run": True,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["dry_run"] is True


def test_post_build_dry_run(http_session: dict[str, Any]) -> None:
    """POST /semantic-layer/build — dry-run heuristic builder.

    The build path needs Storage schemas — provide a synthetic tableId
    that the service will attempt to fetch. The fetch failure is
    captured in ``fetch_errors`` (the route still returns 200; that's
    sufficient signal for "route mounted + body validates").
    """
    res = http_session["client"].post(
        "/semantic-layer/build",
        headers=_auth(),
        json={
            "project": _PROJECT_ALIAS,
            "tables": ["out.c-syn.fact_a"],
            "name": f"{http_session['tag']}_build_target",
            "dry_run": True,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["dry_run"] is True
    assert body["fallback_used"] == "heuristic"


def test_post_build_keep_on_failure_propagates(http_session: dict[str, Any]) -> None:
    """POST /semantic-layer/build with keep_on_failure=True propagates to the service.

    Issue #295 added the rollback flag to the CLI; this verifies HTTP-API
    parity (the BuildRequest pydantic model carries `keep_on_failure: bool`
    and the route handler threads it through).
    """
    res = http_session["client"].post(
        "/semantic-layer/build",
        headers=_auth(),
        json={
            "project": _PROJECT_ALIAS,
            "tables": ["out.c-syn.fact_a"],
            "name": f"{http_session['tag']}_keep_on_failure_target",
            "dry_run": True,
            "keep_on_failure": True,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["keep_on_failure"] is True


def test_post_token_encrypt(http_session: dict[str, Any]) -> None:
    """POST /semantic-layer/token/encrypt — KBC::ProjectSecure envelope."""
    res = http_session["client"].post(
        "/semantic-layer/token/encrypt",
        headers=_auth(),
        json={
            "project": _PROJECT_ALIAS,
            "component_id": "keboola.ex-db-snowflake",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    token_field = body["encrypted"].get("#metastore_token", "")
    assert token_field.startswith("KBC::ProjectSecure"), (
        f"Expected KBC::ProjectSecure envelope, got: {token_field[:50]}"
    )


def test_delete_items_remove_glossary(http_session: dict[str, Any]) -> None:
    """DELETE /semantic-layer/items/glossary/{term} — removes the glossary entry."""
    term = f"{http_session['tag']}_term"
    res = http_session["client"].delete(
        f"/semantic-layer/items/glossary/{term}"
        f"?project={_PROJECT_ALIAS}&model={http_session['model_name']}",
        headers=_auth(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["removed"]["name"] == term
    http_session["created_items"] = [
        (t, i) for (t, i) in http_session["created_items"] if t != "semantic-glossary"
    ]


# Note on `DELETE /semantic-layer/models/{model}`: deletion is exercised
# as part of the session-end cleanup (the model is direct-deleted via
# MetastoreClient if its row still exists). Adding an explicit test
# here would clobber the bootstrapped model mid-suite.


# ── Negative paths ──────────────────────────────────────────────────


def test_negative_unknown_project_returns_400(http_session: dict[str, Any]) -> None:
    """Missing project alias → CONFIG_ERROR → HTTP 400."""
    res = http_session["client"].get(
        "/semantic-layer/models?project=does-not-exist-alias",
        headers=_auth(),
    )
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "CONFIG_ERROR"


def test_negative_unknown_kind_returns_404(http_session: dict[str, Any]) -> None:
    """POST /semantic-layer/items/<unknown> → 404."""
    res = http_session["client"].post(
        "/semantic-layer/items/widget",
        headers=_auth(),
        json={"project": _PROJECT_ALIAS, "name": "x"},
    )
    assert res.status_code == 404


def test_negative_invalid_constraint_name_returns_4xx(
    http_session: dict[str, Any],
) -> None:
    """Constraint name with uppercase → VALIDATION_ERROR via service.

    Service raises VALIDATION_ERROR (KeboolaApiError) which the global
    handler maps to 502 with the code intact. The test asserts the wire
    code matches our enum.
    """
    res = http_session["client"].post(
        "/semantic-layer/items/constraint",
        headers=_auth(),
        json={
            "project": _PROJECT_ALIAS,
            "model": http_session["model_name"],
            "name": "BadName",  # uppercase rejected by regex
            "constraint_type": "range",
            "rule": "between 0 and 100",
            "metrics": [f"{http_session['tag']}_m_revenue"],
        },
    )
    # Service-side validation → 502 (KeboolaApiError → HTTP_502_via_handler).
    assert res.status_code in (400, 422, 502)
    assert "constraint" in res.text.lower() or "name" in res.text.lower()


def test_negative_duplicate_name_maps_to_already_exists(
    http_session: dict[str, Any],
) -> None:
    """Duplicate model name → service translates 500 to ALREADY_EXISTS."""
    res = http_session["client"].post(
        "/semantic-layer/models",
        headers=_auth(),
        json={
            "project": _PROJECT_ALIAS,
            "name": http_session["model_name"],  # already exists
            "description": "duplicate",
            "sql_dialect": "Snowflake",
        },
    )
    # KeboolaApiError(ALREADY_EXISTS) → HTTP 502 envelope (per global
    # handler in server/__init__.py), with the canonical code in the
    # error.code field.
    assert res.status_code in (400, 409, 502)
    body = res.json()
    assert body.get("error", {}).get("code") in (
        "ALREADY_EXISTS",
        "API_ERROR",
    ), body
